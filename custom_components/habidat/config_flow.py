"""Config flow for Habidat."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import crear_cliente
from .api import HabidatAuthError, HabidatConnectionError, HabidatError, Vivienda
from .const import CONF_VIVIENDA, DOMAIN

_LOGGER = logging.getLogger(__name__)

CREDENCIALES = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


class HabidatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Habidat account and the dwelling."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._datos: dict[str, Any] = {}
        self._viviendas: list[Vivienda] = []
        self._detalle = ""

    async def _probar(self, email: str, password: str) -> tuple[list[Vivienda], str | None]:
        self._detalle = ""
        client = crear_cliente(self.hass, email, password, auto_cleanup=False)
        try:
            await client.login()
            return await client.viviendas(), None
        except HabidatAuthError as err:
            _LOGGER.info("Habidat login rejected: %s", err)
            self._detalle = str(err)
            return [], "invalid_auth"
        except HabidatConnectionError:
            return [], "cannot_connect"
        except HabidatError:
            _LOGGER.exception("Unexpected answer from Habidat")
            return [], "unknown"
        finally:
            client.close()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Email and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            viviendas, error = await self._probar(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            elif not viviendas:
                errors["base"] = "no_viviendas"
            else:
                self._datos = user_input
                self._viviendas = viviendas
                configuradas = {
                    e.unique_id for e in self._async_current_entries(include_ignore=False)
                }
                libres = [v for v in viviendas if v.id not in configuradas]
                if not libres:
                    return self.async_abort(reason="already_configured")
                if len(libres) == 1:
                    return await self._crear(libres[0])
                self._viviendas = libres
                return await self.async_step_vivienda()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CREDENCIALES, user_input),
            errors=errors,
            description_placeholders={"detalle": self._detalle},
        )

    async def async_step_vivienda(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a dwelling when the account has several."""
        if user_input is not None:
            for v in self._viviendas:
                if v.id == user_input[CONF_VIVIENDA]:
                    return await self._crear(v)
        opciones = [
            SelectOptionDict(value=v.id, label=f"{v.nombre} · {v.comunidad}")
            for v in self._viviendas
        ]
        return self.async_show_form(
            step_id="vivienda",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VIVIENDA): SelectSelector(
                        SelectSelectorConfig(options=opciones)
                    )
                }
            ),
        )

    async def _crear(self, vivienda: Vivienda) -> ConfigFlowResult:
        await self.async_set_unique_id(vivienda.id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=vivienda.nombre,
            data={**self._datos, CONF_VIVIENDA: vivienda.id},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The password changed or the session is refused."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the credentials again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            viviendas, error = await self._probar(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            elif entry.data[CONF_VIVIENDA] not in {v.id for v in viviendas}:
                errors["base"] = "wrong_account"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                CREDENCIALES, {CONF_EMAIL: entry.data[CONF_EMAIL]}
            ),
            errors=errors,
            description_placeholders={"detalle": self._detalle},
        )
