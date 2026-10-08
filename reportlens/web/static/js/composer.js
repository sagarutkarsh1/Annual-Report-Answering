// Message composer: auto-growing textarea, document pill (lock state), send / stop button.

import { h } from "./dom.js";
import { truncName } from "./format.js";
import { icon } from "./icons.js";

export const MAX_QUESTION_LENGTH = 4000;
const MAX_HEIGHT = 200;

const PLACEHOLDER = {
  empty: "Upload a document to start asking questions",
  indexing: "Indexing your document...",
  failed: "Upload a different PDF to start asking questions",
  ready: "Ask a question about this report...",
  locked: "Ask a question about this report...",
};

const LOCK_TIP = {
  ready: "Locked after your first question",
  locked: "This chat is bound to this document. Start a new chat to use a different one.",
};

export class Composer {
  /** @param {{onSend: (text: string) => void, onStop: () => void, onAttach: () => void, onOpenDocument: () => void}} handlers */
  constructor(handlers) {
    this.handlers = handlers;
    this.mode = "empty";
    this.streaming = false;
    this.filename = "";
    this.blocked = ""; // non-empty: questions are paused and this is why (usage budget used up)
    this.build();
    this.apply();
  }

  build() {
    this.input = h("textarea", {
      class: "composer__input",
      rows: "1",
      maxlength: String(MAX_QUESTION_LENGTH),
      "aria-label": "Ask a question",
      id: "composer-input",
      on: {
        input: () => this.onInput(),
        keydown: (e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            this.submit();
          } else if (e.key === "Escape") this.input.blur();
        },
      },
    });
    this.pill = h("button", { type: "button", class: "doc-pill", on: { click: () => this.onPill() } });
    this.counter = h("span", { class: "composer__count tnum", hidden: true, "aria-live": "off" });
    this.send = h("button", { type: "submit", class: "send on-accent" });
    this.el = h(
      "form",
      { class: "composer", autocomplete: "off", on: { submit: (e) => (e.preventDefault(), this.submit()) } },
      this.input,
      h("div", { class: "composer__row" }, this.pill, h("span", { class: "composer__spacer" }), this.counter, this.send),
    );
    this.el.addEventListener("click", (e) => {
      if (e.target === this.el) this.input.focus();
    });
  }

  /** @param {{mode: string, filename?: string, streaming?: boolean, blocked?: string}} next */
  setState({ mode, filename = "", streaming = false, blocked = "" }) {
    this.mode = mode;
    this.filename = filename;
    this.streaming = streaming;
    this.blocked = blocked;
    this.apply();
  }

  /** The chat has a ready document (the pill can open it, reading is always allowed). */
  get enabled() {
    return this.mode === "ready" || this.mode === "locked";
  }

  /** Questions may be typed and sent. */
  get canAsk() {
    return this.enabled && !this.blocked;
  }

  apply() {
    const canAsk = this.canAsk;
    this.input.disabled = !canAsk;
    this.input.placeholder = this.blocked && this.enabled ? this.blocked : PLACEHOLDER[this.mode] || PLACEHOLDER.empty;
    this.el.classList.toggle("is-disabled", !canAsk);
    this.renderPill();
    this.renderSend();
  }

  renderPill() {
    const { mode, filename } = this;
    const pill = this.pill;
    pill.className = `doc-pill doc-pill--${mode}`;
    pill.removeAttribute("data-tip");
    if (mode === "empty" || mode === "failed") {
      pill.disabled = false;
      pill.innerHTML = `${icon("plus", { size: 14 })}<span>Attach PDF</span>`;
      pill.setAttribute("aria-label", "Attach a PDF document");
    } else if (mode === "indexing") {
      pill.disabled = true;
      pill.innerHTML = `${icon("loader-circle", { size: 14, cls: "spin" })}<span class="doc-pill__name"></span>`;
      pill.querySelector(".doc-pill__name").textContent = truncName(filename, 28);
      pill.setAttribute("aria-label", `Indexing ${filename}`);
    } else {
      pill.disabled = false;
      pill.innerHTML = `${icon(mode === "locked" ? "lock" : "lock-open", { size: 14 })}<span class="doc-pill__name"></span>`;
      pill.querySelector(".doc-pill__name").textContent = truncName(filename, 28);
      pill.setAttribute("data-tip", LOCK_TIP[mode]);
      pill.setAttribute("aria-label", `Document ${filename}. ${LOCK_TIP[mode]} Opens the document.`);
    }
  }

  renderSend() {
    const send = this.send;
    if (this.streaming) {
      send.classList.add("is-stop");
      send.classList.remove("is-disabled");
      send.removeAttribute("aria-disabled");
      send.innerHTML = icon("square", { size: 14, cls: "fill-current" });
      send.setAttribute("aria-label", "Stop generating");
      send.setAttribute("data-tip", "Stop generating");
    } else {
      const canSend = this.canAsk && this.input.value.trim() !== "";
      send.classList.remove("is-stop");
      send.classList.toggle("is-disabled", !canSend);
      send.setAttribute("aria-disabled", String(!canSend));
      send.innerHTML = icon("arrow-up", { size: 18, cls: "stroke-2" });
      send.setAttribute("aria-label", "Send message");
      send.removeAttribute("data-tip");
    }
  }

  onInput() {
    this.input.style.height = "auto";
    this.input.style.height = `${Math.min(this.input.scrollHeight, MAX_HEIGHT)}px`;
    this.input.style.overflowY = this.input.scrollHeight > MAX_HEIGHT ? "auto" : "hidden";
    const n = this.input.value.length;
    this.counter.hidden = n < MAX_QUESTION_LENGTH * 0.9;
    this.counter.textContent = `${n.toLocaleString()} / ${MAX_QUESTION_LENGTH.toLocaleString()}`;
    this.counter.classList.toggle("is-limit", n >= MAX_QUESTION_LENGTH);
    this.renderSend();
  }

  onPill() {
    if (this.mode === "empty" || this.mode === "failed") this.handlers.onAttach();
    else if (this.enabled) this.handlers.onOpenDocument();
  }

  submit() {
    if (this.streaming) return this.handlers.onStop();
    const text = this.input.value.trim();
    if (!this.canAsk || !text) return;
    this.setValue("");
    this.handlers.onSend(text);
  }

  setValue(text) {
    this.input.value = text;
    this.onInput();
  }

  /** Puts a rejected question back so the user does not have to retype it (never overwrites new typing). */
  restore(text) {
    if (!this.input.value) this.setValue(text);
  }

  focus() {
    if (this.canAsk) this.input.focus();
  }
}
