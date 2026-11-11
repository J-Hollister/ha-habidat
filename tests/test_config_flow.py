"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.habidat.api import HabidatAuthError, HabidatConnectionError
from custom_components.habidat.const import CONF_VIVIENDA, DOMAIN

from .conftest import OTRA, VIVIENDA


@pytest.fixture(autouse=True)
def sin_setup():
    """The flow is tested alone; setting up the entry is test_init's job."""
    with patch("custom_components.habidat.async_setup_entry", return_value=True):
        yield


CRED = {CONF_EMAIL: "vecino@example.com", CONF_PASSWORD: "secreta"}


async def test_una_vivienda(hass: HomeAssistant, mock_habidat) -> None:
    """One dwelling: straight to the entry."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    r = await hass.config_entries.flow.async_configure(r["flow_id"], CRED)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "Calle Falsa 3 2-B"
    assert r["data"] == {**CRED, CONF_VIVIENDA: "1001"}
    assert r["result"].unique_id == "1001"
    await hass.async_block_till_done()


async def test_varias_viviendas(hass: HomeAssistant, mock_habidat) -> None:
    """Several dwellings: ask which one."""
    mock_habidat["viviendas"].return_value = [VIVIENDA, OTRA]
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], CRED)
    assert r["type"] is FlowResultType.FORM
    assert r["step_id"] == "vivienda"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_VIVIENDA: "1002"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"][CONF_VIVIENDA] == "1002"
    await hass.async_block_till_done()


async def test_errores(hass: HomeAssistant, mock_habidat) -> None:
    """Wrong password and no connection show an error and let you retry."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    mock_habidat["login"].side_effect = HabidatAuthError("mal")
    r = await hass.config_entries.flow.async_configure(r["flow_id"], CRED)
    assert r["errors"] == {"base": "invalid_auth"}
    mock_habidat["login"].side_effect = HabidatConnectionError("caído")
    r = await hass.config_entries.flow.async_configure(r["flow_id"], CRED)
    assert r["errors"] == {"base": "cannot_connect"}
    mock_habidat["login"].side_effect = None
    r = await hass.config_entries.flow.async_configure(r["flow_id"], CRED)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_ya_configurada(hass: HomeAssistant, mock_habidat) -> None:
    """The same dwelling cannot be added twice."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="1001", data={**CRED, CONF_VIVIENDA: "1001"}
    ).add_to_hass(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], CRED)
    assert r["type"] is FlowResultType.ABORT
    assert r["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_habidat) -> None:
    """New password is stored."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1001", data={**CRED, CONF_VIVIENDA: "1001"})
    entry.add_to_hass(hass)
    r = await entry.start_reauth_flow(hass)
    assert r["step_id"] == "reauth_confirm"
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {CONF_EMAIL: "vecino@example.com", CONF_PASSWORD: "nueva"}
    )
    assert r["type"] is FlowResultType.ABORT
    assert r["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "nueva"
