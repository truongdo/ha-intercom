# Host-Initiated Proactive Call: Design

Date: 2026-09-22

## Goal

The third of three related sub-projects (see [`newfeat.md`](../../../newfeat.md)),
building on sub-project 1's spec
([`2026-09-22-admin-telegram-design.md`](2026-09-22-admin-telegram-design.md))
and sub-project 2's spec
([`2026-09-22-ring-confirm-design.md`](2026-09-22-ring-confirm-design.md)),
both done and merged:

1. Admin page + Telegram integration.
2. Phone-like ring/confirm flow.
3. **Host-initiated proactive call** (this spec) — since the host has no
   interface of its own, Home Assistant hits a new endpoint to signal
   "someone should call in"; the host notifies the configured Telegram
   group; whoever opens the app and taps the button connects immediately,
   skipping the ring/confirm flow even if `pickup_mode` is `"confirm"`.

## Decisions

| Topic | Decision |
|---|---|
| Trigger endpoint auth | Reuses `call_confirm_token` from sub-project 2 — one token for all HA-facing intercom actions |
| Skipping confirmation for the resulting call | An in-memory, time-limited "next call skips confirmation" flag on `SessionManager`, not a temporary change to the persisted `pickup_mode` setting |
| Bypass window | 5 minutes, hardcoded (not a new config.toml knob — declined in favor of a fixed sensible default) |
| Telegram message link | An optional `public_url` in `config.toml`; message includes a link if set, otherwise a plain text notification |
| Frontend changes | None — the resulting call is just a normal connection that happens to skip ring/confirm, identical to `pickup_mode: "auto"` from the browser's perspective |

## Architecture

- **`backend/live_intercom/config.py`**: `Config` gains a top-level
  `public_url: str = ""` (alongside `host`/`port`/`static_dir` — a plain
  string, not a path, so no `resolve()` logic needed).
- **`backend/live_intercom/session.py`**:
  - `SessionManager` gains `trigger_bypass() -> None` — records a
    timestamped "bypass" flag (using an injectable clock, matching the
    existing `RateLimiter`/`SessionSigner` pattern elsewhere in this
    codebase, for testability).
  - Inside `_run()`, where `pickup_mode` is currently read via
    `self._pickup_mode_provider()`, it now also checks the bypass flag:
    if set and still within the 5-minute window, this call is treated as
    `"auto"` regardless of the configured `pickup_mode`, and the flag is
    cleared (one-shot — consumed by whichever call connects next,
    matching or not). A stale flag (older than 5 minutes) is ignored and
    cleared lazily on read, with no background timer needed.
  - If a call is already active when the trigger fires, the flag is set
    anyway and just waits for the next session — the existing
    one-active-session lock already serializes this naturally.
- **`backend/live_intercom/web.py`**: one new route,
  `GET /api/call/trigger?token=...`, gated by the exact same
  `hmac.compare_digest`-over-bytes check already used by
  `/api/call/confirm`/`reject` (the fix from sub-project 2's final
  review), checked against `call_confirm_token`. On a valid token: calls
  `manager.trigger_bypass()`, then sends a Telegram message via the
  existing `telegram.send_message(token, chat_id, text)` off-loop.
- **No frontend changes.** The browser never knows or cares that a call
  skipped confirmation — it's the same WS message sequence as auto mode.

## Data flow

1. HA hits `GET /api/call/trigger?token=...`.
2. Token validated → `manager.trigger_bypass()` records the flag →
   `telegram.send_message(...)` sends
   `"📞 Someone wants to talk — open the intercom to answer: {public_url}"`
   (if `public_url` is configured) or
   `"📞 Someone wants to talk — open the intercom to answer."` (if not) to
   the configured Telegram group.
3. Response mirrors `/api/admin/telegram/test`'s shape: `{"ok": true}` on
   success; `{"ok": false, "reason": "not_configured"}` if Telegram isn't
   set up; `{"ok": false, "reason": "<telegram error>"}` if the send
   itself fails.
4. Someone taps the Telegram link (or already has the app open) → logs
   in as normal → taps the intercom button → WS connects →
   `SessionManager._run()` sees the fresh bypass flag → treats the call
   as `"auto"` regardless of `pickup_mode` → clears the flag → call goes
   straight live, identical to today's auto-mode flow from that point on.

## Error handling

- Trigger endpoint auth failure (bad/missing/unset token) → `401`, same
  pattern as confirm/reject.
- `trigger_bypass()` always succeeds — it only records an in-memory flag.
  The only failure mode on this endpoint is the Telegram send itself,
  which `telegram.send_message` already guarantees never raises.
- The bypass flag is purely time-based and lazily cleared wherever it's
  read — no background cleanup task.
- A call already in progress when the trigger fires doesn't block or
  fail the trigger; the flag just waits for the next session.

## Testing

- `backend/tests/test_config.py`: `public_url` default (empty string)
  and override.
- `backend/tests/test_session.py`: `trigger_bypass()` makes the next
  call go `"auto"` even when `pickup_mode_provider` returns `"confirm"`;
  the flag is one-shot (a second call afterward does not bypass); the
  flag expires after the 5-minute window using an injectable clock.
- `backend/tests/test_web.py`: `/api/call/trigger` auth (valid/invalid/
  missing/unset token); a successful trigger sends the correct message
  text, both with and without `public_url` configured; a Telegram
  failure surfaces the right reason; unconfigured Telegram →
  `not_configured`.
- No frontend tests (no frontend changes in this sub-project).

## Out of scope

- A separate token for the trigger endpoint (reuses `call_confirm_token`
  per the decision above).
- Any admin-page UI for `public_url` (it's a `config.toml` value only,
  like `ring_timeout_s`).
- Rate-limiting the trigger endpoint (same trust boundary as
  confirm/reject — HA is fully trusted, and the token itself is the
  access control).
- Revisiting the token-transport design (query string vs. header) or
  adding a per-ring nonce — both were deferred from sub-project 2's
  final review as follow-ups, not required here, and this sub-project
  doesn't introduce a new instance of either concern (the trigger
  endpoint's token usage is identical to confirm/reject's).
