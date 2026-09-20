# Holocron — Project History & Handoff

*A narrative record of how this project came to be, what it is, what was learned
building it, and where it stands. Written for a developer picking it up fresh.
For how to build/deploy/operate it, read the [Build Guide](BUILD_GUIDE.md); for
the design rationale, read the two spec docs referenced below. This document is
the "why and when," not the "how."*

**Owner:** Fred Hill (Danny)
**Repo:** https://github.com/fredhill/holocron-projector (public)
**Status as of Sep 2026:** Built, deployed, and running on the target hardware.
Video, scheduling, manual control, web UI, and analog audio are all validated.
Amp + speaker install and the first fully-unattended holiday run are the
remaining open items.

---

## 1. The original idea

Play holiday videos on a projector, automatically, without anyone having to
think about it. Point a projector at the house/porch, and on the right days —
Halloween, Christmas, the 4th of July, birthdays — have themed video start
playing at the right time of the evening and stop later, looping and shuffled,
with no manual intervention.

The seed constraints that shaped everything:

- **The schedule should track the seasons.** "Start at dusk," not "start at
  6pm" — because dusk is 4:30pm in December and 9pm in June. This one
  requirement is why there's a solar-time engine in the code.
- **One brain, one clock.** All the date/time intelligence lives in exactly one
  place (the Pi). The smart-home hub (Homey) is a dumb relay that only mirrors
  an on/off signal to the projector. No duplicated schedules, no drift.
- **It must never "go dark" from a bad edit.** A misconfigured holiday should
  never take the whole system down.
- **Runs on cheap, low-power hardware** left on 24/7 in a basement.

## 2. Scope

**In scope (built):**

- A Raspberry Pi 4 that plays video full-screen via `mpv` on the DRM/KMS
  display stack (no desktop environment).
- A scheduling engine that picks the active holiday by date and decides whether
  *now* is inside one of that holiday's play windows, resolving solar anchors
  (`dusk`/`sunset`/`sunrise`/`dawn`, with offsets) for the current date and
  location.
- Five holiday rule types: fixed annual date, annual date range, floating
  weekday (e.g. "4th Thursday of November"), Easter (computed), and multi-week
  spans (e.g. Black Friday → Dec 31 for Christmas).
- Multiple play windows per holiday (e.g. New Year's plays 00:00–01:00 **and**
  07:30–22:00).
- MQTT integration so Homey can switch the projector on/off, plus manual
  override commands (`force`/`stop`/`resume`/`reload`).
- A web config UI (Flask) for editing the whole schedule without touching JSON.
- Audio out the Pi's analog jack to a (future) porch speaker.

**Explicitly out of scope / deliberately excluded:**

- **No scheduling logic in Homey.** Homey mirrors one retained MQTT topic. That's it.
- **No HDMI audio extractor.** Audio was decided to come off the 3.5mm jack to a
  local amp + speaker wire, keeping the video path untouched. (See audio doc §"Why the amp lives at the Pi.")
- **No transcoding pipeline.** Videos are pre-encoded to Pi-friendly H.264/H.265;
  the player just skips anything it can't read.
- **No authentication on the web UI or MQTT.** Trusted, isolated IoT VLAN only.
  This is a documented, accepted trade-off, not an oversight.

## 3. Architecture in one paragraph

The Pi runs two systemd services. `holocron-player` is the brain: every ~30s it
reads `/data/holidays.json`, figures out the active holiday and whether the
current time falls in a play window, and starts or stops `mpv` accordingly —
publishing `holocron/projector = on|off` (retained) to an MQTT broker. Homey
subscribes to that one topic and flips the projector (and, later, the amp's
smart plug). `holocron-web` is a Flask app on `:8080` that edits the JSON config
and pokes the player to reload. Videos are read over a read-only SMB mount from
a Synology NAS. That's the whole system.

```
Pi (player + web) ──HDMI──► projector (video)
   │  └──3.5mm──► amp ──► porch speaker (audio)
   └──MQTT──► broker ──► Homey ──► projector on/off
   └──SMB (ro)──► NAS (video files)
```

## 4. What's been done (chronological)

The project was built in a tight burst in June 2026, then hardware-tested over
the following weeks.

**Build & harden (June 7, 2026)** — The initial implementation landed complete:
scheduler engine, player, Flask web UI, systemd units, installer, seed config,
and a pytest suite. Same day, three fast follow-ups: an editable Location form,
a security/correctness hardening pass (see "Lessons" below), and a full dark-mode
UI restyle.

**Polish (June 10, 2026)** — Console blanking (so the projector shows black, not
the Pi's terminal, between videos) and holiday emoji icons in the web UI.

**Hardware bring-up (late June 2026)** — The HDMI cable arrived and the system
was tested on the real projector for the first time. Video worked; audio was
silent at every output. Root-caused to mpv having no audio configured and Pi OS
Lite shipping no sound server. Fixed by driving ALSA directly. Also hardened the
web UI against a transient "Internal Server Error" traced to a briefly-wedged
SMB mount.

**Documentation (June 29, 2026)** — A full end-to-end build guide.

**Validation (late June – early July 2026)** — Confirmed on hardware: forced
holidays play the correct folder, manual stop works, console blanking works, and
the analog audio path passes sound (verified with headphones on the 3.5mm jack).

## 5. What was learned

Real lessons from building and testing this, worth knowing before you change anything:

- **mpv on Pi OS Lite needs audio spelled out explicitly.** There's no
  PulseAudio/PipeWire, so mpv's automatic audio output finds nothing and fails
  *silently*. The fix is `--ao=alsa` + an explicit `--audio-device=`. The device
  name (`alsa/plughw:CARD=Headphones`) varies per Pi/OS and must be confirmed
  with `mpv --audio-device=help`. This cost a full test cycle to diagnose.
- **Audio is on the analog jack by design — the projector is meant to be silent.**
  A future maintainer will "discover" no sound from the projector and think it's
  broken. It isn't. Sound goes out the 3.5mm jack to a porch speaker; HDMI
  carries video only. This is called out in three docs for exactly this reason.
- **mpv hands the VT back showing the kernel console when it exits.** On a
  projector that looks like garbage between videos. The player now clears tty1
  and hides the cursor on every stop and at startup.
- **Retained MQTT state can go stale after a reboot.** State topics only publish
  on *change*, and the first post-boot publish can race the broker connection
  (QoS 0 = dropped). The player now re-asserts all state on every MQTT
  (re)connect, so Homey re-syncs within one tick. This was caught in review, not
  in the field — but it's the kind of thing that would've caused a
  "projector stuck on" mystery months later.
- **A wedged SMB mount can 500 the web UI in a way only a reboot clears.** A
  stale CIFS handle makes directory listing throw. The listing now degrades to
  an empty dropdown and a catch-all error handler renders a readable page. (Note
  the residual risk: a *hung* mount can block rather than raise; that would need
  a mount timeout, not a code change. Not yet hit in practice.)
- **The "projector won't turn off" class of bug is almost always the Homey flow,
  not the Pi.** Because the Pi and Homey are cleanly separated by one retained
  topic, `mosquitto_sub -h <broker> -t 'holocron/#' -v` instantly tells you which
  side is at fault. This diagnostic separation has paid off repeatedly.
- **Keep the schedule math pure and testable.** All date/window logic lives in
  `scheduler.py` with zero I/O, so it runs and is unit-tested on a laptop with no
  Pi, no projector, no broker. The suite grew from 18 → 52 tests and has caught
  regressions on every change since.
- **The Homey "app" is not an app.** Several requests came in to "audit the
  Homey app code." There is none — the Homey side is the stock MQTT Client app
  plus two relay flows built in Homey's UI. Nothing to audit or version here.

## 6. Repositories

| Repo | URL | Contents |
|---|---|---|
| holocron-projector | https://github.com/fredhill/holocron-projector | Everything: player, web UI, scheduler, systemd units, installer, tests, docs. Single source of truth. |

No other repos. There is no separate Homey codebase (see above). The design
docs live in `docs/` alongside the code.

## 7. Version history

The project uses a rolling `main` branch — no tagged releases yet. Commit
history to date:

| # | Date | Commit | Summary |
|---|---|---|---|
| 1 | 2026-06-07 | `ee6061d` | Initial commit — full implementation: scheduler, player, web UI, systemd, installer, 18 tests |
| 2 | 2026-06-07 | `ab913d3` | Ignore `.claude/` local settings (keep editor config out of the public repo) |
| 3 | 2026-06-07 | `e8d1f58` | Editable Location form + `validate_location` (lat/lon range, IANA tz check) |
| 4 | 2026-06-07 | `33826c1` | Hardening pass: path-traversal guard on `force:`, rule-type + window-string validation, config-mtime reload fallback, folder-existence check, `waitress` prod server, mpv stderr → journald, status dedup |
| 5 | 2026-06-07 | `5cd6f5d` | Web UI restyle — dark theme, festive accent, dyslexia-friendly type, responsive |
| 6 | 2026-06-10 | `f9b0553` | Blank console between videos + holiday emoji icons |
| 7 | 2026-06-29 | `e22f2c4` | Analog audio output (ALSA) + web hardening vs. SMB/config errors + graceful error page |
| 8 | 2026-06-29 | `7f7d133` | End-to-end build guide |

**Suggested next milestone:** tag the current state `v1.0` once the first
unattended holiday run is confirmed, so there's a known-good baseline to roll
back to.

## 8. Current state & open items

**Working and validated on hardware:**
- Full-screen shuffled/looped video via DRM/KMS.
- Read-only SMB automount of the video library.
- Date/solar/multi-window scheduling with priority resolution.
- `force`/`stop`/`resume`/`reload` manual commands.
- Web UI: add/edit/delete holidays, folder dropdown, rule + multi-window editor,
  editable location, manual controls, live "now playing" status.
- Last-known-good config on bad edits; graceful web error page.
- Console blanking; holiday icons.
- Analog audio path (headphone test passed).

**Open / not yet done:**
- [ ] **First fully-unattended holiday run** — everything so far has been
  forced manually. The real test is a holiday firing on its own at the right
  time. (Independence Day was the next candidate after the June testing.)
- [ ] **Amp + speaker install** — parts (Fosi V3 amp, Polk Atrium 4 speaker)
  identified but not yet installed. When done, add the amp's Shelly plug as a
  second action on the existing Homey flows.
- [ ] **Phase 2 hardening from the v3 spec** — move to a read-only root
  filesystem (`overlayroot`) with `/data` as its own partition, so a hard power
  pull can't corrupt the SD card. `/data` is already separate to make this clean.
- [ ] **Optional monitoring** — Uptime Kuma on the `holocron/heartbeat` topic;
  ship logs to Loki.
- [ ] Consider a `v1.0` git tag (see §7).

## 9. Where a new developer should start

1. Read the [Build Guide](BUILD_GUIDE.md) top to bottom — it's the operational manual.
2. Skim [Claude_Code_Handoff_Holocron_v3.md](Claude_Code_Handoff_Holocron_v3.md)
   (system spec, source of truth) and
   [Holocron_Audio_Addon_v2.md](Holocron_Audio_Addon_v2.md) (audio design).
3. Clone the repo and run the tests on your laptop — no hardware needed:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   PYTHONPATH=src .venv/bin/python -m pytest tests/
   ```
4. Read `src/scheduler.py` first — it's pure, self-contained, and holds all the
   date logic. Then `src/player.py` (the runtime loop + mpv + MQTT), then
   `src/web.py` (the config UI).
5. To understand the deployed system, SSH to the Pi (`ssh holocron@holocron`),
   `systemctl status holocron-player holocron-web`, and
   `journalctl -u holocron-player -f`.

**Mental model to hold onto:** the Pi decides *everything*; Homey mirrors one
retained topic; audio is analog-jack-only by design; and the schedule math is
pure and tested. If you keep those four facts in mind, the rest follows.

---

## 10. Document map

| Doc | Purpose |
|---|---|
| `README.md` | Repo landing page, component list, quick reference |
| `docs/Holocron_Project_History.md` | **This file** — origin, scope, lessons, version history, handoff |
| `docs/BUILD_GUIDE.md` | How to build, deploy, configure, operate, troubleshoot |
| `docs/Claude_Code_Handoff_Holocron_v3.md` | Full system specification (source of truth) |
| `docs/Holocron_Audio_Addon_v2.md` | Audio/speaker hardware design |
