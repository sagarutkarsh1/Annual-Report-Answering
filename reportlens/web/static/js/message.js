// Chat message views: the user bubble and the assistant message (steps, markdown answer, sources,
// evaluation, actions). The assistant view updates its parts independently so streaming stays cheap.

import { humanMessage } from "./api.js";
import { copyText, h, patchChildren, rafThrottle } from "./dom.js";
import { compactNumber, formatCost, formatDuration, formatTimestamp } from "./format.js";
import { icon } from "./icons.js";
import { markersToReferences, renderMarkdown, stripMarkers } from "./markdown.js";
import { EvalBlock } from "./scores.js";
import { renderSources } from "./sources.js";
import { renderSteps } from "./steps.js";
import { toast, announce } from "./toast.js";
import { uiState } from "./stream.js";

export function renderUserMessage(msg) {
  return h("article", { class: "msg msg--user", dataset: { mid: msg.id } }, h("div", { class: "bubble", text: msg.content }));
}

const FINAL = new Set(["answered", "no_sources"]);

/**
 * @typedef {object} MessageEnv
 * @property {() => string} docName
 * @property {object} metrics            GET /api/config -> metrics
 * @property {(mid: string, n: number) => boolean} isActiveCite
 * @property {(page: number) => void} onOpenPage
 * @property {(msg: object) => void} onRetry
 * @property {(msg: object) => void} onRerunEval
 */
export class AssistantMessageView {
  /** @param {object} msg @param {MessageEnv} env */
  constructor(msg, env) {
    this.msg = msg;
    this.env = env;
    this.expandedSteps = new Set();
    this.stepsCollapsed = false;
    this.sourcesOpen = true;
    this.sigs = {};
    this.evalBlock = null;
    this.build();
    this.schedule = rafThrottle(() => this.render());
    this.render();
  }

  build() {
    this.stepsEl = h("div", { class: "msg__steps" });
    this.bodyEl = h("div", { class: "md", "aria-live": "off" });
    this.noticeEl = h("div", { class: "msg__notice" });
    this.sourcesEl = h("div", { class: "msg__sources" });
    this.evalEl = h("div", { class: "msg__eval" });
    this.actionsEl = h("div", { class: "msg__actions" });
    this.el = h("article", { class: "msg msg--assistant", dataset: { mid: this.msg.id } }, this.stepsEl, this.bodyEl, this.noticeEl, this.sourcesEl, this.evalEl, this.actionsEl);
  }

  /** Coalesces bursts of token events into one render per frame. */
  refresh() {
    this.schedule();
  }

  flush() {
    this.schedule.cancel();
    this.render();
  }

  destroy() {
    this.schedule.cancel();
    this.evalBlock?.destroy();
  }

  render() {
    const { msg } = this;
    const ui = uiState(msg);
    this.el.dataset.mid = msg.id;
    this.el.classList.toggle("is-streaming", ui.streaming && msg.status === "streaming");
    this.renderSteps(ui);
    this.renderBody(ui);
    this.renderNotice(ui);
    const final = FINAL.has(msg.status);
    this.renderSources(final);
    this.renderEval(ui, final);
    this.renderActions(final);
  }

  renderSteps(ui) {
    const sig = JSON.stringify([this.msg.steps, ui.streaming, this.stepsCollapsed, [...this.expandedSteps], this.env.docName()]);
    if (sig === this.sigs.steps) return;
    this.sigs.steps = sig;
    this.stepsEl.replaceChildren(
      renderSteps(this.msg.steps, {
        docName: this.env.docName(),
        streaming: ui.streaming,
        expanded: this.expandedSteps,
        collapsed: this.stepsCollapsed,
        onToggleRow: (id) => {
          if (!this.expandedSteps.delete(id)) this.expandedSteps.add(id);
          this.render();
        },
        onToggleAll: () => {
          this.stepsCollapsed = !this.stepsCollapsed;
          this.render();
        },
        onOpenPage: (page) => this.env.onOpenPage(page),
      }),
    );
  }

  renderBody(ui) {
    const { msg } = this;
    const streaming = ui.streaming && msg.status === "streaming";
    if (streaming && !msg.content && !msg.steps.length) {
      if (this.sigs.body !== "skeleton") {
        this.sigs.body = "skeleton";
        this.bodyEl.replaceChildren(h("div", { class: "answer-skeleton", "aria-hidden": "true" }, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }), h("div", { class: "skeleton" })));
      }
      return;
    }
    this.sigs.body = "md";
    const fragment = renderMarkdown(msg.content, {
      messageId: msg.id,
      citations: msg.citations,
      streaming,
      isActive: this.env.isActiveCite,
    });
    patchChildren(this.bodyEl, fragment);
  }

  renderNotice(ui) {
    const { msg } = this;
    const sig = JSON.stringify([msg.status, msg.error, msg.content.trim() === "", msg.stopped]);
    if (sig === this.sigs.notice) return;
    this.sigs.notice = sig;
    const notes = [];
    if (msg.status === "no_sources") {
      notes.push(
        notice("warn", "triangle-alert", "No passages from the report were cited. Treat this answer with caution: it may not be grounded in the document."),
      );
    } else if (msg.status === "error") {
      const stopped = msg.stopped || msg.error === "cancelled";
      const text = stopped ? "Stopped before the answer was finished." : humanMessage({ code: msg.error, message: msg.error }) || "Something went wrong while answering.";
      notes.push(notice(stopped ? "info" : "error", stopped ? "circle-alert" : "triangle-alert", text, h("button", { type: "button", class: "btn btn-sm", text: "Retry", on: { click: () => this.env.onRetry(msg) } })));
    } else if (FINAL.has(msg.status) && msg.content.trim() === "") {
      notes.push(notice("warn", "triangle-alert", "The model returned an empty answer.", h("button", { type: "button", class: "btn btn-sm", text: "Retry", on: { click: () => this.env.onRetry(msg) } })));
    }
    this.noticeEl.replaceChildren(...notes);
  }

  renderSources(final) {
    const { msg } = this;
    const sig = JSON.stringify([final, msg.sources, msg.citations.length, this.sourcesOpen, this.env.docName()]);
    if (sig === this.sigs.sources) return;
    this.sigs.sources = sig;
    const el = final
      ? renderSources(msg, {
          docName: this.env.docName(),
          open: this.sourcesOpen,
          onToggle: () => {
            this.sourcesOpen = !this.sourcesOpen;
            this.render();
          },
          onOpenPage: (page) => this.env.onOpenPage(page),
        })
      : null;
    this.sourcesEl.replaceChildren(...(el ? [el] : []));
  }

  renderEval(ui, final) {
    const { msg } = this;
    if (!final || !msg.evaluation) {
      this.evalEl.hidden = true;
      return;
    }
    this.evalEl.hidden = false;
    if (!this.evalBlock) {
      this.evalBlock = new EvalBlock({ metrics: this.env.metrics, onRerun: this.env.readOnly?.() ? null : () => this.env.onRerunEval(msg) });
      this.evalEl.append(this.evalBlock.el);
    }
    this.evalBlock.update({ evaluation: msg.evaluation, running: ui.eval.running, startedAt: ui.eval.startedAt, nContexts: ui.eval.nContexts });
  }

  renderActions(final) {
    const { msg } = this;
    const show = final && msg.content.trim() !== "";
    this.actionsEl.hidden = !show;
    if (!show) return;
    const usage = msg.usage;
    const sig = JSON.stringify([msg.created_at, usage, msg.elapsed_ms]);
    if (sig === this.sigs.actions) return;
    this.sigs.actions = sig;

    const meta = [];
    if (Number.isFinite(msg.elapsed_ms)) meta.push(`Answered in ${formatDuration(msg.elapsed_ms)}`);
    let usageTip = "";
    if (usage && (usage.input_tokens || usage.output_tokens)) {
      meta.push(`${compactNumber(usage.input_tokens)} in · ${compactNumber(usage.output_tokens)} out`);
      const cost = formatCost(usage.cost_usd);
      if (cost) meta.push(cost);
      usageTip = [
        usage.model && `Model ${usage.model}`,
        `Input ${usage.input_tokens.toLocaleString()} tokens${usage.cached_tokens ? ` (${usage.cached_tokens.toLocaleString()} cached)` : ""}`,
        `Output ${usage.output_tokens.toLocaleString()} tokens${usage.reasoning_tokens ? ` (${usage.reasoning_tokens.toLocaleString()} reasoning)` : ""}`,
        cost ? "Cost is an estimate from list prices" : "No price known for this model",
      ]
        .filter(Boolean)
        .join(" · ");
    }

    this.actionsEl.replaceChildren(
      this.copyButton("Copy answer", "copy", () => stripMarkers(msg.content)),
      this.copyButton("Copy as Markdown", "file-code", () => markersToReferences(msg.content, msg.citations)),
      h(
        "div",
        { class: "msg__meta" },
        meta.length ? h("span", { class: "msg__usage tnum", text: meta.join(" · "), "data-tip": usageTip || null, tabindex: usageTip ? "0" : null }) : null,
        h("time", { class: "msg__time tnum", datetime: msg.created_at, text: formatTimestamp(msg.created_at) }),
      ),
    );
  }

  copyButton(label, iconName, getText) {
    const btn = h("button", { type: "button", class: "icon-btn", "aria-label": label, "data-tip": label, html: icon(iconName) });
    btn.addEventListener("click", async () => {
      const ok = await copyText(getText());
      if (!ok) return toast("Copy failed. Select the text and copy it manually.", { kind: "error" });
      btn.innerHTML = icon("check");
      announce("Copied");
      setTimeout(() => (btn.innerHTML = icon(iconName)), 1500);
    });
    return btn;
  }
}

function notice(kind, iconName, text, action) {
  return h("div", { class: `notice notice--${kind}` }, h("span", { class: "notice__icon", html: icon(iconName, { size: 16 }) }), h("span", { class: "notice__text", text }), action || null);
}
