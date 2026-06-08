# Claude Code Handoff — "Holocron" Holiday Video Projection Controller (v3)

**Owner:** Danny (Fred Hill)
**Target hardware:** Raspberry Pi 4 Model B + PoE HAT, driving a BenQ SH915 projector (1080p)
**Purpose:** On a schedule, play a folder of holiday videos full-screen, shuffled and looped, on a projector. The active holiday, and the hours it plays, are chosen automatically by date — with evening windows that start at **dusk** so they track the seasons. Holocron owns all scheduling; Homey only switches the projector.

**Changes from v2:**
- Each holiday now has a **list** of play windows (a single day can have multiple on/off blocks).
- Window start/end can be a clock time **or a solar anchor** (`dusk`, `sunset`, `sunrise`, `dawn`) with an optional ± offset. Solar times are **computed on the Pi** from a configured location (`astral`), so evening windows start earlier in fall/winter automatically. Homey stays a relay — no lux sensor, no Homey scheduling.
- Default evening window now starts at `dusk` (was 18:00).
- New Year's split into 00:00–01:00 + 07:30–22:00; birthdays set to 07:30–22:00.

---

## 1. System overview

```
        ┌─────────────┐
        │   Homey     │  subscribes holocron/projector → BenQ app ON/OFF
        │ (CT 103)    │ ───────────────────────────────────────────────► Projector
        └──────▲──────┘                                                   (10.50.0.29)
               │ holocron/projector (retained on/off)
        ┌──────┴──────────────┐
        │ Mosquitto broker     │ (CT 100, 10.0.0.147:1883, no auth)
        └──────────▲──────────┘
        ┌──────────┴───────────────────┐
        │ Holocron (Raspberry Pi 4)     │ ── micro-HDMI → DRM/KMS ──► Projector
        │ IoT VLAN, 10.50.x.x           │
        │  • scheduler (date + solar    │
        │    + multi-window) + mpv      │
        │  • web config form            │
        │  • SMB read-only mount        │ ── reads videos ──► Hoth (10.0.0.219)
        └──────────────────────────────┘
```

**Division of responsibility:** Holocron is the brain and the clock — it decides which holiday is active and whether *now* falls inside any of that holiday's play windows (resolving solar anchors for today's date). When it should show video it starts mpv and publishes `holocron/projector = on`; otherwise it stops mpv and publishes `off`. **Homey only mirrors that topic to the projector.** No date, time, or sensor logic in Homey.

---

## 2. Hardware & environment (in place)

| Item | Detail |
|---|---|
| Board | Raspberry Pi 4 Model B |
| Power/Network | PoE HAT, leave on 24/7. Heatsink present; basement 50–60°F — cooling fine. |
| Video out | **micro-HDMI → HDMI** (USB-C is power only). |
| Storage | 32 GB high-endurance microSD, Raspberry Pi OS **Lite** 64-bit (Bookworm). |
| Imaging | hostname `holocron`; **username `holocron`** (uid/gid 1000 → matches fstab + units); SSH on; no Wi-Fi. |
| Network | IoT VLAN (10.50.x.x); firewall to Admin VLAN (10.0.x.x) allowed; static DHCP lease. |

**Must reach:** SMB `10.0.0.219:445`, MQTT `10.0.0.147:1883`.

---

## 3. Video source (SMB)

- **Share:** `//10.0.0.219/The Jedi Archives` → mount `/mnt/jedi-archives` (read-only, automount)
- **Folders:** `/mnt/jedi-archives/Media/Projector Movies/HORIZONTAL/<Folder>/`

**`/etc/fstab`:**
```
//10.0.0.219/The\040Jedi\040Archives  /mnt/jedi-archives  cifs  credentials=/etc/holocron/smb-credentials,ro,uid=1000,gid=1000,iocharset=utf8,vers=3.0,_netdev,nofail,x-systemd.automount  0  0
```
- `\040` = space in share name; `x-systemd.automount` = mount on first access (avoids boot race).
- `/etc/holocron/smb-credentials` (0600 root): `username=holiday-ro` / `password=...`.
- **Synology:** `holiday-ro` read-only, and grant read in the **Advanced Permissions ACL** (error-13 gotcha).

---

## 4. Video file requirements

mpv plays nearly anything; the **Pi 4 hardware decoder** is the real constraint. Codec matters more than container.

- **Containers — good:** `.mp4`, `.mkv`. Fine: `.mov`, `.m4v`. **Avoid:** `.webm` (usually VP9/AV1), `.avi/.wmv/.flv/.mpg`.
- **Codecs — good:** **H.264** (best at 1080p) and **H.265/HEVC** — both HW-decoded. **Bad:** **VP9, AV1** (no Pi 4 HW decode → stutter), MPEG-2/VC-1.
- Target H.264 in MP4/MKV, ~1080p. Check with `ffprobe <file>`; transcode VP9/AV1 → H.264. The player ignores non-video files in a folder.

---

## 5. MQTT topic contract

Broker `10.0.0.147:1883`, unauthenticated. Client ID `holocron`.

| Topic | Direction | Payload | Notes |
|---|---|---|---|
| `holocron/projector` | Pi → Homey | `on` \| `off` | **Retained.** Homey mirrors to the projector. |
| `holocron/cmd` | Homey/manual → Pi | `force:<folder>` \| `stop` \| `resume` \| `reload` | Manual overrides. |
| `holocron/active_holiday` | Pi → broker | `<name>` or `none` | **Retained.** Informational. |
| `holocron/status` | Pi → broker | `playing:<name>` \| `idle` \| `manual:<folder>` \| `error:<msg>` | **Retained.** |
| `holocron/heartbeat` | Pi → broker | epoch ts, 60s | Optional Uptime Kuma monitor. |

**Commands:** `force:<folder>` = manual mode (play now, projector on, ignore schedule); `stop` = stop + projector off + stay off; `resume` = return to schedule; `reload` = re-read config.

---

## 6. Scheduler + date/solar engine

### 6.1 Config file
`/data/holidays.json` (persistent partition, §9). JSON, written by the web form, read by the player.

### 6.2 Location (for solar times)
Top-level block; used by `astral` to compute sunrise/sunset/dawn/dusk per date. **Verify these coordinates** (The Dalles, OR):
```json
"location": { "lat": 45.5946, "lon": -121.1787, "tz": "America/Los_Angeles" }
```
`tz` handles DST automatically.

### 6.3 Windows
- Each holiday has `play_windows`: an **array** of `{ start, end }`. "Should play" = today matches the rule AND now is inside **any** window. Multiple windows = union (lets one day have separate blocks).
- `start`/`end` accept:
  - a clock time `"HH:MM"`,
  - `"24:00"` = end of day (the following midnight),
  - a **solar anchor**: `"sunset"`, `"dusk"` (civil twilight end), `"sunrise"`, `"dawn"`, each with optional ± offset, e.g. `"dusk-00:15"`, `"sunset+00:30"`.
- Solar anchors are resolved for **today's date** at the configured location. Keep each window within one day (no crossing midnight; use a separate window for the small-hours block, as New Year's does).
- **Default window** (if a holiday omits `play_windows`): `[{ "start": "dusk", "end": "24:00" }]` — starts at dark, tracks the seasons.

### 6.4 Schema + final config
```json
{
  "version": 3,
  "location": { "lat": 45.5946, "lon": -121.1787, "tz": "America/Los_Angeles" },
  "defaults": { "play_windows": [ { "start": "dusk", "end": "24:00" } ] },
  "holidays": [
    { "name": "New Year's Day", "folder": "New Years", "enabled": true, "priority": 10,
      "rule": { "type": "annual_date", "date": "01-01" },
      "play_windows": [ { "start": "00:00", "end": "01:00" }, { "start": "07:30", "end": "22:00" } ] },

    { "name": "Alexa's Birthday", "folder": "Birthday", "enabled": true, "priority": 30,
      "rule": { "type": "annual_date", "date": "03-07" },
      "play_windows": [ { "start": "07:30", "end": "22:00" } ] },

    { "name": "St. Patrick's Day", "folder": "St Patricks", "enabled": true, "priority": 10,
      "rule": { "type": "annual_date", "date": "03-17" } },

    { "name": "Easter", "folder": "Easter", "enabled": true, "priority": 10,
      "rule": { "type": "easter", "days_before": 2, "days_after": 0 } },

    { "name": "Memorial Day", "folder": "Memorial Day", "enabled": true, "priority": 10,
      "rule": { "type": "floating", "month": 5, "weekday": "monday", "n": -1, "days_before": 2, "days_after": 0 } },

    { "name": "Star Wars Day", "folder": "Star Wars", "enabled": true, "priority": 20,
      "rule": { "type": "annual_date", "date": "05-04" } },

    { "name": "Independence Day", "folder": "Independence Day", "enabled": true, "priority": 10,
      "rule": { "type": "annual_date", "date": "07-04" } },

    { "name": "Danny's Birthday", "folder": "Birthday", "enabled": true, "priority": 30,
      "rule": { "type": "annual_date", "date": "09-10" },
      "play_windows": [ { "start": "07:30", "end": "22:00" } ] },

    { "name": "Halloween", "folder": "Halloween", "enabled": true, "priority": 10,
      "rule": { "type": "annual_range", "start": "10-01", "end": "10-31" } },

    { "name": "Veterans Day", "folder": "Memorial Day", "enabled": true, "priority": 10,
      "rule": { "type": "annual_date", "date": "11-11" } },

    { "name": "Thanksgiving", "folder": "Thanksgiving", "enabled": true, "priority": 10,
      "rule": { "type": "floating", "month": 11, "weekday": "thursday", "n": 4, "days_before": 3, "days_after": 0 } },

    { "name": "Christmas", "folder": "Christmas", "enabled": true, "priority": 10,
      "rule": { "type": "span",
                "start": { "floating": { "month": 11, "weekday": "thursday", "n": 4, "offset_days": 1 } },
                "end":   { "date": "12-31" } } }
  ]
}
```
Holidays without `play_windows` (St Patrick's, Easter, Memorial Day, Star Wars, Independence Day, Halloween, Veterans Day, Thanksgiving, Christmas) inherit the default **dusk → midnight** window.

### 6.5 Rule types (`python-dateutil`)
- `annual_date` — `{ date, days_before?, days_after? }`.
- `annual_range` — `{ start, end }`, handles year-wrap.
- `floating` — `{ month, weekday, n, days_before?, days_after? }`; `n` 1–5 or **`-1` = last** weekday.
- `easter` — `{ days_before?, days_after? }`; Easter Sunday **computed** via `dateutil.easter()`.
- `span` — `{ start, end }` where each anchor is `{date}`, `{floating:{…,offset_days}}`, or `{easter:{offset_days}}`. (Christmas: Friday after Thanksgiving → Dec 31.)

### 6.6 Window summary (as specified)
- **New Year's** — 00:00–01:00 and 07:30–22:00 (clock; split to limit projector runtime).
- **Birthdays** (Alexa Mar 7, Danny Sep 10) — 07:30–22:00.
- **Everything else** — default **dusk → midnight** (starts earlier in fall/winter automatically).
- Heads-up for the daytime windows (birthdays, New Year's morning/day): a projector washes out in daylight — fine in a dim/curtained room, weak in bright daylight even at 5000 lm. Tunable in the web UI.

### 6.7 Scheduling logic
1. Find enabled holidays matching today; highest `priority` wins (ties by list order). Birthdays(30) > Star Wars(20) > seasons(10).
2. Resolve the winner's `play_windows` for today (solar anchors → clock times via `astral`).
3. If now ∈ any window → should play; else → should not.
4. Re-evaluate every 30–60 s and on `reload`; on transition, start/stop mpv and publish `projector` on/off + `status`.
5. `force:`/`stop` suspend the loop (manual mode) until `resume`.

### 6.8 Safety
Validate config on load; on bad JSON / missing / empty folder, keep **last known-good**, publish `error:`, log — never go dark. Folder names must match real subfolders under `HORIZONTAL/`; the web form populates choices from the live listing.

---

## 7. Player service (`holocron-player`)

Python 3 (`paho-mqtt`, `python-dateutil`, `astral`), systemd service owning the display. Player = `mpv` to KMS/DRM. On "should play": enumerate allowed video files, write a temp playlist, launch:
```
mpv --vo=drm --hwdec=auto --fullscreen --loop-playlist=inf --shuffle \
    --no-osc --no-input-default-bindings --no-input-terminal --really-quiet \
    --playlist=/run/holocron/playlist.txt
```
**DRM/KMS gotcha:** needs DRM master + a free VT. `systemctl disable --now getty@tty1`, run on tty1, user in `video`/`render`:
```ini
# /etc/systemd/system/holocron-player.service
[Unit]
Description=Holocron scheduler + player
After=network-online.target mnt-jedi\x2darchives.automount
Wants=network-online.target
[Service]
User=holocron
SupplementaryGroups=video render
StandardInput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
ExecStart=/opt/holocron/venv/bin/python /opt/holocron/player.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

---

## 8. Web config form (`holocron-web`)

Flask on the Pi's LAN IP, port `8080`, no auth (trusted LAN; not exposed externally). Clean **semantic HTML, minimal CSS** for easy restyling. Features: list/add/edit/delete holidays; **folder = dropdown of real `HORIZONTAL/` subfolders**; rule editor (all five types); **multi-window editor** with clock-or-solar start/end (and offset) per window; priority; enabled. "What's playing now / next" readout; Play-now/Stop buttons. Validate, write `/data/holidays.json` atomically, publish `reload`. Second systemd service (`holocron-web.service`, no tty).

---

## 9. Filesystem & read-only root

```
/opt/holocron/        # code + venv (static)
/etc/holocron/        # smb-credentials (0600 root)
/data/                # persistent partition: holidays.json
/mnt/jedi-archives/   # SMB automount (ro)
```
Build writable first (Phase 1); `/data` is its own partition from day one so the later `overlayroot` flip (Phase 2) is clean and the config persists. Logs → journald (volatile) or shipped to Loki.

---

## 10. Homey side

App: **MQTT Client** (`nl.scanno.mqtt`), broker `10.0.0.147:1883`. Flows are relay-only:
- MQTT `holocron/projector` = `on` → BenQ app: projector ON.
- MQTT `holocron/projector` = `off` → BenQ app: projector OFF.
- (Optional) subscribe `holocron/active_holiday` → Logic text variable for dashboards/notifications.
Retained topic = correct state on reconnect. **No time, date, or lux logic in Homey.**

---

## 11. Build phases

**Phase 0 — prerequisites:** Synology `holiday-ro` + ACL; confirm `HORIZONTAL/` folder names; verify location coordinates; static IP; MQTT Client app (done).

**Phase 1 — working system (writable FS):** Pi OS Lite + user `holocron` + groups + disable `getty@tty1`; install `mpv`, venv (`paho-mqtt`, `python-dateutil`, `astral`), `cifs-utils`; SMB mount; **prove one clip full-screen on the projector via mpv/DRM first**; build `player.py` (scheduler + date/solar engine + multi-window + mpv + MQTT) and `web.py`; seed `/data/holidays.json`; wire Homey relay flows. Test all windows + rules below.

**Phase 2 — hardening:** `/data` own partition; enable `overlayroot`; verify config survives reboot + hard power pull.

**Phase 3 — optional:** Uptime Kuma MQTT monitor on `heartbeat`; ship logs to Loki.

---

## 12. Acceptance checklist
- [ ] mpv plays a folder full-screen/shuffled/looped via DRM (no desktop).
- [ ] SMB automounts read-only; survives boot network race.
- [ ] Scheduler drives `holocron/projector` at correct window edges; Homey mirrors it.
- [ ] **Solar windows resolve correctly:** default evening window starts at dusk and shifts earlier in fall/winter, later in spring/summer (verify a winter date vs a summer date).
- [ ] **Multi-window works:** New Year's plays 00:00–01:00 and 07:30–22:00; birthdays 07:30–22:00.
- [ ] Christmas runs Black Friday → 23:59 Dec 31, hands to New Year's at midnight; Thanksgiving Mon–Thu; Memorial Day last Monday (+weekend); Easter computed; Halloween Oct 1–31; Independence Day Jul 4 only.
- [ ] Priority overlaps resolve (birthday > Star Wars > season).
- [ ] `force:`/`stop`/`resume`/`reload` behave as specified.
- [ ] Web form: add/edit/delete, folder dropdown reflects real folders, multi-window + solar/clock editor, validation blocks bad input.
- [ ] Broken config → last-good kept, `error:` published, never goes dark.
- [ ] (Phase 2) Config survives reboot + hard power pull under read-only root.
- [ ] Folders match: New Years, Birthday (Mar 7 + Sep 10), St Patricks, Easter, Memorial Day (also Veterans Day), Star Wars, Independence Day, Halloween, Thanksgiving, Christmas.
