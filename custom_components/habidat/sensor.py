"""Sensors for Habidat."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import parse_date
from .const import ATTRIBUTION, DOMAIN
from .coordinator import HabidatConfigEntry, HabidatCoordinator, HabidatData, Suministro
from .statistics import statistic_id, unidad

PARALLEL_UPDATES = 0

type Valor = float | str | date | datetime | None


def _dia(value: Any) -> str | None:
    parsed = parse_date(value)
    return parsed.date().isoformat() if parsed else None


def _clase(unit_class: str | None) -> SensorDeviceClass | None:
    if unit_class == "energy":
        return SensorDeviceClass.ENERGY
    if unit_class == "volume":
        return SensorDeviceClass.WATER
    return None


# --------------------------------------------------------------------------
# Per supply
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SuministroDescription(SensorEntityDescription):
    """Sensor about one supply."""

    value_fn: Callable[[Suministro, HabidatData], Valor]
    attrs_fn: Callable[[Suministro, HabidatData], dict[str, Any]] | None = None
    unit: str = "supply"  # "supply", "money", "price", "percent" or "none"
    supply_class: bool = False


def _coste_estimado(s: Suministro, _: HabidatData) -> float | None:
    if s.acumulado_periodo is None or s.precio_ultimo_recibo is None:
        return None
    return round(s.acumulado_periodo * s.precio_ultimo_recibo, 2)


def _porcentaje(s: Suministro, _: HabidatData) -> float | None:
    if not s.consumo_edificio or s.acumulado_periodo is None:
        return None
    return round(100 * s.acumulado_periodo / s.consumo_edificio, 2)


def _attrs_lectura(s: Suministro, _: HabidatData) -> dict[str, Any]:
    u = s.ultima
    if not u:
        return {}
    return {
        "fecha": u.dia.isoformat(),
        "contadores": [{"serie": e.get("serie"), "lectura": e.get("lectura")} for e in u.equipos],
    }


def _attrs_ultimo_dia(s: Suministro, _: HabidatData) -> dict[str, Any]:
    if not s.ultima:
        return {}
    return {
        "fecha": s.ultima.dia.isoformat(),
        "ultimos_30_dias": {x.dia.isoformat(): x.incremento for x in s.lecturas},
    }


def _attrs_mes(s: Suministro, _: HabidatData) -> dict[str, Any]:
    return {
        "consumo_mes": s.consumo_mes,
        "importe_mes": round(s.importe_mes, 2) if s.importe_mes is not None else None,
        "media_diaria_mismo_mes_ano_anterior": (
            round(s.media_diaria_mes_anterior_ano, 2)
            if s.media_diaria_mes_anterior_ano is not None
            else None
        ),
    }


SUMINISTRO_SENSORES: tuple[SuministroDescription, ...] = (
    SuministroDescription(
        key="lectura",
        translation_key="lectura",
        state_class=SensorStateClass.TOTAL_INCREASING,
        supply_class=True,
        suggested_display_precision=1,
        value_fn=lambda s, _: s.ultima.lectura if s.ultima else None,
        attrs_fn=_attrs_lectura,
    ),
    SuministroDescription(
        key="ultimo_dia",
        translation_key="ultimo_dia",
        supply_class=True,
        suggested_display_precision=1,
        value_fn=lambda s, _: s.ultima.incremento if s.ultima else None,
        attrs_fn=_attrs_ultimo_dia,
    ),
    SuministroDescription(
        key="fecha_lectura",
        translation_key="fecha_lectura",
        device_class=SensorDeviceClass.DATE,
        unit="none",
        value_fn=lambda s, _: s.ultima.dia if s.ultima else None,
    ),
    SuministroDescription(
        key="periodo",
        translation_key="periodo",
        state_class=SensorStateClass.TOTAL,
        supply_class=True,
        suggested_display_precision=1,
        value_fn=lambda s, _: s.acumulado_periodo,
    ),
    SuministroDescription(
        key="coste_estimado",
        translation_key="coste_estimado",
        device_class=SensorDeviceClass.MONETARY,
        unit="money",
        suggested_display_precision=2,
        value_fn=_coste_estimado,
        attrs_fn=lambda s, _: {"precio_usado": s.precio_ultimo_recibo},
    ),
    SuministroDescription(
        key="precio",
        translation_key="precio",
        unit="price",
        suggested_display_precision=4,
        value_fn=lambda s, _: s.precio_ultimo_recibo,
    ),
    SuministroDescription(
        key="media_diaria_mes",
        translation_key="media_diaria_mes",
        suggested_display_precision=1,
        value_fn=lambda s, _: (
            round(s.media_diaria_mes, 2) if s.media_diaria_mes is not None else None
        ),
        attrs_fn=_attrs_mes,
    ),
    SuministroDescription(
        key="media_diaria_edificio",
        translation_key="media_diaria_edificio",
        suggested_display_precision=1,
        value_fn=lambda s, _: (
            round(s.media_diaria_edificio_mes, 2)
            if s.media_diaria_edificio_mes is not None
            else None
        ),
    ),
    SuministroDescription(
        key="edificio_periodo",
        translation_key="edificio_periodo",
        supply_class=True,
        suggested_display_precision=0,
        value_fn=lambda s, _: s.consumo_edificio,
        attrs_fn=lambda s, d: {"viviendas": d.numero_viviendas},
    ),
    SuministroDescription(
        key="porcentaje_edificio",
        translation_key="porcentaje_edificio",
        unit="percent",
        suggested_display_precision=2,
        value_fn=_porcentaje,
    ),
)


# --------------------------------------------------------------------------
# Per dwelling
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ViviendaDescription(SensorEntityDescription):
    """Sensor about the dwelling."""

    value_fn: Callable[[HabidatData], Valor]
    attrs_fn: Callable[[HabidatData], dict[str, Any]] | None = None
    exists_fn: Callable[[HabidatData], bool] = lambda _: True


def _attrs_recibo(d: HabidatData) -> dict[str, Any]:
    if not d.facturas:
        return {}
    f = d.facturas[0]
    consumos = {
        k: v.get("consumo_periodo")
        for k, v in f.items()
        if isinstance(v, dict) and "consumo_periodo" in v
    }
    return {
        "periodo_desde": _dia(f.get("fecha_inicio_periodo")),
        "periodo_hasta": _dia(f.get("fecha_fin_periodo")),
        "emitido": _dia(f.get("fecha_emision")),
        "fecha_cargo": _dia(f.get("fecha_cargo")),
        "consumos": consumos,
        "importes_variables": f.get("importes_variables"),
        "importes_fijos": f.get("importes_fijos"),
        "precios_variables": f.get("precios_variables"),
        "base_imponible": f.get("importe_base_imponible"),
        "iva": f.get("importe_IVA"),
        "recibos": [
            {
                "desde": _dia(x.get("fecha_inicio_periodo")),
                "hasta": _dia(x.get("fecha_fin_periodo")),
                "total": x.get("importe_total"),
            }
            for x in d.facturas
        ],
    }


VIVIENDA_SENSORES: tuple[ViviendaDescription, ...] = (
    ViviendaDescription(
        key="ultimo_recibo",
        translation_key="ultimo_recibo",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        value_fn=lambda d: d.facturas[0].get("importe_total") if d.facturas else None,
        attrs_fn=_attrs_recibo,
    ),
    ViviendaDescription(
        key="inicio_periodo",
        translation_key="inicio_periodo",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: d.inicio_periodo,
    ),
    ViviendaDescription(
        key="saldo",
        translation_key="saldo",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        value_fn=lambda d: d.saldo,
        exists_fn=lambda d: d.saldo is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HabidatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors."""
    coordinator = entry.runtime_data
    data = coordinator.data
    entities: list[SensorEntity] = [
        ViviendaSensor(coordinator, desc) for desc in VIVIENDA_SENSORES if desc.exists_fn(data)
    ]
    for tipo in data.suministros:
        entities.extend(SuministroSensor(coordinator, desc, tipo) for desc in SUMINISTRO_SENSORES)
    async_add_entities(entities)


class HabidatEntity(CoordinatorEntity[HabidatCoordinator], SensorEntity):
    """Common bits."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: HabidatCoordinator) -> None:
        """Attach to the dwelling device."""
        super().__init__(coordinator)
        v = coordinator.data.vivienda
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, v.id)},
            name=v.nombre,
            manufacturer="ista",
            model="Habidat",
            model_id=v.comunidad,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://plataforma.habidat.es/",
        )


class ViviendaSensor(HabidatEntity):
    """Sensor of the dwelling."""

    entity_description: ViviendaDescription

    def __init__(self, coordinator: HabidatCoordinator, description: ViviendaDescription) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.vivienda_id}_{description.key}"

    @property
    def native_value(self) -> Valor:
        """Value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra attributes."""
        if fn := self.entity_description.attrs_fn:
            return fn(self.coordinator.data)
        return None


class SuministroSensor(HabidatEntity):
    """Sensor of one supply."""

    entity_description: SuministroDescription

    def __init__(
        self,
        coordinator: HabidatCoordinator,
        description: SuministroDescription,
        tipo: str,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._tipo = tipo
        s = coordinator.data.suministros[tipo]
        unit, unit_class = unidad(s.tipo)
        self._attr_unique_id = f"{coordinator.vivienda_id}_{tipo.lower()}_{description.key}"
        self._attr_translation_placeholders = {"suministro": s.tipo.alias.capitalize()}
        if description.supply_class:
            self._attr_device_class = _clase(unit_class)
        match description.unit:
            case "supply":
                self._attr_native_unit_of_measurement = unit
            case "money":
                self._attr_native_unit_of_measurement = CURRENCY_EURO
            case "price":
                self._attr_native_unit_of_measurement = (
                    f"{CURRENCY_EURO}/{unit}" if unit else CURRENCY_EURO
                )
            case "percent":
                self._attr_native_unit_of_measurement = PERCENTAGE
        self._stat_id = statistic_id(coordinator.data.vivienda, s.tipo, "consumo")

    @property
    def _s(self) -> Suministro | None:
        return self.coordinator.data.suministros.get(self._tipo)

    @property
    def available(self) -> bool:
        """Only while the supply is still reported."""
        return super().available and self._s is not None

    @property
    def native_value(self) -> Valor:
        """Value."""
        s = self._s
        return self.entity_description.value_fn(s, self.coordinator.data) if s else None

    @property
    def last_reset(self) -> datetime | None:
        """Start of the billing period for the period total."""
        if self.entity_description.key != "periodo":
            return None
        inicio = self.coordinator.data.inicio_periodo
        return dt_util.start_of_local_day(inicio) if inicio else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra attributes."""
        s = self._s
        attrs: dict[str, Any] = {}
        if s and (fn := self.entity_description.attrs_fn):
            attrs.update(fn(s, self.coordinator.data))
        if self.entity_description.key == "lectura":
            attrs["estadistica"] = self._stat_id
        return attrs or None
