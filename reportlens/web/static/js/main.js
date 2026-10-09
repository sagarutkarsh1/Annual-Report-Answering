// Application bootstrap and session orchestration: routing, selection, polling of indexing documents.

import { api, setErrorHooks, setLimits } from "./api.js";
import { APP_NAME } from "./brand.js";
import { ChatPane } from "./chat.js";
import { confirmDialog } from "./dialog.js";
import { h } from "./dom.js";
import { initFloating } from "./hovercard.js";
import { icon } from "./icons.js";
import { promptLogin } from "./login.js";
import { configMax } from "./questionset.js";
import { initLayout } from "./layout.js";
import { llmSummary, openLLMDialog } from "./llm.js";
import { closePanel, dropSessionCache, initPanel, panelState } from "./panel.js";
import { Sidebar, sessionLabel } from "./sidebar.js";
import { removeSession, sessionById, setState, state, upsertSession } from "./state.js";
import { announce, initToasts, showError } from "./toast.js";

const $ = (id) => document.getElementById(id);
const POLL_ACTIVE_MS = 1000;
const POLL_LIST_MS = 3000;

let chat;
let sidebar;
let layout;
let openToken = 0;
let pollTimer = 0;
let pollTicks = 0;

// ---------------------------------------------------------------------------------- session actions
const routeSid = () => /^#\/s\/([0-9a-f]{32})$/.exec(location.hash)?.[1] || null;

async function refreshSessions() {
  if (state.demoOnly) return; // browsing the demo without a code: there is no chat list to fetch
  try {
    setState({ sessions: await api.listSessions() });
  } catch (err) {
    showError(err);
  }
  schedulePoll();
}

async function openSession(sid) {
  if (sid === state.activeId && state.detail) return;
  const token = ++openToken;
  closePanel();
  let detail = chat.runDetail(sid); // a stream is still writing into this object: do not refetch over it
  if (!detail) {
    try {
      detail = await api.getSession(sid);
    } catch (err) {
      if (token !== openToken) return;
      showError(err);
      if (err?.code === "session_not_found") {
        removeSession(sid);
        return openFallback();
      }
      return;
    }
  }
  if (token !== openToken) return; // a newer navigation won
  chat.unmount();
  setState({ activeId: sid, detail });
  upsertSession(detail);
  if (routeSid() !== sid) location.hash = `#/s/${sid}`;
  layout.closeDrawer();
  chat.render(detail);
  schedulePoll();
}

/** Opens the most recent chat, or starts a new one when there are none (the demo, when browsing it without a code). */
async function openFallback() {
  if (state.demoOnly) return state.demo?.available ? openSession(state.demo.session_id) : signIn();
  const next = state.sessions[0];
  if (next) return openSession(next.id);
  return createAndOpen();
}

function openDemo() {
  if (state.demo?.available) openSession(state.demo.session_id);
}

/** "Ask your own question about this report": a chat of one's own with the demo's document, already indexed (no cost). */
async function askAboutDemo() {
  if (state.demoOnly) return signIn();
  // A static demo has no document to copy: "ask your own" means a blank chat to upload a report into.
  return createAndOpen(state.demo?.has_document === false ? undefined : state.demo?.session_id);
}

/** From the demo: show the access-code screen again; a valid code reloads into the full app. */
async function signIn() {
  const choice = await promptLogin({ requestEmail: state.auth?.requestEmail, demo: null, cancellable: !!state.demo?.available });
  if (choice === "login") location.reload();
}

async function createAndOpen(fromSession) {
  try {
    const session = await api.createSession(fromSession);
    upsertSession(session);
    await openSession(session.id);
  } catch (err) {
    showError(err);
  }
}

async function newChat() {
  if (state.demoOnly) return signIn();
  const current = state.detail;
  if (current?.state === "empty") {
    layout.closeDrawer();
    chat.focusUpload();
    return;
  }
  const blank = state.sessions.find((s) => s.state === "empty");
  if (blank) return openSession(blank.id);
  return createAndOpen();
}

async function deleteSession(sid) {
  const session = sessionById(sid);
  if (!session) return;
  const ok = await confirmDialog({
    title: "Delete this chat?",
    text: `"${sessionLabel(session)}" and its uploaded document will be permanently removed.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (ok) await removeNow(sid);
}

async function removeNow(sid) {
  chat.cancelRuns(sid);
  try {
    await api.deleteSession(sid);
  } catch (err) {
    if (err?.code !== "session_not_found") return showError(err);
  }
  dropSessionCache(sid);
  removeSession(sid);
  if (state.activeId === sid) {
    openToken += 1;
    chat.unmount();
    setState({ activeId: null, detail: null });
    await openFallback();
  }
}

async function renameSession(sid, title) {
  try {
    const session = await api.renameSession(sid, title);
    upsertSession(session);
    if (state.detail?.id === sid) {
      state.detail.title = session.title;
      chat.render(state.detail);
    }
  } catch (err) {
    showError(err);
  }
}

// ---------------------------------------------------------------------------------- the visitor's own model and key
let llmOpen = false;
async function openLLM() {
  if (llmOpen || state.demoOnly) return;
  llmOpen = true;
  try {
    if (await openLLMDialog()) chat.refreshUsage(); // banner and composer depend on whether a key is set
  } finally {
    llmOpen = false;
  }
}

// ---------------------------------------------------------------------------------- access code and usage budget
/** The server said 401 auth_required (the login cookie expired): ask for the code again, then reload the page state. */
function onAuthRequired() {
  promptLogin({ requestEmail: state.auth?.requestEmail, demo: state.demo }).then((choice) => {
    if (choice === "login") location.reload();
    else if (choice === "demo") location.replace(`#/s/${state.demo.session_id}`), location.reload();
  });
}

async function signOut() {
  try {
    await api.logout();
  } catch (err) {
    return showError(err);
  }
  location.reload();
}

/** The server said 402 budget_exhausted: show the paused state at once instead of waiting for the next config fetch. */
function onBudgetExhausted() {
  if (!state.config) return;
  setState({ config: { ...state.config, usage_budget: { enabled: true, used_fraction: 1 } } });
  chat?.refreshUsage();
}

/** Re-reads the budget fraction (after an answer); only costs a request when a budget is configured. */
async function refreshUsage() {
  if (!state.config?.usage_budget?.enabled) return;
  try {
    setState({ config: await api.config() });
    chat.refreshUsage();
  } catch {
    /* transient: the banner updates on the next answer */
  }
}

// ---------------------------------------------------------------------------------- indexing poller
function schedulePoll() {
  clearTimeout(pollTimer);
  if (document.hidden) return;
  const activeIndexing = state.detail?.state === "indexing";
  const othersIndexing = state.sessions.some((s) => s.state === "indexing" && s.id !== state.activeId);
  if (!activeIndexing && !othersIndexing) return;
  pollTimer = setTimeout(pollOnce, activeIndexing ? POLL_ACTIVE_MS : POLL_LIST_MS);
}

async function pollOnce() {
  if (state.demoOnly) return; // nothing of one's own can be indexing (and the list needs the access code)
  const sid = state.activeId;
  try {
    if (state.detail?.state === "indexing") {
      const detail = await api.getSession(sid);
      if (state.activeId === sid) applyPolledDetail(detail);
      pollTicks += 1;
      if (pollTicks % 3 === 0 && state.sessions.some((s) => s.state === "indexing" && s.id !== sid)) {
        setState({ sessions: mergeActive(await api.listSessions()) });
      }
    } else {
      const list = await api.listSessions();
      if (await checkActiveStillExists(list, sid)) return;
      setState({ sessions: mergeActive(list) });
    }
  } catch (err) {
    if (err?.code === "session_not_found" && state.activeId === sid) return handleSessionGone(sid, err);
    /* transient network error: the next tick retries */
  }
  schedulePoll();
}

/** The server no longer knows the open chat (on a free host it slept and restarted empty): say so, forget everything about it, start fresh. */
async function handleSessionGone(sid, err) {
  chat.cancelRuns(sid);
  dropSessionCache(sid);
  removeSession(sid);
  closePanel();
  showError(err);
  openToken += 1;
  chat.unmount();
  setState({ activeId: null, detail: null });
  return openFallback();
}

/** After a quiet spell the server may have slept and restarted without any state; the list endpoint would not mention the open chat. */
async function checkActiveStillExists(list, sid) {
  if (!sid || state.activeId !== sid || list.some((s) => s.id === sid)) return false;
  try {
    await api.getSession(sid);
    return false; // the list simply lagged
  } catch (err) {
    if (err?.code !== "session_not_found" || state.activeId !== sid) return false;
    await handleSessionGone(sid, err);
    return true;
  }
}

/** The list endpoint may lag the detail we just polled; keep the freshest row for the active chat. */
function mergeActive(list) {
  const active = state.detail;
  return active ? list.map((s) => (s.id === active.id ? { ...s, ...stripMessages(active) } : s)) : list;
}

function stripMessages({ messages, ...rest }) {
  return rest;
}

function applyPolledDetail(detail) {
  const previous = state.detail?.state;
  setState({ detail });
  upsertSession(detail);
  chat.render(detail);
  if (previous === "indexing" && detail.state === "ready") announce("Document ready. You can ask a question now.");
  else if (previous === "indexing" && detail.state === "failed") announce("Indexing failed.");
}

// ---------------------------------------------------------------------------------- boot
async function boot() {
  initToasts();
  const app = $("app");
  chat = new ChatPane($("chat-pane"), {
    onSessionPatch: (sid, patch) => sessionById(sid) && upsertSession({ ...sessionById(sid), ...patch }),
    onSessionUpdated: (session) => {
      upsertSession(session);
      if (state.activeId === session.id) {
        const detail = { ...session, messages: state.detail?.messages || [] };
        setState({ detail });
        chat.render(detail);
        schedulePoll();
      }
    },
    onRunFinished: async (sid) => {
      refreshUsage();
      await refreshSessions();
      const row = sessionById(sid);
      if (row && state.detail?.id === sid) {
        state.detail.title = row.title;
        chat.render(state.detail);
      }
    },
    onCancelIndexing: (sid) => removeNow(sid),
    onOpenSidebar: () => layout.openDrawer(),
    onSessionGone: (sid) => removeNow(sid),
    onAskAboutDemo: askAboutDemo,
    onSignIn: signIn,
  });
  sidebar = new Sidebar($("sidebar"), {
    onSelect: (sid) => openSession(sid),
    onNewChat: newChat,
    onRename: renameSession,
    onDelete: deleteSession,
    onNewFromDocument: (sid) => createAndOpen(sid),
    onToggle: () => layout.toggleSidebar(),
    onSignOut: signOut,
    onSignIn: signIn,
    onOpenDemo: openDemo,
    onOpenLLM: openLLM,
  });
  layout = initLayout({
    app,
    card: $("card"),
    resizer: $("panel-resizer"),
    scrim: $("scrim"),
    onSidebarState: ({ mobile, rail, drawerOpen }) => {
      sidebar.setCollapsed(rail);
      $("sidebar").toggleAttribute("inert", mobile && !drawerOpen);
      if (mobile && drawerOpen) $("sidebar").querySelector(".nav-item").focus();
    },
    onScrimClick: () => closePanel(),
  });
  initPanel($("source-panel"), { onClose: () => chat.onPanelClosed() });
  initFloating({
    getCitation: (mid, n) => chat.findCitation(mid, n),
    getPageCount: () => state.detail?.document?.page_count || null,
    onOpen: (cite, chip) => chat.openCite(cite, chip.dataset.mid, Number(chip.dataset.cite), chip),
  });
  bindGlobalKeys();
  window.addEventListener("hashchange", () => {
    const sid = routeSid();
    if (sid && sid !== state.activeId) openSession(sid);
  });
  document.addEventListener("visibilitychange", () => (document.hidden ? clearTimeout(pollTimer) : pollOnce()));

  setErrorHooks({ authRequired: onAuthRequired, budgetExhausted: onBudgetExhausted, ownKeyRequired: () => openLLM() });
  try {
    const [auth, demo] = await Promise.all([api.auth(), api.demo().catch(() => ({ available: false }))]);
    setState({ auth: { required: auth.required, requestEmail: auth.request_email || null }, demo });
    if (auth.required && !auth.authenticated) {
      const wantsDemo = demo.available && routeSid() === demo.session_id; // a shared link to the demo opens it straight away
      const choice = wantsDemo ? "demo" : await promptLogin({ requestEmail: auth.request_email, demo });
      setState({ demoOnly: choice === "demo" });
    }
  } catch (err) {
    return showBootError(err);
  }
  try {
    if (state.demoOnly) {
      const config = await loadConfig();
      applyLimits(config);
      setState({ config, sessions: [] });
    } else {
      const [config, health, sessions] = await Promise.all([loadConfig(), api.health().catch(() => null), api.listSessions()]);
      applyLimits(config);
      setState({ config, health, sessions });
      setState({ llm: config?.llm?.visitor_keys === "off" ? null : llmSummary() });
    }
  } catch (err) {
    return showBootError(err);
  }
  const wanted = routeSid();
  try {
    if (wanted && (sessionById(wanted) || wanted === state.demo?.session_id)) await openSession(wanted);
    else await openFallback();
  } finally {
    $("boot").remove();
  }
}

/** GET /api/config is not essential to start: without it the UI uses its built-in defaults (questions, limits). */
async function loadConfig() {
  try {
    return await api.config();
  } catch (err) {
    console.warn("GET /api/config failed; continuing with built-in defaults", err);
    return null;
  }
}

function applyLimits(config) {
  if (!config) return;
  setLimits({ maxUploadMb: config.max_upload_mb, maxPages: config.max_pages, publicMode: !!config.public_mode, maxBatch: configMax(config) });
}

function showBootError(err) {
  const boot = $("boot");
  boot.replaceChildren(
    h("div", { class: "state-card" }, h("span", { html: icon("triangle-alert", { size: 28 }) }), h("h2", { class: "state-card__title", text: `Can't reach ${APP_NAME}` }), h("p", { class: "state-card__sub", text: err?.message || "The server did not answer." }), h("button", { type: "button", class: "btn btn-primary", text: "Try again", on: { click: () => location.reload() } })),
  );
}

function bindGlobalKeys() {
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.shiftKey && e.key.toLowerCase() === "o") {
      e.preventDefault();
      newChat();
    } else if (mod && !e.shiftKey && e.key.toLowerCase() === "b") {
      e.preventDefault();
      layout.toggleSidebar();
    } else if (e.key === "Escape" && !e.defaultPrevented) {
      if (document.querySelector("dialog[open], .menu")) return;
      if (e.target.closest?.("textarea, input")) return;
      if (layout.isMobile() && $("app").classList.contains("drawer-open")) layout.closeDrawer();
      else if (panelState.isOpen) closePanel();
    }
  });
}

boot();
