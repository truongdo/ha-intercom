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
