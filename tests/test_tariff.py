"""Tests for the EWZ tariff model (:mod:`smart_place_client.tariff`).

Ground truth is the user's two ZEV member invoices (X0000025329 /
X0000025513): the 2025 rate table must reproduce every monthly line to
the cent, and the HT/NT calendar must match the server-side bucketing
they bill (verified live 2026-07-13 — see DESIGN.md).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smart_place_client.tariff import (
    TARIFF_TZ,
    TARIFFS,
    energy_cost_chf,
    is_high_tariff,
    local_day_start,
    local_month_start,
    next_tariff_boundary,
    price_chf_per_kwh,
    rates_for,
    today_range_epochs,
)


def _zrh(spec: str) -> datetime:
    return datetime.fromisoformat(spec).replace(tzinfo=TARIFF_TZ)


# -- HT/NT calendar ---------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("2026-07-13 05:59", False),  # Monday before window
        ("2026-07-13 06:00", True),  # Monday window start
        ("2026-07-13 21:59", True),  # Monday window end - 1min
        ("2026-07-13 22:00", False),  # Monday window end
        ("2026-07-18 12:00", True),  # Saturday is an HT day
        ("2026-07-19 12:00", False),  # Sunday is all NT
        ("2026-07-19 06:00", False),  # ... even inside the weekday window
    ],
)
def test_high_tariff_window(when: str, expected: bool) -> None:
    assert is_high_tariff(_zrh(when)) is expected


def test_high_tariff_accepts_naive_and_foreign_tz() -> None:
    """Naive datetimes read as Zurich wall clock; aware ones are converted."""
    assert is_high_tariff(datetime(2026, 7, 13, 12, 0)) is True
    # 05:30 UTC in July == 07:30 CEST -> inside the window.
    assert is_high_tariff(datetime(2026, 7, 13, 5, 30, tzinfo=UTC)) is True


# -- rate table + staleness --------------------------------------------------


def test_2025_all_in_rates_match_invoice_components() -> None:
    tariff = TARIFFS[2025]
    assert tariff.total_ht == pytest.approx(0.2775)
    assert tariff.total_nt == pytest.approx(0.1685)


def test_2026_all_in_rates_match_published_sheet() -> None:
    tariff = TARIFFS[2026]
    assert tariff.total_ht == pytest.approx(0.2625)
    assert tariff.total_nt == pytest.approx(0.1590)


def test_rates_for_uses_calendar_year() -> None:
    tariff, stale = rates_for(_zrh("2025-12-31 23:59"))
    assert (tariff.year, stale) == (2025, False)
    tariff, stale = rates_for(_zrh("2026-01-01 00:00"))
    assert (tariff.year, stale) == (2026, False)


def test_rates_for_flags_missing_future_year_as_stale() -> None:
    """A year without a table falls back to the latest known one, flagged."""
    future = max(TARIFFS) + 1
    tariff, stale = rates_for(_zrh(f"{future}-06-01 12:00"))
    assert tariff.year == max(TARIFFS)
    assert stale is True


def test_price_now_switches_at_window_edges() -> None:
    assert price_chf_per_kwh(_zrh("2026-07-13 12:00")) == pytest.approx(0.2625)
    assert price_chf_per_kwh(_zrh("2026-07-13 23:00")) == pytest.approx(0.1590)


# -- invoice reproduction ----------------------------------------------------


@pytest.mark.parametrize(
    ("ht_kwh", "nt_kwh", "pv_kwh", "invoice_chf"),
    [
        (23, 14, 5, 14.71),  # Aug 2025
        (36, 21, 6, 19.70),  # Sep 2025
        (95, 38, 9, 39.53),  # Oct 2025
        (106, 51, 7, 44.38),  # Nov 2025
        (105, 54, 3, 43.83),  # Dec 2025
    ],
)
def test_2025_invoices_reproduce_to_the_cent(ht_kwh: int, nt_kwh: int, pv_kwh: int, invoice_chf: float) -> None:
    """Every monthly invoice total = buckets x table (+ PV line + fixed fee).

    The invoice rounds per line item, so the comparison allows the
    accumulated rounding of the eight lines (< 4 Rappen).
    """
    tariff = TARIFFS[2025]
    assert tariff.pv_chf_kwh is not None
    model = energy_cost_chf(ht_kwh, nt_kwh, tariff) + pv_kwh * tariff.pv_chf_kwh + tariff.fixed_chf_month
    assert model == pytest.approx(invoice_chf, abs=0.04)


# -- boundaries + today range -----------------------------------------------


def test_next_tariff_boundary_sequence() -> None:
    """From a Monday noon: 22:00 -> midnight -> 06:00 next day."""
    b1 = next_tariff_boundary(_zrh("2026-07-13 12:00"))
    assert (b1.hour, b1.day) == (22, 13)
    b2 = next_tariff_boundary(b1)
    assert (b2.hour, b2.day) == (0, 14)
    b3 = next_tariff_boundary(b2)
    assert (b3.hour, b3.day) == (6, 14)


def test_next_tariff_boundary_handles_dst_days() -> None:
    """On DST-change days the boundary lands on the wall-clock hour."""
    # Spring forward: 2026-03-29 02:00 CET -> 03:00 CEST (23h day).
    boundary = next_tariff_boundary(_zrh("2026-03-29 01:30"))
    assert boundary.hour == 6
    assert boundary.utcoffset() is not None
    # Fall back: 2026-10-25 (25h day).
    boundary = next_tariff_boundary(_zrh("2026-10-25 01:30"))
    assert boundary.hour == 6


def test_today_range_epochs_cover_the_local_day() -> None:
    von, bis = today_range_epochs(_zrh("2026-07-13 15:00"))
    assert bis - von == 24 * 3600
    start = datetime.fromtimestamp(von, tz=TARIFF_TZ)
    assert (start.hour, start.minute, start.day) == (0, 0, 13)


def test_today_range_epochs_dst_days_are_23_or_25_hours() -> None:
    von, bis = today_range_epochs(_zrh("2026-03-29 12:00"))
    assert bis - von == 23 * 3600
    von, bis = today_range_epochs(_zrh("2026-10-25 12:00"))
    assert bis - von == 25 * 3600


def test_local_period_starts() -> None:
    now = _zrh("2026-07-13 15:30")
    assert local_day_start(now).isoformat() == "2026-07-13T00:00:00+02:00"
    assert local_month_start(now).isoformat() == "2026-07-01T00:00:00+02:00"
    # the month start keeps its own offset (March 1 is CET) even when
    # queried from after the DST switch (March 31 is CEST)
    start = local_month_start(_zrh("2026-03-31 12:00"))
    assert start.isoformat() == "2026-03-01T00:00:00+01:00"
