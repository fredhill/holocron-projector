"""Holocron scheduler + player.

Reads /data/holidays.json, decides whether *now* falls inside any of the
active holiday's play windows, and starts/stops mpv accordingly. Publishes
projector state to MQTT so Homey can mirror it to the projector.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from astral import LocationInfo
from astral.sun import sun

import scheduler as S

# ---------- constants ----------

CONFIG_PATH = Path(os.environ.get("HOLOCRON_CONFIG", "/data/holidays.json"))
VIDEO_ROOT = Path(os.environ.get("HOLOCRON_VIDEO_ROOT", "/mnt/jedi-archives/Media/Projector Movies/HORIZONTAL"))
PLAYLIST_PATH = Path(os.environ.get("HOLOCRON_PLAYLIST", "/run/holocron/playlist.txt"))

MQTT_HOST = os.environ.get("HOLOCRON_MQTT_HOST", "10.0.0.147")
MQTT_PORT = int(os.environ.get("HOLOCRON_MQTT_PORT", "1883"))
MQTT_CLIENT_ID = "holocron"

T_PROJECTOR = "holocron/projector"
T_CMD = "holocron/cmd"
T_HOLIDAY = "holocron/active_holiday"
T_STATUS = "holocron/status"
T_HEARTBEAT = "holocron/heartbeat"

TICK_SECONDS = 30
HEARTBEAT_SECONDS = 60

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v"}

MPV_ARGS = [
    "mpv",
    "--vo=drm",
    "--hwdec=auto",
    "--fullscreen",
    "--loop-playlist=inf",
    "--shuffle",
    "--no-osc",
    "--no-input-default-bindings",
    "--no-input-terminal",
    "--really-quiet",
]

log = logging.getLogger("holocron.player")


# ---------- config loading w/ last-known-good ----------

class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.cfg: Optional[dict] = None
        self.last_error: Optional[str] = None

    def load(self) -> tuple[dict | None, str | None]:
        try:
            raw = self.path.read_text()
            new = json.loads(raw)
        except FileNotFoundError:
            err = f"config not found: {self.path}"
            log.error(err)
            self.last_error = err
            return self.cfg, err
        except json.JSONDecodeError as e:
            err = f"invalid JSON: {e}"
            log.error(err)
            self.last_error = err
            return self.cfg, err

        errs = S.validate_config(new)
        if errs:
            err = "; ".join(errs)
            log.error("config invalid, keeping last-known-good: %s", err)
            self.last_error = err
            return self.cfg, err

        self.cfg = new
        self.last_error = None
        log.info("config loaded: %d holidays", len(new.get("holidays", [])))
        return self.cfg, None


# ---------- solar ----------

def solar_for(day: dt.date, lat: float, lon: float, tz: ZoneInfo) -> dict[str, dt.datetime]:
    loc = LocationInfo(latitude=lat, longitude=lon, timezone=str(tz))
    s = sun(loc.observer, date=day, tzinfo=tz)
    # astral provides sunrise/sunset/dawn/dusk (dusk = civil twilight end)
    return {
        "sunrise": s["sunrise"],
        "sunset": s["sunset"],
        "dawn": s["dawn"],
        "dusk": s["dusk"],
    }


# ---------- mpv subprocess ----------

class MpvProc:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.current_folder: Optional[str] = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, folder: Path) -> bool:
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
        if not files:
            log.warning("no playable videos in %s", folder)
            return False

        PLAYLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAYLIST_PATH.write_text("\n".join(str(f) for f in files) + "\n")

        if self.is_running():
            self.stop()

        log.info("starting mpv with %d files from %s", len(files), folder)
        self.proc = subprocess.Popen(
            MPV_ARGS + [f"--playlist={PLAYLIST_PATH}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.current_folder = folder.name
        return True

    def stop(self) -> None:
        if not self.is_running():
            self.proc = None
            self.current_folder = None
            return
        log.info("stopping mpv")
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        except Exception as e:
            log.warning("error stopping mpv: %s", e)
        self.proc = None
        self.current_folder = None


# ---------- main loop ----------

class Holocron:
    def __init__(self):
        self.cfg_store = ConfigStore(CONFIG_PATH)
        self.mpv = MpvProc()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        # state
        self.manual_mode = False         # True after `force:` or `stop`
        self.manual_folder: Optional[str] = None  # set by `force:<folder>`; None = forced-stop
        self.last_projector_state: Optional[str] = None
        self.last_active_holiday: Optional[str] = None
        self.reload_requested = False
        self.shutdown = threading.Event()

    # --- MQTT ---

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("MQTT connected: %s", reason_code)
        client.subscribe(T_CMD)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        log.info("cmd: %s", payload)
        if payload.startswith("force:"):
            folder = payload[len("force:"):].strip()
            self.manual_mode = True
            self.manual_folder = folder
        elif payload == "stop":
            self.manual_mode = True
            self.manual_folder = None
        elif payload == "resume":
            self.manual_mode = False
            self.manual_folder = None
        elif payload == "reload":
            self.reload_requested = True
        else:
            log.warning("unknown command: %s", payload)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.client.publish(topic, payload, qos=0, retain=retain)

    def _set_projector(self, state: str) -> None:
        if state != self.last_projector_state:
            log.info("projector → %s", state)
            self._publish(T_PROJECTOR, state, retain=True)
            self.last_projector_state = state

    def _set_active_holiday(self, name: str) -> None:
        if name != self.last_active_holiday:
            self._publish(T_HOLIDAY, name, retain=True)
            self.last_active_holiday = name

    # --- evaluation ---

    def evaluate(self) -> None:
        """One tick of the schedule loop."""
        cfg = self.cfg_store.cfg
        if cfg is None:
            # try to load; if still nothing, idle
            cfg, err = self.cfg_store.load()
            if cfg is None:
                self._publish(T_STATUS, f"error:{err}", retain=True)
                self._set_projector("off")
                if self.mpv.is_running():
                    self.mpv.stop()
                return

        # manual override?
        if self.manual_mode:
            if self.manual_folder:
                folder = VIDEO_ROOT / self.manual_folder
                if not folder.is_dir():
                    self._publish(T_STATUS, f"error:folder not found: {self.manual_folder}", retain=True)
                    self._set_projector("off")
                    if self.mpv.is_running():
                        self.mpv.stop()
                    return
                if self.mpv.current_folder != self.manual_folder or not self.mpv.is_running():
                    if not self.mpv.start(folder):
                        self._publish(T_STATUS, f"error:no videos in {self.manual_folder}", retain=True)
                        self._set_projector("off")
                        return
                self._set_projector("on")
                self._publish(T_STATUS, f"manual:{self.manual_folder}", retain=True)
            else:
                # forced stop
                if self.mpv.is_running():
                    self.mpv.stop()
                self._set_projector("off")
                self._publish(T_STATUS, "idle", retain=True)
            return

        # normal scheduling
        loc = cfg["location"]
        tz = ZoneInfo(loc["tz"])
        now = dt.datetime.now(tz)
        today = now.date()

        holiday = S.pick_active_holiday(cfg["holidays"], today)
        if holiday is None:
            self._set_active_holiday("none")
            self._goto_idle()
            return

        self._set_active_holiday(holiday["name"])

        try:
            solar = solar_for(today, loc["lat"], loc["lon"], tz)
        except Exception as e:
            log.error("solar computation failed: %s", e)
            self._publish(T_STATUS, f"error:solar: {e}", retain=True)
            self._goto_idle()
            return

        windows = S.resolve_windows(
            S.holiday_windows(holiday, cfg.get("defaults", {})),
            today, tz, solar,
        )

        if S.in_any_window(now, windows):
            folder = VIDEO_ROOT / holiday["folder"]
            if not folder.is_dir():
                self._publish(T_STATUS, f"error:folder not found: {holiday['folder']}", retain=True)
                self._goto_idle()
                return
            if self.mpv.current_folder != holiday["folder"] or not self.mpv.is_running():
                if not self.mpv.start(folder):
                    self._publish(T_STATUS, f"error:no videos in {holiday['folder']}", retain=True)
                    self._goto_idle()
                    return
            self._set_projector("on")
            self._publish(T_STATUS, f"playing:{holiday['name']}", retain=True)
        else:
            self._goto_idle()

    def _goto_idle(self) -> None:
        if self.mpv.is_running():
            self.mpv.stop()
        self._set_projector("off")
        self._publish(T_STATUS, "idle", retain=True)

    # --- run ---

    def run(self) -> int:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)

        self.cfg_store.load()

        # MQTT — best effort; keep running even if broker is down so the
        # projector keeps doing the right thing when it comes back.
        try:
            self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            log.error("MQTT connect failed: %s — continuing", e)

        last_heartbeat = 0.0

        while not self.shutdown.is_set():
            if self.reload_requested:
                self.cfg_store.load()
                self.reload_requested = False

            try:
                self.evaluate()
            except Exception as e:
                log.exception("evaluate failed: %s", e)
                self._publish(T_STATUS, f"error:{e}", retain=True)

            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                self._publish(T_HEARTBEAT, str(int(now)))
                last_heartbeat = now

            self.shutdown.wait(TICK_SECONDS)

        log.info("shutting down")
        if self.mpv.is_running():
            self.mpv.stop()
        self._set_projector("off")
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            pass
        return 0

    def _signal(self, signum, frame):
        log.info("signal %d", signum)
        self.shutdown.set()


if __name__ == "__main__":
    sys.exit(Holocron().run())
