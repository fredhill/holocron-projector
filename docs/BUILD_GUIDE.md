# Holocron — Build Guide

End-to-end guide to building, deploying, and operating Holocron: the Raspberry
Pi controller that plays holiday videos on a projector, on an automatic
date- and dusk-driven schedule, with sound out to a porch speaker.

This guide is the practical "how to stand it up and run it" companion to the
two design docs, which remain the source of truth for *why*:

- [Claude_Code_Handoff_Holocron_v3.md](Claude_Code_Handoff_Holocron_v3.md) — full system spec.
- [Holocron_Audio_Addon_v2.md](Holocron_Audio_Addon_v2.md) — audio/speaker design.

---

## 1. What it does

On a schedule, Holocron plays a folder of holiday videos full-screen, shuffled
and looped, on a BenQ SH915 projector. It picks the active holiday by date
(annual dates, floating holidays like Thanksgiving, Easter, multi-week spans
like Christmas) and decides whether *right now* falls inside that holiday's
play windows — where a window edge can be a clock time **or** a solar anchor
(`dusk`, `sunset`, `sunrise`, `dawn`), so evening shows start earlier in winter
and later in summer, automatically.

**Holocron owns all the logic and timing.** When it should show video it starts
mpv and publishes `holocron/projector = on` over MQTT; otherwise it stops mpv
and publishes `off`. **Homey only mirrors that one topic** to the projector
(and, once added, the amp's smart plug). No dates, times, or sensors in Homey.

```
        ┌─────────────┐
        │   Homey     │  subscribes holocron/projector → BenQ ON/OFF (+ amp plug)
        └──────▲──────┘ ───────────────────────────────────────────────► Projector
               │ holocron/projector (retained on/off)
        ┌──────┴──────────────┐
        │ Mosquitto broker     │ (10.0.0.147:1883, no auth)
        └──────────▲──────────┘
        ┌──────────┴───────────────────┐
        │ Holocron (Raspberry Pi 4)     │ ── micro-HDMI → DRM/KMS ──► Projector (video)
        │  • scheduler + mpv (player)   │ ── 3.5mm jack → amp ──────► Porch speaker (audio)
        │  • web config form :8080      │
        │  • SMB read-only mount        │ ── reads videos ──► NAS (10.0.0.219)
        └──────────────────────────────┘
```

---

## 2. Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 4 Model B + PoE HAT | Runs 24/7. micro-HDMI → HDMI to the projector (USB-C is power only). |
| 32 GB high-endurance microSD | Raspberry Pi OS **Lite** 64-bit (Bookworm/Trixie). |
| BenQ SH915 projector (1080p) | Switched on/off by Homey via the BenQ app. |
| Synology NAS | Holds the videos; serves them over SMB read-only. |
| Audio (later) | Fosi V3 amp at the Pi + Polk Atrium 4 speaker on ~25 ft of 16 AWG. See the audio doc. |

Network: Pi on the IoT VLAN (`10.50.x.x`), must reach SMB `10.0.0.219:445` and
MQTT `10.0.0.147:1883`.

---

## 3. Prerequisites (already built — do not redo)

These were set up and validated before the app work and are **not** part of the
install script:

- **SMB mount.** `//10.0.0.219/The Jedi Archives` → `/mnt/jedi-archives`
  (read-only automount) via `/etc/fstab`, credentials in
  `/etc/holocron/smb-credentials` (root, 0600). Videos live under
  `/mnt/jedi-archives/Media/Projector Movies/HORIZONTAL/<Folder>/`.
- **Synology `holiday-ro` account** — read-only, with the Advanced Permissions
  ACL granted (avoids the error-13 gotcha).
- **Homey MQTT Client app** (`nl.scanno.mqtt`) installed and pointed at the broker.

If you're rebuilding the Pi from scratch, restore these first (see §3 of the
v3 handoff doc), then continue below.

---

## 4. One-time Pi setup

1. Image Raspberry Pi OS **Lite** 64-bit. Set hostname `holocron`, username
   `holocron` (uid/gid 1000 — matters for the SMB mount), enable SSH, no Wi-Fi.
2. Give it a static DHCP lease on the IoT VLAN.
3. Confirm it can reach the NAS and broker, and that `/mnt/jedi-archives` mounts
   and lists your holiday folders:
   ```bash
   ls "/mnt/jedi-archives/Media/Projector Movies/HORIZONTAL/"
   ```

---

## 5. Deploy the app

Install `git` if it isn't there, clone, and run the installer:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/fredhill/holocron-projector.git
cd holocron-projector
sudo bash scripts/install.sh
```

The installer is **idempotent** — safe to re-run after every `git pull`. It:

- installs `mpv`, `python3-venv`, `cifs-utils`;
- stages the code to `/opt/holocron` and builds the venv from `requirements.txt`;
- seeds `/data/holidays.json` from `config/holidays.example.json` **only if it
  doesn't already exist** (your live config is never overwritten);
- creates `/run/holocron` (mpv playlist scratch) and makes it persist via tmpfiles;
- disables `getty@tty1` so mpv can take DRM master on tty1;
- adds `holocron` to the `video`, `render`, and `audio` groups;
- enables `dtparam=audio=on` in the boot config (reboot required the first time
  only — the script tells you);
- installs and starts the two systemd services.

It does **not** touch `/etc/fstab` or the SMB credentials.

### Updating later

```bash
cd ~/holocron-projector && git pull && sudo bash scripts/install.sh
```

A reboot is only needed when the installer reports it changed the audio boot
parameter. Otherwise the service restart it performs is enough.

---

## 6. Configuration

The schedule lives in `/data/holidays.json` (its own persistent partition). You
normally edit it through the **web UI**, never by hand:

```
http://holocron:8080      (or http://10.50.0.116:8080)
```

From there you can:

- See **what's playing now** (status card, green when playing, plus today's
  resolved windows and solar times).
- **Add/edit/delete holidays** — the folder field is a dropdown of the real
  subfolders under `HORIZONTAL/`, so you can't typo it.
- Set each holiday's **rule** (annual date, annual range, floating weekday,
  Easter, or span) and its **play windows** (clock times, `24:00` for
  end-of-day, or solar anchors like `dusk-00:15`).
- Edit the **location** (lat/lon/timezone) — this drives the solar-anchor math.
- **Manual controls:** Play a folder now, Stop, Resume schedule, Reload config.

Holidays without their own play windows inherit the default **dusk → midnight**.
Changes are written atomically and the player reloads them within ~30 seconds
(it also watches the file's modification time as a fallback if the reload
message is missed).

The default schedule covers New Year's, birthdays, St. Patrick's, Easter,
Memorial Day, Star Wars Day, Independence Day, Halloween, Veterans Day,
Thanksgiving, and Christmas. See `config/holidays.example.json`.

---

## 7. Audio

Audio goes out the Pi's **3.5mm analog jack**, downmixed to mono for one porch
speaker — **not** over HDMI. The projector's own speaker stays silent by
design.

The player drives ALSA directly (Pi OS Lite has no sound server), with defaults
that assume the analog jack is `alsa/plughw:CARD=Headphones`. Verify the path
before relying on it — plug in headphones and:

```bash
speaker-test -D plughw:CARD=Headphones -c2 -twav
```

If the jack name differs (check `mpv --audio-device=help`), override it in the
service file and restart:

```ini
# /etc/systemd/system/holocron-player.service  (uncomment + edit)
Environment=HOLOCRON_AUDIO_DEVICE=alsa/plughw:CARD=Headphones
```
```bash
sudo systemctl daemon-reload && sudo systemctl restart holocron-player
```

| Env var | Default | Purpose |
|---|---|---|
| `HOLOCRON_AUDIO` | `on` | `off` mutes (passes `--no-audio`) |
| `HOLOCRON_AUDIO_DEVICE` | `alsa/plughw:CARD=Headphones` | exact ALSA device; empty = mpv default |
| `HOLOCRON_AUDIO_CHANNELS` | `mono` | `stereo` for two speakers |

When the amp + speaker arrive: speaker to one amp channel, amp RCA in from the
Pi's 3.5mm jack, amp powered by a Shelly plug. Then add the plug as a **second
action** on the existing Homey flows (§9) — audio follows video for free.

---

## 8. MQTT contract

Broker `10.0.0.147:1883`, unauthenticated, client id `holocron`.

| Topic | Direction | Payload |
|---|---|---|
| `holocron/projector` | Pi → Homey | `on` \| `off` (**retained**) |
| `holocron/cmd` | → Pi | `force:<folder>` \| `stop` \| `resume` \| `reload` |
| `holocron/active_holiday` | Pi → broker | `<name>` or `none` (retained) |
| `holocron/status` | Pi → broker | `playing:<name>` \| `idle` \| `manual:<folder>` \| `error:<msg>` (retained) |
| `holocron/heartbeat` | Pi → broker | epoch ts, every 60s |

Commands: `force:<folder>` = play now and ignore the schedule; `stop` = stop and
stay off; `resume` = back to the schedule; `reload` = re-read config.

To watch what's actually on the wire (install `mosquitto-clients` first):

```bash
mosquitto_sub -h 10.0.0.147 -t 'holocron/#' -v
```

---

## 9. Homey side

Two relay flows, built in Homey's own UI — no Homey code:

- **When** `holocron/projector` = `on` → BenQ projector ON (+ amp plug ON).
- **When** `holocron/projector` = `off` → BenQ projector OFF (+ amp plug OFF).

The topic is retained, so Homey gets the correct state on reconnect. Match the
payload exactly (`on` / `off`, no quotes or whitespace). **No date, time, or
sensor logic in Homey.**

---

## 10. Operations

```bash
# service health
systemctl status holocron-player holocron-web

# live logs (mpv output and errors land here)
journalctl -u holocron-player -f
journalctl -u holocron-web -f

# restart after a manual change
sudo systemctl restart holocron-player
```

- **Code:** `/opt/holocron` (rebuilt by the installer; don't edit in place).
- **Config:** `/data/holidays.json` (persistent; survives reinstalls).
- **Playlist scratch:** `/run/holocron/playlist.txt` (tmpfs).
- The player keeps the **last known-good config** if a reload is invalid, and
  the web UI degrades to a friendly error page (rather than a 500) if the SMB
  share is briefly unreadable.

---

## 11. Troubleshooting

**Projector turns on but plays the wrong/nothing, or won't turn off.**
First confirm what the Pi published: run the `mosquitto_sub` command above and
watch for `holocron/projector on`/`off` when you Play/Stop in the web UI.
- If the correct value appears → the Pi is fine; the issue is the Homey flow.
  Check the trigger card's payload match is exactly `on`/`off` and the flow is
  enabled.
- If it doesn't appear → check `journalctl -u holocron-player`.

**~30–40 s delay turning on.** Expected: up to one 30 s scheduler tick plus the
projector's lamp warm-up. Not a fault.

**No sound from the speaker, but headphones in the jack work.** That's the
design — audio is on the analog jack only, never HDMI/projector. The speaker
lives on that jack. If even headphones are silent, recheck the audio device name
(§7).

**Web UI shows "Internal Server Error".** Usually a momentarily wedged SMB
mount. The page now degrades gracefully, but if it persists grab
`journalctl -u holocron-web -n 50` for the traceback. A reboot remounts the
share as a last resort.

**Black screen between videos.** Intended — the player blanks the console so the
projector never shows the Pi's terminal.

---

## 12. Acceptance checklist

- [x] mpv plays a folder full-screen / shuffled / looped via DRM (no desktop).
- [x] SMB automounts read-only and survives the boot network race.
- [x] Scheduler drives `holocron/projector`; Homey mirrors it to the projector.
- [x] Solar windows resolve and shift with the seasons; multi-window (New
      Year's / birthdays) works.
- [x] Priority overlaps resolve (birthday > Star Wars > season).
- [x] `force:` / `stop` / `resume` / `reload` behave as specified.
- [x] Web form: add/edit/delete, folder dropdown, multi-window + solar editor,
      validation, editable location.
- [x] Broken config → last-good kept, never goes dark.
- [x] Console blanks between videos.
- [x] Audio out the analog jack (headphone test passed).
- [ ] **Unattended holiday run** — verify a real holiday fires on its own
      (next test: Independence Day).
- [ ] Amp + speaker installed and Homey amp-plug action added.

---

## 13. Repo layout

```
src/scheduler.py   pure date/window logic (unit-tested, no I/O)
src/player.py      scheduler loop + mpv (DRM/KMS) + MQTT + console blanking + audio
src/web.py         Flask config UI on :8080
templates/         HTML (base, index, edit, location, error)
static/style.css   dark themeable UI
systemd/           holocron-player.service, holocron-web.service
config/            holidays.example.json (seed)
scripts/install.sh idempotent Pi installer
tests/             pytest suite for the scheduler/audio logic
docs/              this guide + the two design docs
```

Run the tests on any machine:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m pytest tests/
```
