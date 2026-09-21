# Deploying live-intercom

## Deploy

Run `deploy/install.sh` from the repo root. It builds the frontend, copies the project to
`/opt/live-com-ha` on the host and runs `deploy/remote-setup.sh` there. That script:

- creates the `intercom` system user (in group `audio`) and `/var/lib/live-intercom`,
- creates an empty `/var/lib/live-intercom/users.toml` owned by `intercom` (mode 0600),
- installs `/etc/live-intercom/config.toml` from `config.example.toml` if it is missing,
- installs the venv and the systemd unit, then restarts the service.

`INSTALL_APT=1` additionally installs the system packages (needs the owner's approval).

## Add a user

Always run `add-user` as the `intercom` user, never as root. A file created by root is
unreadable by the service and every login would fail with HTTP 503.

    runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom \
        --config /etc/live-intercom/config.toml add-user <name>

Re-running it for an existing name changes that user's password. The service reads the file
on every login, so no restart is needed.

## Configuration

`/etc/live-intercom/config.toml` (template: `deploy/config.example.toml`). Restart with
`systemctl restart live-intercom` after editing.

- `[audio]` `device_match` is a substring of the ALSA device name, `jitter_ms` sizes the server
  jitter buffer, `echo_cancel = "off" | "speex"` (`speex` needs the optional speexdsp package).
- `[auth]` `session_hours`, `users_file`, `secret_file`, `secure_cookie`.
- `[session]` `idle_timeout_s` ends a session that receives no client audio.

### `secure_cookie`

- Behind Cloudflare Tunnel (HTTPS at the edge): keep `secure_cookie = true`.
- Testing over plain HTTP, for example `ssh -L 8000:127.0.0.1:8000 host` and opening
  `http://localhost:8000`: set `secure_cookie = false`, otherwise the browser drops the
  session cookie and login appears to succeed but `/api/me` stays 401. Set it back to `true`
  for production.

The WebSocket refuses handshakes whose `Origin` host differs from the request `Host`, so the
tunnel must pass the original `Host` header through (cloudflared does by default).
