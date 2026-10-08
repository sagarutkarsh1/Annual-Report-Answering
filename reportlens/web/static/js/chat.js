// The main pane: header, upload / indexing / failed cards, the conversation, composer and scrolling.
// Streams are owned by this module and keep running when the user switches to another chat.

import { api, humanMessage, uploadDocument } from "./api.js";
import { Composer } from "./composer.js";
import { tableDialog } from "./dialog.js";
import { copyText, h, prefersReducedMotion } from "./dom.js";
import { formatDuration, plural } from "./format.js";
import { icon, logoMark } from "./icons.js";
import { renderUserMessage, AssistantMessageView } from "./message.js";
import { markersToReferences, tableToTsv } from "./markdown.js";
import { openCitation, openPage } from "./panel.js";
import { postSSE } from "./sse.js";
import { state } from "./state.js";
import { applyStreamEvent, finalizeStream, newAssistantMessage, newUserMessage, uiState } from "./stream.js";
import { announce, showError, toast } from "./toast.js";
import { IndexingCard, UploadCard, failedHeading, validateFile } from "./upload.js";
import { METRIC_KEYS } from "./scores.js";
import { EXHAUSTED_BANNER, EXHAUSTED_COMPOSER, LOW_BUDGET_BANNER, usageLevel } from "./usage.js";

const HINTS = ["Summarise the key financial highlights", "What are the principal risks?", "What was operating cash flow?"];
const POLL_MS = 3000;
const POLL_GIVE_UP_MS = 180000;
const STICK_PX = 80;
const FINAL = new Set(["answered", "no_sources"]);
const EVAL_OPEN = new Set(["pending", "running"]);

/** Keeps the view pinned to the bottom while streaming, unless the user scrolled up. */
class AutoScroll {
  constructor(scrollEl, fab) {
    this.scrollEl = scrollEl;
    this.fab = fab;
    this.stick = true;
    scrollEl.addEventListener("scroll", () => this.onScroll(), { passive: true });
    fab.addEventListener("click", () => this.toBottom(true));
  }

  distance() {
    const el = this.scrollEl;
    return el.scrollHeight - el.scrollTop - el.clientHeight;
  }

  onScroll() {
    const d = this.distance();
    this.stick = d < STICK_PX;
    this.fab.hidden = d < STICK_PX * 1.5;
  }

  /** Called after content grew. */
  follow() {
    if (this.stick) this.scrollEl.scrollTop = this.scrollEl.scrollHeight;
    else this.onScroll();
  }

  toBottom(smooth = false) {
    this.stick = true;
    this.scrollEl.scrollTo({ top: this.scrollEl.scrollHeight, behavior: smooth && !prefersReducedMotion() ? "smooth" : "auto" });
    this.fab.hidden = true;
  }
}

export class ChatPane {
  /**
   * @param {HTMLElement} root  <main id="chat-pane">
   * @param {{onSessionPatch: (sid: string, patch: object) => void, onSessionUpdated: (session: object) => void,
   *          onRunFinished: (sid: string) => void, onCancelIndexing: (sid: string) => void, onOpenSidebar: () => void,
   *          onSessionGone: (sid: string) => void}} hooks
   */
  constructor(root, hooks) {
    this.root = root;
    this.hooks = hooks;
    this.detail = null;
    this.group = null; // "empty" | "failed" | "indexing" | "chat"
    this.views = new Map(); // message object -> view
    this.runs = new Set();
    this.pollers = new Map();
    this.drafts = new Map(); // unsent composer text per chat
    this.activeCite = null;
    this.opener = null;
    this.env = {
      docName: () => this.detail?.document?.filename || "document.pdf",
      get metrics() {
        return state.config?.metrics;
      },
      isActiveCite: (mid, n) => this.activeCite?.mid === mid && this.activeCite?.n === n,
      onOpenPage: (page) => this.openDocPage(page),
      onRetry: (msg) => this.retry(msg),
      onRerunEval: (msg) => this.rerunEval(msg),
    };
    this.build();
  }

  // ------------------------------------------------------------------ construction
  build() {
    this.menuBtn = h("button", { type: "button", class: "icon-btn menu-btn", "aria-label": "Open sidebar", html: icon("menu", { size: 18 }), on: { click: () => this.hooks.onOpenSidebar() } });
    this.titleEl = h("div", { class: "chat-title" });
    this.exportBtn = h("button", { type: "button", class: "pill-btn", hidden: true, on: { click: () => this.exportChat() }, html: `${icon("download", { size: 14 })}<span>Export</span>`, "data-tip": "Download this chat as Markdown" });
    this.header = h("header", { class: "chat-header" }, this.menuBtn, h("h1", { class: "sr-only", text: "ReportLens" }), this.titleEl, this.exportBtn);
    this.banner = h("div", { class: "banner", role: "status", hidden: true });
    this.col = h("div", { class: "col", id: "col" });
    this.scrollEl = h("div", { class: "scroll", id: "scroll" }, this.col);
    this.fab = h("button", { type: "button", class: "fab", hidden: true, "aria-label": "Scroll to latest message", html: icon("arrow-down") });
    this.composer = new Composer({
      onSend: (text) => this.send(text),
      onStop: () => this.stop(),
      onAttach: () => this.uploadCard?.pick(),
      onOpenDocument: () => this.openDocPage(1),
    });
    this.dropOverlay = h("div", { class: "drop-overlay", hidden: true, "aria-hidden": "true" }, h("div", { class: "drop-overlay__box" }, h("span", { html: icon("file-up", { size: 28 }) }), h("strong", { text: "Drop your PDF to upload" })));
    this.root.append(this.header, this.banner, this.scrollEl, h("div", { class: "composer-wrap" }, this.fab, this.composer.el), this.dropOverlay);
    this.scroller = new AutoScroll(this.scrollEl, this.fab);
    // Content growth (streaming, late layout) keeps following the bottom only while the user has not scrolled away.
    new ResizeObserver(() => this.scroller.follow()).observe(this.col);

    this.col.addEventListener("click", (e) => this.onColClick(e));
    this.bindDragAndDrop();
  }

  bindDragAndDrop() {
    let depth = 0;
    const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
    this.root.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      this.dropOverlay.hidden = !this.canUpload();
    });
    this.root.addEventListener("dragover", (e) => hasFiles(e) && e.preventDefault());
    this.root.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (!depth) this.dropOverlay.hidden = true;
    });
    this.root.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      this.dropOverlay.hidden = true;
      const file = e.dataTransfer.files?.[0];
      if (!file) return;
      if (this.canUpload()) this.startUpload(file);
      else showError({ code: this.detail?.state === "locked" ? "document_locked" : "document_already_uploaded" });
    });
  }

  canUpload() {
    return this.group === "empty" || this.group === "failed";
  }

  // ------------------------------------------------------------------ rendering a session
  /** Shows `detail`. Cheap to call repeatedly (the indexing poller does, once a second). */
  render(detail) {
    const sameSession = this.detail?.id === detail.id;
    if (!sameSession) this.composer.setValue(this.drafts.get(detail.id) || "");
    this.detail = detail;
    const group = detail.state === "ready" || detail.state === "locked" ? "chat" : detail.state;
    const remount = !sameSession || group !== this.group;
    if (remount) this.mount(group);
    else if (group === "indexing") this.indexing.update(detail);
    else if (group === "chat") this.syncHero();
    this.updateHeader();
    this.updateComposer();
    document.title = `${detail.title && detail.title !== "New chat" ? detail.title : detail.document?.filename || "New chat"} · ReportLens`;
    if (group === "chat" && remount) this.composer.focus();
  }

  /** Drops everything shown for the current session (streams keep running in the background). */
  unmount() {
    if (this.detail) this.drafts.set(this.detail.id, this.composer.input.value);
    this.teardown();
    this.detail = null;
    this.group = null;
    this.col.replaceChildren();
  }

  teardown() {
    for (const view of this.views.values()) view.destroy?.();
    this.views.clear();
    for (const timer of this.pollers.values()) clearTimeout(timer);
    this.pollers.clear();
    this.indexing?.destroy();
    this.indexing = null;
    this.uploadCard = null;
    this.listEl = null;
    this.hero = null;
    this.activeCite = null;
  }

  mount(group) {
    this.teardown();
    this.group = group;
    this.col.replaceChildren();
    const { detail } = this;
    if (group === "empty" || group === "failed") {
      this.uploadCard = new UploadCard({
        maxUploadMb: state.config?.max_upload_mb || 100,
        heading: group === "failed" ? failedHeading(detail) : undefined,
        onFile: (file) => this.startUpload(file),
        onCancelUpload: () => this.uploadAbort?.abort(),
      });
      this.col.append(this.uploadCard.el);
    } else if (group === "indexing") {
      this.indexing = new IndexingCard({ onCancel: () => this.hooks.onCancelIndexing(detail.id) });
      this.indexing.update(detail);
      this.col.append(this.indexing.el);
    } else {
      this.listEl = h("div", { class: "messages", role: "log", "aria-live": "off", "aria-label": "Conversation" });
      this.col.append(this.listEl);
      for (const msg of detail.messages) this.addMessage(msg);
      this.syncHero();
      for (const msg of detail.messages) this.watch(msg);
    }
    this.scroller.stick = true;
    this.scrollEl.scrollTop = this.scrollEl.scrollHeight;
    this.fab.hidden = true;
  }

  addMessage(msg) {
    if (msg.role === "user") {
      const el = renderUserMessage(msg);
      this.views.set(msg, { el });
    } else {
      this.views.set(msg, new AssistantMessageView(msg, this.env));
    }
    this.listEl.append(this.views.get(msg).el);
  }

  removeMessage(msg) {
    const view = this.views.get(msg);
    view?.destroy?.();
    view?.el.remove();
    this.views.delete(msg);
    const i = this.detail.messages.indexOf(msg);
    if (i >= 0) this.detail.messages.splice(i, 1);
  }

  /** Example-question hero while the chat is ready but empty. */
  syncHero() {
    const empty = this.detail.messages.length === 0;
    if (empty && !this.hero) {
      const doc = this.detail.document;
      const bits = [];
      if (doc?.page_count) bits.push(plural(doc.page_count, "page"));
      if (doc?.node_count) bits.push(`${plural(doc.node_count, "section")} indexed`);
      if (Number.isFinite(doc?.index_seconds)) bits.push(`in ${formatDuration(doc.index_seconds * 1000)}`);
      this.hero = h(
        "div",
        { class: "hero" },
        h("div", { class: "hero__logo", html: logoMark(40) }),
        h("h2", { class: "hero__title", text: "Ask a question about this report" }),
        h("p", { class: "hero__doc" }, h("span", { html: icon("file-text", { size: 14 }) }), h("span", { text: doc?.filename || "document.pdf" })),
        bits.length ? h("p", { class: "hero__meta", text: bits.join(" · ") }) : null,
        h("div", { class: "hints", role: "group", "aria-label": "Example questions" }, HINTS.map((q) => h("button", { type: "button", class: "hint", text: q, on: { click: () => this.send(q) } }))),
      );
      this.col.prepend(this.hero);
    } else if (!empty && this.hero) {
      this.hero.remove();
      this.hero = null;
    }
  }

  updateHeader() {
    const d = this.detail;
    const title = d.title && d.title !== "New chat" ? d.title : d.document?.filename || "New chat";
    this.titleEl.textContent = title;
    this.titleEl.title = title;
    this.exportBtn.hidden = !(this.group === "chat" && d.messages.some((m) => m.role === "assistant" && FINAL.has(m.status)));
    const cfg = state.config;
    const noKey = cfg && !cfg.openai_configured;
    const usage = usageLevel();
    const text = noKey ? humanMessage({ code: "openai_not_configured" }) : usage === "exhausted" ? EXHAUSTED_BANNER : usage === "low" ? LOW_BUDGET_BANNER : "";
    this.banner.hidden = !text;
    this.banner.classList.toggle("banner--bad", !noKey && usage === "exhausted");
    this.banner.replaceChildren(...(text ? [h("span", { html: icon("triangle-alert", { size: 14 }) }), h("span", { text })] : []));
  }

  updateComposer() {
    const d = this.detail;
    this.composer.setState({ mode: d.state, filename: d.document?.filename || "", streaming: this.isAnswering(d.id), blocked: usageLevel() === "exhausted" ? EXHAUSTED_COMPOSER : "" });
  }

  /** The usage budget changed (answer finished, or the server said 402): refresh the banner and the composer. */
  refreshUsage() {
    if (!this.detail) return;
    this.updateHeader();
    this.updateComposer();
  }

  // ------------------------------------------------------------------ upload
  async startUpload(file) {
    const card = this.uploadCard;
    const sid = this.detail?.id;
    if (!card || !sid) return;
    const problem = validateFile(file, state.config?.max_upload_mb || 100);
    if (problem) return card.showError(problem);
    this.uploadAbort = new AbortController();
    card.showUploading(file, 0);
    try {
      const session = await uploadDocument(sid, file, { signal: this.uploadAbort.signal, onProgress: (f) => this.uploadCard === card && card.showUploading(file, f) });
      this.hooks.onSessionUpdated(session);
    } catch (err) {
      if (this.uploadCard !== card) return err?.name === "AbortError" ? undefined : showError(err);
      card.endUpload();
      if (err?.name !== "AbortError") card.showApiError(err);
    } finally {
      this.uploadAbort = null;
    }
  }

  // ------------------------------------------------------------------ asking
  isAnswering(sid) {
    return Array.from(this.runs).some((r) => r.sid === sid && r.phase === "answering");
  }

  runDetail(sid) {
    return Array.from(this.runs).find((r) => r.sid === sid)?.detail || null;
  }

  stop() {
    for (const run of this.runs) if (run.sid === this.detail?.id && run.phase === "answering") run.abort.abort();
  }

  cancelRuns(sid) {
    for (const run of this.runs) if (run.sid === sid) run.abort.abort();
  }

  async send(text) {
    const detail = this.detail;
    if (!detail || this.group !== "chat") return;
    if (this.isAnswering(detail.id)) return showError({ code: "session_busy" });
    const userMsg = newUserMessage(detail.id, text);
    const msg = newAssistantMessage(detail.id);
    const run = { sid: detail.id, detail, msg, userMsg, phase: "answering", started: false, abort: new AbortController(), text };
    this.runs.add(run);
    detail.messages.push(userMsg, msg);
    this.syncHero();
    this.addMessage(userMsg);
    this.addMessage(msg);
    this.scroller.toBottom(false);
    this.updateComposer();

    const { aborted } = await postSSE(api.messagesUrl(detail.id), { content: text }, { signal: run.abort.signal, onEvent: (name, data) => this.onStreamEvent(run, name, data) });

    finalizeStream(msg, { aborted });
    if (aborted && run.phase === "answering") announce("Stopped.");
    this.runs.delete(run);
    if (this.detail === detail) {
      this.views.get(msg)?.flush();
      this.watch(msg);
      this.updateHeader();
      this.updateComposer();
    }
    this.hooks.onRunFinished(detail.id);
  }

  onStreamEvent(run, name, data) {
    const { msg, userMsg, detail } = run;
    if (name === "error" && !run.started) return this.failBeforeStart(run, data);
    if (name === "message_start") {
      run.started = true;
      detail.state = "locked";
      detail.message_count = (detail.message_count || 0) + 2;
      this.hooks.onSessionPatch(detail.id, { state: "locked", message_count: detail.message_count });
      if (this.detail === detail) this.updateComposer();
    }
    applyStreamEvent(msg, userMsg, name, data);

    if (name === "answer_done") {
      run.phase = "evaluating";
      announce(msg.status === "no_sources" ? "Answer ready. No sources were cited." : `Answer ready, ${plural(msg.citations.length, "reference")}.`);
    } else if (name === "eval_done") {
      announce(evalAnnouncement(msg.evaluation));
    } else if (name === "error") {
      run.phase = "ended";
      announce(`Error: ${humanMessage(data)}`);
    }

    if (this.detail !== detail) return;
    if (name === "message_start") this.views.get(userMsg).el.dataset.mid = userMsg.id;
    const view = this.views.get(msg);
    view?.refresh();
    if (name === "answer_done" || name === "error" || name === "eval_done") {
      view?.flush();
      this.updateComposer();
      this.updateHeader();
    }
  }

  /** The server rejected the question before streaming (400/404/409/503): nothing was saved, so undo the optimistic UI. */
  failBeforeStart(run, data) {
    this.runs.delete(run);
    run.phase = "failed";
    const { detail } = run;
    const show = this.detail === detail;
    for (const m of [run.userMsg, run.msg]) {
      if (show) this.removeMessage(m);
      else detail.messages.splice(detail.messages.indexOf(m), 1);
    }
    showError(data);
    if (show) {
      this.syncHero();
      this.composer.restore(run.text);
      this.updateComposer();
    }
    if (data.code === "session_not_found") this.hooks.onSessionGone(detail.id);
  }

  retry(msg) {
    const i = this.detail.messages.indexOf(msg);
    const question = this.detail.messages.slice(0, i).reverse().find((m) => m.role === "user");
    if (question) this.send(question.content);
  }

  async rerunEval(msg) {
    const sid = msg.session_id;
    const ui = uiState(msg);
    const view = this.views.get(msg);
    ui.eval = { running: true, startedAt: Date.now(), nContexts: msg.evaluation?.n_contexts_input || 0 };
    msg.evaluation = { status: "running", errors: {}, n_contexts_input: ui.eval.nContexts };
    view?.flush();
    try {
      msg.evaluation = await api.evaluate(sid, msg.id);
    } catch (err) {
      msg.evaluation = { status: "failed", errors: { evaluation: humanMessage(err) } };
      showError(err);
    } finally {
      ui.eval.running = false;
      view?.flush();
      announce(evalAnnouncement(msg.evaluation));
    }
  }

  // ------------------------------------------------------------------ messages loaded from the server
  /** Polls a message that is still being produced elsewhere (page reload, dropped stream) until it settles. */
  watch(msg) {
    const needs = () =>
      msg.role === "assistant" &&
      ![...this.runs].some((r) => r.msg === msg) &&
      !msg.id.startsWith("tmp-") &&
      (msg.status === "streaming" || (FINAL.has(msg.status) && EVAL_OPEN.has(msg.evaluation?.status)));
    if (!needs() || this.pollers.has(msg)) return;
    const startedAt = Date.now();
    const tick = async () => {
      if (!this.views.has(msg)) return this.pollers.delete(msg);
      try {
        Object.assign(msg, await api.getMessage(msg.session_id, msg.id));
      } catch (err) {
        if (err?.status === 404) return this.pollers.delete(msg);
      }
      this.views.get(msg)?.flush();
      if (!needs()) return this.pollers.delete(msg);
      if (Date.now() - startedAt > POLL_GIVE_UP_MS) {
        if (EVAL_OPEN.has(msg.evaluation?.status)) {
          msg.evaluation = { ...msg.evaluation, status: "failed", errors: { evaluation: "The evaluation did not finish. Re-run it to try again." } };
          this.views.get(msg)?.flush();
        }
        return this.pollers.delete(msg);
      }
      this.pollers.set(msg, setTimeout(tick, POLL_MS));
    };
    this.pollers.set(msg, setTimeout(tick, POLL_MS));
  }

  // ------------------------------------------------------------------ citations and panel
  findCitation(mid, n) {
    const msg = this.detail?.messages.find((m) => m.id === mid);
    return msg?.citations?.find((c) => c.index === n) || msg?.citations?.[n - 1] || null;
  }

  docMeta() {
    const doc = this.detail.document;
    return { filename: doc?.filename || "document.pdf", pageCount: doc?.page_count || null };
  }

  openCite(cite, mid, n, opener) {
    this.opener = opener || null;
    this.activeCite = { mid, n };
    this.refreshChips();
    openCitation(this.detail.id, cite, this.docMeta());
  }

  openDocPage(page) {
    this.activeCite = null;
    this.refreshChips();
    openPage(this.detail.id, page, this.docMeta());
  }

  refreshChips() {
    for (const view of this.views.values()) view.refresh?.();
  }

  /** Panel closed (x, Esc or session switch): clear the active chip and give focus back to it. */
  onPanelClosed() {
    if (!this.activeCite && !this.opener) return;
    this.activeCite = null;
    this.refreshChips();
    const { mid, cite } = this.opener?.dataset || {};
    this.opener = null;
    // The chip element is re-rendered when it loses its active state, so look it up again.
    requestAnimationFrame(() => mid && this.listEl?.querySelector(`.cite-chip[data-mid="${CSS.escape(mid)}"][data-cite="${cite}"]`)?.focus());
  }

  onColClick(e) {
    const chip = e.target.closest(".cite-chip[data-cite]");
    if (chip) {
      const cite = this.findCitation(chip.dataset.mid, Number(chip.dataset.cite));
      if (cite) this.openCite(cite, chip.dataset.mid, Number(chip.dataset.cite), chip);
      return;
    }
    const act = e.target.closest("[data-act]");
    if (!act) return;
    const table = act.closest(".table-card")?.querySelector("table");
    if (!table) return;
    if (act.dataset.act === "table-copy") {
      copyText(tableToTsv(table)).then((ok) => (ok ? (toast("Table copied (tab-separated)"), announce("Table copied")) : showError({ message: "Copy failed." })));
    } else if (act.dataset.act === "table-full") {
      tableDialog(table);
    }
  }

  // ------------------------------------------------------------------ export
  exportChat() {
    const d = this.detail;
    const lines = [`# ${this.titleEl.textContent}`, "", `Document: ${d.document?.filename || "unknown"}`, ""];
    for (const m of d.messages) {
      if (m.role === "user") lines.push(`## Question`, "", m.content, "");
      else if (FINAL.has(m.status)) {
        lines.push("## Answer", "", markersToReferences(m.content, m.citations), "");
        const ev = m.evaluation;
        if (ev && ev.status !== "pending" && ev.status !== "running" && ev.status !== "skipped") {
          const scores = METRIC_KEYS.filter((k) => Number.isFinite(ev[k])).map((k) => `${k.replace("_", " ")} ${ev[k].toFixed(2)}`);
          if (scores.length) lines.push(`_RAGAS (judge ${ev.judge_model || "unknown"}): ${scores.join(", ")}_`, "");
        }
      }
    }
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" }));
    const a = h("a", { href: url, download: `${(this.titleEl.textContent || "chat").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 60)}.md` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }


  focusUpload() {
    this.uploadCard?.zone.focus();
  }

}

function evalAnnouncement(ev) {
  if (!ev) return "Evaluation finished.";
  if (ev.status === "skipped") return "Evaluation skipped.";
  if (ev.status === "failed") return "Evaluation failed.";
  const parts = METRIC_KEYS.filter((k) => Number.isFinite(ev[k])).map((k) => `${k.replace("_", " ")} ${ev[k].toFixed(2)}`);
  return `Evaluation complete: ${parts.join(", ")}.`;
}
