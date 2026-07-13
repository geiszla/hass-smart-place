"""EWZ tariff model: what one kWh costs this apartment right now.

Pure calendar + rate-table logic, no I/O and no HA imports (same rule as
the rest of :mod:`smart_place_client`). The Smart Place server itself
provides the consumption split (``SingelStandUpdate`` HT/NT range buckets,
see :mod:`.messages`); this module supplies the CHF per kWh to multiply
those buckets with.

Provenance / maintenance (READ BEFORE EDITING RATES):

- Swiss basic-supply and grid tariffs change **only on 1 January** (ElCom
  ruled mid-year adjustments impermissible; publication deadline is 31
  August for the following year). One :class:`TariffYear` per calendar
  year is therefore exact, not an approximation.
- 2025 rates were taken verbatim from the user's EWZ ZEV member invoices
  (X0000025329 / X0000025513, periods 2025-07..2025-12) and reproduce
  both invoices to the cent (see ``tests/test_tariff.py``).
- 2026 rates come from the official ewz sheet "Stromtarife 2026"
  (product ewz.natur, grid tariff NNA, City of Zurich) — the same
  product/category the 2025 invoice lines match exactly. New-for-2026
  flat items (SDL, Stromreserve, solidarisierte Kosten) fold into
  ``levies`` since they apply per grid kWh at one rate in both windows.
- **Manual step, once a year (~September):** append next year's
  :class:`TariffYear` from the published ewz sheet, then confirm against
  the first ZEV invoice of that year when it arrives (~April). Until the
  new entry lands, consumers see ``rates_are_stale`` and keep using the
  latest known year.
- ZEV members pay no VAT (the landlord's ZEV is the supplier; ewz only
  bills on its behalf), so these are final CHF figures.

Known model bias: the buckets this multiplies are the raw meter's HT/NT
registers, which *include* the ZEV solar allocation that EWZ later bills
at the cheaper PV rate (2025: 19.52 Rp). The allocation is computed
inside EWZ's ZEV billing from building-level 15-min data and is not
available live (the Smart Place ``PV Anteil Elektro`` chart holds no
usable history — verified 2026-07-13). Against the 2025 invoices the
resulting overestimate was +0.3 % … +2.3 % per month (≈ 0.4 CHF/month).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

TARIFF_TZ: Final = ZoneInfo("Europe/Zurich")

# Hochtarif window: Mon-Sat 06:00-22:00 local; Niedertarif otherwise
# (nights + all of Sunday). Confirmed on the ewz 2025 + 2026 tariff
# sheets AND empirically: bucketing the EWZ 15-min portal data with
# this calendar reproduces the Smart Place server's own HT (STAND 97)
# / NT (STAND 96) range buckets within ~1 % (2026-07-13 analysis), and
# a Sunday range returns HT = 0.
_HT_LAST_WEEKDAY: Final = 5  # Monday=0 .. Saturday=5
_HT_START_HOUR: Final = 6
_HT_END_HOUR: Final = 22


@dataclass(frozen=True, slots=True)
class TariffYear:
    """All-in EWZ rates for one calendar year, CHF per kWh (no VAT).

    ``levies`` bundles every flat per-grid-kWh surcharge billed on top of
    energy + grid usage (kommunale Abgabe, Netzzuschlag/KEV, and from
    2026 the Swissgrid SDL / Stromreserve / solidarisierte Kosten items).
    They apply to grid kWh only — the ZEV solar allocation is exempt,
    which is part of why ``pv_chf_kwh`` is cheaper.

    ``pv_chf_kwh`` is the ZEV-internal solar price. ``None`` = not yet
    published for that year (it is set by the ZEV administration, not on
    the public tariff sheet). It is informational — the live cost
    sensors cannot know the PV allocation anyway (see module docstring).

    ``fixed_chf_month`` is the per-meter metering/billing fee
    ("Messkosten" on the invoice).
    """

    year: int
    energy_ht: float
    energy_nt: float
    grid_ht: float
    grid_nt: float
    levies: float
    fixed_chf_month: float
    pv_chf_kwh: float | None
    source: str

    @property
    def total_ht(self) -> float:
        """All-in high-tariff price per grid kWh."""
        return round(self.energy_ht + self.grid_ht + self.levies, 6)

    @property
    def total_nt(self) -> float:
        """All-in low-tariff price per grid kWh."""
        return round(self.energy_nt + self.grid_nt + self.levies, 6)


TARIFFS: Final[dict[int, TariffYear]] = {
    2025: TariffYear(
        year=2025,
        energy_ht=0.0910,
        energy_nt=0.0470,
        grid_ht=0.1380,
        grid_nt=0.0730,
        levies=0.0255 + 0.0230,  # kommunale Abgabe + Netzzuschlag (KEV)
        fixed_chf_month=5.00,
        pv_chf_kwh=0.1952,
        source="EWZ ZEV invoices X0000025329/X0000025513 (reproduced to the cent)",
    ),
    2026: TariffYear(
        year=2026,
        energy_ht=0.0930,
        energy_nt=0.0490,
        grid_ht=0.1192,
        grid_nt=0.0597,
        # kommunale Abgabe 2.00 + Netzzuschlag 2.30 + SDL 0.27
        # + Stromreserve 0.41 + solidarisierte Kosten 0.05 (all Rp/kWh)
        levies=0.0200 + 0.0230 + 0.0027 + 0.0041 + 0.0005,
        # Official 2026 Messtarif (CHF 6.90/meter/month). The 2025 invoices
        # carried a CHF 5.00 ZEV-service fee instead; confirm against the
        # first 2026 invoice when it arrives.
        fixed_chf_month=6.90,
        pv_chf_kwh=None,  # ZEV solar price 2026 not published; set from the first 2026 invoice
        source="ewz Stromtarife 2026 sheet (ewz.natur + NNA, Stadt Zuerich); unconfirmed by invoice yet",
    ),
}


def _localize(now: datetime) -> datetime:
    """Interpret ``now`` in the installation's tariff timezone.

    Naive datetimes are assumed to already be Zurich wall-clock time
    (convenient for tests); aware ones are converted.
    """
    if now.tzinfo is None:
        return now.replace(tzinfo=TARIFF_TZ)
    return now.astimezone(TARIFF_TZ)


def is_high_tariff(now: datetime) -> bool:
    """True while the Hochtarif window (Mon-Sat 06:00-22:00 local) is active."""
    local = _localize(now)
    return local.weekday() <= _HT_LAST_WEEKDAY and _HT_START_HOUR <= local.hour < _HT_END_HOUR


def rates_for(now: datetime) -> tuple[TariffYear, bool]:
    """Return the rate table for ``now``'s calendar year.

    Falls back to the latest earlier year when the current year has no
    entry yet (the yearly manual update hasn't happened) — the second
    element flags that staleness so consumers can surface it instead of
    silently billing at last year's prices.
    """
    year = _localize(now).year
    if year in TARIFFS:
        return TARIFFS[year], False
    known = sorted(TARIFFS)
    fallback = max((y for y in known if y < year), default=known[0])
    return TARIFFS[fallback], True


def price_chf_per_kwh(now: datetime) -> float:
    """All-in price of one grid kWh drawn at ``now``."""
    tariff, _ = rates_for(now)
    return tariff.total_ht if is_high_tariff(now) else tariff.total_nt


def energy_cost_chf(ht_kwh: float, nt_kwh: float, tariff: TariffYear) -> float:
    """Variable cost of the given HT/NT consumption (no fixed fee, no PV credit)."""
    return round(ht_kwh * tariff.total_ht + nt_kwh * tariff.total_nt, 4)


def next_tariff_boundary(now: datetime) -> datetime:
    """Next wall-clock instant the per-kWh price can change.

    Price flips happen at 06:00 and 22:00 local (HT window edges) and at
    local midnight (weekday change into/out of Sunday; 1 January rate
    change). Returns the earliest such instant strictly after ``now``.
    Days are rebuilt via ``replace``/date arithmetic so DST transitions
    (23 h / 25 h days) land on the correct wall-clock hour.
    """
    local = _localize(now)
    day = local.date()
    candidates = []
    for offset in (0, 1):
        base = datetime(day.year, day.month, day.day, tzinfo=TARIFF_TZ) + timedelta(days=offset)
        candidates.append(base)  # midnight
        candidates.append(base.replace(hour=_HT_START_HOUR))
        candidates.append(base.replace(hour=_HT_END_HOUR))
    return min(c for c in candidates if c > local)


def local_day_start(now: datetime | None = None) -> datetime:
    """Midnight of the current local (Zurich) day, tz-aware."""
    local = _localize(now) if now is not None else datetime.now(tz=TARIFF_TZ)
    return datetime(local.year, local.month, local.day, tzinfo=TARIFF_TZ)


def local_month_start(now: datetime | None = None) -> datetime:
    """Midnight of the 1st of the current local month, tz-aware."""
    local = _localize(now) if now is not None else datetime.now(tz=TARIFF_TZ)
    return datetime(local.year, local.month, 1, tzinfo=TARIFF_TZ)


def today_range_epochs(now: datetime | None = None) -> tuple[int, int]:
    """Epoch-second bounds of the current local day, for ``Commands.ChartStandRange``.

    Mirrors the vendor SPA's ``StartShowChart``: local midnight to next
    local midnight, as integer epoch seconds.
    """
    local = _localize(now) if now is not None else datetime.now(tz=TARIFF_TZ)
    start = datetime(local.year, local.month, local.day, tzinfo=TARIFF_TZ)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())
