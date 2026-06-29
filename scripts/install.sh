#!/usr/bin/env bash
# Install Holocron on the Raspberry Pi.
#
# Assumes the SMB mount + /etc/holocron/smb-credentials are already in place
# (Phase 0 done). Run as root from the cloned repo root:
#
#   sudo bash scripts/install.sh
#
# Idempotent — safe to re-run after a pull.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="/opt/holocron"
DATA_DIR="/data"
USER_NAME="holocron"

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root" >&2
  exit 1
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
  echo "user $USER_NAME does not exist — create it before running this script" >&2
  exit 1
fi

echo "==> apt: mpv, python3-venv, cifs-utils"
apt-get update
apt-get install -y mpv python3-venv python3-pip cifs-utils

echo "==> staging code → $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude 'tests' \
  "$REPO_DIR"/ "$APP_DIR"/
chown -R "$USER_NAME":"$USER_NAME" "$APP_DIR"

echo "==> venv + deps"
if [[ ! -d "$APP_DIR/venv" ]]; then
  sudo -u "$USER_NAME" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$USER_NAME" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$USER_NAME" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> /data + seed config (only if missing)"
mkdir -p "$DATA_DIR"
chown "$USER_NAME":"$USER_NAME" "$DATA_DIR"
if [[ ! -f "$DATA_DIR/holidays.json" ]]; then
  install -o "$USER_NAME" -g "$USER_NAME" -m 644 \
    "$REPO_DIR/config/holidays.example.json" "$DATA_DIR/holidays.json"
  echo "    seeded $DATA_DIR/holidays.json"
else
  echo "    $DATA_DIR/holidays.json already present — leaving alone"
fi

echo "==> /run/holocron"
mkdir -p /run/holocron
chown "$USER_NAME":"$USER_NAME" /run/holocron
# persistent across reboots
cat >/etc/tmpfiles.d/holocron.conf <<EOF
d /run/holocron 0755 $USER_NAME $USER_NAME -
EOF

echo "==> disable getty@tty1 (mpv needs DRM master on tty1)"
systemctl disable --now getty@tty1.service || true

echo "==> ensure $USER_NAME is in video/render/audio groups"
usermod -aG video,render,audio "$USER_NAME"

echo "==> ensure analog audio is enabled (dtparam=audio=on)"
BOOT_CONFIG=/boot/firmware/config.txt
[[ -f "$BOOT_CONFIG" ]] || BOOT_CONFIG=/boot/config.txt   # pre-Bookworm fallback
if [[ -f "$BOOT_CONFIG" ]]; then
  if grep -Eq '^\s*dtparam=audio=on' "$BOOT_CONFIG"; then
    echo "    already enabled in $BOOT_CONFIG"
  else
    echo "dtparam=audio=on" >> "$BOOT_CONFIG"
    echo "    added dtparam=audio=on to $BOOT_CONFIG — REBOOT REQUIRED for audio"
    NEED_REBOOT=1
  fi
else
  echo "    WARNING: no boot config found; enable dtparam=audio=on manually"
fi

echo "==> install systemd units"
install -m 644 "$REPO_DIR/systemd/holocron-player.service" /etc/systemd/system/
install -m 644 "$REPO_DIR/systemd/holocron-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable holocron-player.service holocron-web.service
systemctl restart holocron-player.service holocron-web.service

echo
echo "Done. Status:"
systemctl --no-pager --lines=5 status holocron-player.service || true
systemctl --no-pager --lines=5 status holocron-web.service || true
echo
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8080"
if [[ "${NEED_REBOOT:-0}" == "1" ]]; then
  echo
  echo "*** Reboot required: analog audio (dtparam=audio=on) was just enabled. ***"
  echo "    After reboot, verify the jack name with:  mpv --audio-device=help"
  echo "    If it isn't 'alsa/plughw:CARD=Headphones', set HOLOCRON_AUDIO_DEVICE"
  echo "    in /etc/systemd/system/holocron-player.service and restart the unit."
fi
