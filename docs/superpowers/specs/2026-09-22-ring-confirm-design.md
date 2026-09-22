# Phone-Like Ring/Confirm Flow: Design

Date: 2026-09-22

## Goal

The second of three related sub-projects (see [`newfeat.md`](../../../newfeat.md)
and sub-project 1's spec,
[`2026-09-22-admin-telegram-design.md`](2026-09-22-admin-telegram-design.md)):

1. Admin page + Telegram integration (done, merged).
2. **Phone-like ring/confirm flow** (this spec) — when a remote client taps
   the button, the host rings (audibly, through the Jabra speaker) instead
   of connecting immediately, and waits for a Home Assistant REST call to
   confirm or reject before the call goes live. Configurable per-install
   between "automatic pickup" (today's behavior) and "wait for
   confirmation", via the admin page.
3. Host-initiated proactive call (later spec) — builds on both prior
   sub-projects.

This spec covers only #2. Home Assistant is never proactively notified
that a call is ringing (the ringtone itself is the notification) — HA only
ever calls *in*, to confirm or reject a call already ringing.

## Decisions

| Topic | Decision |
|---|---|
| How the host "rings" | Plays a looping ringtone through the existing full-duplex audio stream, via the speakerphone |
| Does HA get pushed a "ringing" notification | No — ringtone only; HA calls in to confirm/reject, nothing calls out to HA |
| Ring→live audio handoff | One continuous PortAudio stream per call attempt; the frame-source/mic-sink are swapped via indirection the instant confirm arrives — no stream teardown, no re-open latency |
| Confirm/reject auth | A shared-secret token in the URL query string (`?token=...`), not cookie/session auth — HA isn't a logged-in browser |
| Ring timeout location | `[session] ring_timeout_s` in `config.toml` — a deploy-time knob, not admin-page-editable |
| Settings persistence | `pickup_mode`/`call_confirm_token` save independently from the Telegram fields already in `settings.toml`, even though they share the file — so toggling one never requires the other to be valid |

## Architecture

- **`backend/live_intercom/audio/ringtone.py`** (new): `RingtoneSource` —
  precomputes a short dual-tone ring cadence (on/off pattern) as PCM once at
  import, and hands out `FRAME_BYTES`-sized chunks via `.next_frame()`,
  looping forever from wherever it last left off.
- **`backend/live_intercom/protocol.py`**: two new message builders —
  `ringing_message() -> {"type": "ringing"}`,
  `rejected_message(reason) -> {"type": "rejected", "reason": "declined" | "timeout"}`.
- **`backend/live_intercom/session.py`** (the core change):
  - `SessionManager.__init__` gains `pickup_mode_provider: Callable[[], str]`
    (reads the current mode fresh per call, since it's admin-editable at
    runtime — not baked in at process start) and `ring_timeout_s: float`.
  - `_run()`: the device is opened once with indirection callables —
    `pull_box`/`mic_sink_box`, single-item mutable lists — instead of
    passing `jitter.pop`/the real mic handler directly to
    `device.start(...)`. `pull_indirect()` calls `pull_box[0]()`;
    `on_mic_indirect(frame)` calls `mic_sink_box[0](frame)`.
  - The existing `receive()` task (reads `ws.receive()`, pushes binary
    frames into `jitter`, watches for `{"type": "stop"}` or disconnect)
    now starts immediately after the device opens, before ringing even
    begins, and keeps running unchanged straight through into the live
    phase — no new task, no special-casing for "cancel while ringing"
    versus "hang up while live".
  - **`"auto"` mode:** `pull_box`/`mic_sink_box` are set to the real
    handlers immediately — identical to today's behavior.
  - **`"confirm"` mode:** send `ringing_message()`. Create an
    `asyncio.Event` for this call, store it plus a decision slot on
    `self._pending` (only one call can be active at a time — the existing
    `self._active` invariant already guarantees this). Race, via
    `asyncio.wait(FIRST_COMPLETED)`: the confirm event, a
    `ring_timeout_s` sleep, and the `receive()` task (client
    cancels/disconnects).
    - **Confirmed:** swap `pull_box[0]`/`mic_sink_box[0]` to the real
      handlers, send `ready_message()`, proceed into the same live-phase
      task loop as today (reusing the already-running `receive()` task,
      not a new one) — the PortAudio stream never stops, so pickup is
      instant.
    - **Rejected or timed out:** send `rejected_message("declined")` or
      `rejected_message("timeout")`, stop the device, end the session —
      same shape as today's error paths.
    - **Client cancels while ringing:** `receive()` completes first
      (stop message or disconnect) — session ends the same way a live
      call ending does today.
- **`SessionManager.confirm() -> bool` / `.reject() -> bool`**: plain
  methods (same event loop as the HTTP handlers — no `call_soon_threadsafe`
  needed, unlike the PortAudio-thread callbacks). Each checks
  `self._pending is not None`; if so, records the decision, fires the
  event, clears `self._pending`, returns `True`; otherwise returns `False`
  (nothing pending — a late or duplicate call).
- **`backend/live_intercom/web.py`**: two new routes, deliberately
  *outside* the cookie-session auth system:
  - `GET /api/call/confirm?token=...`
  - `GET /api/call/reject?token=...`
  Token checked with `hmac.compare_digest` against
  `Settings.call_confirm_token`. If `call_confirm_token` is unset (empty),
  these always return `401` regardless of the supplied token (guards a
  hand-edited `settings.toml` with no token configured). On a token match:
  call `manager.confirm()`/`.reject()`; `True` → `200 {"ok": true}`;
  `False` → `200 {"ok": false, "reason": "no_pending_call"}` (not an error
  status — a stale/duplicate call from HA is an expected race, not a
  client mistake).
- **`Settings`** (extends sub-project 1's dataclass) gains
  `pickup_mode: str = "auto"` and `call_confirm_token: str = ""`.
  Two new persistence functions, each reading-modifying-writing only its
  own TOML section of the same `settings.toml`:
  - `save_telegram_settings(path, token, chat_id)` — today's
    `save_settings`, renamed/scoped to `[telegram]` only.
  - `save_call_settings(path, pickup_mode)` — validates
    `pickup_mode in ("auto", "confirm")`, writes `[call]` only.
  `call_confirm_token` is generated (`secrets.token_urlsafe(24)`, same
  style as the existing session-signing secret) the first time the admin
  page loads if unset — via a `POST /api/admin/call-token/regenerate`
  endpoint also usable to rotate it later.
- **`web.py`** admin routes: `GET /api/admin/settings` response gains
  `pickup_mode` and `call_confirm_token` (shown **in full** — unlike the
  Telegram bot token, the admin needs to copy this into Home Assistant's
  `rest_command:` config). New `POST /api/admin/call-settings` (body
  `{pickup_mode}`) and `POST /api/admin/call-token/regenerate`.
- **Frontend:**
  - `frontend/src/intercom.ts`: `IntercomState` gains `"ringing"` (new
    status text, e.g. "Ringing…", button disabled — same shape as the
    existing `"connecting"` handling). `onControl` gains a `"ringing"`
    case; the `"rejected"` case maps to the existing `"error"` state with
    reason-specific copy ("Call was declined." / "No answer.") — no new
    visual treatment, the button just re-enables the same way `"error"`
    already does, matching "the remote client can try to make the call
    again" as a manual retry, not an automatic loop.
  - `frontend/src/main.ts`: new "Phone-like calls" section on the admin
    view — a pickup-mode select (Automatic / Wait for confirmation), the
    full `call_confirm_token` with a Regenerate button, and (a convenience,
    computed client-side from `location.origin`) the two exact URLs to
    paste into Home Assistant's `rest_command:` config.

## Data flow

**Auto mode:** unchanged from today.

**Confirm mode:**
1. Remote client taps the button → WS connects → device opens (ringtone
   routed in) → `{"type": "ringing"}` sent → client shows "Ringing…".
2. Someone near the host hears the ring and, via a Home Assistant
   automation/script/voice command, triggers a `rest_command` GET to
   `/api/call/confirm?token=...` or `/api/call/reject?token=...`.
3. **Confirm:** `manager.confirm()` fires the event → session swaps to
   real audio, sends `{"type": "ready", ...}` → client starts audio →
   `"live"`, indistinguishable from auto mode from here on.
4. **Reject:** `manager.reject()` fires the event → session sends
   `{"type": "rejected", "reason": "declined"}` → client shows "Call was
   declined.", button re-enables for a manual retry.
5. **No response within `ring_timeout_s`:** session sends
   `{"type": "rejected", "reason": "timeout"}` → client shows "No
   answer.", same retry pattern.
6. **Caller cancels mid-ring** (taps the button again → sends
   `{"type": "stop"}`): session ends the same way a live call ending does.

## Error handling

- Device-open failure (`device_unavailable`/`device_error`) happens before
  the ring/auto branch — unchanged from today, the caller never hears a
  ring if the device can't open.
- Confirm/reject race safety: `confirm()`/`reject()` both check
  `self._pending is not None` before acting, so a double-tap from HA (or a
  reject arriving just after a confirm already fired) resolves to
  `no_pending_call` rather than double-processing.
- Cancel-vs-confirm race: `asyncio.wait(FIRST_COMPLETED)` picks whichever
  actually happened first; if confirm narrowly wins a race with a
  disconnect, the very next `receive()` iteration catches the disconnect
  and ends the session normally regardless.
- `call_confirm_token` unset → confirm/reject always `401`, regardless of
  the token supplied — never an implicit "empty token matches empty token"
  bypass.
- `manager.confirm()`/`.reject()` called with nothing pending (stale HA
  call, no call currently ringing) → `200 {"ok": false, "reason":
  "no_pending_call"}`, not an error — this is an expected, harmless race
  from HA's perspective, not a client mistake.

## Testing

- `backend/tests/test_ringtone.py` (new): cadence cycles correctly, every
  chunk is exactly `FRAME_BYTES`, wraps around cleanly at the pattern
  boundary.
- `backend/tests/test_protocol.py`: extend for the two new message shapes.
- `backend/tests/test_session.py`: extend significantly — auto mode
  regression-tested unchanged; confirm mode: ringing sent, confirm → ready
  + live audio actually flows (verified via the existing `FakeDevice`'s
  `pull_speaker()`/`emit_mic()`, showing ringtone vs. real routing before
  and after the swap — no changes needed to the `FakeDevice` test double
  itself, it already exposes the callables generically); reject →
  `rejected(declined)`; timeout → `rejected(timeout)`; cancel-while-ringing
  ends cleanly; `confirm()`/`reject()` with nothing pending → `False`.
- `backend/tests/test_web.py`: new `/api/call/confirm`/`/api/call/reject`
  route tests (valid/invalid/missing token, `no_pending_call`, unset-token
  safeguard); `/api/admin/call-settings` save+validation; token-regenerate
  endpoint.
- `backend/tests/test_settings.py`: extend for `pickup_mode`/
  `call_confirm_token` roundtrip + validation, and confirm the sectioned
  save (saving Telegram fields doesn't clobber call fields and vice
  versa).
- Frontend: same convention as sub-project 1 — no DOM test harness exists
  in this project, verified via `npm run build` (strict type check) plus
  manual browser testing.

## Out of scope (deferred to sub-project 3 or beyond)

- Pushing a "ringing" notification to Home Assistant (e.g. a webhook) —
  the ringtone is the only notification, per the decision above.
- The host-initiated proactive-call endpoint and Telegram notification
  (sub-project 3).
- Any admin-page-editable ring timeout (it's a `config.toml` value only).
- Ringtone customization (upload a custom tone, adjustable cadence) —
  a single built-in cadence for now.
