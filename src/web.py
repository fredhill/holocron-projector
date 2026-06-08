"""Holocron web config form.

Flask app on :8080 (no auth — trusted LAN). Edits /data/holidays.json
atomically and publishes `reload` on holocron/cmd so the player picks up
changes without a restart.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import paho.mqtt.publish as mqtt_publish
from astral import LocationInfo
from astral.sun import sun
from flask import Flask, abort, redirect, render_template, request, url_for

import scheduler as S

CONFIG_PATH = Path(os.environ.get("HOLOCRON_CONFIG", "/data/holidays.json"))
VIDEO_ROOT = Path(os.environ.get("HOLOCRON_VIDEO_ROOT", "/mnt/jedi-archives/Media/Projector Movies/HORIZONTAL"))
MQTT_HOST = os.environ.get("HOLOCRON_MQTT_HOST", "10.0.0.147")
MQTT_PORT = int(os.environ.get("HOLOCRON_MQTT_PORT", "1883"))
LISTEN_HOST = os.environ.get("HOLOCRON_WEB_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("HOLOCRON_WEB_PORT", "8080"))

log = logging.getLogger("holocron.web")

app = Flask(__name__, template_folder="../templates", static_folder="../static")


# ---------- helpers ----------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    """Atomic write: tempfile + rename."""
    errs = S.validate_config(cfg)
    if errs:
        raise ValueError("; ".join(errs))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), prefix=".holidays.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def publish_cmd(payload: str) -> None:
    try:
        mqtt_publish.single(
            "holocron/cmd",
            payload=payload,
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            client_id="holocron-web",
            qos=0,
        )
    except Exception as e:
        log.warning("MQTT publish failed: %s", e)


def list_video_folders() -> list[str]:
    if not VIDEO_ROOT.is_dir():
        return []
    return sorted(p.name for p in VIDEO_ROOT.iterdir() if p.is_dir())


def now_and_next(cfg: dict) -> dict:
    """What's playing now and when does it end / what's next today."""
    tz = ZoneInfo(cfg["location"]["tz"])
    now = dt.datetime.now(tz)
    today = now.date()

    holiday = S.pick_active_holiday(cfg["holidays"], today)
    if not holiday:
        return {"holiday": None, "in_window": False, "windows": [], "now": now}

    loc = LocationInfo(
        latitude=cfg["location"]["lat"],
        longitude=cfg["location"]["lon"],
        timezone=cfg["location"]["tz"],
    )
    s = sun(loc.observer, date=today, tzinfo=tz)
    solar = {k: s[k] for k in ("sunrise", "sunset", "dawn", "dusk")}

    windows = S.resolve_windows(
        S.holiday_windows(holiday, cfg.get("defaults", {})),
        today, tz, solar,
    )
    return {
        "holiday": holiday,
        "in_window": S.in_any_window(now, windows),
        "windows": windows,
        "now": now,
        "solar": solar,
    }


# ---------- form parsing ----------

def _form_int(name: str, default: int = 0) -> int:
    v = request.form.get(name, "").strip()
    return int(v) if v else default


def parse_holiday_form(form) -> dict:
    """Turn the form POST into a holiday dict. Raises ValueError on bad input."""
    name = form.get("name", "").strip()
    folder = form.get("folder", "").strip()
    if not name or not folder:
        raise ValueError("name and folder are required")

    enabled = form.get("enabled") == "on"
    priority = int(form.get("priority", "10") or 10)
    rule_type = form.get("rule_type", "annual_date")

    rule: dict[str, Any] = {"type": rule_type}
    if rule_type == "annual_date":
        rule["date"] = form.get("rule_date", "").strip()
        if form.get("rule_days_before"):
            rule["days_before"] = int(form["rule_days_before"])
        if form.get("rule_days_after"):
            rule["days_after"] = int(form["rule_days_after"])
    elif rule_type == "annual_range":
        rule["start"] = form.get("rule_start", "").strip()
        rule["end"] = form.get("rule_end", "").strip()
    elif rule_type == "floating":
        rule["month"] = int(form.get("rule_month", "0"))
        rule["weekday"] = form.get("rule_weekday", "monday").lower()
        rule["n"] = int(form.get("rule_n", "1"))
        if form.get("rule_days_before"):
            rule["days_before"] = int(form["rule_days_before"])
        if form.get("rule_days_after"):
            rule["days_after"] = int(form["rule_days_after"])
    elif rule_type == "easter":
        if form.get("rule_days_before"):
            rule["days_before"] = int(form["rule_days_before"])
        if form.get("rule_days_after"):
            rule["days_after"] = int(form["rule_days_after"])
    elif rule_type == "span":
        # Two anchors; each can be {date} or {floating} or {easter}
        rule["start"] = _parse_span_anchor(form, "span_start")
        rule["end"] = _parse_span_anchor(form, "span_end")
    else:
        raise ValueError(f"unknown rule type: {rule_type}")

    # play windows
    windows: list[dict] = []
    starts = form.getlist("window_start")
    ends = form.getlist("window_end")
    for s, e in zip(starts, ends):
        s, e = s.strip(), e.strip()
        if s and e:
            windows.append({"start": s, "end": e})

    out: dict[str, Any] = {
        "name": name, "folder": folder,
        "enabled": enabled, "priority": priority,
        "rule": rule,
    }
    if windows:
        out["play_windows"] = windows
    return out


def _parse_span_anchor(form, prefix: str) -> dict:
    kind = form.get(f"{prefix}_kind", "date")
    if kind == "date":
        return {"date": form.get(f"{prefix}_date", "").strip()}
    if kind == "floating":
        f = {
            "month": int(form.get(f"{prefix}_month", "0")),
            "weekday": form.get(f"{prefix}_weekday", "monday").lower(),
            "n": int(form.get(f"{prefix}_n", "1")),
        }
        if form.get(f"{prefix}_offset_days"):
            f["offset_days"] = int(form[f"{prefix}_offset_days"])
        return {"floating": f}
    if kind == "easter":
        e: dict[str, Any] = {}
        if form.get(f"{prefix}_offset_days"):
            e["offset_days"] = int(form[f"{prefix}_offset_days"])
        return {"easter": e}
    raise ValueError(f"unknown span anchor kind: {kind}")


# ---------- routes ----------

@app.route("/")
def index():
    cfg = load_config()
    status = now_and_next(cfg)
    return render_template("index.html", cfg=cfg, status=status,
                           folders=list_video_folders())


@app.route("/holiday/new", methods=["GET", "POST"])
def holiday_new():
    cfg = load_config()
    if request.method == "POST":
        try:
            h = parse_holiday_form(request.form)
            cfg["holidays"].append(h)
            save_config(cfg)
            publish_cmd("reload")
            return redirect(url_for("index"))
        except (ValueError, KeyError) as e:
            return render_template("edit.html", cfg=cfg, holiday=None,
                                   folders=list_video_folders(), error=str(e),
                                   index=None)
    return render_template("edit.html", cfg=cfg, holiday=None,
                           folders=list_video_folders(), error=None, index=None)


@app.route("/holiday/<int:idx>/edit", methods=["GET", "POST"])
def holiday_edit(idx: int):
    cfg = load_config()
    if idx < 0 or idx >= len(cfg["holidays"]):
        abort(404)
    if request.method == "POST":
        try:
            cfg["holidays"][idx] = parse_holiday_form(request.form)
            save_config(cfg)
            publish_cmd("reload")
            return redirect(url_for("index"))
        except (ValueError, KeyError) as e:
            return render_template("edit.html", cfg=cfg,
                                   holiday=cfg["holidays"][idx],
                                   folders=list_video_folders(), error=str(e),
                                   index=idx)
    return render_template("edit.html", cfg=cfg, holiday=cfg["holidays"][idx],
                           folders=list_video_folders(), error=None, index=idx)


@app.route("/holiday/<int:idx>/delete", methods=["POST"])
def holiday_delete(idx: int):
    cfg = load_config()
    if idx < 0 or idx >= len(cfg["holidays"]):
        abort(404)
    del cfg["holidays"][idx]
    save_config(cfg)
    publish_cmd("reload")
    return redirect(url_for("index"))


@app.route("/control/<action>", methods=["POST"])
def control(action: str):
    folder = request.form.get("folder", "").strip()
    if action == "play" and folder:
        publish_cmd(f"force:{folder}")
    elif action == "stop":
        publish_cmd("stop")
    elif action == "resume":
        publish_cmd("resume")
    elif action == "reload":
        publish_cmd("reload")
    else:
        abort(400)
    return redirect(url_for("index"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False)
