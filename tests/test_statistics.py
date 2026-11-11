"""Daily cost: the bill wins over Habidat's own daily estimate."""

from __future__ import annotations

from datetime import date

from custom_components.habidat.api import Lectura
from custom_components.habidat.coordinator import precios_recibos
from custom_components.habidat.statistics import coste_dia

from .conftest import FACTURAS


def _dia(d: date, kwh: float, importe: float | None) -> Lectura:
    return Lectura(
        dia=d, incremento=kwh, acumulado_periodo=None, lectura=None, importe_incremento=importe
    )


def test_precio_del_recibo() -> None:
    """A day covered by a bill is priced with that bill, not with Habidat's figure."""
    precios = precios_recibos(FACTURAS, "CALEF")
    assert (date(2026, 3, 26), date(2026, 5, 25), 0.285354) in precios
    # Habidat said 9.99 € for this day; the bill says 10 kWh × 0.285354
    assert coste_dia(_dia(date(2026, 4, 10), 10.0, 9.99), precios) == 2.85354


def test_sin_recibo() -> None:
    """Without a bill, Habidat's estimate (or nothing) is used."""
    precios = precios_recibos(FACTURAS, "CALEF")
    assert coste_dia(_dia(date(2026, 11, 3), 10.0, 1.2), precios) == 1.2
    assert coste_dia(_dia(date(2026, 11, 3), 10.0, None), precios) is None


def test_recibo_sin_precio() -> None:
    """Bills without a price for the supply are ignored."""
    assert (
        precios_recibos(
            [{"fecha_inicio_periodo": "2026-01-01", "fecha_fin_periodo": "2026-01-31"}], "CALEF"
        )
        == []
    )


def test_precio_medio() -> None:
    """Average price: everything paid divided by all the kWh of recent bills."""
    from custom_components.habidat.coordinator import precio_medio

    facturas = [
        {
            "fecha_fin_periodo": "2026-05-25",
            "CALEF": {"consumo_periodo": 230.8},
            "importes_variables": {"CALEF": 65.86},
        },
        {
            "fecha_fin_periodo": "2026-01-25",
            "CALEF": {"consumo_periodo": 979.4},
            "importes_variables": {"CALEF": 107.68},
        },
        {
            "fecha_fin_periodo": "2023-12-31",
            "CALEF": {"consumo_periodo": 4795.3},
            "importes_variables": {"CALEF": 390.77},
        },
    ]
    precio, n = precio_medio(facturas, "CALEF", date(2024, 11, 11))
    assert n == 2
    assert precio == round((65.86 + 107.68) / (230.8 + 979.4), 6)
    assert precio_medio([], "CALEF", date(2024, 1, 1)) == (None, 0)
