// "Questions to run on this report": the editable question set offered right after an upload, and the slim progress bar
// shown while the set is being answered (POST /api/sessions/{sid}/batch, streamed by chat.js).
// The list is edited in place (a textarea per row), kept per browser in localStorage, and limited to `max_batch_questions`.

import { h } from "./dom.js";
import { icon } from "./icons.js";
import { MAX_QUESTION_LENGTH } from "./composer.js";

export const BUILT_IN_QUESTIONS = [
  "What is the status of GHG reduction technology available to the company?",
  "Has the company undertaken or announced / earmarked capex to meet its transition plans in the next 5 years?",
  "To the best of your knowledge, how prepared is the company for acute and chronic physical risk events through adaptation and resiliency measures on its business?",
  "What are the primary sources of operating cash flows?",
  "What is the management outlook?",
];
export const DEFAULT_MAX_QUESTIONS = 10;
const STORAGE_KEY = "rl.questionset.v1"; // one list for the whole browser, not per chat

const keyOf = (q) => q.trim().replace(/\s+/g, " ").toLowerCase();

/** Trims, drops empty / over-long / duplicate (case-insensitive) entries, and keeps at most `max`. */
export function cleanQuestions(list, max = Infinity) {
  const out = [];
  const seen = new Set();
  for (const raw of Array.isArray(list) ? list : []) {
    if (typeof raw !== "string") continue;
    const q = raw.trim();
    if (!q || q.length > MAX_QUESTION_LENGTH || seen.has(keyOf(q))) continue;
    seen.add(keyOf(q));
    out.push(q);
    if (out.length >= max) break;
  }
  return out;
}

/** The owner's question set from GET /api/config (`default_questions`; older servers call it `question_set`), else the built-in five. */
export function configDefaults(cfg) {
  const max = configMax(cfg);
  const fromServer = cleanQuestions(cfg?.default_questions ?? cfg?.question_set, max);
  return fromServer.length ? fromServer : cleanQuestions(BUILT_IN_QUESTIONS, max);
}

export function configMax(cfg) {
  const n = Math.floor(Number(cfg?.max_batch_questions));
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_MAX_QUESTIONS;
}

function readStored() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeStored(list, defaults) {
  try {
    if (JSON.stringify(list) === JSON.stringify(defaults)) window.localStorage.removeItem(STORAGE_KEY); // untouched: keep following the server's set
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable: the edits simply are not remembered */
  }
}

const grow = (ta) => {
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight}px`;
};

export class QuestionSet {
  /**
   * @param {{defaults: string[], max: number, onRun: (questions: string[]) => void}} opts
   */
  constructor({ defaults, max, onRun }) {
    this.max = Math.max(1, max || DEFAULT_MAX_QUESTIONS);
    this.defaults = cleanQuestions(defaults, this.max);
    this.onRun = onRun;
    const stored = readStored();
    this.items = stored ? cleanQuestions(stored, this.max) : this.defaults.slice();
    this.busy = false;
    this.blocked = "";
    this.rendering = false;
    this.build();
    this.renderList();
    this.renderState();
    this.ro = new ResizeObserver(() => {
      const w = this.el.clientWidth;
      if (w === this.lastWidth) return;
      this.lastWidth = w;
      requestAnimationFrame(() => this.list.querySelectorAll("textarea").forEach(grow));
    });
    this.ro.observe(this.el);
  }

  build() {
    this.title = h("h2", { class: "qset__title", id: "qset-title", text: "Questions to run on this report" });
    this.count = h("span", { class: "qset__count tnum", role: "status", "aria-live": "polite" });
    this.list = h("ol", { class: "qset__list", "aria-labelledby": "qset-title" });
    this.addInput = h("input", {
      type: "text",
      class: "qset__add-input",
      id: "qset-add",
      placeholder: "Add a question",
      "aria-label": "Add a question",
      maxlength: String(MAX_QUESTION_LENGTH),
      autocomplete: "off",
      on: {
        keydown: (e) => {
          if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.isComposing) {
            e.preventDefault();
            this.add();
          }
        },
        input: () => this.setError(""),
      },
    });
    this.addBtn = h("button", { type: "button", class: "btn btn-sm qset__add-btn", on: { click: () => this.add() }, html: `${icon("plus", { size: 14 })}<span>Add</span>` });
    this.error = h("p", { class: "qset__error", role: "alert", hidden: true });
    this.resetBtn = h("button", { type: "button", class: "btn btn-sm btn-ghost qset__reset", text: "Reset to defaults", on: { click: () => this.reset() } });
    this.runBtn = h("button", { type: "button", class: "btn btn-primary qset__run", on: { click: () => this.run() } });
    this.blockedEl = h("p", { class: "qset__blocked", hidden: true });
    this.el = h(
      "section",
      { class: "qset", "aria-labelledby": "qset-title", on: { keydown: (e) => this.onKey(e) } },
      h("header", { class: "qset__head" }, this.title, this.count),
      this.list,
      h("div", { class: "qset__add" }, this.addInput, this.addBtn),
      this.error,
      h("div", { class: "qset__actions" }, this.resetBtn, h("span", { class: "qset__spacer" }), this.runBtn),
      this.blockedEl,
      h("p", { class: "qset__note", text: "Answered in parallel. Each question is independent (no follow-up context). You can still type your own question below." }),
    );
  }

  // ----------------------------------------------------------------------------------------------- state
  setBusy(busy) {
    this.busy = Boolean(busy);
    this.renderState();
  }

  /** `reason` non-empty: the questions are paused (usage budget used up, own key missing) and this says why. */
  setBlocked(reason) {
    this.blocked = reason || "";
    this.renderState();
  }

  get atMax() {
    return this.items.length >= this.max;
  }

  renderState() {
    const n = this.items.length;
    const off = this.busy;
    this.count.replaceChildren(h("span", { text: `${n} ${n === 1 ? "question" : "questions"}` }), this.atMax ? h("span", { class: "qset__max is-full", text: " · max reached" }) : h("span", { class: "qset__max", text: ` · max ${this.max}` }));
    this.runBtn.disabled = off || n === 0 || Boolean(this.blocked);
    this.runBtn.replaceChildren(
      ...(off ? [h("span", { html: icon("loader-circle", { size: 14, cls: "spin" }) }), h("span", { text: "Starting..." })] : [h("span", { text: n === 1 ? "Run 1 question" : `Run all ${n} questions` })]),
    );
    this.addInput.disabled = off || this.atMax;
    this.addBtn.disabled = off || this.atMax;
    this.addInput.placeholder = this.atMax ? `Maximum of ${this.max} questions reached` : "Add a question";
    this.resetBtn.disabled = off;
    this.list.querySelectorAll("textarea").forEach((ta) => (ta.readOnly = off));
    this.list.querySelectorAll(".qset__remove").forEach((b) => (b.disabled = off));
    this.blockedEl.hidden = !this.blocked;
    this.blockedEl.textContent = this.blocked;
    this.el.setAttribute("aria-busy", String(off));
  }

  setError(text) {
    this.error.hidden = !text;
    this.error.textContent = text;
  }

  persist() {
    writeStored(this.items, this.defaults);
  }

  // ----------------------------------------------------------------------------------------------- list
  renderList(focusIndex = -1) {
    this.rendering = true;
    const rows = this.items.map((q, i) => this.row(q, i));
    this.list.replaceChildren(...rows);
    this.rendering = false;
    this.list.querySelectorAll("textarea").forEach(grow);
    if (focusIndex >= 0) this.list.querySelectorAll("textarea")[focusIndex]?.focus();
    this.renderState();
  }

  row(q, i) {
    const ta = h("textarea", { class: "qset__text", rows: "1", maxlength: String(MAX_QUESTION_LENGTH), "aria-label": `Question ${i + 1}`, spellcheck: "true" });
    ta.value = q;
    ta.addEventListener("input", () => {
      grow(ta);
      this.setError("");
    });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.isComposing) {
        e.preventDefault();
        this.commit(i, ta);
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        ta.value = this.items[i];
        grow(ta);
        this.setError("");
        ta.blur();
      }
    });
    ta.addEventListener("blur", () => !this.rendering && this.commit(i, ta));
    const remove = h("button", { type: "button", class: "icon-btn qset__remove", "aria-label": `Remove question ${i + 1}`, title: "Remove", html: icon("x", { size: 14 }), on: { click: () => this.remove(i) } });
    return h("li", { class: "qset__row" }, h("span", { class: "qset__num tnum", "aria-hidden": "true", text: String(i + 1) }), ta, remove);
  }

  /** Saves the edit of row `i`; an empty or duplicate text is refused and the row goes back to its saved text. */
  commit(i, ta) {
    if (this.items[i] === undefined) return true;
    const value = ta.value.trim();
    if (value === this.items[i]) {
      ta.value = value;
      return true;
    }
    if (!value) {
      ta.value = this.items[i];
      grow(ta);
      this.setError("A question cannot be empty. Use the remove button to delete it.");
      return false;
    }
    if (this.items.some((q, j) => j !== i && keyOf(q) === keyOf(value))) {
      ta.value = this.items[i];
      grow(ta);
      this.setError("That question is already in the list.");
      return false;
    }
    this.items[i] = value;
    ta.value = value;
    grow(ta);
    this.setError("");
    this.persist();
    return true;
  }

  add() {
    const value = this.addInput.value.trim();
    if (!value) return true;
    if (this.atMax) {
      this.setError(`You can run at most ${this.max} questions at once.`);
      return false;
    }
    if (this.items.some((q) => keyOf(q) === keyOf(value))) {
      this.setError("That question is already in the list.");
      return false;
    }
    this.items.push(value);
    this.addInput.value = "";
    this.setError("");
    this.persist();
    this.renderList();
    (this.addInput.disabled ? this.runBtn : this.addInput).focus(); // the box turns off at the limit: do not drop the focus
    return true;
  }

  remove(i) {
    this.items.splice(i, 1);
    this.setError("");
    this.persist();
    const next = this.items.length ? Math.min(i, this.items.length - 1) : -1;
    this.renderList(next);
    if (next < 0) this.addInput.focus();
  }

  reset() {
    this.items = this.defaults.slice();
    this.setError("");
    this.persist();
    this.renderList();
    this.resetBtn.focus();
  }

  onKey(e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) {
      e.preventDefault();
      this.run();
    }
  }

  run() {
    if (this.busy || this.blocked) return;
    // Finish whatever is half-typed first: the row being edited, and a question waiting in the add box.
    const active = document.activeElement;
    if (active instanceof HTMLTextAreaElement && this.list.contains(active)) {
      const i = Array.from(this.list.querySelectorAll("textarea")).indexOf(active);
      if (i >= 0 && !this.commit(i, active)) return;
    }
    if (this.addInput.value.trim() && !this.add()) return;
    if (!this.items.length) return;
    this.onRun(this.items.slice());
  }

  focus() {
    (this.list.querySelector("textarea") || this.addInput).focus({ preventScroll: true });
  }

  destroy() {
    this.ro.disconnect();
    this.el.remove();
  }
}

// ----------------------------------------------------------------------------------------------- progress bar
/** The slim bar at the top of the chat while a question set is answered; afterwards it shows the result for a few seconds. */
export class BatchBar {
  constructor() {
    this.label = h("span", { class: "batchbar__label tnum" });
    this.fill = h("span", { class: "batchbar__fill" });
    this.track = h("div", { class: "batchbar__track", role: "progressbar", "aria-label": "Question set progress", "aria-valuemin": "0" }, this.fill);
    this.el = h("div", { class: "batchbar", hidden: true }, this.label, this.track);
    this.timer = 0;
  }

  /** @param {{label: string, value: number, max: number, tone?: "run"|"ok"|"warn", autoHide?: boolean}} s */
  show({ label, value, max, tone = "run", autoHide = false }) {
    clearTimeout(this.timer);
    this.el.hidden = false;
    this.el.dataset.tone = tone;
    this.label.textContent = label;
    this.fill.style.width = `${max > 0 ? Math.round((Math.min(value, max) / max) * 100) : 0}%`;
    this.track.setAttribute("aria-valuemax", String(max));
    this.track.setAttribute("aria-valuenow", String(Math.min(value, max)));
    this.track.setAttribute("aria-valuetext", label);
    if (autoHide) this.timer = setTimeout(() => this.hide(), 8000);
  }

  hide() {
    clearTimeout(this.timer);
    this.el.hidden = true;
  }
}
