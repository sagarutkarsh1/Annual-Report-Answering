// Toasts (aria-live) and the screen-reader status region.

import { humanMessage } from "./api.js";
import { h } from "./dom.js";
import { icon } from "./icons.js";

const MAX_TOASTS = 3;
let host = null;
let status = null;

export function initToasts() {
  host = document.getElementById("toasts");
  status = document.getElementById("sr-status");
}

/** Polite announcement for screen readers ("Answer ready, 3 references."). */
export function announce(text) {
  if (!status) return;
  status.textContent = "";
  // Re-set on the next tick so repeating the same text is announced again.
  setTimeout(() => {
    status.textContent = text;
  }, 30);
}

export function toast(message, { kind = "info", timeout = 5000 } = {}) {
  if (!host) return;
  while (host.children.length >= MAX_TOASTS) host.firstElementChild.remove();
  const el = h(
    "div",
    { class: `toast toast--${kind}`, role: kind === "error" ? "alert" : "status" },
    h("div", { class: "toast__msg", text: message }),
    h("button", { type: "button", class: "icon-btn toast__close", aria: { label: "Dismiss" }, html: icon("x") }),
  );
  const dismiss = () => {
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 160);
  };
  el.querySelector("button").addEventListener("click", dismiss);
  host.append(el);
  if (timeout) setTimeout(dismiss, timeout);
}

/** Toast with the human wording for an ApiError / SSE error payload. */
export function showError(err) {
  if (err?.name === "AbortError") return;
  toast(humanMessage(err), { kind: "error", timeout: 7000 });
}
