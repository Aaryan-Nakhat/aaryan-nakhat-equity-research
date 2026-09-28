"""The weekly-push slot and its catch-up window (a laptop asleep at Saturday 18:00 still gets the
weekly pack on the next wake within WEEKLY_CATCHUP_HOURS) — fixed clocks, no network."""

from __future__ import annotations

from datetime import datetime

import pytest

from equity_research import config, schedule

IST = config.TZ


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=IST)


@pytest.fixture(autouse=True)
def saturday_18h_48h(monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_PUSH_WEEKDAY", 5)      # Saturday
    monkeypatch.setattr(config, "EOD_HOUR", 18)
    monkeypatch.setattr(config, "WEEKLY_CATCHUP_HOURS", 48)


SAT = at("2026-09-26 18:00")                                     # the missed slot


@pytest.mark.parametrize("now, open_", [
    ("2026-09-26 17:59", False),     # before the slot (last week's window long closed)
    ("2026-09-26 18:00", True),      # on time
    ("2026-09-26 23:59", True),
    ("2026-09-27 10:00", True),      # Sunday catch-up
    ("2026-09-28 09:31", True),      # Monday morning wake — the real case
    ("2026-09-28 17:59", True),
    ("2026-09-28 18:00", False),     # 48h passed → stale, skipped
    ("2026-09-29 09:00", False),
])
def test_window(now, open_):
    got = schedule.open_slot(at(now))
    assert (got == SAT) if open_ else got is None


def test_catch_up_is_labelled_only_when_late():
    assert schedule.catch_up_note(SAT, at("2026-09-26 18:05")) == ""
    assert schedule.catch_up_note(SAT, at("2026-09-28 09:31")) == " (catch-up for Sat 26-Sep)"


def test_zero_catch_up_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CATCHUP_HOURS", 0)
    assert schedule.open_slot(at("2026-09-26 23:30")) == SAT        # rest of the push day still ok
    assert schedule.open_slot(at("2026-09-27 00:30")) is None


def test_a_monday_catch_up_never_blocks_the_next_saturday():
    marker = schedule.slot_key(SAT)                                 # written by Monday's catch-up
    assert schedule.is_done(marker, SAT)
    assert not schedule.is_done(marker, at("2026-10-03 18:00"))    # next Saturday still fires


def test_legacy_iso_week_markers_still_count_as_sent():
    assert schedule.is_done("2026-W39", SAT)                        # Sat 26-Sep is ISO week 39
    assert not schedule.is_done("2026-W38", SAT)
    assert not schedule.is_done(None, SAT)


def test_other_push_day(monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_PUSH_WEEKDAY", 6)          # Sunday
    assert schedule.weekly_slot(at("2026-09-29 12:00")) == at("2026-09-27 18:00")


def test_scan_markers_round_trip(tmp_path, monkeypatch):
    from equity_research import scan
    from equity_research.common import db

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.setattr(schedule, "open_slot", lambda now=None: SAT)
    assert scan.tailwind_due()
    scan.mark_tailwind()
    assert not scan.tailwind_due()                                  # sent once per slot
    monkeypatch.setattr(schedule, "open_slot", lambda now=None: None)
    assert not scan.results_radar_due()                             # window closed → nothing fires
