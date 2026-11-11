"""Constants for the Habidat (ista España) integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "habidat"

CONF_VIVIENDA: Final = "vivienda"

# How often Habidat is polled. Readings are published once a day (with a lag
# of one or two days), so there is no point in polling more often.
UPDATE_INTERVAL: Final = timedelta(hours=6)

# Months re-imported into long-term statistics on every update. Costs per day
# only appear once the bill of that period is closed, so a window of a few
# months lets those costs land on the days they belong to.
STATISTICS_WINDOW_MONTHS: Final = 6

# Estimated cost of the current period: price of the same month in up to
# this many previous years (from the bills that covered those days)...
PRECIO_MES_ANOS: Final = 3
# A month only counts if it has at least this many daily readings.
DIAS_MINIMOS_MES: Final = 20
# ...or, if those months have no bill, the average of this many years of bills.
PRECIO_MEDIO_ANOS: Final = 2

# Bump to rewrite the whole history once (e.g. after changing how it is computed).
STATISTICS_VERSION: Final = 2

# How far back the first import goes when the platform does not list the
# eligible months.
STATISTICS_MAX_MONTHS: Final = 36

ATTRIBUTION: Final = "Data provided by Habidat (ista)"
