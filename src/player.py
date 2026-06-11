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

TTY_PATH = os.environ.get("HOLOCRON_TTY", "/dev/tty1")

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


def blank_console() -> None:
    """Clear the tty and hide the cursor so the projector shows black,
    not boot messages, whenever mpv isn't running.

    mpv restores the VT when it exits, which brings the kernel console
    (with whatever scrollback it had) back onto the projector. Clearing
    immediately after each stop — and once at startup — keeps the output
    black between videos.
    """
    try:
        with open(TTY_PATH, "wb") as t:
            # ESC[2J clear screen, ESC[3J clear scrollback, ESC[H home, ESC[?25l hide cursor
            t.write(b"\x1b[2J\x1b[3J\x1b[H\x1b[?25l")
    except OSError as e:
        log.debug("could not blank %s: %s", TTY_PATH, e)


def safe_folder(name: str, root: Path = VIDEO_ROOT) -> Path | None:
    """Resolve a folder name under VIDEO_ROOT, rejecting traversal.

    Returns the resolved Path if `name` is a direct child folder of `root`,
    else None. Used to gate MQTT `force:<folder>` commands and any path
    coming from external input.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    try:
        candidate = (root / name).resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    if candidate.parent != root_resolved:
        return None
    if not candidate.is_dir():
        return None
    return candidate


# ---------- config loading w/ last-known-good ----------

class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.cfg: Optional[dict] = None
        self.last_error: Optional[str] = None
        self.last_mtime: float = 0.0

    def file_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def changed_on_disk(self) -> bool:
        """True if the config file mtime differs from what we last loaded."""
        m = self.file_mtime()
        return m != 0.0 and m != self.last_mtime

    def load(self) -> tuple[dict | None, str | None]:
        mtime_at_read = self.file_mtime()
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
        self.last_mtime = mtime_at_read
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
        # Drop --really-quiet so warnings reach stderr; keep noise low with msg-level.
        args = [a for a in MPV_ARGS if a != "--really-quiet"] + [
            "--msg-level=all=warn",
            f"--playlist={PLAYLIST_PATH}",
        ]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.current_folder = folder.name
        # daemon thread drains stderr into journald; dies when proc closes its pipe
        threading.Thread(
            target=self._drain_stderr, args=(self.proc,), daemon=True,
        ).start()
        return True

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                log.info("mpv: %s", line)

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
        # mpv hands the VT back showing the kernel console — black it out
        blank_console()


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
        self.last_status: Optional[str] = None
        self.reload_requested = False
        self.shutdown = threading.Event()

    # --- MQTT ---

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("MQTT connected: %s", reason_code)
        client.subscribe(T_CMD)
        # Force the next tick to republish state. Retained MQTT topics can go
        # stale after a Pi reboot if the first publish raced the connect; this
        # guarantees Homey sees the true state within one tick of (re)connect.
        self.last_projector_state = None
        self.last_active_holiday = None
        self.last_status = None

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        log.info("cmd: %s", payload)
        if payload.startswith("force:"):
            folder = payload[len("force:"):].strip()
            if safe_folder(folder) is None:
                log.warning("force: rejected, not a valid subfolder: %r", folder)
                self._set_status(f"error:bad folder: {folder}")
                return
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

    def _set_status(self, status: str) -> None:
        if status != self.last_status:
            self._publish(T_STATUS, status, retain=True)
            self.last_status = status

    # --- evaluation ---

    def evaluate(self) -> None:
        """One tick of the schedule loop."""
        # mtime fallback: if the file changed on disk (web form edit) and the
        # MQTT reload command was missed (broker hiccup), still pick it up.
        if self.cfg_store.changed_on_disk():
            log.info("config file changed on disk — reloading")
            self.cfg_store.load()

        cfg = self.cfg_store.cfg
        if cfg is None:
            # try to load; if still nothing, idle
            cfg, err = self.cfg_store.load()
            if cfg is None:
                self._set_status(f"error:{err}")
                self._set_projector("off")
                if self.mpv.is_running():
                    self.mpv.stop()
                return

        # manual override?
        if self.manual_mode:
            if self.manual_folder:
                folder = safe_folder(self.manual_folder)
                if folder is None:
                    self._set_status(f"error:bad folder: {self.manual_folder}")
                    self._set_projector("off")
                    if self.mpv.is_running():
                        self.mpv.stop()
                    return
                if self.mpv.current_folder != self.manual_folder or not self.mpv.is_running():
                    if not self.mpv.start(folder):
                        self._set_status(f"error:no videos in {self.manual_folder}")
                        self._set_projector("off")
                        return
                self._set_projector("on")
                self._set_status(f"manual:{self.manual_folder}")
            else:
                # forced stop
                if self.mpv.is_running():
                    self.mpv.stop()
                self._set_projector("off")
                self._set_status("idle")
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
            self._set_status(f"error:solar: {e}")
            self._goto_idle()
            return

        windows = S.resolve_windows(
            S.holiday_windows(holiday, cfg.get("defaults", {})),
            today, tz, solar,
        )

        if S.in_any_window(now, windows):
            folder = safe_folder(holiday["folder"])
            if folder is None:
                self._set_status(f"error:bad folder: {holiday['folder']}")
                self._goto_idle()
                return
            if self.mpv.current_folder != holiday["folder"] or not self.mpv.is_running():
                if not self.mpv.start(folder):
                    self._set_status(f"error:no videos in {holiday['folder']}")
                    self._goto_idle()
                    return
            self._set_projector("on")
            self._set_status(f"playing:{holiday['name']}")
        else:
            self._goto_idle()

    def _goto_idle(self) -> None:
        if self.mpv.is_running():
            self.mpv.stop()
        self._set_projector("off")
        self._set_status("idle")

    # --- run ---

    def run(self) -> int:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)

        # start with a black screen, not leftover boot messages
        blank_console()

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
