// RAGAS evaluation block: three metric cards that fill in as results arrive.
// The DOM is built once and updated in place, so bars animate and values fade in per metric.

import { h } from "./dom.js";
import { formatClock, formatDuration, formatScore, plural, scoreBand } from "./format.js";
import { icon } from "./icons.js";

export const METRIC_KEYS = ["faithfulness", "answer_relevancy", "context_precision"];

const FALLBACK_INFO = {
  faithfulness: {
    label: "Faithfulness",
    tooltip: "How many of the statements in the answer are supported by the pages the agent read. Higher means fewer unsupported claims. Judge-model estimate.",
  },
  answer_relevancy: {
    label: "Answer relevancy",
    tooltip: "How directly the answer addresses your question. Low scores flag off-topic or evasive answers. Judge-model estimate.",
  },
  context_precision: {
    label: "Context precision",
    tooltip: "How much of the material the agent read was relevant to the question, with relevant pages ranked first. Judge-model estimate.",
  },
};

const SKIP_REASON = {
  disabled: "evaluation is turned off (EVAL_ENABLED)",
  no_contexts: "no pages were read, so there is nothing to score against",
  no_api_key: "no OpenAI API key is configured",
  empty_answer: "the answer was empty",
  empty_question: "the question was empty",
  interrupted: "the server restarted before the scores were ready",
};

export class EvalBlock {
  /**
   * @param {{metrics: object, open?: boolean, onRerun: () => void}} options  metrics = GET /api/config -> metrics
   */
  constructor({ metrics, onRerun }) {
    this.info = Object.fromEntries(METRIC_KEYS.map((k) => [k, { ...FALLBACK_INFO[k], ...(metrics?.[k] || {}) }]));
    this.onRerun = onRerun;
    this.open = true;
    this.shown = {}; // metric -> value already rendered (to detect new arrivals)
    this.timer = 0;
    this.startedAt = 0;
    this.build();
  }

  build() {
    const id = `eval-${Math.random().toString(36).slice(2, 8)}`;
    this.bodyId = `${id}-body`;
    this.headBtn = h(
      "button",
      { type: "button", class: "block-head", aria: { expanded: "true", controls: this.bodyId }, on: { click: () => this.toggle() } },
      h("span", { class: "block-head__title", text: "Evaluation" }),
      (this.metaEl = h("span", { class: "block-head__meta", text: "RAGAS · 3 metrics" })),
      h("span", { class: "block-head__rule" }),
      h("span", { class: "block-head__chev", html: icon("chevron-up", { size: 16 }) }),
    );
    this.banner = h("div", { class: "eval__banner", hidden: true });
    this.grid = h("div", { class: "eval__grid" });
    this.cards = {};
    for (const key of METRIC_KEYS) {
      const labelId = `${id}-${key}`;
      const card = {
        key,
        el: h("div", { class: "metric is-skeleton" }),
        labelId,
        value: h("div", { class: "metric__value tnum" }),
        band: h("div", { class: "metric__band" }),
        track: h("div", { class: "metric__track" }),
        fill: h("div", { class: "metric__fill" }),
        note: h("div", { class: "metric__note" }),
      };
      card.track.append(card.fill);
      card.el.append(
        h(
          "div",
          { class: "metric__head" },
          h("span", { class: "metric__label", id: labelId, text: this.info[key].label }),
          h("button", {
            type: "button",
            class: "metric__info",
            "data-tip": this.info[key].tooltip,
            "aria-label": `About ${this.info[key].label}`,
            html: icon("info", { size: 14 }),
          }),
        ),
        card.value,
        card.track,
        h("div", { class: "metric__foot" }, card.band, card.note),
      );
      this.cards[key] = card;
      this.grid.append(card.el);
    }
    this.footer = h("div", { class: "eval__footer" });
    this.body = h("div", { class: "eval__body", id: this.bodyId }, this.banner, this.grid, this.footer);
    this.el = h("section", { class: "eval is-open", "aria-label": "Answer evaluation" }, this.headBtn, this.body);
  }

  toggle() {
    this.open = !this.open;
    this.headBtn.setAttribute("aria-expanded", String(this.open));
    this.el.classList.toggle("is-open", this.open);
    this.body.hidden = !this.open;
  }

  /**
   * @param {{evaluation: object|null, running: boolean, startedAt?: number, nContexts?: number}} model
   */
  update({ evaluation, running, startedAt, nContexts }) {
    const ev = evaluation || { status: "pending" };
    const inFlight = running || ev.status === "running" || ev.status === "pending";
    const finished = !inFlight;
    this.el.classList.toggle("is-skipped", ev.status === "skipped");
    this.el.dataset.status = inFlight ? "running" : ev.status;

    this.syncTimer(inFlight, startedAt);
    this.renderBanner(ev, inFlight, nContexts);

    const hideCards = finished && (ev.status === "skipped" || ev.status === "failed");
    this.grid.hidden = hideCards;
    for (const key of METRIC_KEYS) this.renderMetric(this.cards[key], ev, inFlight);

    this.renderFooter(ev, finished);
    this.metaEl.textContent = this.metaText(ev, inFlight);
  }

  metaText(ev, inFlight) {
    if (inFlight) return ev.status === "pending" ? "Evaluation queued" : "Evaluating…";
    if (ev.status === "skipped") return "Not evaluated";
    if (ev.status === "failed") return "Evaluation failed";
    if (ev.status === "partial") return "RAGAS · partial results";
    return "RAGAS · 3 metrics";
  }

  renderBanner(ev, inFlight, nContexts) {
    const b = this.banner;
    b.className = "eval__banner";
    b.hidden = false;
    if (inFlight) {
      const pages = nContexts ? ` over ${plural(nContexts, "page")}` : "";
      b.innerHTML = icon("loader-circle", { size: 14, cls: "spin" });
      b.append(
        h("span", { text: `Evaluating with RAGAS${pages} – this can take up to a minute` }),
        (this.clockEl = h("span", { class: "eval__clock tnum", text: formatClock(this.elapsedSeconds()) })),
      );
    } else if (ev.status === "skipped") {
      b.classList.add("is-info");
      b.innerHTML = icon("info", { size: 14 });
      const why = SKIP_REASON[ev.skipped_reason] || ev.skipped_reason || "no reason given";
      b.append(h("span", { text: `Not evaluated: ${why}.` }));
      if (["disabled", "no_api_key", "interrupted"].includes(ev.skipped_reason)) b.append(this.rerunButton("Run evaluation"));
    } else if (ev.status === "failed") {
      b.classList.add("is-error");
      b.innerHTML = icon("triangle-alert", { size: 14 });
      const reason = Object.values(ev.errors || {})[0];
      b.append(h("span", { text: `Evaluation failed${reason ? `: ${reason}` : "."}` }), this.rerunButton("Retry"));
    } else {
      b.hidden = true;
      b.replaceChildren();
    }
  }

  /** No re-run handler (the read-only demo): an empty node, so callers can append it unconditionally. */
  rerunButton(label) {
    if (!this.onRerun) return document.createTextNode("");
    return h("button", { type: "button", class: "btn btn-sm eval__rerun", text: label, on: { click: () => this.onRerun() } });
  }

  renderMetric(card, ev, inFlight) {
    const value = ev[card.key];
    const error = ev.errors?.[card.key];
    const has = Number.isFinite(value);
    const waiting = inFlight && !has && !error;
    card.el.classList.toggle("is-skeleton", waiting);

    if (waiting) {
      card.value.replaceChildren(h("span", { class: "skeleton metric__skel" }));
      card.band.textContent = "";
      card.note.textContent = "";
      card.fill.style.width = "0%";
      card.track.removeAttribute("role");
      card.track.setAttribute("aria-hidden", "true");
      this.shown[card.key] = null;
      return;
    }

    const arrived = has && this.shown[card.key] === null; // was a skeleton a moment ago
    if (has) {
      const band = scoreBand(value);
      card.value.textContent = formatScore(value);
      card.band.textContent = band.label;
      card.band.className = `metric__band band-${band.key}`;
      card.note.textContent = "";
      card.fill.className = `metric__fill band-${band.key}`;
      card.track.setAttribute("role", "meter");
      card.track.removeAttribute("aria-hidden");
      card.track.setAttribute("aria-labelledby", card.labelId);
      card.track.setAttribute("aria-valuemin", "0");
      card.track.setAttribute("aria-valuemax", "1");
      card.track.setAttribute("aria-valuenow", value.toFixed(2));
      card.track.setAttribute("aria-valuetext", `${formatScore(value)}, ${band.label}`);
      const pct = `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%`;
      if (arrived) {
        card.fill.style.width = "0%";
        requestAnimationFrame(() => requestAnimationFrame(() => (card.fill.style.width = pct)));
        card.el.classList.add("is-new");
        card.el.addEventListener("animationend", () => card.el.classList.remove("is-new"), { once: true });
      } else {
        card.fill.style.width = pct;
      }
      this.shown[card.key] = value;
    } else {
      card.value.replaceChildren(h("span", { class: "metric__na", html: error ? icon("triangle-alert", { size: 18 }) : "" }), "n/a");
      card.band.textContent = "";
      card.band.className = "metric__band";
      card.note.textContent = error || "";
      card.fill.style.width = "0%";
      card.track.removeAttribute("role");
      card.track.setAttribute("aria-hidden", "true");
      this.shown[card.key] = undefined;
    }
  }

  renderFooter(ev, finished) {
    const f = this.footer;
    f.replaceChildren();
    f.hidden = !finished || ev.status === "skipped" || ev.status === "failed";
    if (f.hidden) return;
    const parts = [];
    if (Number.isFinite(ev.latency_s)) parts.push(`Evaluated in ${formatDuration(ev.latency_s * 1000)}`);
    if (ev.judge_model) {
      const scored = ev.n_contexts_scored;
      const input = ev.n_contexts_input;
      let over = "";
      if (scored && input && scored < input) over = ` · scored the first ${scored} of ${plural(input, "page")} read`;
      else if (scored) over = ` over ${plural(scored, "page")}`;
      parts.push(`Judged by ${ev.judge_model}${over}`);
    }
    f.append(h("span", { class: "eval__caption", text: parts.join(" · ") || "Judge-model estimate" }));
    f.append(this.rerunButton(ev.status === "partial" ? "Retry failed" : "Re-run"));
  }

  elapsedSeconds() {
    return this.startedAt ? (Date.now() - this.startedAt) / 1000 : 0;
  }

  syncTimer(inFlight, startedAt) {
    if (inFlight) {
      if (startedAt) this.startedAt = startedAt;
      else if (!this.startedAt) this.startedAt = Date.now();
      if (!this.timer) {
        this.timer = setInterval(() => {
          if (this.clockEl) this.clockEl.textContent = formatClock(this.elapsedSeconds());
        }, 1000);
      }
    } else {
      this.stopTimer();
      this.startedAt = 0;
    }
  }

  stopTimer() {
    clearInterval(this.timer);
    this.timer = 0;
  }

  destroy() {
    this.stopTimer();
  }
}
