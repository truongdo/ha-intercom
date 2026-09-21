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
| `/etc/systemd/system/live-intercom.service` | the service, runs as user `intercom` (group `audio`) on `127.0.0.1:8000` |

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

## Day-to-day commands (on the host)

```bash
systemctl status live-intercom
systemctl restart live-intercom
journalctl -u live-intercom -f          # live log
journalctl -u live-intercom -n 100 --no-pager
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

## Verification status (2026-09-21)

Verified on the host: apt packages and venv installed, systemd service enabled and running,
listening on `127.0.0.1:8000`, the page is served (HTTP 200), `/api/me` is 401 without a
session, `list-devices` sees the Jabra as duplex at 16 kHz, `loopback` captured exactly
48000 samples in 3 s (full duplex works).

Not yet verified (needs a person with a browser): login, the mic button, audio in both
directions, the one-client lock, unplug/replug, echo behaviour, CPU load during a session
(`top` on the host while live), and the Cloudflare route. The checklist is in the plan,
`docs/superpowers/plans/2026-09-21-live-intercom.md`, Task 9, steps 4 to 7.
