# Holocron — Holiday Video Projection Controller

Raspberry Pi 4 app that drives a BenQ SH915 projector to play a folder of
holiday videos on a schedule. The active holiday and the hours it plays are
chosen automatically by date, with evening windows that start at **dusk** so
they track the seasons. Holocron owns scheduling; Homey only mirrors the
`holocron/projector` MQTT topic to the projector.

See [docs/Claude_Code_Handoff_Holocron_v3.md](docs/Claude_Code_Handoff_Holocron_v3.md)
for the full spec — it is the source of truth.

## Components

- `src/player.py` — scheduler + date/solar engine + `mpv` (DRM/KMS) + MQTT.
- `src/web.py` — Flask config form on `:8080` for editing `holidays.json`.
- `src/scheduler.py` — pure date/window logic (no I/O), unit-testable.
- `systemd/` — `holocron-player.service`, `holocron-web.service`.
- `config/holidays.example.json` — seed config (copy to `/data/holidays.json`).
- `scripts/install.sh` — one-shot installer for the Pi.

## Layout on the Pi

```
/opt/holocron/        code + venv (static)
/etc/holocron/        smb-credentials (0600 root)
/data/                persistent partition: holidays.json
/mnt/jedi-archives/   SMB automount (ro)
```

## Quick install (on the Pi, as the `holocron` user)

```bash
sudo bash scripts/install.sh
```

The installer creates `/opt/holocron`, the venv, the systemd units, seeds
`/data/holidays.json` if missing, and disables `getty@tty1` (needed so `mpv`
can take DRM master on tty1). It does **not** touch `/etc/fstab` or
`/etc/holocron/smb-credentials` — the SMB mount is already in place.

## MQTT contract

| Topic | Direction | Payload |
|---|---|---|
| `holocron/projector` | Pi → Homey | `on` \| `off` (retained) |
| `holocron/cmd` | → Pi | `force:<folder>` \| `stop` \| `resume` \| `reload` |
| `holocron/active_holiday` | Pi → broker | `<name>` or `none` (retained) |
| `holocron/status` | Pi → broker | `playing:<name>` \| `idle` \| `manual:<folder>` \| `error:<msg>` (retained) |
| `holocron/heartbeat` | Pi → broker | epoch ts, every 60s |

Broker: `10.0.0.147:1883`, unauthenticated, client id `holocron`.

## Local development

The scheduler logic is pure functions in `src/scheduler.py` — runs and tests
on any machine:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/
```

## License

MIT — see [LICENSE](LICENSE).
