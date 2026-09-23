#!/usr/bin/env bash
# Runs on the dev machine. Usage: HOST=root@192.168.0.17 [INTERCOM_HOST=...] [ACME_EMAIL=...] deploy/install-traefik.sh
set -euo pipefail
HOST="${HOST:-root@192.168.0.17}"
INTERCOM_HOST="${INTERCOM_HOST:-intercom.truongdo.com}"
ACME_EMAIL="${ACME_EMAIL:-}"
TRAEFIK_VERSION="${TRAEFIK_VERSION:-v3.7.13}"
DEST=/opt/live-com-ha/deploy/traefik
cd "$(dirname "$0")/traefik"

TAR_CREATE=(tar)
if tar --version 2>/dev/null | grep -qi bsdtar; then TAR_CREATE=(tar --no-xattrs); fi

ssh "$HOST" "rm -rf $DEST && mkdir -p $DEST"
"${TAR_CREATE[@]}" -czf - traefik.yml dynamic.yml traefik.service traefik-setup.sh | ssh "$HOST" "tar -xzf - -C $DEST"
ssh "$HOST" "TRAEFIK_VERSION=$TRAEFIK_VERSION INTERCOM_HOST=$INTERCOM_HOST ACME_EMAIL='$ACME_EMAIL' bash $DEST/traefik-setup.sh"
