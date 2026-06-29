# Holocron Audio Add-on — Porch Speaker (v2)

*Companion to the v3 Holocron handoff. Gets sound out at the porch whenever Holocron plays, through one hidden outdoor speaker. Audio follows video automatically — no separate playback logic.*

**Decisions locked (June 2026):**
- **Audio source: the Pi's 3.5mm analog jack** — no HDMI extractor. Keeps the video path completely untouched.
- **Amp lives at the Pi**, with ~25 ft of speaker wire out to the porch.
- **Mono** — one concealed speaker.

---

## Signal path

```
Holocron Pi ── micro-HDMI ──► BenQ SH915 (video only)
     │
     └─ 3.5mm jack ─► amp ─── speaker wire (~25 ft) ───► outdoor speaker
                       ▲
                  Shelly plug (Homey switches it ON with the projector)
```

The Pi already sends video over HDMI. Audio is taken separately off the 3.5mm jack into the amp. Nothing new sits in the HDMI path.

---

## Why the amp lives at the Pi (not at the speaker)

With ~25 ft between the Pi and the stairs, the real question is *which signal travels that distance*:

- **Line-level analog** (the Pi's 3.5mm output) is a weak, unbalanced signal. Run 25 ft it readily picks up hum and interference, especially near AC wiring.
- **Speaker-level** wiring (the amp's output) is higher-voltage and far more forgiving over distance — 25 ft of 16 AWG is trivial.

So the amp sits **at the Pi**, and the long run is **speaker wire**. The amp, its power brick, and the Shelly plug all live in a dry spot by the Pi; only the speaker goes outside.

(This is also why the optical option from the first draft is gone — without the extractor there's no digital output to send, and speaker-level wiring solves the noise problem better anyway.)

---

## Parts list (revised)

| Item | Qty | Notes |
|---|---|---|
| Polk Atrium 4 outdoor speaker (passive) | 1 | Mounts under the overhang, facing out. ~$100–180/pair (often sold in pairs — keep the spare). *Verify current price.* |
| Fosi Audio V3 amp | 1 | **Confirmed compatible:** RCA input, drives passive speakers, 2–8Ω (Polk is 8Ω). Lives by the Pi. **Buy a variant that INCLUDES the power supply** (32V/5A is plenty — skip 48V/GaN); do not pick "V3-No power supply." ~$72 with code `SPD20`. Manual volume — set it modestly and leave it (300W amp into a small speaker; don't crank it). |
| 3.5mm-to-RCA cable | 1 | Short. Pi 3.5mm jack → amp RCA input. (Confirm the Fosi input is RCA.) |
| 16 AWG speaker wire | ~50 ft | ~25 ft run + slack. Outdoor / direct-burial rated for any exposed portion. |
| Shelly Plug US (Gen3 / Gen4) | 1 | Powers the amp; Homey switches it with the projector. Already part of the smart-plug plan. |
| Weatherproof enclosure | — | **No longer needed for the electronics** — they're dry by the Pi now. Only relevant if the Pi itself sits outdoors. |

**Removed from the original draft:** the HDMI audio extractor + its USB power, and the optical/RCA long-run interconnect.

---

## Getting sound out (root cause + fix)

**Confirmed by testing (June): video works, but no audio comes out of any jack** — even HDMI to the projector was silent at max volume. The cause: the player's mpv command has no audio configured, **and Pi OS Lite has no sound server** (no PulseAudio/PipeWire). mpv's automatic audio output expects one, finds nothing, and silently fails.

**The fix — three additions to the mpv command, all in one place:**

1. **Talk to ALSA directly:** `--ao=alsa` (don't rely on a sound server that isn't installed).
2. **Pick the analog jack:** `--audio-device=alsa/<analog device>`. Get the exact name with `mpv --audio-device=help` — it's the bcm2835 headphones/analog entry (HDMI shows up as separate entries). Confirm `dtparam=audio=on` is set in `/boot/firmware/config.txt` so the jack exists under the KMS video driver.
3. **Downmix to mono:** `--audio-channels=mono` so both-channel content lands on the one speaker, nothing lost. Connect the speaker to one amp channel (e.g. L); the other goes unused.

Everything else in the mpv command stays exactly as-is.

**Prove the jack works first — 2-minute test, no amp needed.** Plug headphones (or any powered speaker) into the Pi's 3.5mm jack and run:
```
speaker-test -D plughw:CARD=Headphones -c2 -twav
```
If you hear it, the jack + routing are good and only the mpv flags remain. If silent, confirm `dtparam=audio=on` and check `aplay -l` for the analog card name. This validates the whole audio path before the amp even arrives — so the purchase carries no surprise.

---

## Homey side (one added action — no new flow)

In the existing relay flows, add the amp's Shelly plug as a second action:

- **When** `holocron/projector` = `on` → projector ON **+ amp plug ON**
- **When** `holocron/projector` = `off` → projector OFF **+ amp plug OFF**

Audio follows video for free — same retained topic, same single source of truth. No new playback logic anywhere.

---

## Confirm during Wednesday's cable test

Before buying the amp + speaker, prove audio is actually leaving the Pi:

- Force a holiday and check whether sound reaches the **projector** (the SH915 has a built-in speaker / audio-out). If you hear it, the audio chain works end-to-end and only the routing flag above may need setting.
- If it's silent, that's the mpv/ALSA routing fix — a config change, not a hardware problem.

This costs nothing and de-risks the purchase.

---

## Open / verify before buying
- **Pi location** — indoors (dry) or outdoors? Decides whether even the amp needs any weather protection. If the Pi is indoors projecting out a window, only the speaker is exposed — ideal.
- **Fosi input type** — V3 is RCA; confirm so the 3.5mm cable matches.
- **Speaker placement** — under the overhang, facing out. Don't box it into a cavity (muffles it); only the electronics want enclosure.
- All prices rough — verify stock/pricing at purchase.
