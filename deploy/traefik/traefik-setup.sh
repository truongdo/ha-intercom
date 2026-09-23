#!/usr/bin/env bash
# Runs on the host (via deploy/install-traefik.sh). Installs Traefik as an HTTPS front for the app.
set -euo pipefail
SRC=/opt/live-com-ha/deploy/traefik
VERSION="${TRAEFIK_VERSION:?}"
INTERCOM_HOST="${INTERCOM_HOST:?}"
ACME_EMAIL="${ACME_EMAIL:-}"

if [ "$(/usr/local/bin/traefik version 2>/dev/null | awk '/^Version:/ {print $2}')" != "${VERSION#v}" ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  base="https://github.com/traefik/traefik/releases/download/$VERSION"
  tarball="traefik_${VERSION}_linux_armv7.tar.gz"
  curl -fsSL -o "$tmp/$tarball" "$base/$tarball"
  curl -fsSL -o "$tmp/checksums.txt" "$base/traefik_${VERSION}_checksums.txt"
  (cd "$tmp" && grep " $tarball\$" checksums.txt | sha256sum -c -)
  tar -xzf "$tmp/$tarball" -C "$tmp" traefik
  install -m 0755 "$tmp/traefik" /usr/local/bin/traefik
fi

id traefik >/dev/null 2>&1 || useradd --system --home /var/lib/traefik --shell /usr/sbin/nologin traefik
# acme.json holds the account and certificate keys: Traefik refuses it unless mode 0600.
install -d -o traefik -g traefik -m 0700 /var/lib/traefik
[ -f /var/lib/traefik/acme.json ] || install -o traefik -g traefik -m 0600 /dev/null /var/lib/traefik/acme.json

install -d -m 0755 /etc/traefik
sed "s|__ACME_EMAIL__|$ACME_EMAIL|" "$SRC/traefik.yml" > /etc/traefik/traefik.yml.new
mv /etc/traefik/traefik.yml.new /etc/traefik/traefik.yml
sed "s|__INTERCOM_HOST__|$INTERCOM_HOST|" "$SRC/dynamic.yml" > /etc/traefik/dynamic.yml.new
mv /etc/traefik/dynamic.yml.new /etc/traefik/dynamic.yml
chmod 0644 /etc/traefik/traefik.yml /etc/traefik/dynamic.yml

install -m 0644 "$SRC/traefik.service" /etc/systemd/system/traefik.service
systemctl daemon-reload
# Flush before starting: / is mounted with commit=120 (see remote-setup.sh).
sync

if ! grep -qs '^CF_DNS_API_TOKEN=.\+' /etc/traefik/cloudflare.env; then
  cat <<MSG
Traefik is installed but not started: /etc/traefik/cloudflare.env is missing the token.
Create a scoped Cloudflare API token ("Edit zone DNS" template, not the Global API Key) for the
zone of $INTERCOM_HOST, then on the host:
  install -m 0600 /dev/null /etc/traefik/cloudflare.env
  echo 'CF_DNS_API_TOKEN=<token>' > /etc/traefik/cloudflare.env
  systemctl enable --now traefik
MSG
  exit 0
fi
chmod 0600 /etc/traefik/cloudflare.env
systemctl enable traefik
systemctl restart traefik
systemctl --no-pager status traefik | head -n 12
