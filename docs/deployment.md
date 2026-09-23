# Deployment and operations guide

How the live intercom is deployed to the Armbian host, how to operate it, and what went
wrong the first time. Last verified 2026-09-21 on `root@192.168.0.17`.

## Target host

| Item | Value |
|---|---|
| Host | `root@192.168.0.17` (Orange Pi PC Plus), SSH key login |
| OS / CPU | Armbian, Debian 13 (trixie), armv7l (32-bit ARM), 1 GB RAM |
| Python | 3.13.5 from apt |
| Audio | Jabra Speak 710 = ALSA card 2, seen by PortAudio as `Jabra Speak 710: USB Audio (hw:2,0)`, 1 in / 2 out, **duplex at 16 kHz only** |
| Sound server | none (no PulseAudio/PipeWire); the app uses ALSA directly |
| Tunnel | `cloudflared` runs as a system service (token file `/etc/cloudflared/token`); its tunnel config is the owner's, not part of this project |

Audio travels as Opus (see `backend/live_intercom/audio/opus.py`), using the system
`libopus0` (1.5.2 on Debian 13), which `INSTALL_APT=1` installs. On this board, encoding and
decoding one call costs about 9 % of one core (complexity 5, measured 2026-09-23). If the
library is missing, the service logs `libopus not found; calls will use raw PCM` at startup
and keeps working at about 10× the bandwidth. The per-call log line reports the codec
(`codec opus`), the `bad packets` count and the jitter buffer's `concealed_frames`.

## What gets installed where

| Path | Contents |
|---|---|
| `/opt/live-com-ha/backend/` | the `live_intercom` Python package (copied from `backend/`) |
| `/opt/live-com-ha/frontend-dist/` | built web page (from `frontend/dist`) |
| `/opt/live-com-ha/venv/` | virtualenv created with `--system-site-packages` |
| `/opt/live-com-ha/deploy/` | copies of the deploy files, for reference |
| `/etc/live-intercom/config.toml` | the live configuration; created once from `deploy/config.example.toml`, never overwritten by a redeploy |
| `/var/lib/live-intercom/users.toml` | users and argon2 password hashes (owner `intercom`, mode 0600) |
| `/var/lib/live-intercom/secret.key` | cookie-signing key, created on first start (mode 0600) |
| `/var/lib/live-intercom/settings.toml` | admin-editable settings: Telegram bot token/chat ID, pickup mode, call-confirm token; created on first Settings-page save (owner `intercom`, mode 0600) |
| `/etc/systemd/system/live-intercom.service` | the service, runs as user `intercom` (group `audio`) on `127.0.0.1:8000` |
| `/etc/systemd/journald.conf.d/live-intercom.conf` | caps the journal at 20 MB (`SystemMaxUse`) so logs can't slowly fill the SD card |

The service binds to loopback only. It is reachable through the Cloudflare tunnel, or from
your machine through an SSH port forward.

## First-time deploy

From the repo root on the dev machine:

```bash
INSTALL_APT=1 deploy/install.sh
```

This builds the frontend locally, copies everything to the host with `tar` over SSH (no
`rsync` needed), then runs `deploy/remote-setup.sh` on the host. `INSTALL_APT=1` installs
the system packages and changes the host, so use it only with the owner's approval.

apt packages installed: `python3-venv python3-numpy python3-cffi python3-argon2 libportaudio2`.

Environment overrides for `install.sh`: `HOST` (default `root@192.168.0.17`), `NPM` (path to
npm), `INSTALL_APT` (`1` to install system packages).

If `npm` is a broken shell shim on the dev machine (nvm), run:

```bash
N=$HOME/.nvm/versions/node/v22.18.0/bin
PATH=$N:$PATH NPM=$N/npm deploy/install.sh
```

The first pip install on the board takes several minutes (slow SD card, pip builds its own
build environment). Later redeploys are quicker but still take a few minutes.

## Redeploy after a code change

```bash
deploy/install.sh        # add NPM=... if npm is broken; INSTALL_APT is not needed again
```

It replaces the code and the built frontend, keeps `config.toml`, `users.toml` and
`secret.key`, and restarts the service. Users stay signed in across a restart.

## Create and manage users

Always run `add-user` as the `intercom` user, never as plain root. A file written by root is
unreadable by the service and every login then fails with HTTP 503.

```bash
ssh -t root@192.168.0.17 'runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom --config /etc/live-intercom/config.toml add-user <name>'
```

It asks for the password twice. Running it again for an existing name changes that password.
No restart is needed; the service reads the file on every login. To remove a user, edit
`/var/lib/live-intercom/users.toml` and delete their line (keep the file owned by
`intercom`, mode 0600).

## Configuration

`/etc/live-intercom/config.toml`, restart with `systemctl restart live-intercom` after edits.

```toml
host = "127.0.0.1"
port = 8000
static_dir = "/opt/live-com-ha/frontend-dist"
settings_file = "/var/lib/live-intercom/settings.toml"
# public_url = "https://intercom.example.com"  # optional: included as a link in the host-initiated-call Telegram message

[audio]
device_match = "Jabra"   # substring of the ALSA device name, case-insensitive
echo_cancel = "off"      # "speex" only if the Jabra's own echo cancellation is not enough
jitter_ms = 60           # raise to 80-100 if audio is choppy over the tunnel

[auth]
users_file = "/var/lib/live-intercom/users.toml"
secret_file = "/var/lib/live-intercom/secret.key"
session_hours = 12
secure_cookie = false    # default; see below (true is safer behind HTTPS)

[session]
idle_timeout_s = 10      # a session with no client audio for this long is ended
ring_timeout_s = 30      # how long a "wait for confirmation" ring waits before timing out
```

### `secure_cookie`

- The default is `false`, chosen by the owner so plain-HTTP testing over the SSH forward
  (`http://localhost:8000`) works without config changes. With `true` the browser drops the
  cookie there: login appears to succeed but every request after it is 401.
- Trade-off: with `false` the cookie has no `Secure` flag, so a browser will also send it
  over plain `http://`. The cookie is the only gate to a live microphone. If you serve the
  app through the Cloudflare tunnel, turn on "Always Use HTTPS" and HSTS for the hostname
  (SSL/TLS, Edge Certificates), or set `secure_cookie = true` and use `false` only briefly
  for local tests.
- `/etc/live-intercom/config.toml` is created from the example only once and never
  overwritten, so changing the example does not change an existing host. Edit the live file
  and run `systemctl restart live-intercom`.

### `settings_file`

- `config.example.toml` now sets `settings_file`, but (like `secure_cookie` above)
  `/etc/live-intercom/config.toml` on an already-deployed host is created once and never
  overwritten by a redeploy. Add `settings_file = "/var/lib/live-intercom/settings.toml"` to
  the live file by hand and restart — otherwise the admin Settings page's Save fails with
  `settings_unavailable`, since `/etc` is read-only to the service (`ProtectSystem=strict`).

### Home Assistant integration

Two endpoints are meant to be called by Home Assistant, not a browser — each takes the
`call_confirm_token` shown on the admin page's "Phone-like calls" section (also shown there
as two ready-to-paste URLs: `Press:`, `Reject:`).

Example `rest_command:` entries for `configuration.yaml`:

```yaml
rest_command:
  intercom_press:
    url: "https://your-tunnel-hostname/api/call/press?token=YOUR_TOKEN"
    method: GET
  intercom_reject:
    url: "https://your-tunnel-hostname/api/call/reject?token=YOUR_TOKEN"
    method: GET
```

- `intercom_press` — one button that does the right thing depending on what's happening: if
  a call is currently ringing (`pickup_mode = "confirm"`), it answers it, same as the old
  `confirm` endpoint (`{"ok": true, "action": "confirmed"}`). If nothing is ringing, it
  instead notifies the configured Telegram group ("someone wants to talk") and lets the next
  person who opens the app connect immediately, skipping ring/confirm even if `pickup_mode`
  is `"confirm"` (`{"ok": ..., "action": "triggered"}`). The bypass stays armed for 5
  minutes; if nobody connects in that window, it simply expires and the next call rings
  normally again.
- `intercom_reject` — call this from an automation while a call is ringing to decline it. A
  stale or duplicate call (nothing currently ringing) returns
  `{"ok": false, "reason": "no_pending_call"}`, not an error — safe to call more than once.
- The token is a shared secret across both routes — rotate it from the admin page's
  "Regenerate" button if it's ever exposed, and update both `rest_command:` entries.

### Changing the USB audio device

Set `device_match` to part of the new device's name. List what the host sees:

```bash
ssh root@192.168.0.17 'runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom list-devices'
```

The audio layer picks a supported sample rate and converts to the fixed 16 kHz wire format.
A device without built-in echo cancellation may need `echo_cancel = "speex"`, which needs
the optional `speexdsp` package (`libspeexdsp-dev` plus a C compiler on the host, since
there is no armv7 wheel). Unplug and replug is handled without restarting the service.

## Testing from your machine (SSH port forward)

```bash
ssh -N -L 8000:127.0.0.1:8000 root@192.168.0.17
```

Then open `http://localhost:8000` in Chrome (`localhost` counts as a secure context, so the
microphone works). This needs `secure_cookie = false`, see above.

## Cloudflare tunnel

Point a public hostname of the existing tunnel at `http://localhost:8000` (in the Cloudflare
dashboard for a token-based tunnel). Prefer `secure_cookie = true` here, or enable "Always Use HTTPS" and HSTS. The app refuses WebSocket
handshakes whose `Origin` host differs from the request `Host` header; cloudflared passes the
original `Host` through by default, so nothing to configure. Failed-login rate limiting keys
on the client IP that uvicorn takes from `X-Forwarded-For` (trusted only from `127.0.0.1`).
Expect more audio delay through the tunnel than over the SSH forward.

## Direct HTTPS with Traefik

Besides the tunnel, the app is served directly at `https://intercom.truongdo.com:8443` by
Traefik (v3, armv7 binary, systemd), with a Let's Encrypt certificate from the DNS-01 challenge
through Cloudflare. The ISP blocks inbound 80 and 443, which rules out the HTTP-01 and
TLS-ALPN-01 challenges (Let's Encrypt only connects on those ports; a TLS-ALPN-01 attempt
failed with `Timeout during connect`), and `truongdo.ruijieddns.com` offers no DNS API. DNS-01
needs no inbound port for issuance or renewal.

    deploy/install-traefik.sh    # HOST, INTERCOM_HOST, ACME_EMAIL, TRAEFIK_VERSION overridable

It copies `deploy/traefik/` to the host and runs `traefik-setup.sh` there, which downloads
and checksum-verifies the pinned release to `/usr/local/bin/traefik`, creates the `traefik`
user, renders `/etc/traefik/traefik.yml` and `dynamic.yml`, and installs `traefik.service`
(binds 80, 443 and 8443 through `CAP_NET_BIND_SERVICE`). The app is served on `http://` (80)
and `https://` (443 and 8443), with no redirect and no HSTS. Over plain HTTP the page and login work,
but browsers only allow the microphone on HTTPS (or `localhost`), so calls need the HTTPS URL.

| Path | Contents |
|---|---|
| `/etc/traefik/cloudflare.env` | `CF_DNS_API_TOKEN=...`, root-only (0600), created by hand; a scoped Cloudflare API token (My Profile → API Tokens → Create Token, "Edit zone DNS" template, zone `truongdo.com`). Not the 37-character Global API Key: Cloudflare rejects that with `6111: Invalid format for Authorization header` |
| `/etc/traefik/traefik.yml` | entry points, ACME resolver `letsencrypt` (overwritten on every run) |
| `/etc/traefik/dynamic.yml` | routers `Host(intercom.truongdo.com)` on 80, 443 and 8443 → `http://127.0.0.1:8000` (overwritten on every run) |
| `/var/lib/traefik/acme.json` | account and certificate keys (owner `traefik`, 0600); renewal is automatic |

The service is not started until `cloudflare.env` exists:

    install -m 0600 /dev/null /etc/traefik/cloudflare.env
    echo 'CF_DNS_API_TOKEN=<token>' > /etc/traefik/cloudflare.env
    systemctl enable --now traefik
    journalctl -u traefik -f          # watch for the certificate being obtained

### Network path

```
browser --https:8443--> intercom.truongdo.com
          CNAME truongdo.ruijieddns.com (DNS-only, grey cloud)
          A 14.248.178.168 (kept current by the Ruijie router's DDNS)
      --> Ruijie router: port forward TCP 8443 -> 192.168.0.17:8443
      --> Traefik :8443 (TLS, Let's Encrypt cert) --> http://127.0.0.1:8000 (the app)
```

| Piece | Setting |
|---|---|
| Cloudflare DNS | `intercom` → `CNAME truongdo.ruijieddns.com`, **DNS-only**, so browsers connect straight to the home IP and see Traefik's certificate |
| Router | port forward / virtual server: external TCP **8443** → `192.168.0.17`, internal port **8443** |
| Public URL | **`https://intercom.truongdo.com:8443`**, the port is required; without it the browser uses 443, which the ISP blocks |
| LAN | `https://intercom.truongdo.com:8443` works too (through the router's NAT loopback); 443 and plain `http://` on 80 also answer on the LAN |

Traefik connects to the app from `127.0.0.1`, so uvicorn trusts its `X-Forwarded-For` for
login rate limiting. The original `Host` (with `:8443`) passes through, and it matches the
browser's `Origin`, so the WebSocket Origin check passes.

### Operations

```bash
systemctl status traefik
journalctl -u traefik -f                 # certificate issuance/renewal shows up here
deploy/install-traefik.sh                # (dev machine) re-render config, upgrade to TRAEFIK_VERSION
```

- Renewal is automatic, about 30 days before expiry, through the same Cloudflare DNS
  challenge. It only needs the token in `cloudflare.env` to stay valid.
- To see the served certificate:
  `echo | openssl s_client -connect 192.168.0.17:8443 -servername intercom.truongdo.com 2>/dev/null | openssl x509 -noout -issuer -enddate`
- For more detail, set `log.level: DEBUG` in `/etc/traefik/traefik.yml` and restart; set it back
  to `INFO` afterwards (the next `install-traefik.sh` run also resets it).
- **Testing from the LAN is not proof of outside reachability.** Connecting to the public IP
  from inside goes through the router's NAT loopback, which skips the ISP. Test the public URL
  from a phone on mobile data.

## Day-to-day commands (on the host)

```bash
systemctl status live-intercom
systemctl restart live-intercom
journalctl -u live-intercom -f          # live log
journalctl -u live-intercom -n 100 --no-pager
journalctl --disk-usage                 # journal size across all services (capped at 20 MB total)
```

Diagnostics that do not need the browser:

```bash
runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom list-devices
runuser -u intercom -- /opt/live-com-ha/venv/bin/python -m live_intercom --config /etc/live-intercom/config.toml loopback
```

`loopback` plays a 440 Hz tone for 3 s on the speaker and records the mic, printing the
sample count (expect 48000) and the level. A speakerphone with hardware echo cancellation
records the tone quietly; that is normal.

Speaker and mic levels are not controlled by the app; use `alsamixer -c 2`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Login returns 503 | `users.toml` is unreadable by `intercom`. `chown intercom:intercom /var/lib/live-intercom/users.toml && chmod 600 ...`; create users only with the `runuser` command above |
| Login "works" but the page returns to the login form | `secure_cookie = true` over plain HTTP. Set `false` for SSH-forward testing |
| Mic button disabled, "Audio device unavailable" | Jabra unplugged, or `device_match` does not match. Run `list-devices`; the page re-checks on reload |
| "In use" | Another client holds the single session. It ends when they press stop, close the tab, or after `idle_timeout_s` without audio |
| Session ends with "The audio device was disconnected" | USB device dropped; check `dmesg`, replug, press the button again |
| WebSocket refused behind a proxy | The proxy changed the `Host` header; make it pass the original `Host` |
| Choppy audio over the tunnel | Raise `jitter_ms` (80-100) and restart |
| Echo or howl | The Jabra's hardware cancellation is normally enough. Otherwise set `echo_cancel = "speex"` (needs the optional package) |
| Browser asks for the mic and nothing happens | Non-HTTPS, non-localhost origin. Use the tunnel URL or the SSH forward |
| Ringing… and nothing happens | `pickup_mode` is `"confirm"` but nothing is calling the `intercom_press`/`intercom_reject` Home Assistant automation. The ring times out after `ring_timeout_s` (default 30s) and the caller can retry |
| `ERR_CONNECTION_REFUSED` on `https://intercom.truongdo.com:8443` | The router has no port forward for 8443 (the router itself refuses). Add TCP 8443 → `192.168.0.17:8443` |
| Timeout on `https://intercom.truongdo.com` (no port) | Port 443 is blocked inbound by the ISP. Use `:8443` |
| Certificate warning, issuer `TRAEFIK DEFAULT CERT` | Traefik has no certificate for the name. Check `journalctl -u traefik` for the ACME error; the router rule must be `Host(intercom.truongdo.com)` and `cloudflare.env` must hold a valid token |
| ACME error `6111: Invalid format for Authorization header` | `cloudflare.env` holds the Global API Key. Use a scoped "Edit zone DNS" API token |
| Host-initiated call notification never arrives | Telegram isn't configured on the admin page (`chat_id`/bot token), or `intercom_press`'s token doesn't match the current `call_confirm_token` — the admin page's `Press:` URL always shows the current one |

## Problems hit during the first deployment (and their fixes)

1. **`python3-sounddevice` does not exist in Debian trixie.** The apt step failed. Fix: apt
   installs `python3-cffi`; `sounddevice` is a pure-Python wheel that pip installs from PyPI
   and it uses the apt `libportaudio2`.
2. **pip tried to compile `argon2-cffi-bindings` and failed** (no C compiler on the host).
   Debian ships `argon2-cffi` 21.1, but the project required 23 or newer, so pip fetched a
   newer one that needs a source build. Fix: the requirement is `argon2-cffi>=21.1` and the
   code falls back from `InvalidHashError` to the 21.1 name `InvalidHash`. General rule for
   this host: **prefer apt packages for anything with native code** (numpy, cffi, argon2),
   and keep the venv on `--system-site-packages`.
3. **Root-owned `users.toml` broke login** (found in review). `remote-setup.sh` now creates
   the file as `intercom`, and login returns a clear 503 if it is unreadable.
4. **macOS `tar` noise.** The dev machine's tar added extended-attribute headers that GNU
   tar on the host warned about. `install.sh` now uses `tar --no-xattrs` when it detects bsdtar.
5. **`npm` shim.** On the dev machine `npm` was a broken zsh nvm function; `install.sh`
   accepts `NPM=/path/to/npm`.

### Problems hit setting up Traefik (2026-09-23)

1. **Cloudflare Global API Key used as the token.** The DNS challenge failed with `6111:
   Invalid format for Authorization header`. Fix: a scoped API token ("Edit zone DNS").
2. **The ISP blocks inbound 80 and 443.** A TLS-ALPN-01 attempt for `truongdo.ruijieddns.com`
   failed with `Timeout during connect (likely firewall problem)`, and HTTP-01 is impossible
   for the same reason. The Ruijie DDNS name has no DNS API, so it cannot get a Let's Encrypt
   certificate at all. Fix: a `truongdo.com` name (DNS-01 through Cloudflare) CNAMEd to the
   DDNS name, served on 8443.
3. **A LAN test said 443 was reachable when it was not.** The request to the public IP went
   through the router's NAT loopback, not the ISP. Test from mobile data.
4. **`ERR_CONNECTION_REFUSED` on 8443.** The router had no forward for 8443 yet. Fix: the port
   forward above.

## Verification status (2026-09-21)

Verified on the host: apt packages and venv installed, systemd service enabled and running,
listening on `127.0.0.1:8000`, the page is served (HTTP 200), `/api/me` is 401 without a
session, `list-devices` sees the Jabra as duplex at 16 kHz, `loopback` captured exactly
48000 samples in 3 s (full duplex works).

Not yet verified (needs a person with a browser): login, the mic button, audio in both
directions, the one-client lock, unplug/replug, echo behaviour, CPU load during a session
(`top` on the host while live), and the Cloudflare route. The checklist is in the plan,
`docs/superpowers/plans/2026-09-21-live-intercom.md`, Task 9, steps 4 to 7.

2026-09-23: Traefik (v3.7.13) serves `https://intercom.truongdo.com:8443` with a Let's
Encrypt certificate (issuer `YE2`, expires 2026-12-22), and the page was reached from outside
the home network.
