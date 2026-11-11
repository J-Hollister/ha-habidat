"""Fixtures: a fake Habidat account with one dwelling and heating."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.habidat.api import Lectura, Tipo, Vivienda

VIVIENDA = Vivienda(
    id="1001",
    comunidad_id="7",
    comunidad="Comunidad Prueba",
    calle="Calle Falsa",
    portal="3",
    piso="2",
    mano="B",
    municipio="Ciudad",
)
OTRA = Vivienda(
    id="1002",
    comunidad_id="7",
    comunidad="Comunidad Prueba",
    calle="Calle Falsa",
    portal="3",
    piso="3",
    mano="B",
)
CALEF = Tipo(nombre="CALEF", alias="calefacción", unidades="kWh")


def lecturas(desde: date, dias: int, inicio: float = 1000.0, coste: bool = False) -> list[Lectura]:
    """``dias`` readings of 10 kWh/day starting at ``desde``."""
    out = []
    total = inicio
    for i in range(dias):
        total += 10
        out.append(
            Lectura(
                dia=desde + timedelta(days=i),
                incremento=10.0,
                acumulado_periodo=10.0 * (i + 1),
                lectura=total,
                importe_incremento=1.5 if coste else None,
                equipos=[{"serie": "X1", "lectura": total}],
            )
        )
    return out


FACTURAS = [
    {
        "fecha_inicio_periodo": {"$date": {"$numberLong": "1774483200000"}},
        "fecha_fin_periodo": {"$date": {"$numberLong": "1779667200000"}},
        "CALEF": {"consumo_periodo": 230.8, "importe_periodo": 65.86},
        "importe_total": 99.59,
        "importes_fijos": {"Gas": 33.73},
        "importes_variables": {"CALEF": 65.86},
        "precios_variables": {"CALEF": 0.285354},
    },
    {
        "fecha_inicio_periodo": {"$date": {"$numberLong": "1772064000000"}},
        "fecha_fin_periodo": {"$date": {"$numberLong": "1774396800000"}},
        "importe_total": 115.88,
        "precios_variables": {"CALEF": 0.140281},
    },
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Recorder (a dependency) and custom_components/ in every test."""
    return


@pytest.fixture
def mock_habidat():
    """Patch every network call of the client."""
    hoy = date.today()
    inicio = hoy.replace(day=1)

    async def lecturas_mes(comunidad, vivienda, mes):
        y, m = (int(x) for x in mes.split("-"))
        primero = date(y, m, 1)
        if primero > hoy:
            return {}, {}
        ult = min(
            hoy - timedelta(days=2),
            (primero + timedelta(days=32)).replace(day=1) - timedelta(days=1),
        )
        dias = (ult - primero).days + 1
        if dias <= 0:
            return {}, {}
        base_lectura = 1000.0 + 10 * (primero - date(2025, 1, 1)).days
        return {"CALEF": lecturas(primero, dias, base_lectura, coste=primero < inicio)}, {}

    mocks = {
        "login": AsyncMock(return_value={"id": "1", "tipo": "vecino"}),
        "viviendas": AsyncMock(return_value=[VIVIENDA]),
        "info_comunidad": AsyncMock(return_value={"prepago": "0", "cantidad_viviendas": 45}),
        "tipos": AsyncMock(return_value=[CALEF]),
        "ultimas_lecturas": AsyncMock(
            return_value={"CALEF": lecturas(hoy - timedelta(days=30), 29, 5000.0)}
        ),
        "lecturas_mes": AsyncMock(side_effect=lecturas_mes),
        "informe_mes": AsyncMock(
            return_value={
                "CALEF": {
                    "v": {
                        "seleccionado": [
                            {
                                "media_periodo": 37.6,
                                "suma_periodo": 1165.8,
                                "importe_periodo": 136.45,
                            }
                        ],
                        "anterior": [{"media_periodo": 12.5}],
                    },
                    "c": {"seleccionado": [{"media_periodo": 1776.95}]},
                }
            }
        ),
        "meses_informe": AsyncMock(return_value=["2025-01", "2025-02"]),
        "inicio_periodo": AsyncMock(return_value=inicio),
        "acumulado_periodo": AsyncMock(return_value={"CALEF": 52.6}),
        "consumo_edificio": AsyncMock(return_value={"CALEF": 2928.2}),
        "facturas": AsyncMock(return_value=FACTURAS),
        "saldo": AsyncMock(return_value=None),
        "recomendacion": AsyncMock(return_value="Baja las persianas por la noche."),
    }
    with patch.multiple("custom_components.habidat.api.HabidatClient", **mocks):
        yield mocks
