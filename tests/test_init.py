"""Setup, sensors and statistics."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.habidat.api import HabidatAuthError
from custom_components.habidat.const import CONF_VIVIENDA, DOMAIN

DATA = {CONF_EMAIL: "vecino@example.com", CONF_PASSWORD: "secreta", CONF_VIVIENDA: "1001"}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1001", data=DATA, title="Calle Falsa 3 2-B")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _estado(hass: HomeAssistant, unique_id: str):
    ent = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert ent, unique_id
    return hass.states.get(ent)


async def test_sensores(hass: HomeAssistant, mock_habidat) -> None:
    """All sensors get a value."""
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    lectura = _estado(hass, "1001_calef_lectura")
    assert float(lectura.state) == 5290.0
    assert lectura.attributes["unit_of_measurement"] == "kWh"
    assert lectura.attributes["device_class"] == "energy"
    assert lectura.attributes["state_class"] == "total_increasing"
    assert lectura.attributes["estadistica"] == "habidat:1001_calef_consumo"

    assert float(_estado(hass, "1001_calef_ultimo_dia").state) == 10.0
    assert float(_estado(hass, "1001_calef_periodo").state) == 52.6
    coste = _estado(hass, "1001_calef_coste_estimado")
    assert coste.attributes["recibos_en_la_media"] == 1
    precio = coste.attributes["precio_usado"]
    assert precio in (coste.attributes["precio_mismo_mes"], coste.attributes["precio_medio"])
    assert float(coste.state) == round(52.6 * precio, 2)
    assert float(_estado(hass, "1001_calef_precio").state) == 0.285354
    assert _estado(hass, "1001_calef_precio").attributes["unit_of_measurement"] == "€/kWh"
    assert float(_estado(hass, "1001_calef_media_diaria_mes").state) == 37.6
    assert float(_estado(hass, "1001_calef_media_diaria_edificio").state) == round(1776.95 / 45, 2)
    assert float(_estado(hass, "1001_calef_edificio_periodo").state) == 2928.2
    assert float(_estado(hass, "1001_calef_porcentaje_edificio").state) == round(
        100 * 52.6 / 2928.2, 2
    )

    recibo = _estado(hass, "1001_ultimo_recibo")
    assert float(recibo.state) == 99.59
    assert recibo.attributes["periodo_desde"] == "2026-03-26"
    assert recibo.attributes["periodo_hasta"] == "2026-05-25"
    assert len(recibo.attributes["recibos"]) == 2
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "1001_consejo") is None
    # No prepaid: no balance sensor
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "1001_saldo") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_estadisticas(hass: HomeAssistant, mock_habidat) -> None:
    """Daily history lands on its own day with a running sum."""
    await _setup(hass)
    await async_wait_recording_done(hass)

    inicio = dt_util.as_utc(dt_util.start_of_local_day(dt_util.now().date() - timedelta(days=60)))
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        inicio,
        None,
        {"habidat:1001_calef_consumo", "habidat:1001_calef_coste"},
        "hour",
        None,
        {"sum", "state"},
    )
    consumo = stats["habidat:1001_calef_consumo"]
    assert len(consumo) > 30
    sumas = [x["sum"] for x in consumo]
    assert sumas == sorted(sumas)
    assert all(round(b - a, 6) == 10.0 for a, b in zip(sumas, sumas[1:], strict=False))
    # Each row starts at local midnight
    for fila in consumo:
        local = dt_util.as_local(dt_util.utc_from_timestamp(fila["start"]))
        assert (local.hour, local.minute) == (0, 0)
    assert "habidat:1001_calef_coste" in stats


async def test_reauth_al_fallar(hass: HomeAssistant, mock_habidat) -> None:
    """A refused login starts a reauth flow."""
    mock_habidat["viviendas"].side_effect = HabidatAuthError("caducada")
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)
