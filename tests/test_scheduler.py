"""Tests for the scheduler's date/window logic.

Run with: PYTHONPATH=src python -m pytest tests/
"""

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scheduler as S


# ---------- rule matching ----------

def test_annual_date_exact():
    rule = {"type": "annual_date", "date": "07-04"}
    assert S.rule_matches(rule, dt.date(2026, 7, 4))
    assert not S.rule_matches(rule, dt.date(2026, 7, 3))


def test_annual_date_with_window():
    rule = {"type": "annual_date", "date": "12-25", "days_before": 1, "days_after": 0}
    assert S.rule_matches(rule, dt.date(2026, 12, 24))
    assert S.rule_matches(rule, dt.date(2026, 12, 25))
    assert not S.rule_matches(rule, dt.date(2026, 12, 26))


def test_annual_range_wraps_year():
    rule = {"type": "annual_range", "start": "12-15", "end": "01-05"}
    assert S.rule_matches(rule, dt.date(2026, 12, 20))
    assert S.rule_matches(rule, dt.date(2026, 1, 3))
    assert not S.rule_matches(rule, dt.date(2026, 6, 1))


def test_annual_range_october():
    rule = {"type": "annual_range", "start": "10-01", "end": "10-31"}
    assert S.rule_matches(rule, dt.date(2026, 10, 15))
    assert not S.rule_matches(rule, dt.date(2026, 11, 1))


def test_floating_last_monday():
    # Memorial Day 2026 = May 25
    rule = {"type": "floating", "month": 5, "weekday": "monday", "n": -1}
    assert S.rule_matches(rule, dt.date(2026, 5, 25))
    assert not S.rule_matches(rule, dt.date(2026, 5, 18))


def test_floating_4th_thursday():
    # Thanksgiving 2026 = Nov 26
    rule = {"type": "floating", "month": 11, "weekday": "thursday", "n": 4}
    assert S.rule_matches(rule, dt.date(2026, 11, 26))


def test_easter_2026():
    # Easter Sunday 2026 = Apr 5
    rule = {"type": "easter", "days_before": 2, "days_after": 0}
    assert S.rule_matches(rule, dt.date(2026, 4, 5))   # Sunday
    assert S.rule_matches(rule, dt.date(2026, 4, 3))   # Good Friday
    assert not S.rule_matches(rule, dt.date(2026, 4, 6))


def test_span_christmas_2025():
    # Black Friday 2025 = Nov 28; through Dec 31
    rule = {
        "type": "span",
        "start": {"floating": {"month": 11, "weekday": "thursday", "n": 4, "offset_days": 1}},
        "end": {"date": "12-31"},
    }
    assert S.rule_matches(rule, dt.date(2025, 11, 28))
    assert S.rule_matches(rule, dt.date(2025, 12, 15))
    assert S.rule_matches(rule, dt.date(2025, 12, 31))
    assert not S.rule_matches(rule, dt.date(2026, 1, 1))
    assert not S.rule_matches(rule, dt.date(2025, 11, 27))


# ---------- priority ----------

def test_priority_picks_highest():
    holidays = [
        {"name": "low", "rule": {"type": "annual_date", "date": "05-04"}, "priority": 10},
        {"name": "high", "rule": {"type": "annual_date", "date": "05-04"}, "priority": 20},
    ]
    pick = S.pick_active_holiday(holidays, dt.date(2026, 5, 4))
    assert pick["name"] == "high"


def test_disabled_ignored():
    holidays = [
        {"name": "off", "rule": {"type": "annual_date", "date": "05-04"}, "priority": 50, "enabled": False},
        {"name": "on", "rule": {"type": "annual_date", "date": "05-04"}, "priority": 10},
    ]
    pick = S.pick_active_holiday(holidays, dt.date(2026, 5, 4))
    assert pick["name"] == "on"


# ---------- window resolution ----------

TZ = ZoneInfo("America/Los_Angeles")

def _day(): return dt.date(2026, 6, 21)

def _solar():
    # Stub: realistic-ish dawn/sunrise/sunset/dusk for The Dalles, OR on summer solstice.
    d = _day()
    def at(h, m): return dt.datetime.combine(d, dt.time(h, m), tzinfo=TZ)
    return {
        "dawn":    at(5, 0),
        "sunrise": at(5, 30),
        "sunset":  at(21, 0),
        "dusk":    at(21, 30),
    }


def test_resolve_clock_window():
    w = S.resolve_time("07:30", _day(), TZ, _solar())
    assert w.hour == 7 and w.minute == 30


def test_resolve_24_00_is_next_midnight():
    w = S.resolve_time("24:00", _day(), TZ, _solar())
    assert w.date() == _day() + dt.timedelta(days=1)
    assert w.hour == 0 and w.minute == 0


def test_resolve_solar_anchor():
    w = S.resolve_time("dusk", _day(), TZ, _solar())
    assert w.hour == 21 and w.minute == 30


def test_resolve_solar_with_offset():
    w = S.resolve_time("dusk-00:15", _day(), TZ, _solar())
    assert w.hour == 21 and w.minute == 15
    w = S.resolve_time("sunset+00:30", _day(), TZ, _solar())
    assert w.hour == 21 and w.minute == 30


def test_resolve_windows_skips_degenerate():
    out = S.resolve_windows(
        [{"start": "10:00", "end": "10:00"}, {"start": "12:00", "end": "13:00"}],
        _day(), TZ, _solar(),
    )
    assert len(out) == 1


def test_in_any_window_multi():
    windows = S.resolve_windows(
        [{"start": "00:00", "end": "01:00"}, {"start": "07:30", "end": "22:00"}],
        _day(), TZ, _solar(),
    )
    now1 = dt.datetime.combine(_day(), dt.time(0, 30), tzinfo=TZ)
    now2 = dt.datetime.combine(_day(), dt.time(3, 0), tzinfo=TZ)
    now3 = dt.datetime.combine(_day(), dt.time(12, 0), tzinfo=TZ)
    now4 = dt.datetime.combine(_day(), dt.time(23, 0), tzinfo=TZ)
    assert S.in_any_window(now1, windows)
    assert not S.in_any_window(now2, windows)
    assert S.in_any_window(now3, windows)
    assert not S.in_any_window(now4, windows)


# ---------- example config validates ----------

def test_example_config_validates():
    cfg_path = Path(__file__).parent.parent / "config" / "holidays.example.json"
    cfg = json.loads(cfg_path.read_text())
    errs = S.validate_config(cfg)
    assert errs == [], f"example config invalid: {errs}"


def test_validate_rejects_bad_version():
    assert "version must be 3" in "; ".join(S.validate_config({"version": 2}))
