export interface Me {
  username: string;
  audio_available: boolean;
}

export async function getMe(): Promise<Me | null> {
  const response = await fetch("api/me");
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`status check failed (${response.status})`);
  return response.json();
}

export type LoginResult = "ok" | "invalid" | "rate_limited" | "error";

export async function login(username: string, password: string): Promise<LoginResult> {
  const response = await fetch("login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (response.ok) return "ok";
  if (response.status === 401) return "invalid";
  if (response.status === 429) return "rate_limited";
  return "error";
}

export async function logout(): Promise<void> {
  await fetch("logout", { method: "POST" });
}

export interface AdminSettings {
  chat_id: string;
  token_masked: string;
  token_set: boolean;
}

export async function getAdminSettings(): Promise<AdminSettings> {
  const response = await fetch("api/admin/settings");
  if (!response.ok) throw new Error(`status check failed (${response.status})`);
  return response.json();
}

export type SaveSettingsResult = "ok" | "invalid_chat_id" | "invalid_token" | "error";

export async function saveAdminSettings(chatId: string, token: string): Promise<SaveSettingsResult> {
  const response = await fetch("api/admin/settings", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, token }),
  });
  if (response.ok) return "ok";
  const body = await response.json().catch(() => ({}));
  return body.error === "invalid_chat_id" || body.error === "invalid_token" ? body.error : "error";
}

export interface TelegramTestResult {
  ok: boolean;
  reason?: string;
}

export async function sendTelegramTest(): Promise<TelegramTestResult> {
  const response = await fetch("api/admin/telegram/test", { method: "POST" });
  if (!response.ok) return { ok: false, reason: "error" };
  return response.json();
}
