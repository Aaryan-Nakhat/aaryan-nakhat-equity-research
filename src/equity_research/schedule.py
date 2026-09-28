"""When the weekly pushes may fire — the weekly slot and its catch-up window.

The weekly pushes (screener movements, sector rotation, Tailwind, Concalls, Results Radar, and the
monthly Pickaxe) are scheduled for ``WEEKLY_PUSH_WEEKDAY`` at ``EOD_HOUR`` (default Saturday 18:00).
A self-hosted bot often runs on a laptop that is asleep at that moment, so a push that couldn't
fire is **caught up** on the next wake within ``WEEKLY_CATCHUP_HOURS`` (default 48h → by Monday
18:00); after that it's stale and skipped until the next slot.

"Already sent" is keyed to the **slot** (its date), not to the calendar week of the send — a Monday
catch-up belongs to the previous Saturday, so it never marks the coming Saturday as done. Markers
written before this change (ISO-week strings like ``2026-W39``) are still recognised.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from equity_research import config


def weekly_slot(now: datetime | None = None) -> datetime:
    """The most recent scheduled weekly-push moment at or before ``now``."""
    now = now or datetime.now(config.TZ)
    back = (now.weekday() - config.WEEKLY_PUSH_WEEKDAY) % 7
    day = now.date() - timedelta(days=back)
    slot = datetime.combine(day, time(config.EOD_HOUR), tzinfo=now.tzinfo)
    return slot - timedelta(days=7) if slot > now else slot


def open_slot(now: datetime | None = None) -> datetime | None:
    """The slot a weekly push may fire for right now, or None. Open from the slot until the end of
    the push day or ``WEEKLY_CATCHUP_HOURS`` after it, whichever is later."""
    now = now or datetime.now(config.TZ)
    slot = weekly_slot(now)
    end_of_day = datetime.combine(slot.date() + timedelta(days=1), time(0), tzinfo=slot.tzinfo)
    close = max(end_of_day, slot + timedelta(hours=max(config.WEEKLY_CATCHUP_HOURS, 0)))
    return slot if now < close else None


def slot_key(slot: datetime) -> str:
    """The marker value recorded once a slot's push has gone out (the slot's date)."""
    return slot.date().isoformat()


def is_done(marker: str | None, slot: datetime) -> bool:
    """Has the push for ``slot`` already gone out? Accepts the legacy ISO-week marker too."""
    if not marker:
        return False
    y, w, _ = slot.isocalendar()
    return marker in (slot_key(slot), f"{y}-W{w:02d}")


def is_catch_up(slot: datetime, now: datetime | None = None) -> bool:
    """True when firing for a slot from an earlier day (the machine was asleep / off at the time)."""
    now = now or datetime.now(config.TZ)
    return now.date() != slot.date()


def catch_up_note(slot: datetime | None, now: datetime | None = None) -> str:
    """' (catch-up for Sat 26-Sep)' when firing late, else '' — for logs and email subjects."""
    if slot is None or not is_catch_up(slot, now):
        return ""
    return f" (catch-up for {slot:%a %d-%b})"
