import "./style.css";
import { getMe, login, logout } from "./api";
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
  signOut.onclick = async () => { intercom.stop(); await logout(); showLogin(); };
  app.replaceChildren(el("h1", { textContent: "Live Intercom" }), button, status, signOut);
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
