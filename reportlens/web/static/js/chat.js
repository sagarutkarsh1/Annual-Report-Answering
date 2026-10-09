// The main pane: header, upload / indexing / failed cards, the conversation, composer and scrolling.
// Streams are owned by this module and keep running when the user switches to another chat.

import { api, humanMessage, uploadDocument } from "./api.js";
import { APP_NAME, requestCodeHref } from "./brand.js";
import { Composer } from "./composer.js";
import { hasLLM } from "./llmstore.js";
import { tableDialog } from "./dialog.js";
import { copyText, h, prefersReducedMotion } from "./dom.js";
import { formatDuration, plural } from "./format.js";
import { icon, logoMark } from "./icons.js";
import { renderUserMessage, AssistantMessageView } from "./message.js";
import { markersToReferences, tableToTsv } from "./markdown.js";
import { openCitation, openPage } from "./panel.js";
import { BatchBar, QuestionSet, configDefaults, configMax } from "./questionset.js";
import { postSSE } from "./sse.js";
import { state } from "./state.js";
import { isStaticDemo } from "./sourcecard.js";
import { applyStreamEvent, batchPair, finalizeStream, newAssistantMessage, newUserMessage, uiState } from "./stream.js";
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
    this.hold = false; // a question set is streaming in: stay where the reader is instead of following the bottom
    this.inputAt = 0;
    scrollEl.addEventListener("scroll", () => this.onScroll(), { passive: true });
    for (const type of ["wheel", "touchstart", "pointerdown", "keydown"]) scrollEl.addEventListener(type, () => (this.inputAt = Date.now()), { passive: true });
    fab.addEventListener("click", () => this.toBottom(true));
  }

  distance() {
    const el = this.scrollEl;
    return el.scrollHeight - el.scrollTop - el.clientHeight;
  }

  onScroll() {
    const d = this.distance();
    if (this.hold) {
      // Programmatic scrolling and layout growth never release the hold; the reader scrolling does.
      if (Date.now() - this.inputAt > 500 || this.scrollEl.scrollTop <= 0) {
        this.fab.hidden = d < STICK_PX * 1.5;
        return;
      }
      this.hold = false;
    }
    this.stick = d < STICK_PX;
    this.fab.hidden = d < STICK_PX * 1.5;
  }

  /** Called after content grew. */
  follow() {
    if (this.hold) this.onScroll();
    else if (this.stick) this.scrollEl.scrollTop = this.scrollEl.scrollHeight;
    else this.onScroll();
  }

  /** Keeps `el` (the first question of a set) at the top of the view while the answers stream in. */
  holdAt(el) {
    this.hold = true;
    this.stick = false;
    const top = el.getBoundingClientRect().top - this.scrollEl.getBoundingClientRect().top + this.scrollEl.scrollTop;
    this.scrollEl.scrollTop = Math.max(0, top - 8);
    this.onScroll();
  }

  toBottom(smooth = false) {
    this.hold = false;
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
      readOnly: () => !!this.detail?.read_only,
      onOpenPage: (page, msg) => this.openDocPage(page, msg),
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
    this.demoBadge = h("span", { class: "badge badge--demo-chat", text: "Demo", hidden: true, "data-tip": "A real chat, shown read-only" });
    this.header = h("header", { class: "chat-header" }, this.menuBtn, h("h1", { class: "sr-only", text: APP_NAME }), this.titleEl, this.demoBadge, this.exportBtn);
    this.banner = h("div", { class: "banner", role: "status", hidden: true });
    this.batchBar = new BatchBar();
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
    this.demoBar = h("section", { class: "demo-bar", hidden: true, "aria-label": "About this demo" });
    this.root.append(this.header, this.banner, this.batchBar.el, this.scrollEl, h("div", { class: "composer-wrap" }, this.fab, this.composer.el, this.demoBar), this.dropOverlay);
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
      else showError({ code: this.detail?.read_only ? "demo_read_only" : this.detail?.state === "locked" ? "document_locked" : "document_already_uploaded" });
    });
  }

  canUpload() {
    return this.group === "empty" || this.group === "failed";
  }

  // ------------------------------------------------------------------ rendering a session
  /** Shows `detail`. Cheap to call repeatedly (the indexing poller does, once a second). */
  render(detail) {
    const sameSession = this.detail?.id === detail.id;
    if (!sameSession) {
      this.composer.setValue(this.drafts.get(detail.id) || "");
      this.batchBar.hide();
    }
    this.detail = detail;
    const group = detail.state === "ready" || detail.state === "locked" ? "chat" : detail.state;
    const remount = !sameSession || group !== this.group;
    if (remount) this.mount(group);
    else if (group === "indexing") this.indexing.update(detail);
    else if (group === "chat") this.syncEmptyState();
    this.updateHeader();
    this.updateComposer();
    if (group === "chat" && (remount || !sameSession)) this.updateBatchBar();
    document.title = `${detail.title && detail.title !== "New chat" ? detail.title : detail.document?.filename || "New chat"} · ${APP_NAME}`;
    if (group === "chat" && remount && !detail.read_only) this.composer.focus();
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
    this.qset?.destroy();
    this.qset = null;
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
      this.syncEmptyState();
      for (const msg of detail.messages) this.watch(msg);
    }
    this.scroller.stick = true;
    this.scroller.hold = false;
    this.scrollEl.scrollTop = this.scrollEl.scrollHeight;
    if (this.qset) {
      this.scroller.stick = false; // the question set is read from its top, not from its foot
      this.scrollEl.scrollTop = 0;
    }
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

  /** The hero and, in a ready chat that has no message yet, the editable question set above the composer. */
  syncEmptyState() {
    this.syncHero();
    this.syncQuestions();
  }

  syncQuestions() {
    const d = this.detail;
    const want = this.group === "chat" && d.state === "ready" && d.messages.length === 0 && !d.read_only;
    if (want && !this.qset) {
      this.qset = new QuestionSet({ defaults: configDefaults(state.config), max: configMax(state.config), onRun: (questions) => this.runBatch(questions) });
      this.col.append(this.qset.el);
    } else if (!want && this.qset) {
      this.qset.destroy();
      this.qset = null;
    }
    this.hero?.classList.toggle("hero--set", Boolean(this.qset));
    this.qset?.setBusy(Boolean(this.batchRun(d.id)));
    this.qset?.setBlocked(this.blockedReason());
  }

  updateHeader() {
    const d = this.detail;
    const title = d.title && d.title !== "New chat" ? d.title : d.document?.filename || "New chat";
    this.titleEl.textContent = title;
    this.titleEl.title = title;
    this.demoBadge.hidden = !d.read_only;
    this.exportBtn.hidden = !(this.group === "chat" && d.messages.some((m) => m.role === "assistant" && FINAL.has(m.status)));
    const cfg = state.config;
    const own = hasLLM() && cfg?.llm?.visitor_keys !== "off";
    const needKey = cfg?.llm?.visitor_keys === "required" && !own && !cfg?.demo_mock && !d.read_only;
    const noKey = cfg && !cfg.openai_configured && !own && !needKey && !d.read_only;
    const usage = own ? "off" : usageLevel(); // your own key: this server's budget does not apply to you
    const text = needKey ? humanMessage({ code: "own_key_required" }) : noKey ? humanMessage({ code: "openai_not_configured" })
      : usage === "exhausted" ? EXHAUSTED_BANNER : usage === "low" ? LOW_BUDGET_BANNER : "";
    this.banner.hidden = !text;
    this.banner.classList.toggle("banner--bad", !noKey && !needKey && usage === "exhausted");
    this.banner.replaceChildren(...(text ? [h("span", { html: icon("triangle-alert", { size: 14 }) }), h("span", { text })] : []));
  }

  updateComposer() {
    const d = this.detail;
    const readOnly = !!d.read_only;
    this.composer.el.hidden = readOnly;
    this.demoBar.hidden = !readOnly;
    if (readOnly) return this.renderDemoBar();
    const batch = this.batchRun(d.id);
    this.composer.setState({ mode: d.state, filename: d.document?.filename || "", streaming: this.isAnswering(d.id), blocked: this.blockedReason(), busy: batch?.phase === "answering" ? "Answering your questions..." : "" });
    this.qset?.setBlocked(this.blockedReason());
  }

  /** Why new questions are paused right now ("" = they are not). */
  blockedReason() {
    const cfg = state.config;
    const own = hasLLM() && cfg?.llm?.visitor_keys !== "off";
    const needKey = cfg?.llm?.visitor_keys === "required" && !own && !cfg?.demo_mock;
    return needKey ? "Add your API key under 'Model & API key' to ask" : !own && usageLevel() === "exhausted" ? EXHAUSTED_COMPOSER : "";
  }

  /** In place of the composer on the read-only demo: what this is, where the document comes from, and how to try it yourself. */
  renderDemoBar() {
    const d = this.detail;
    const demo = state.demo || {};
    const answer = d.messages.find((m) => m.role === "assistant" && FINAL.has(m.status));
    const model = answer?.usage?.model;
    const noPdf = isStaticDemo(d.id); // the packaged demo has no PDF: a citation opens the verified quote instead
    const text = `A real chat with ${d.document?.filename || "an annual report"}${model ? `, answered by ${model}` : ""}, every answer scored live with RAGAS. ${noPdf ? "Click a citation to see the page, section and verified quote behind it." : "Click a citation to see the passage highlighted in the PDF."}`;
    const actions = [];
    if (state.demoOnly) {
      actions.push(h("button", { type: "button", class: "btn btn-primary btn-sm", on: { click: () => this.hooks.onSignIn() } }, h("span", { html: icon("log-in", { size: 14 }) }), h("span", { text: "Sign in to ask your own questions" })));
      if (state.auth?.requestEmail) actions.push(h("a", { class: "btn btn-sm", href: requestCodeHref(state.auth.requestEmail) }, h("span", { html: icon("mail", { size: 14 }) }), h("span", { text: "Request an access code" })));
    } else {
      actions.push(h("button", { type: "button", class: "btn btn-primary btn-sm", on: { click: () => this.hooks.onAskAboutDemo() } }, h("span", { html: icon(noPdf ? "file-up" : "message-square-plus", { size: 14 }) }), h("span", { text: noPdf ? "Upload your own report" : "Ask your own question about this report" })));
    }
    const source = demo.attribution
      ? h("p", { class: "demo-bar__source" }, h("span", { text: `Source: ${demo.attribution} ` }), demo.attribution_url ? h("a", { href: demo.attribution_url, target: "_blank", rel: "noopener noreferrer", html: `Publisher's site ${icon("external-link", { size: 12 })}` }) : null)
      : null;
    this.demoBar.replaceChildren(
      h("div", { class: "demo-bar__head" }, h("span", { class: "demo-bar__icon", html: icon("sparkles", { size: 16 }) }), h("strong", { text: "Read-only demo" })),
      h("p", { class: "demo-bar__text", text }),
      h("div", { class: "demo-bar__actions" }, actions),
      source,
    );
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
    this.syncEmptyState();
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
      this.syncEmptyState();
      this.composer.restore(run.text);
      this.updateComposer();
    }
    if (data.code === "session_not_found") this.hooks.onSessionGone(detail.id);
  }

  // ------------------------------------------------------------------ question set (POST /batch)
  batchRun(sid = this.detail?.id) {
    return Array.from(this.runs).find((r) => r.kind === "batch" && r.sid === sid) || null;
  }

  async runBatch(questions) {
    const detail = this.detail;
    if (!detail || this.group !== "chat" || detail.messages.length || detail.read_only || !questions?.length) return;
    if (this.isAnswering(detail.id)) return showError({ code: "session_busy" });
    const run = { kind: "batch", sid: detail.id, detail, phase: "answering", started: false, abort: new AbortController(), questions, items: [], total: questions.length };
    this.runs.add(run);
    this.qset?.setBusy(true);
    this.updateComposer();
    this.updateBatchBar();

    const { aborted } = await postSSE(api.batchUrl(detail.id), { questions }, { signal: run.abort.signal, onEvent: (name, data) => this.onBatchEvent(run, name, data) });

    for (const item of run.items) finalizeStream(item.msg, { aborted });
    if (aborted && run.started) announce("Stopped.");
    run.phase = "ended";
    this.runs.delete(run);
    if (this.detail === detail) {
      for (const item of run.items) {
        this.views.get(item.msg)?.flush();
        this.watch(item.msg);
      }
      this.scroller.hold = false;
      if (!run.started) this.qset?.setBusy(false);
      this.finishBatchBar(run, aborted);
      this.updateHeader();
      this.updateComposer();
    }
    this.hooks.onRunFinished(detail.id);
  }

  onBatchEvent(run, name, data) {
    const { detail } = run;
    if (name === "error" && !run.started) return this.failBatchBeforeStart(run, data);
    if (name === "batch_start") return this.startBatch(run, data);
    if (name === "batch_done") {
      run.summary = data;
      return;
    }
    if (name === "done") return;
    const item = (data.message_id && run.items.find((i) => i.msg.id === data.message_id)) || run.items.find((i) => i.index === data.index);
    if (!item) {
      // A failure of the whole run (not of one question): every unanswered question ends with it.
      if (name === "error") for (const rest of run.items) if (rest.msg.status === "streaming") this.applyItemEvent(run, rest, name, data);
      return;
    }
    this.applyItemEvent(run, item, name, data);
    if (this.detail === detail) this.updateBatchBar();
  }

  applyItemEvent(run, item, name, data) {
    const { msg, userMsg } = item;
    const { detail } = run;
    applyStreamEvent(msg, userMsg, name, data);
    if (name === "answer_done") announce(msg.status === "no_sources" ? `Answer ${item.position} of ${run.total} ready. No sources were cited.` : `Answer ${item.position} of ${run.total} ready.`);
    else if (name === "error") announce(`Question ${item.position} failed: ${humanMessage(data)}`);
    const p = this.batchProgress(run);
    if (p.done >= p.total && run.phase === "answering") {
      run.phase = "evaluating"; // everything is answered (scoring may still run): the composer is free again
      if (this.detail === detail) this.updateComposer();
    }
    if (this.detail !== detail) return;
    const view = this.views.get(msg);
    view?.refresh();
    if (name === "answer_done" || name === "error" || name === "eval_done") view?.flush();
    if (name === "eval_done") announce(evalAnnouncement(msg.evaluation));
  }

  /** `batch_start` arrives first: every question and its answer placeholder appear at once, in order. */
  startBatch(run, data) {
    const { detail } = run;
    const items = Array.isArray(data.items) ? data.items.slice().sort((a, b) => (a.index ?? 0) - (b.index ?? 0)) : [];
    if (!items.length) return;
    run.started = true;
    run.total = items.length;
    run.concurrency = data.concurrency;
    detail.state = "locked";
    detail.message_count = (detail.message_count || 0) + items.length * 2;
    this.hooks.onSessionPatch(detail.id, { state: "locked", message_count: detail.message_count });
    items.forEach((raw, i) => {
      const { userMsg, msg } = batchPair(detail.id, raw);
      run.items.push({ index: raw.index, position: i + 1, userMsg, msg });
      detail.messages.push(userMsg, msg);
    });
    announce(`Answering ${plural(items.length, "question")}.`);
    if (this.detail !== detail) return;
    this.syncEmptyState();
    for (const item of run.items) {
      this.addMessage(item.userMsg);
      this.addMessage(item.msg);
    }
    this.scroller.holdAt(this.views.get(run.items[0].userMsg).el);
    this.updateComposer();
    this.updateBatchBar();
  }

  /** The server refused the set before streaming (400/402/404/409/429/503): nothing was saved, the card stays as it was. */
  failBatchBeforeStart(run, data) {
    run.phase = "failed";
    showError(data);
    if (this.detail === run.detail) {
      this.qset?.setBusy(false);
      this.updateComposer();
    }
    if (data.code === "session_not_found") this.hooks.onSessionGone(run.sid);
  }

  batchProgress(run) {
    let done = 0;
    let failed = 0;
    let begun = 0;
    let scoring = false;
    for (const { msg } of run.items) {
      if (!uiState(msg).queued) begun += 1;
      if (FINAL.has(msg.status)) {
        done += 1;
        scoring ||= EVAL_OPEN.has(msg.evaluation?.status);
      } else if (msg.status === "error") {
        done += 1;
        failed += 1;
      }
    }
    return { total: run.total, begun, done, failed, scoring };
  }

  updateBatchBar() {
    const run = this.detail && this.batchRun();
    if (!run) return;
    const p = this.batchProgress(run);
    if (!run.started) return this.batchBar.show({ label: "Starting...", value: 0, max: run.total });
    if (p.done >= p.total) return this.batchBar.show({ label: `${plural(p.total - p.failed, "answer")} ready${p.failed ? ` · ${p.failed} failed` : ""} · scoring...`, value: p.total, max: p.total, tone: p.failed ? "warn" : "run" });
    this.batchBar.show({ label: `Answering ${p.begun} of ${p.total} · ${p.done} done`, value: p.done, max: p.total });
  }

  finishBatchBar(run, aborted) {
    if (!run.started) return this.batchBar.hide();
    const p = this.batchProgress(run);
    const answered = p.done - p.failed;
    const label = aborted ? `Stopped · ${answered} of ${p.total} answered` : p.failed ? `${answered} of ${p.total} answered · ${p.failed} failed` : `All ${p.total} answered`;
    this.batchBar.show({ label, value: p.total, max: p.total, tone: aborted || p.failed ? "warn" : "ok", autoHide: true });
    announce(label);
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
  runOwns(msg) {
    return Array.from(this.runs).some((r) => r.msg === msg || r.items?.some((i) => i.msg === msg));
  }

  /** Polls a message that is still being produced elsewhere (page reload, dropped stream) until it settles. */
  watch(msg) {
    const needs = () =>
      msg.role === "assistant" &&
      !this.runOwns(msg) &&
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
    const msg = this.detail.messages.find((m) => m.id === mid);
    openCitation(this.detail.id, cite, this.docMeta(), { cites: msg?.citations || [], onSelect: (c, i) => this.followCard(mid, c, i) });
  }

  /** `msg`: the answer whose Sources / step link was clicked (without it: any answer of the chat, e.g. the document pill). */
  openDocPage(page, msg) {
    this.activeCite = null;
    this.refreshChips();
    const cites = msg?.citations?.length ? msg.citations : this.detail.messages.flatMap((m) => m.citations || []);
    openPage(this.detail.id, page, this.docMeta(), null, { cites, onSelect: (c, i) => msg && this.followCard(msg.id, c, i) });
  }

  /** The source card stepped to another citation of the answer: light up its chip. */
  followCard(mid, cite, i) {
    this.activeCite = { mid, n: cite.index ?? i + 1 };
    this.refreshChips();
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
