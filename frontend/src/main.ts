import "./style.css";
import {
  getMe,
  login,
  logout,
  getAdminSettings,
  saveAdminSettings,
  sendTelegramTest,
  saveCallSettings,
  regenerateCallToken,
  type PickupMode,
} from "./api";
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

const ICON_PATHS: Record<string, string> = {
  gear: `<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2.5 12h3M18.5 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>`,
  signOut: `<path d="M15 4H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h8"/><path d="M11 12h9"/><path d="M17 8l3 4-3 4"/>`,
  mic: `<path d="M12 15a3.5 3.5 0 0 0 3.5-3.5V6.5A3.5 3.5 0 0 0 8.5 6.5v5A3.5 3.5 0 0 0 12 15Z"/><path d="M6 11v.5a6 6 0 0 0 12 0V11"/><path d="M12 17.5V21"/>`,
  micOff: `<path d="M12 15a3.5 3.5 0 0 0 3.5-3.5V6.5A3.5 3.5 0 0 0 8.5 6.5v1.6"/><path d="M6 11v.5a6 6 0 0 0 9.5 4.9"/><path d="M12 17.5V21"/><path d="M4 4l16 16"/>`,
  back: `<path d="M15 5l-7 7 7 7"/>`,
};

function icon(name: keyof typeof ICON_PATHS): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.innerHTML = ICON_PATHS[name];
  return svg;
}

function iconButton(name: keyof typeof ICON_PATHS, label: string, extraClass = ""): HTMLButtonElement {
  return el("button", { type: "button", className: `icon-btn ${extraClass}`.trim(), title: label, ariaLabel: label }, icon(name));
}

function showLogin(message = ""): void {
  const user = el("input", { type: "text", autocomplete: "username", required: true, placeholder: "Username" });
  const pass = el("input", { type: "password", autocomplete: "current-password", required: true, placeholder: "Password" });
  const status = el("p", { className: "status", textContent: message });
  const form = el("form", {}, user, pass, el("button", { type: "submit", textContent: "Sign in" }));
  const card = el(
    "div",
    { className: "auth-card" },
    el("p", { className: "wordmark", textContent: "Live Intercom" }),
    el("p", { className: "subtitle", textContent: "Sign in to connect." }),
    form,
    status,
  );
  form.onsubmit = async (event) => {
    event.preventDefault();
    const result = await login(user.value, pass.value).catch(() => "error" as const);
    if (result === "ok") return void start();
    status.textContent =
      result === "invalid" ? "Wrong username or password." :
      result === "rate_limited" ? "Too many attempts. Try again in a minute." :
      "Sign-in failed.";
  };
  app.replaceChildren(el("div", { className: "shell" }, card));
}

const LABELS: Record<IntercomState, string> = {
  idle: "Start talking",
  connecting: "Connecting…",
  ringing: "Ringing…",
  live: "Live: tap to stop",
  busy: "In use",
  error: "Start talking",
};

function showIntercom(username: string, audioAvailable: boolean): void {
  const button = el("button", { className: "call-button", textContent: LABELS.idle });
  const muteButton = iconButton("mic", "Mute microphone");
  const status = el("p", { className: "status", textContent: audioAvailable ? "" : "Audio device unavailable." });
  const settingsButton = iconButton("gear", "Settings");
  const signOutButton = iconButton("signOut", `Sign out ${username}`);
  button.disabled = !audioAvailable;
  const callActions = el("div", { className: "call-actions" });

  let running = false;
  let muted = false;
  const intercom = new Intercom((state, detail) => {
    running = state === "live" || state === "connecting" || state === "ringing";
    button.textContent = LABELS[state];
    button.dataset.state = state;
    button.disabled = state === "connecting";
    status.textContent = detail ?? (state === "live" ? "You are connected to the room." : "");
    callActions.replaceChildren(...(running ? [muteButton] : []));
    settingsButton.disabled = running;
    signOutButton.disabled = running;
    if (!running) {
      muted = false;
      muteButton.replaceChildren(icon("mic"));
      muteButton.title = muteButton.ariaLabel = "Mute microphone";
      muteButton.classList.remove("muted-on");
    }
  });
  button.onclick = () => {
    if (running) intercom.stop();
    else void intercom.start();
  };
  muteButton.onclick = () => {
    muted = !muted;
    intercom.setMuted(muted);
    muteButton.replaceChildren(icon(muted ? "micOff" : "mic"));
    muteButton.title = muteButton.ariaLabel = muted ? "Unmute microphone" : "Mute microphone";
    muteButton.classList.toggle("muted-on", muted);
  };
  settingsButton.onclick = () => { intercom.stop(); showAdmin(username); };
  signOutButton.onclick = async () => { intercom.stop(); await logout(); showLogin(); };

  const topbar = el(
    "header",
    { className: "topbar" },
    el("span", { className: "wordmark", textContent: "Live Intercom" }),
    el("div", { className: "topbar-actions" }, settingsButton, signOutButton),
  );
  const callPanel = el("main", { className: "call-panel" }, button, callActions, status);
  app.replaceChildren(el("div", { className: "shell" }, topbar, callPanel));

  if (audioAvailable && new URLSearchParams(location.search).has("go")) {
    void intercom.start();
  }
}

function showAdmin(username: string): void {
  const chatId = el("input", { type: "text", placeholder: "Chat ID (e.g. -1001234567890)" });
  const token = el("input", { type: "text", placeholder: "Bot token" });
  const status = el("p", { className: "status" });
  const testButton = el("button", { type: "button", className: "secondary", textContent: "Send test message" });
  const telegramForm = el(
    "form",
    {},
    chatId,
    token,
    el("button", { type: "submit", textContent: "Save" }),
    testButton,
    status,
  );
  const telegramPanel = el("section", { className: "panel" }, el("h2", { textContent: "Telegram" }), telegramForm);

  const pickupMode = el(
    "select",
    {},
    el("option", { value: "auto", textContent: "Automatic pickup" }),
    el("option", { value: "confirm", textContent: "Wait for confirmation" }),
  );
  const callStatus = el("p", { className: "status" });
  const tokenDisplay = el("input", { type: "text", className: "code-field", readOnly: true });
  const regenButton = el("button", { type: "button", className: "secondary", textContent: "Regenerate" });
  const pressUrl = el("p", { className: "status code-field" });
  const rejectUrl = el("p", { className: "status code-field" });
  const callForm = el(
    "form",
    {},
    pickupMode,
    el("button", { type: "submit", textContent: "Save" }),
    tokenDisplay,
    regenButton,
    pressUrl,
    rejectUrl,
    callStatus,
  );
  const callPanel = el("section", { className: "panel" }, el("h2", { textContent: "Phone-like calls" }), callForm);

  const showToken = (callConfirmToken: string): void => {
    tokenDisplay.value = callConfirmToken;
    pressUrl.textContent = `Press: ${location.origin}/api/call/press?token=${callConfirmToken}`;
    rejectUrl.textContent = `Reject: ${location.origin}/api/call/reject?token=${callConfirmToken}`;
  };

  void getAdminSettings()
    .then((settings) => {
      chatId.value = settings.chat_id;
      token.value = settings.token_set ? settings.token_masked : "";
      pickupMode.value = settings.pickup_mode;
      showToken(settings.call_confirm_token);
    })
    .catch(() => {
      status.textContent = "Could not load settings.";
    });

  telegramForm.onsubmit = async (event) => {
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

  callForm.onsubmit = async (event) => {
    event.preventDefault();
    callStatus.textContent = "Saving…";
    const result = await saveCallSettings(pickupMode.value as PickupMode).catch(() => "error" as const);
    callStatus.textContent =
      result === "ok" ? "Saved." :
      result === "invalid_pickup_mode" ? "Invalid mode." :
      "Could not save.";
  };
  regenButton.onclick = async () => {
    callStatus.textContent = "Regenerating…";
    const newToken = await regenerateCallToken().catch(() => null);
    if (newToken) {
      showToken(newToken);
      callStatus.textContent = "Regenerated.";
    } else {
      callStatus.textContent = "Could not regenerate.";
    }
  };

  const backButton = iconButton("back", "Back");
  backButton.onclick = () => void start();
  const topbar = el(
    "header",
    { className: "topbar" },
    el("div", { className: "topbar-actions" }, backButton),
    el("span", { className: "wordmark", textContent: "Settings" }),
    el("div", { className: "topbar-actions" }),
  );
  app.replaceChildren(el("div", { className: "shell" }, topbar, el("div", { className: "panels" }, telegramPanel, callPanel)));
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
