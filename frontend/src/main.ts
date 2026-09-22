import "./style.css";
import { getMe, login, logout, getAdminSettings, saveAdminSettings, sendTelegramTest } from "./api";
import { Intercom, type IntercomState } from "./intercom";

const app = document.getElementById("app")!;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> = {},
  ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function showLogin(message = ""): void {
  const user = el("input", { type: "text", autocomplete: "username", required: true, placeholder: "Username" });
  const pass = el("input", { type: "password", autocomplete: "current-password", required: true, placeholder: "Password" });
  const status = el("p", { className: "status", textContent: message });
  const form = el("form", {}, el("h1", { textContent: "Live Intercom" }), user, pass, el("button", { textContent: "Sign in" }), status);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const result = await login(user.value, pass.value).catch(() => "error" as const);
    if (result === "ok") return void start();
    status.textContent =
      result === "invalid" ? "Wrong username or password." :
      result === "rate_limited" ? "Too many attempts. Try again in a minute." :
      "Sign-in failed.";
  };
  app.replaceChildren(form);
}

const LABELS: Record<IntercomState, string> = {
  idle: "Start talking",
  connecting: "Connecting…",
  live: "Live: tap to stop",
  busy: "In use",
  error: "Start talking",
};

function showIntercom(username: string, audioAvailable: boolean): void {
  const button = el("button", { className: "mic", textContent: LABELS.idle });
  const status = el("p", { className: "status", textContent: audioAvailable ? "" : "Audio device unavailable." });
  const settingsLink = el("button", { className: "link", textContent: "Settings" });
  const signOut = el("button", { className: "link", textContent: `Sign out ${username}` });
  button.disabled = !audioAvailable;

  let running = false;
  const intercom = new Intercom((state, detail) => {
    running = state === "live" || state === "connecting";
    button.textContent = LABELS[state];
    button.dataset.state = state;
    button.disabled = state === "connecting";
    status.textContent = detail ?? (state === "live" ? "You are connected to the room." : "");
  });
  button.onclick = () => {
    if (running) intercom.stop();
    else void intercom.start();
  };
  settingsLink.onclick = () => showAdmin(username);
  signOut.onclick = async () => { intercom.stop(); await logout(); showLogin(); };
  app.replaceChildren(el("h1", { textContent: "Live Intercom" }), button, status, settingsLink, signOut);
}

function showAdmin(username: string): void {
  const chatId = el("input", { type: "text", placeholder: "Chat ID (e.g. -1001234567890)" });
  const token = el("input", { type: "text", placeholder: "Bot token" });
  const status = el("p", { className: "status" });
  const testButton = el("button", { type: "button", textContent: "Send test message" });
  const back = el("button", { type: "button", className: "link", textContent: "Back" });
  const form = el(
    "form",
    {},
    el("h1", { textContent: "Settings" }),
    chatId,
    token,
    el("button", { textContent: "Save" }),
    testButton,
    status,
    back,
  );

  void getAdminSettings()
    .then((settings) => {
      chatId.value = settings.chat_id;
      token.value = settings.token_set ? settings.token_masked : "";
    })
    .catch(() => {
      status.textContent = "Could not load settings.";
    });

  form.onsubmit = async (event) => {
    event.preventDefault();
    status.textContent = "Saving…";
    const result = await saveAdminSettings(chatId.value, token.value).catch(() => "error" as const);
    status.textContent =
      result === "ok" ? "Saved." :
      result === "invalid_chat_id" ? "Chat ID looks wrong." :
      result === "invalid_token" ? "Bot token looks wrong." :
      "Could not save settings.";
  };
  testButton.onclick = async () => {
    status.textContent = "Sending…";
    const result = await sendTelegramTest().catch(() => ({ ok: false, reason: "error" }) as const);
    status.textContent = result.ok ? "Sent." : `Failed: ${result.reason ?? "error"}`;
  };
  back.onclick = () => void start();

  app.replaceChildren(form);
}

async function start(): Promise<void> {
  try {
    const me = await getMe();
    if (me) showIntercom(me.username, me.audio_available);
    else showLogin();
  } catch {
    showLogin("Could not reach the server.");
  }
}

void start();
