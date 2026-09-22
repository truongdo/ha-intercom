# Admin Page + Telegram Integration: Design

Date: 2026-09-22

## Goal

The first of three related sub-projects (see [`newfeat.md`](../../../newfeat.md)):

1. **Admin page + Telegram integration** (this spec) — persistent, admin-editable
   settings storage and the ability to send a message to a Telegram group.
2. Phone-like ring/confirm flow (later spec) — builds on the settings store here
   for its auto-pickup/confirm toggle, and on Home Assistant REST commands.
3. Host-initiated proactive call (later spec) — builds on the Telegram sending
   added here to notify a group when the host wants to reach someone.

This spec covers only #1: any logged-in user can open a Settings view, enter a
Telegram bot token and group chat ID, save them, and send a test message to
confirm they work.

## Decisions

| Topic | Decision |
|---|---|
| Who can reach the admin page | Any logged-in user — no new role; reuses existing session auth |
| Settings storage | New `settings.toml`, same atomic-write/`0600` pattern as `users.toml` |
| Telegram chat ID discovery | Manual: admin finds and pastes the numeric group chat ID themselves |
| HTTP client for Telegram's Bot API | Stdlib `urllib.request`, off-loop via `asyncio.to_thread` — no new runtime dependency |
| Frontend navigation | A "Settings" link from the main view swaps in an admin form in the same SPA, no router |
| Token round-tripping | The saved token is never sent back to the browser in full — only a masked suffix |

## Architecture

Two new backend modules, following the existing module boundaries in
`backend/live_intercom/`:

- **`settings.py`** — `Settings` dataclass (`telegram_bot_token: str = ""`,
  `telegram_chat_id: str = ""`), `load_settings(path) -> Settings`, and
  `save_settings(path, settings)`. Mirrors `auth.load_users`/`auth.save_user`:
  missing file returns defaults, save is an atomic tmp-file-then-`replace()`
  write with `0600` permissions. `Config` gains `settings_file: Path`
  (default `settings.toml`), resolved the same way as `users_file`.
- **`telegram.py`** — `send_message(token, chat_id, text) -> tuple[bool, str]`.
  Calls `https://api.telegram.org/bot{token}/sendMessage` via
  `urllib.request.urlopen`. Never raises: catches `HTTPError`/`URLError`/JSON
  errors and returns `(False, reason)`. Callers run it via
  `asyncio.to_thread`, the same off-loop pattern `session.py` uses for
  PortAudio calls.

`web.py` additions, gated by the existing `current_user(request) is not None`
check (no new role, per the decision above):

- `GET /api/admin/settings` → `{chat_id, token_masked, token_set}`. The token
  is never returned in full — `token_masked` is `"••••" + token[-4:]` (or
  `""` if unset), `token_set` is a bool.
- `POST /api/admin/settings` → body `{chat_id, token}`. If `token` equals the
  masked placeholder the client was shown, the stored token is left
  unchanged; otherwise it's overwritten with the new value. Validates
  `chat_id` is non-empty and `token` looks like `NNNN:rest` (Telegram bot
  token shape) before writing. Returns `{ok: true}` or `400` with a reason.
- `POST /api/admin/telegram/test` → loads the currently *saved* settings (not
  unsaved form state), calls `send_message` with a fixed test string, returns
  `{ok: true}` or `{ok: false, reason}`.

Frontend (`frontend/src/main.ts`, vanilla TS, no framework/router):

- `showIntercom` gains a "Settings" link next to "Sign out".
- New `showAdmin(username)` view: chat-ID text field, token field (prefilled
  with the masked placeholder when a token is set), Save button, "Send test
  message" button with a status line, and a Back link to `showIntercom`.
  Built with the same `el()` helper already used throughout `main.ts`.
- New `frontend/src/api.ts` functions: `getAdminSettings`, `saveAdminSettings`,
  `sendTelegramTest`, alongside the existing `getMe`/`login`/`logout`.

## Data flow

1. Open Settings → `GET /api/admin/settings` → form fields populate (token
   field shows the masked placeholder, or is empty if unset).
2. Edit chat ID and/or token (leaving the token field as the masked
   placeholder means "keep the existing token") → Save →
   `POST /api/admin/settings` → validated → atomic write to `settings.toml`
   → `{ok: true}` → status line "Saved."
3. "Send test message" → `POST /api/admin/telegram/test` → backend reloads
   `settings.toml`, calls `send_message` off-loop → status line shows "Sent."
   or "Failed: <reason>".

## Error handling

- No `settings.toml` yet → `load_settings` returns defaults (empty strings),
  same as `load_users` returning `{}` — the admin page shows empty fields,
  not an error.
- `POST /api/admin/settings` with empty `chat_id` or a token that doesn't
  match the `NNNN:...` shape → `400 {error: "invalid_chat_id" | "invalid_token"}`.
- `POST /api/admin/telegram/test` with no token/chat ID saved →
  short-circuits to `{ok: false, reason: "not_configured"}` without calling
  Telegram.
- Telegram API error (bad token, bot not a group member, chat not found) →
  `send_message` returns Telegram's `description` field (truncated to a
  reasonable length) as `reason`.
- Network failure/timeout calling Telegram → collapses to
  `{ok: false, reason: "network_error"}`.
- Nothing in `telegram.py` or the admin routes ever raises into the request
  handler — every failure path returns a typed result.

## Testing

- `backend/tests/test_settings.py` (new): load/save roundtrip, missing-file
  defaults, atomic write, `0600` perms — mirrors the existing `save_user`
  coverage in `test_auth.py`.
- `backend/tests/test_telegram.py` (new): `send_message` against a
  monkeypatched `urlopen` — success, Telegram error JSON, network exception.
  No real network calls.
- `backend/tests/test_web.py` (extended): admin routes via Starlette
  `TestClient`, with `telegram.send_message` monkeypatched — auth-required
  (401 when logged out), save/roundtrip including masked-token "keep
  existing" behavior, validation errors, test-send success/failure paths.
- Frontend: no DOM test harness exists today (`framing.test.ts` is
  logic-only, no jsdom configured) — verified via `npm run build` (strict
  type check) and manual testing in the browser, consistent with how the
  project already treats browser-only surfaces.

## Out of scope (deferred to later sub-projects)

- The auto-pickup vs. wait-for-confirmation toggle (sub-project 2 adds this
  to `settings.toml`).
- Any Home Assistant REST-command integration (sub-project 2).
- The host-initiated proactive-call endpoint (sub-project 3), though it will
  reuse `telegram.send_message` added here.
- Telegram chat-ID auto-discovery (`getUpdates`) — deferred; manual entry
  only for now.
