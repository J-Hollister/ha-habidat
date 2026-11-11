"""Diagnostics for Habidat (personal data redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .coordinator import HabidatConfigEntry

REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "email",
    "nombre",
    "calle",
    "portal",
    "piso",
    "mano",
    "comunidad",
    "comunidad_nombre",
    "cif_comunidad",
    "cuenta_pago",
    "latitud",
    "longitud",
    "serie",
    "administrador",
    "vecino",
    "titular",
    "iban",
    "dni",
    "nif",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HabidatConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    data = entry.runtime_data.data
    suministros = {
        k: {
            "tipo": asdict(s.tipo),
            "ultimas_lecturas": [
                {"dia": x.dia.isoformat(), "incremento": x.incremento, "lectura": x.lectura}
                for x in s.lecturas[-5:]
            ],
            "acumulado_periodo": s.acumulado_periodo,
            "consumo_edificio": s.consumo_edificio,
            "media_diaria_mes": s.media_diaria_mes,
            "precio_ultimo_recibo": s.precio_ultimo_recibo,
            "precio_medio": s.precio_medio,
        }
        for k, s in data.suministros.items()
    }
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "comunidad": data.comunidad,
            "inicio_periodo": str(data.inicio_periodo),
            "numero_viviendas": data.numero_viviendas,
            "suministros": suministros,
            "ultimo_recibo": data.facturas[0] if data.facturas else None,
            "recibos": len(data.facturas),
        },
        REDACT,
    )
