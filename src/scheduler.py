"""Date/window logic for Holocron.

Pure functions — no MQTT, no mpv, no filesystem. The player imports this
module and drives I/O around it; tests exercise it directly.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import easter as _easter

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_SOLAR_ANCHORS = {"sunrise", "sunset", "dawn", "dusk"}

_VALID_RULE_TYPES = {"annual_date", "annual_range", "floating", "easter", "span"}

# matches HH:MM (00-23:00-59), 24:00, or <anchor>[±HH:MM]
_WINDOW_RE = re.compile(
    r"^(?:"
    r"(?:[01]\d|2[0-3]):[0-5]\d"      # HH:MM
    r"|24:00"                          # end-of-day sentinel
    r"|(?:sunrise|sunset|dawn|dusk)"   # bare anchor
    r"(?:[+-](?:[01]\d|2[0-3]):[0-5]\d)?"  # optional ±HH:MM offset
    r")$"
)


# ---------- holiday rule matching ----------

def nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n=1..5 = nth occurrence, n=-1 = last occurrence."""
    if n == -1:
        # walk back from the last day of the month
        if month == 12:
            last = dt.date(year, 12, 31)
        else:
            last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        offset = (last.weekday() - weekday) % 7
        return last - dt.timedelta(days=offset)
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def resolve_anchor_date(anchor: dict, year: int) -> dt.date:
    """A `span` anchor: {date:'MM-DD'} | {floating:{...,offset_days}} | {easter:{offset_days}}."""
    if "date" in anchor:
        m, d = anchor["date"].split("-")
        return dt.date(year, int(m), int(d))
    if "floating" in anchor:
        f = anchor["floating"]
        base = nth_weekday(year, f["month"], _WEEKDAYS[f["weekday"].lower()], f["n"])
        return base + dt.timedelta(days=f.get("offset_days", 0))
    if "easter" in anchor:
        return _easter.easter(year) + dt.timedelta(days=anchor["easter"].get("offset_days", 0))
    raise ValueError(f"unknown span anchor: {anchor!r}")


def rule_matches(rule: dict, today: dt.date) -> bool:
    t = rule["type"]
    y = today.year

    if t == "annual_date":
        m, d = rule["date"].split("-")
        base = dt.date(y, int(m), int(d))
        start = base - dt.timedelta(days=rule.get("days_before", 0))
        end = base + dt.timedelta(days=rule.get("days_after", 0))
        return start <= today <= end

    if t == "annual_range":
        sm, sd = rule["start"].split("-")
        em, ed = rule["end"].split("-")
        start = dt.date(y, int(sm), int(sd))
        end = dt.date(y, int(em), int(ed))
        if start <= end:
            return start <= today <= end
        # wrap (e.g., 12-15..01-05) — match either side
        return today >= start or today <= end

    if t == "floating":
        base = nth_weekday(y, rule["month"], _WEEKDAYS[rule["weekday"].lower()], rule["n"])
        start = base - dt.timedelta(days=rule.get("days_before", 0))
        end = base + dt.timedelta(days=rule.get("days_after", 0))
        return start <= today <= end

    if t == "easter":
        base = _easter.easter(y)
        start = base - dt.timedelta(days=rule.get("days_before", 0))
        end = base + dt.timedelta(days=rule.get("days_after", 0))
        return start <= today <= end

    if t == "span":
        # span may cross a year boundary; try anchored at this year and last year
        for yr in (y, y - 1):
            s = resolve_anchor_date(rule["start"], yr)
            e = resolve_anchor_date(rule["end"], yr)
            if e < s:
                e = resolve_anchor_date(rule["end"], yr + 1)
            if s <= today <= e:
                return True
        return False

    raise ValueError(f"unknown rule type: {t!r}")


def pick_active_holiday(holidays: list[dict], today: dt.date) -> dict | None:
    """Highest priority matching holiday wins; ties by list order."""
    matches = [h for h in holidays if h.get("enabled", True) and rule_matches(h["rule"], today)]
    if not matches:
        return None
    matches.sort(key=lambda h: -h.get("priority", 0))
    return matches[0]


# ---------- time window resolution ----------

@dataclass
class ResolvedWindow:
    start: dt.datetime
    end: dt.datetime

    def contains(self, now: dt.datetime) -> bool:
        return self.start <= now < self.end


def _parse_offset(token: str) -> dt.timedelta:
    """Parse '+00:15' / '-01:30' / '' → timedelta."""
    if not token:
        return dt.timedelta()
    sign = 1 if token[0] == "+" else -1
    hh, mm = token[1:].split(":")
    return sign * dt.timedelta(hours=int(hh), minutes=int(mm))


def resolve_time(spec: str, day: dt.date, tz: ZoneInfo, solar: dict[str, dt.datetime]) -> dt.datetime:
    """Resolve a window endpoint to an aware datetime on `day`.

    `spec` is one of:
      - "HH:MM" — clock time on `day`
      - "24:00" — start of the next day (midnight after `day`)
      - "<anchor>[±HH:MM]" — solar anchor with optional offset.
    `solar` is a dict of pre-computed solar datetimes for `day` (tz-aware).
    """
    spec = spec.strip()
    if spec == "24:00":
        return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz)

    # solar anchor?
    for anchor in _SOLAR_ANCHORS:
        if spec.startswith(anchor):
            offset = _parse_offset(spec[len(anchor):])
            return solar[anchor] + offset

    # clock time
    hh, mm = spec.split(":")
    return dt.datetime.combine(day, dt.time(int(hh), int(mm)), tzinfo=tz)


def resolve_windows(
    windows: list[dict],
    day: dt.date,
    tz: ZoneInfo,
    solar: dict[str, dt.datetime],
) -> list[ResolvedWindow]:
    out = []
    for w in windows:
        s = resolve_time(w["start"], day, tz, solar)
        e = resolve_time(w["end"], day, tz, solar)
        if e <= s:
            # silently skip degenerate windows; player will log
            continue
        out.append(ResolvedWindow(s, e))
    return out


def in_any_window(now: dt.datetime, windows: list[ResolvedWindow]) -> bool:
    return any(w.contains(now) for w in windows)


# ---------- config helpers ----------

DEFAULT_WINDOWS = [{"start": "dusk", "end": "24:00"}]


def holiday_windows(holiday: dict, defaults: dict) -> list[dict]:
    """A holiday's play_windows, or the config default, or built-in."""
    if "play_windows" in holiday and holiday["play_windows"]:
        return holiday["play_windows"]
    return defaults.get("play_windows", DEFAULT_WINDOWS)


def validate_location(loc: dict) -> list[str]:
    """Return a list of human-readable errors for a location dict."""
    errs: list[str] = []
    for k in ("lat", "lon", "tz"):
        if k not in loc:
            errs.append(f"location.{k} required")
    if "lat" in loc:
        try:
            lat = float(loc["lat"])
            if not -90.0 <= lat <= 90.0:
                errs.append(f"location.lat out of range: {lat}")
        except (TypeError, ValueError):
            errs.append(f"location.lat not a number: {loc['lat']!r}")
    if "lon" in loc:
        try:
            lon = float(loc["lon"])
            if not -180.0 <= lon <= 180.0:
                errs.append(f"location.lon out of range: {lon}")
        except (TypeError, ValueError):
            errs.append(f"location.lon not a number: {loc['lon']!r}")
    if "tz" in loc:
        try:
            ZoneInfo(loc["tz"])
        except Exception:
            errs.append(f"location.tz not a valid IANA zone: {loc['tz']!r}")
    return errs


def is_valid_window_spec(spec: str) -> bool:
    """True if `spec` is a recognized window endpoint (HH:MM, 24:00, or anchor±HH:MM)."""
    return isinstance(spec, str) and bool(_WINDOW_RE.match(spec.strip()))


def validate_config(cfg: dict) -> list[str]:
    """Return a list of human-readable errors. Empty list = valid."""
    errs: list[str] = []
    if cfg.get("version") != 3:
        errs.append("version must be 3")
    errs.extend(validate_location(cfg.get("location") or {}))

    # validate default windows too — if those are broken, every inheriting
    # holiday breaks at runtime
    for j, w in enumerate((cfg.get("defaults") or {}).get("play_windows", []) or []):
        for end in ("start", "end"):
            if end not in w:
                errs.append(f"defaults.play_windows[{j}]: {end} required")
            elif not is_valid_window_spec(w[end]):
                errs.append(f"defaults.play_windows[{j}].{end}: bad spec {w[end]!r}")

    for i, h in enumerate(cfg.get("holidays", [])):
        tag = f"holidays[{i}] ({h.get('name', '?')})"
        for k in ("name", "folder", "rule"):
            if k not in h:
                errs.append(f"{tag}: missing {k}")
        rule = h.get("rule") or {}
        rt = rule.get("type")
        if rt is None:
            errs.append(f"{tag}: rule.type required")
        elif rt not in _VALID_RULE_TYPES:
            errs.append(f"{tag}: unknown rule.type {rt!r} "
                        f"(must be one of {sorted(_VALID_RULE_TYPES)})")
        for j, w in enumerate(h.get("play_windows", []) or []):
            for end in ("start", "end"):
                if end not in w:
                    errs.append(f"{tag}.play_windows[{j}]: {end} required")
                elif not is_valid_window_spec(w[end]):
                    errs.append(f"{tag}.play_windows[{j}].{end}: bad spec {w[end]!r}")
    return errs
