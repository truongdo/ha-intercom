#!/usr/bin/env bash
# Runs on the dev machine. Usage: HOST=root@192.168.0.17 [INSTALL_APT=1] deploy/install.sh
set -euo pipefail
HOST="${HOST:-root@192.168.0.17}"
NPM="${NPM:-npm}"
DEST=/opt/live-com-ha
cd "$(dirname "$0")/.."

# macOS bsdtar adds extended-attribute headers that GNU tar on the host warns about.
TAR_CREATE=(tar)
if tar --version 2>/dev/null | grep -qi bsdtar; then TAR_CREATE=(tar --no-xattrs); fi

(cd frontend && "$NPM" ci && "$NPM" run build)

ssh "$HOST" "mkdir -p $DEST/backend $DEST/frontend-dist $DEST/deploy && rm -rf $DEST/backend/live_intercom $DEST/frontend-dist/*"
"${TAR_CREATE[@]}" -C backend -czf - live_intercom pyproject.toml | ssh "$HOST" "tar -xzf - -C $DEST/backend"
"${TAR_CREATE[@]}" -C frontend/dist -czf - . | ssh "$HOST" "tar -xzf - -C $DEST/frontend-dist"
"${TAR_CREATE[@]}" -C deploy -czf - config.example.toml live-intercom.service remote-setup.sh | ssh "$HOST" "tar -xzf - -C $DEST/deploy"
ssh "$HOST" "INSTALL_APT=${INSTALL_APT:-0} bash $DEST/deploy/remote-setup.sh"
