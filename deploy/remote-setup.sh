#!/usr/bin/env bash
# Runs on the host. INSTALL_APT=1 installs system packages (needs the owner's approval).
set -euo pipefail
DEST=/opt/live-com-ha

if [ "${INSTALL_APT:-0}" = "1" ]; then
  apt-get update
  apt-get install -y python3-venv python3-numpy python3-cffi python3-argon2 libportaudio2 libopus0
fi

id intercom >/dev/null 2>&1 || useradd --system --home /var/lib/live-intercom --shell /usr/sbin/nologin -G audio intercom
install -d -o intercom -g intercom -m 0750 /var/lib/live-intercom
# users.toml must be owned by the service user: add-user has to run as `intercom`, never as root.
[ -f /var/lib/live-intercom/users.toml ] || install -o intercom -g intercom -m 0600 /dev/null /var/lib/live-intercom/users.toml
install -d -m 0755 /etc/live-intercom
[ -f /etc/live-intercom/config.toml ] || install -m 0644 "$DEST/deploy/config.example.toml" /etc/live-intercom/config.toml
install -o intercom -g intercom -m 0644 "$DEST/deploy/ringtone.wav" /var/lib/live-intercom/ringtone.wav

install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$DEST/deploy/journald-live-intercom.conf" /etc/systemd/journald.conf.d/live-intercom.conf
systemctl restart systemd-journald

# --system-site-packages picks up the apt-provided numpy, cffi and argon2 (no armv7 wheels to build); sounddevice is a pure-Python wheel from PyPI that uses libportaudio2.
[ -d "$DEST/venv" ] || python3 -m venv --system-site-packages "$DEST/venv"
# setuptools reuses backend/build/ when its files look newer than the source, so a stale or
# damaged copy there (e.g. zeroed by a power cut) would be packaged as-is: always build fresh.
rm -rf "$DEST/backend/build" "$DEST"/backend/*.egg-info
"$DEST/venv/bin/pip" install --upgrade "$DEST/backend"

install -m 0644 "$DEST/deploy/live-intercom.service" /etc/systemd/system/live-intercom.service
systemctl daemon-reload
systemctl enable live-intercom
systemctl restart live-intercom
# Armbian mounts / with commit=120 (writeback), so freshly installed files can sit in RAM for up
# to two minutes; a power cut in that window left every installed .py file empty and the service
# exiting silently at boot. Flush before reporting the deploy as done.
sync
systemctl --no-pager status live-intercom | head -n 12

cat <<MSG

Next: create a login (run as root; it must run as the intercom user so the service can read the file):
  runuser -u intercom -- $DEST/venv/bin/python -m live_intercom --config /etc/live-intercom/config.toml add-user <name>
MSG
