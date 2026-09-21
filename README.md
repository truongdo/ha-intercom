# Live Intercom

A web page with a microphone button that gives you live two-way voice with the USB
speakerphone plugged into a Linux host. Press the button and your browser microphone plays
out of the host's speaker, while the host's microphone plays in your browser, both at the
same time.

It runs on an Armbian (Debian 13, 32-bit ARM) board with a Jabra Speak 710, behind a
Cloudflare tunnel or an SSH port forward.

## Features

- Full-duplex voice over one WebSocket: 16 kHz mono 16-bit PCM in 20 ms frames.
- Username and password login (argon2 hashes, signed `HttpOnly` session cookie, rate-limited).
- One active client at a time; anyone else sees "In use".
- Echo control: browser echo cancellation always on; optional server-side canceller
  (`speexdsp`) for devices without hardware cancellation. The Jabra has its own.
- The USB device is chosen by name (`device_match`), so swapping the speakerphone is a
  config change, and unplugging and replugging works without a restart.
- Sessions end on their own if the client stops sending audio, and the mic never
  reconnects silently.

## How it works

```
Browser                                          Host (one Python process, 127.0.0.1:8000)
  mic --> AudioWorklet --> 20 ms frames --+
                                          +-- WebSocket /ws --> jitter buffer --> ALSA --> speaker
  speaker <-- AudioWorklet <-- frames  ---+                        ALSA <-- (optional echo canceller) <-- mic
```

- **Backend** (`backend/live_intercom/`): Starlette and uvicorn. `web.py` serves the page and
  login endpoints, `session.py` bridges the WebSocket to the device and holds the one-client
  lock, `audio/` owns the full-duplex ALSA stream (via `sounddevice`), and `auth.py` handles
  users and sessions.
- **Frontend** (`frontend/`): Vite and TypeScript, no framework. Two AudioWorklets capture
  and play audio; resampling and framing live in `src/framing.ts`.
- The app binds to loopback only. TLS is provided outside it, by the Cloudflare tunnel, or
  `localhost` over an SSH forward (which browsers treat as a secure context, so the mic works).

## Run it locally

Needs Python 3.11 or newer and Node 18 or newer.

```bash
# backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
mkdir -p .dev
.venv/bin/python -m live_intercom --config config.dev.toml add-user dev   # asks for a password
.venv/bin/python -m live_intercom --config config.dev.toml serve          # http://127.0.0.1:8000

# frontend, in another terminal (proxies API calls to the backend)
cd frontend
npm install
npm run dev
```

Other commands: `list-devices` prints the audio devices with their supported rates, and
`loopback` plays a tone through the device and records it back.

## Tests

```bash
cd backend && .venv/bin/pytest -q          # 68 tests
cd frontend && npm test                     # 8 tests
cd frontend && npm run build                # strict type check + production build
```

The audio hardware and the browser audio path can only be tested on the real device.

## Deploy

```bash
INSTALL_APT=1 deploy/install.sh   # first time (installs apt packages on the host)
deploy/install.sh                 # later redeploys
```

This builds the frontend, copies the app to `/opt/live-com-ha` on the host, and installs a
systemd service. Create the first user on the host with `add-user`, run as the `intercom`
user. The exact commands, configuration, Cloudflare setup, troubleshooting and the problems
hit on the first deploy are in [`docs/deployment.md`](docs/deployment.md).

## Configuration

Copy `deploy/config.example.toml`. The main options:

| Option | Default | Meaning |
|---|---|---|
| `host`, `port` | `127.0.0.1`, `8000` | where the service listens |
| `[audio] device_match` | `"Jabra"` | substring of the ALSA device name |
| `[audio] echo_cancel` | `"off"` | `"speex"` enables the server-side canceller |
| `[audio] jitter_ms` | `60` | playback buffer; raise if audio is choppy |
| `[auth] session_hours` | `12` | login lifetime |
| `[auth] secure_cookie` | `false` | add the `Secure` flag; see the deployment guide |
| `[session] idle_timeout_s` | `10` | end a session with no client audio |

## Repository layout

```
backend/    Python package, tests, pyproject.toml, config.dev.toml
frontend/   Vite + TypeScript app and the AudioWorklets
deploy/     install.sh, remote-setup.sh, systemd unit, example config
docs/       deployment guide, design spec, implementation plan
```

- Design: [`docs/superpowers/specs/2026-09-21-live-intercom-design.md`](docs/superpowers/specs/2026-09-21-live-intercom-design.md)
- Plan: [`docs/superpowers/plans/2026-09-21-live-intercom.md`](docs/superpowers/plans/2026-09-21-live-intercom.md)

## Status and limits

- Deployed and serving on the target host. Still to verify by hand in a browser: audio in both
  directions, the one-client lock, echo behaviour, CPU load during a session, and the
  Cloudflare route (checklist in the plan, Task 9).
- Out of scope for now: several simultaneous clients, listen-only viewers, recording, user
  management screens, and controlling the device's volume and mute buttons.
- Audio delay is roughly 100 to 150 ms on a LAN and higher through the tunnel.
