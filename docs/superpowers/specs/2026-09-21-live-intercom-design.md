# Live Intercom: Design

Date: 2026-09-21

## Goal

A web app that gives a browser user live two-way voice with the USB audio
device on a Linux host (`root@192.168.0.17`, Armbian/Debian). Opening the page shows
a microphone button. When activated, the browser mic streams to the host
speaker and the host mic streams back to the browser, both at once.

## Decisions

| Topic | Decision |
|---|---|
| Meaning of "2-way" | Live full-duplex voice intercom |
| Transport | WebSocket with raw PCM frames |
| Stack | Python backend (Starlette) + TypeScript frontend (Vite, no framework) |
| Clients | One active client at a time; a second gets "busy" |
| Auth | Username and password, signed session cookie |
| Echo cancellation | Browser-side always on; server-side optional, off by default |
| TLS | None in the app. Cloudflare Tunnel or SSH port forward supplies HTTPS/localhost |
| Structure | Single Python process that owns the USB audio device |

## Target host (probed read-only)

- Armbian (Debian 13, trixie), armv7l (32-bit ARM), Python 3.13.5.
- USB device: ALSA card 2, Jabra Speak 710 (capture and playback). Card 0
  (H3 codec) and card 1 (HDMI) are ignored.
- No PulseAudio or PipeWire; the app uses ALSA directly.
- Not installed: `python3-venv`, `libportaudio2`, `libspeexdsp`, `pip3`, `uv`.
- Port 8000 is free. `cloudflared` is installed and active.
- The Speak 710 is a speakerphone with hardware echo cancellation, so software
  cancellation may be unnecessary.

## Architecture

One Python process bound to `127.0.0.1:8000`, in three parts.

- **web**: Starlette serves the built frontend and the login endpoints. Users
  come from `users.toml` (argon2 hashes). A CLI command `add-user` writes it.
- **audio**: owns the USB device through one full-duplex ALSA stream. Exposes a
  mic-frame queue (out) and a speaker-frame queue (in). Has a playback jitter
  buffer and an optional echo canceller on the mic path. A `list-devices`
  command prints cards with supported rates and channels.
- **session**: the `/ws` WebSocket. Requires a valid session cookie. A lock
  enforces one active client. On disconnect it stops audio and releases the
  lock in a `finally` block.

Frontend: one page with a login form and a mic button (states: idle,
connecting, live, in use, unavailable). An AudioWorklet captures and frames
mic audio; a second worklet plays incoming frames.

### Device independence

- The device is chosen by name substring (`device_match`, default `"Jabra"`),
  not by ALSA card number. It is re-resolved on each new session, so replugging
  works without a restart.
- The wire format is fixed. At startup the audio layer asks the device for its
  supported rates and channels and resamples or mixes down to the wire format.
- Software echo cancellation (`echo_cancel = "speex"`) is the fallback for
  devices without hardware cancellation.
- Missing device at startup: the service still starts, the UI shows "audio
  device unavailable" and the mic button is disabled. Device lost mid-session:
  the session ends with a clear message; discovery is retried on the next
  connection.
- ALSA mixer levels are managed with `alsamixer`, not by the app.

## Data flow and protocol

Session lifecycle:

1. `POST /login` sets a signed session cookie.
2. The user presses the mic button; the page requests mic permission, then
   opens `/ws` (`ws://` locally, `wss://` through the tunnel).
3. The server checks cookie and lock. If busy it sends `{"type":"busy"}` and
   closes.
4. Otherwise it resolves the device, opens the stream and sends
   `{"type":"ready","rate":16000,"channels":1,"frame_ms":20}`.
5. Audio flows both ways until the button is pressed again, the socket closes,
   or the device fails.

Frames:

- Audio: binary WebSocket messages, one per 20 ms frame. 16 kHz mono int16
  little-endian PCM, 320 samples (640 bytes), no header.
- Control: JSON text messages `ready`, `busy`, `error` (reasons such as
  `device_unavailable`, `device_lost`), and `stop` from the client.

Browser to host: mic worklet resamples to 16 kHz and cuts 20 ms frames with
browser echo cancellation, noise suppression and auto gain on. The server holds
about 60 ms (three frames) in a jitter buffer. Underrun plays silence; overflow
drops the oldest frames so latency stays bounded.

Host to browser: the ALSA capture callback produces frames. With software echo
cancellation on, each frame is cleaned against the frame just played (the
reference). The browser playback worklet keeps about 60 ms and drops frames if
it falls behind.

Backpressure: ALSA callbacks never block on the network; frames pass through
bounded queues. If the WebSocket send side is slow, the oldest mic frames are
dropped.

Timing: both directions share one ALSA full-duplex clock, which keeps the
echo canceller aligned and avoids clock drift. Expected one-way latency on a
LAN is roughly 100 to 150 ms; higher through Cloudflare.

Error handling:

- Auth failure: WebSocket refused before accept (HTTP 403); the page returns to login.
- Mic permission denied: the page shows a message and never opens the socket.
- Network drop: the server stops audio and releases the lock. No automatic
  reconnect, because silently reopening a live microphone is a privacy risk.
- Idle protection: a session with no client frames for 10 seconds is ended.

## Authentication

- `users.toml` maps username to argon2 hash. No plaintext, no sign-up flow.
- `POST /login` sets a signed, `HttpOnly`, `SameSite=Lax` cookie (default 12
  hours). `POST /logout` clears it. Failed logins are rate-limited per IP.
- The signing key is generated on first run and stored in a file readable only
  by the service user, so restarts keep sessions valid.
- The cookie `Secure` flag is configurable: on behind Cloudflare, off for plain
  `http://localhost`. `X-Forwarded-Proto` is trusted only from `127.0.0.1`.
- The login page is public. The mic page and `/ws` require a valid session.

## Configuration (`config.toml`)

- `host = "127.0.0.1"`, `port = 8000`
- `[audio]`: `device_match = "Jabra"`, `echo_cancel = "off"`, `jitter_ms = 60`
- `[auth]`: `users_file`, `session_hours = 12`, `secure_cookie = false`
- `[session]`: `idle_timeout_s = 10`

## Repository layout

```
backend/    Python package (web, audio, session), tests, pyproject.toml
frontend/   Vite + TypeScript app and AudioWorklets
deploy/     systemd unit, install script, example config
docs/       specs and plans
```

## Deployment to root@192.168.0.17

1. Build the frontend locally (the host is a small 32-bit ARM board).
2. `rsync` the backend and built frontend to `/opt/live-com-ha`.
3. Install apt packages: `python3-venv`, `libportaudio2`, and `libspeexdsp1`
   only if software echo cancellation is enabled. This step changes the host
   and needs the user's approval when reached.
4. Create a virtualenv on the host and install backend dependencies. Optional
   `speexdsp` bindings may need compiling on armv7.
5. Install a systemd unit that runs the service as a dedicated non-root user in
   the `audio` group, with `Restart=on-failure`, enabled at boot.
6. The user points `cloudflared` at `http://localhost:8000`; the tunnel
   configuration is not touched.

`deploy/install.sh` makes steps 2 to 5 a repeatable one-command redeploy.

## Testing

- Backend unit tests: jitter buffer, framing, auth flow. The audio layer sits
  behind an interface with a fake in-memory device, so session logic and lock
  behavior are testable without hardware.
- Frontend tests: worklet framing and resampling logic.
- Host checklist: login; one client connects; a second is refused; speech from
  the browser is heard on the Jabra; speech at the Jabra is heard in the
  browser; echo check with software cancellation off and on.
- Optional `loopback` command plays a tone and records it to confirm the device
  works independently of the browser.

## Risks

1. Full-duplex ALSA on the Speak 710: **resolved 2026-09-21.** It is duplex at
   16 kHz only (1 in, 2 out), so no resampling is needed; `loopback` captured
   exactly 3 s of audio. Fallback for other devices is to open them at their
   native rate and resample.
2. CPU on 32-bit ARM: plain 16 kHz mono voice should be light; software echo
   cancellation is the expensive part, hence off by default. Measure on host.
3. Cloudflare tunnel latency: adds delay beyond LAN; buffer sizes are
   configurable and tuned with the user.
4. Browser echo cancellation varies: Chrome and Edge are the supported
   targets; Safari and Firefox may differ. Test Chrome first.
5. Packaging: `speexdsp` bindings may need compiling on armv7 with build tools.
   It is optional, so a failure does not block the core system.

## Decided during implementation

- Exact Python audio and echo-cancellation libraries, chosen for what installs
  on armv7 and Python 3.13.
- Frame size and jitter buffer defaults, tuned on the real hardware.
- Frontend tooling details such as the test runner.

## Out of scope (first version)

- Multiple simultaneous clients, listen-only viewers, AI or speech-to-text.
- Recording or storing audio.
- Roles, user management screens, password reset (`add-user` is enough).
- Driving the Jabra's hardware volume and mute controls.
- Automatic reconnect of a live mic session.
