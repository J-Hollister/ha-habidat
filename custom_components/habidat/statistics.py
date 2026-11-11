"""Import Habidat's daily history into Home Assistant long-term statistics.

Readings are published with a lag of one or two days. A plain sensor would put
each day's consumption on the day it was downloaded, so the history is also
written as *external statistics* dated on the day it belongs to. Those are the
ones to use in the Energy dashboard.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import Lectura, Tipo, Vivienda
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def unidad(tipo: Tipo) -> tuple[str | None, str | None]:
    """Home Assistant unit and unit class for a Habidat unit string."""
    raw = (tipo.unidades or "").strip().lower().replace("³", "3")
    if raw == "kwh":
        return UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS
    if raw == "mwh":
        return UnitOfEnergy.MEGA_WATT_HOUR, EnergyConverter.UNIT_CLASS
    if raw in ("m3", "m^3"):
        return UnitOfVolume.CUBIC_METERS, VolumeConverter.UNIT_CLASS
    if raw in ("l", "litros", "litres", "liters"):
        return UnitOfVolume.LITERS, VolumeConverter.UNIT_CLASS
    # Heat cost allocators count "units" with no physical meaning.
    return (tipo.unidades or None), None


def statistic_id(vivienda: Vivienda, tipo: Tipo, que: str) -> str:
    """``habidat:<dwelling>_<supply>_<consumo|coste>``."""
    return f"{DOMAIN}:{slugify(f'{vivienda.id}_{tipo.nombre}_{que}')}"


async def async_has_statistics(hass: HomeAssistant, stat_id: str) -> bool:
    """Whether something was already imported under ``stat_id``."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, False, {"sum"}
    )
    return bool(last.get(stat_id))


async def _sum_before(hass: HomeAssistant, stat_id: str, start: datetime) -> float:
    """Running sum of the last row before ``start`` (0 if none)."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(days=400),
        start,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    serie = rows.get(stat_id) or []
    if not serie:
        return 0.0
    return float(serie[-1].get("sum") or 0.0)


def _start(dia) -> datetime:
    """00:00 local time of the reading's day, in UTC."""
    return dt_util.as_utc(dt_util.start_of_local_day(dia))


async def async_import_history(
    hass: HomeAssistant,
    vivienda: Vivienda,
    tipo: Tipo,
    lecturas: list[Lectura],
    *,
    full: bool,
) -> None:
    """Write (or rewrite) daily consumption and cost for ``lecturas``.

    Rows that already exist are overwritten, so re-importing a window is safe
    and lets late costs (they appear once the bill is closed) fill in.
    """
    if not lecturas:
        return
    unit, unit_class = unidad(tipo)
    nombre = f"{vivienda.nombre} {tipo.alias}".strip()
    inicio = _start(lecturas[0].dia)

    # Consumption
    cons_id = statistic_id(vivienda, tipo, "consumo")
    total = 0.0 if full else await _sum_before(hass, cons_id, inicio)
    filas: list[StatisticData] = []
    for x in lecturas:
        total += x.incremento or 0.0
        fila: StatisticData = {"start": _start(x.dia), "sum": total}
        if x.lectura is not None:
            fila["state"] = x.lectura
        filas.append(fila)
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=nombre,
            source=DOMAIN,
            statistic_id=cons_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        ),
        filas,
    )

    # Cost (only once the platform has priced at least one day)
    if not any(x.importe_incremento is not None for x in lecturas):
        coste_id = statistic_id(vivienda, tipo, "coste")
        if not await async_has_statistics(hass, coste_id):
            return
    coste_id = statistic_id(vivienda, tipo, "coste")
    total = 0.0 if full else await _sum_before(hass, coste_id, inicio)
    filas = []
    for x in lecturas:
        total += x.importe_incremento or 0.0
        filas.append({"start": _start(x.dia), "state": x.importe_incremento or 0.0, "sum": total})
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{nombre} (coste)",
            source=DOMAIN,
            statistic_id=coste_id,
            unit_class=None,
            unit_of_measurement=CURRENCY_EURO,
        ),
        filas,
    )
    _LOGGER.debug(
        "Imported %s days of %s for %s (full=%s)", len(lecturas), tipo.nombre, vivienda.id, full
    )
