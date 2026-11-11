"""Small async client for plataforma.habidat.es (ista Spain).

Habidat has no public API. The web app is an Angular front end that talks to
a PHP back end on port 8443 using a session cookie. The session is opened by
going through ista's Keycloak login form (realm ``HABIDAT-ES-APP``); the
password grant is not enabled for that client, so the client follows the same
redirects a browser does.

This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import html
import json
import logging
import re
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://plataforma.habidat.es:8443/"
LOGIN_HOST = "login.ista.com"
DATA_PATH = "src/php/vecino/selectViviendas.php"
SESSION_PATH = "src/php/get_session_user.php"

_TIMEOUT = aiohttp.ClientTimeout(total=60)
# What the web app sends; some PHP endpoints look at Origin/Referer.
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://plataforma.habidat.es",
    "Referer": "https://plataforma.habidat.es/",
}
_FORM_RE = re.compile(r"<form\b[^>]*\bid=\"kc-form-login\"[^>]*>", re.IGNORECASE)
_ACTION_RE = re.compile(r"\baction=\"([^\"]+)\"", re.IGNORECASE)
_ERROR_RE = re.compile(
    r"<span[^>]*id=\"input-error[^\"]*\"[^>]*>(.*?)</span>|"
    r"<div[^>]*class=\"[^\"]*(?:alert-error|kc-feedback|feedback-error)[^\"]*\"[^>]*>(.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<h1[^>]*id=\"kc-page-title\"[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


class HabidatError(Exception):
    """Generic Habidat error."""


class HabidatConnectionError(HabidatError):
    """Habidat could not be reached."""


class HabidatAuthError(HabidatError):
    """Wrong credentials or the account needs an action on the web."""


class _SessionExpired(Exception):
    """The PHP session is gone; log in again."""


def parse_date(value: Any) -> datetime | None:
    """Parse the Mongo-style dates the back end returns, in UTC."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("$date", value)
        if isinstance(value, dict):
            value = value.get("$numberLong")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def parse_day(value: Any) -> date | None:
    """Calendar day of a reading (the back end stores 00:00 UTC of that day)."""
    parsed = parse_date(value)
    return parsed.date() if parsed else None


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Vivienda:
    """A dwelling the account has access to."""

    id: str
    comunidad_id: str
    comunidad: str
    calle: str | None = None
    portal: str | None = None
    piso: str | None = None
    mano: str | None = None
    municipio: str | None = None

    @property
    def nombre(self) -> str:
        """Human readable name: street, number and floor."""
        parts = [p for p in (self.calle, self.portal) if p]
        name = " ".join(parts) or self.comunidad
        extra = [p for p in (self.piso, self.mano) if p and p != "-"]
        if extra:
            name += " " + "-".join(extra)
        return name

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Vivienda:
        """Build from a ``list=viviendas`` row."""
        return cls(
            id=str(data["ID"]),
            comunidad_id=str(data.get("comunidad", "")),
            comunidad=data.get("comunidad_nombre") or "",
            calle=data.get("calle"),
            portal=data.get("portal"),
            piso=data.get("piso"),
            mano=data.get("mano"),
            municipio=data.get("municipio"),
        )


@dataclass
class Tipo:
    """A kind of supply metered in the dwelling (CALEF, ACS, AF...)."""

    nombre: str
    alias: str
    unidades: str | None
    incrementable: bool = True


@dataclass
class Lectura:
    """One daily reading of one supply."""

    dia: date
    incremento: float | None
    acumulado_periodo: float | None
    lectura: float | None
    importe_incremento: float | None = None
    importe_acumulado: float | None = None
    equipos: list[dict[str, Any]] = field(default_factory=list)


def parse_lecturas(rows: list[dict[str, Any]]) -> dict[str, list[Lectura]]:
    """Group daily rows by supply type, oldest first."""
    result: dict[str, list[Lectura]] = {}
    for row in rows or []:
        dia = parse_day(row.get("fecha"))
        if dia is None:
            continue
        for key, value in row.items():
            if key in ("_id", "fecha") or not isinstance(value, dict):
                continue
            equipos = value.get("equipos") or []
            lecturas = [_num(e.get("lectura")) for e in equipos]
            lecturas = [x for x in lecturas if x is not None]
            importe_inc = _num(value.get("importe_incremento"))
            if importe_inc is None and equipos:
                vals = [_num(e.get("importe_incremento")) for e in equipos]
                vals = [x for x in vals if x is not None]
                importe_inc = sum(vals) if vals else None
            result.setdefault(key, []).append(
                Lectura(
                    dia=dia,
                    incremento=_num(value.get("incremento")),
                    acumulado_periodo=_num(value.get("acumulado_periodo")),
                    lectura=sum(lecturas) if lecturas else None,
                    importe_incremento=importe_inc,
                    importe_acumulado=_num(value.get("importe_acumulado")),
                    equipos=equipos,
                )
            )
    for lista in result.values():
        lista.sort(key=lambda x: x.dia)
    return result


class HabidatClient:
    """Async client. Give it its own ``aiohttp`` session (it keeps cookies)."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        """Store the session and the credentials."""
        self._session = session
        self._email = email
        self._password = password
        self._logged_in = False
        self._lock = asyncio.Lock()
        self.user: dict[str, Any] | None = None

    def close(self) -> None:
        """Release the HTTP session (Home Assistant owns the connector)."""
        detach = getattr(self._session, "detach", None)
        if detach:
            detach()

    # -- session -----------------------------------------------------------

    async def _seguir(self, method: str, url: URL | str, **kwargs: Any) -> tuple[URL, str]:
        """Request following redirects by hand, logging every hop (no secrets)."""
        for _ in range(10):
            async with self._session.request(
                method, url, allow_redirects=False, timeout=_TIMEOUT, **kwargs
            ) as resp:
                status = resp.status
                destino = resp.headers.get("Location")
                cuerpo = await resp.text()
                actual = resp.url
            _LOGGER.debug("Login: %s %s%s -> %s", method, actual.host, actual.path, status)
            if status in (301, 302, 303, 307, 308) and destino:
                url = actual.join(URL(destino, encoded=True))
                if status != 307 and status != 308:
                    method, kwargs = "GET", {}
                continue
            return actual, cuerpo
        raise HabidatError("Too many redirects while logging in")

    async def login(self) -> dict[str, Any]:
        """Open a PHP session through ista's Keycloak form."""
        self._session.cookie_jar.clear()
        self._logged_in = False
        try:
            url, page = await self._seguir("GET", BASE_URL)
            if url.host != LOGIN_HOST:
                # Already logged in (cookies survived): just check it.
                return await self._check_session()

            form = _FORM_RE.search(page)
            action = _ACTION_RE.search(form.group(0)) if form else None
            if not action:
                raise HabidatError("Login form not found on login.ista.com")
            action_url = URL(html.unescape(action.group(1)), encoded=True)
            if action_url.host != LOGIN_HOST:
                raise HabidatError(f"Unexpected login form target: {action_url.host}")

            url, page = await self._seguir(
                "POST",
                action_url,
                data={"username": self._email, "password": self._password, "credentialId": ""},
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HabidatConnectionError(str(err)) from err

        if url.host == LOGIN_HOST:
            # Still on Keycloak: wrong password, or a required action
            # (update password, accept terms, OTP...).
            message = None
            if match := _ERROR_RE.search(page):
                raw = next(g for g in match.groups() if g)
                message = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
            if not message and (title := _TITLE_RE.search(page)):
                message = "ista asks for: " + html.unescape(title.group(1)).strip()
            _LOGGER.debug("Login rejected on %s: %s", url.path, message)
            raise HabidatAuthError(message or "Login rejected by login.ista.com")

        _LOGGER.debug("Login finished on %s%s", url.host, url.path)
        return await self._check_session()

    async def _check_session(self) -> dict[str, Any]:
        try:
            user = await self._fetch("GET", SESSION_PATH, None)
        except _SessionExpired as err:
            _LOGGER.debug("No PHP session after login; cookies: %s", self._cookies())
            raise HabidatAuthError("Habidat did not open a session after login") from err
        if not isinstance(user, dict) or "id" not in user:
            _LOGGER.debug("Unexpected session answer: %s", str(user)[:120])
            raise HabidatAuthError("Habidat did not open a session after login")
        self._logged_in = True
        self.user = user
        return user

    def _cookies(self) -> list[str]:
        """Cookie names and domains (never values) for the logs."""
        return sorted({f"{c.key}@{c['domain'] or '?'}" for c in self._session.cookie_jar})

    async def _fetch(self, method: str, path: str, params: dict[str, str] | None) -> Any:
        try:
            async with self._session.request(
                method,
                BASE_URL + path,
                params=params,
                allow_redirects=False,
                timeout=_TIMEOUT,
                headers=_HEADERS,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 401, 403):
                    _LOGGER.debug(
                        "%s %s (%s) -> %s %s %s",
                        method,
                        path,
                        (params or {}).get("list", ""),
                        resp.status,
                        resp.headers.get("Location", ""),
                        (await resp.text())[:120],
                    )
                    raise _SessionExpired
                if resp.status >= 400:
                    raise HabidatError(f"HTTP {resp.status} on {path}")
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HabidatConnectionError(str(err)) from err
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            _LOGGER.debug("Not JSON from %s: %r", path, text[:200])
            if "<html" in text[:200].lower():
                raise _SessionExpired from err
            raise HabidatError(f"Unexpected answer from {path}: {text[:80]!r}") from err

    async def _call(self, method: str, lista: str, **params: str) -> Any:
        async with self._lock:
            if not self._logged_in:
                await self.login()
            query = {"list": lista, **{k: str(v) for k, v in params.items()}}
            try:
                return await self._fetch(method, DATA_PATH, query)
            except _SessionExpired:
                _LOGGER.debug("Habidat session expired, logging in again")
                await self.login()
                try:
                    return await self._fetch(method, DATA_PATH, query)
                except _SessionExpired as err:
                    self._logged_in = False
                    raise HabidatAuthError("Session rejected after login") from err

    # -- data --------------------------------------------------------------

    async def viviendas(self) -> list[Vivienda]:
        """Dwellings of the account."""
        rows = await self._call("GET", "viviendas") or []
        return [Vivienda.from_api(r) for r in rows if "ID" in r]

    async def info_comunidad(self, vivienda: str) -> dict[str, Any]:
        """Building info: name, number of dwellings, billing mode, prepaid..."""
        return await self._call("GET", "info_comunidad", vivienda=vivienda) or {}

    async def tipos(self, vivienda: str) -> list[Tipo]:
        """Supplies metered in the dwelling with their units."""
        rows = await self._call("POST", "tipos", vivienda=vivienda) or []
        return [
            Tipo(
                nombre=r["nombre"],
                alias=r.get("alias") or r["nombre"],
                unidades=r.get("unidades"),
                incrementable=str(r.get("incrementable", "1")) == "1",
            )
            for r in rows
            if r.get("nombre")
        ]

    async def ultimas_lecturas(self, vivienda: str) -> dict[str, list[Lectura]]:
        """Last ~30 daily readings by supply."""
        rows = await self._call("POST", "ultimasLecturas", vivienda=vivienda)
        return parse_lecturas(rows or [])

    async def lecturas_mes(
        self, comunidad: str, vivienda: str, mes: str
    ) -> tuple[dict[str, list[Lectura]], dict[str, list[Lectura]]]:
        """Daily readings of a month (``YYYY-MM``) and of the same month a year before.

        Days of closed bills carry their cost (``importe_incremento``).
        """
        data = await self._call(
            "POST",
            "datos_informe_vivienda",
            comunidad=comunidad,
            vivienda=vivienda,
            mes_informe=mes,
        )
        if not isinstance(data, dict):
            return {}, {}
        return (
            parse_lecturas(data.get("seleccionado") or []),
            parse_lecturas(data.get("anterior") or []),
        )

    async def informe_mes(self, comunidad: str, vivienda: str, mes: str) -> dict[str, Any]:
        """Month summary: daily mean, total and cost for the dwelling and building."""
        return (
            await self._call(
                "POST",
                "datos_informe_mensual",
                comunidad=comunidad,
                vivienda=vivienda,
                mes_informe=mes,
            )
            or {}
        )

    async def meses_informe(self, comunidad: str, vivienda: str) -> list[str]:
        """Months (``YYYY-MM``) with data."""
        data = await self._call(
            "POST", "informe_meses_elegibles", comunidad=comunidad, vivienda=vivienda
        )
        return [m for m in data or [] if isinstance(m, str)]

    async def inicio_periodo(self, vivienda: str) -> date | None:
        """First day of the current billing period."""
        value = await self._call("POST", "acumulado_fecha_desde", vivienda=vivienda)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return None

    async def acumulado_periodo(self, vivienda: str) -> dict[str, float]:
        """Consumption so far in the current period, by supply."""
        data = await self._call("POST", "acumuladomensual", vivienda=vivienda) or {}
        return {
            k: v
            for k, d in data.items()
            if isinstance(d, dict) and (v := _num(d.get("acumulado_periodo"))) is not None
        }

    async def consumo_edificio(self, vivienda: str) -> dict[str, float]:
        """Consumption of the whole building in the current period, by supply."""
        data = await self._call("GET", "consumoIndividualMediaComunidad", vivienda=vivienda) or {}
        return {k: v for k, x in data.items() if (v := _num(x)) is not None}

    async def facturas(self, vivienda: str) -> list[dict[str, Any]]:
        """Bills, newest first."""
        rows = await self._call("GET", "facturas", vivienda=vivienda) or []
        return sorted(
            rows,
            key=lambda f: (
                parse_date(f.get("fecha_fin_periodo")) or datetime.min.replace(tzinfo=UTC)
            ),
            reverse=True,
        )

    async def saldo(self, vivienda: str, comunidad: str) -> float | None:
        """Balance of prepaid dwellings."""
        return _num(
            await self._call("GET", "saldo_vivienda", vivienda=vivienda, comunidad=comunidad)
        )

    async def recargas(self, vivienda: str) -> list[dict[str, Any]]:
        """Top-ups of prepaid dwellings."""
        return await self._call("GET", "recargas", vivienda=vivienda) or []

    async def prediccion(self, vivienda: str) -> list[dict[str, Any]]:
        """Weather based consumption forecast (empty in many buildings)."""
        return await self._call("POST", "prediccionConsumoMeteo", vivienda=vivienda) or []
