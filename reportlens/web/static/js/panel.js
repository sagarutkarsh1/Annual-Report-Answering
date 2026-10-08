// Glue between the chat and the PDF source panel (js/viewer.js, built separately and loaded lazily).

import { api } from "./api.js";
import { toast, showError } from "./toast.js";

let aside = null;
let appEl = null;
let onClosed = () => {};
let viewerModule = null;
let instance = null;
let openSid = null;
let queue = Promise.resolve();
const pagesCache = new Map(); // sid -> DocumentPages

export function initPanel(asideEl, { onClose }) {
  aside = asideEl;
  appEl = document.getElementById("app");
  onClosed = onClose;
}

export const panelState = {
  get isOpen() {
    return Boolean(openSid) && Boolean(instance?.isOpen);
  },
  get sessionId() {
    return openSid;
  },
};

async function loadViewer() {
  if (!viewerModule) {
    try {
      viewerModule = await import("./viewer.js");
    } catch (err) {
      console.error("viewer.js failed to load", err);
      toast("Source viewer unavailable", { kind: "error" });
      return null;
    }
  }
  return viewerModule;
}

function setLayoutOpen(open) {
  appEl.classList.toggle("panel-open", open);
}

function handleClosed() {
  if (!openSid) return;
  openSid = null;
  setLayoutOpen(false);
  onClosed();
}

async function ensureOpen(sid, doc) {
  const mod = await loadViewer();
  if (!mod) return false;
  if (!instance) instance = new mod.SourcePanel(aside, { onClose: handleClosed });
  if (openSid === sid && instance.isOpen) return true;
  let pages = pagesCache.get(sid);
  if (!pages) {
    try {
      pages = await api.documentPages(sid);
    } catch (err) {
      showError(err);
      return false;
    }
    pagesCache.set(sid, pages);
  }
  setLayoutOpen(true);
  openSid = sid;
  await instance.open({
    sessionId: sid,
    fileUrl: api.documentFileUrl(sid),
    filename: doc.filename,
    pageCount: pages.page_count || doc.pageCount,
    pages: pages.pages,
  });
  return true;
}

/** Rects priority: citation.rects (server) -> GET /locate -> none (the viewer then flashes the whole page honestly). */
async function rectsFor(sid, cite) {
  if (cite.rects?.length) return cite.rects;
  if (!cite.quote && !cite.claim) return [];
  try {
    const located = await api.locate(sid, { page: cite.page, quote: cite.quote, claim: cite.claim });
    return located.rects || [];
  } catch {
    return [];
  }
}

function enqueue(task) {
  queue = queue.then(task, task).catch((err) => showError(err));
  return queue;
}

/** @param {{filename: string, pageCount: number|null}} doc */
export function openCitation(sid, cite, doc) {
  return enqueue(async () => {
    if (!(await ensureOpen(sid, doc))) return;
    const rects = await rectsFor(sid, cite);
    await instance.showCitation({ page: cite.page, rects, quote: cite.quote || null, printedPage: cite.printed_page || null });
  });
}

/** Opens a page without a passage highlight (Sources links, step page links, document pill). */
export function openPage(sid, page, doc, printedPage = null) {
  return enqueue(async () => {
    if (!(await ensureOpen(sid, doc))) return;
    await instance.showCitation({ page, rects: [], quote: null, printedPage, pageOnly: true });
  });
}

export function closePanel() {
  if (instance?.isOpen) instance.close();
  handleClosed();
}

export function dropSessionCache(sid) {
  pagesCache.delete(sid);
}
