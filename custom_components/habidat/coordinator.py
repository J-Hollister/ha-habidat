"""Data update coordinator for Habidat."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    HabidatAuthError,
    HabidatClient,
    HabidatError,
    Lectura,
    Tipo,
    Vivienda,
)
from .const import DOMAIN, STATISTICS_MAX_MONTHS, STATISTICS_WINDOW_MONTHS, UPDATE_INTERVAL
from .statistics import async_has_statistics, async_import_history, statistic_id

_LOGGER = logging.getLogger(__name__)

type HabidatConfigEntry = ConfigEntry[HabidatCoordinator]


@dataclass
class Suministro:
    """Everything known about one supply (heating, hot water...)."""

    tipo: Tipo
    lecturas: list[Lectura] = field(default_factory=list)
    acumulado_periodo: float | None = None
    consumo_edificio: float | None = None
    media_diaria_mes: float | None = None
    media_diaria_mes_anterior_ano: float | None = None
    consumo_mes: float | None = None
    importe_mes: float | None = None
    media_diaria_edificio_mes: float | None = None
    precio_ultimo_recibo: float | None = None

    @property
    def ultima(self) -> Lectura | None:
        """Newest reading."""
        return self.lecturas[-1] if self.lecturas else None


@dataclass
class HabidatData:
    """Snapshot handed to the entities."""

    vivienda: Vivienda
    comunidad: dict[str, Any]
    inicio_periodo: date | None
    suministros: dict[str, Suministro]
    facturas: list[dict[str, Any]]
    saldo: float | None
    numero_viviendas: int | None


def _mes(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _meses_atras(desde: date, n: int) -> list[str]:
    """``n`` months ending in the month of ``desde``, oldest first."""
    y, m = desde.year, desde.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


class HabidatCoordinator(DataUpdateCoordinator[HabidatData]):
    """Polls Habidat for one dwelling."""

    config_entry: HabidatConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HabidatConfigEntry,
        client: HabidatClient,
        vivienda_id: str,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{vivienda_id}",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.vivienda_id = vivienda_id
        self._vivienda: Vivienda | None = None
        self._historico_cargado = False

    async def _async_update_data(self) -> HabidatData:
        try:
            return await self._fetch()
        except HabidatAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HabidatError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> HabidatData:
        c = self.client
        vid = self.vivienda_id

        if self._vivienda is None:
            for v in await c.viviendas():
                if v.id == vid:
                    self._vivienda = v
                    break
            else:
                raise HabidatError(f"Dwelling {vid} is no longer in the account")
        vivienda = self._vivienda
        cid = vivienda.comunidad_id

        comunidad = await c.info_comunidad(vid)
        tipos = await c.tipos(vid)
        ultimas = await c.ultimas_lecturas(vid)
        if not tipos:
            tipos = [Tipo(nombre=t, alias=t, unidades=None) for t in ultimas]
        inicio = await c.inicio_periodo(vid)
        acumulado = await c.acumulado_periodo(vid)
        edificio = await c.consumo_edificio(vid)
        facturas = await c.facturas(vid)

        prepago = str(comunidad.get("prepago", "0")) == "1"
        saldo = await c.saldo(vid, cid) if prepago else None

        hoy = dt_util.now().date()
        informe = await c.informe_mes(cid, vid, _mes(hoy))
        # Early in the month the current one may still be empty.
        if not informe:
            informe = await c.informe_mes(cid, vid, _mes(hoy.replace(day=1) - timedelta(days=1)))

        numero = comunidad.get("cantidad_viviendas")
        try:
            numero_viviendas = int(numero) if numero is not None else None
        except (TypeError, ValueError):
            numero_viviendas = None

        ultimo_recibo = facturas[0] if facturas else {}
        suministros: dict[str, Suministro] = {}
        for tipo in tipos:
            s = Suministro(tipo=tipo, lecturas=ultimas.get(tipo.nombre, []))
            s.acumulado_periodo = acumulado.get(tipo.nombre)
            s.consumo_edificio = edificio.get(tipo.nombre)
            s.precio_ultimo_recibo = (ultimo_recibo.get("precios_variables") or {}).get(tipo.nombre)
            mes = informe.get(tipo.nombre) or {}
            vivienda_mes = mes.get("v") or {}
            edificio_mes = mes.get("c") or {}
            if sel := (vivienda_mes.get("seleccionado") or [None])[0]:
                s.media_diaria_mes = sel.get("media_periodo")
                s.consumo_mes = sel.get("suma_periodo")
                s.importe_mes = sel.get("importe_periodo")
            if ant := (vivienda_mes.get("anterior") or [None])[0]:
                s.media_diaria_mes_anterior_ano = ant.get("media_periodo")
            if (sel_c := (edificio_mes.get("seleccionado") or [None])[0]) and numero_viviendas:
                media = sel_c.get("media_periodo")
                if media is not None:
                    s.media_diaria_edificio_mes = media / numero_viviendas
            suministros[tipo.nombre] = s

        data = HabidatData(
            vivienda=vivienda,
            comunidad=comunidad,
            inicio_periodo=inicio,
            suministros=suministros,
            facturas=facturas,
            saldo=saldo,
            numero_viviendas=numero_viviendas,
        )

        try:
            await self._update_statistics(data, hoy)
        except HabidatAuthError:
            raise
        except HabidatError as err:
            # The sensors are fine; the history will be retried next time.
            _LOGGER.warning("Could not update Habidat history: %s", err)
        return data

    async def _update_statistics(self, data: HabidatData, hoy: date) -> None:
        """Import daily history (consumption and cost) into long-term statistics."""
        v = data.vivienda
        if not self._historico_cargado and data.suministros:
            # After a restart: only reload everything if nothing was imported yet.
            ya = [
                await async_has_statistics(self.hass, statistic_id(v, s.tipo, "consumo"))
                for s in data.suministros.values()
            ]
            self._historico_cargado = all(ya)
        if not self._historico_cargado:
            meses = await self.client.meses_informe(v.comunidad_id, v.id)
            primero = min(meses) if meses else None
            todos = _meses_atras(hoy, STATISTICS_MAX_MONTHS)
            if primero:
                todos = [m for m in todos if m >= primero]
            primera_vez = True
        else:
            todos = _meses_atras(hoy, STATISTICS_WINDOW_MONTHS)
            primera_vez = False

        por_tipo: dict[str, list[Lectura]] = {}
        for mes in todos:
            sel, _ = await self.client.lecturas_mes(v.comunidad_id, v.id, mes)
            for tipo, lecturas in sel.items():
                por_tipo.setdefault(tipo, []).extend(lecturas)

        # The monthly report lags a bit behind the "last readings" list.
        for tipo, s in data.suministros.items():
            dias = {x.dia for x in por_tipo.get(tipo, [])}
            por_tipo.setdefault(tipo, []).extend(x for x in s.lecturas if x.dia not in dias)

        for tipo, lecturas in por_tipo.items():
            suministro = data.suministros.get(tipo)
            if suministro is None:
                continue
            lecturas.sort(key=lambda x: x.dia)
            await async_import_history(self.hass, v, suministro.tipo, lecturas, full=primera_vez)
        self._historico_cargado = True
