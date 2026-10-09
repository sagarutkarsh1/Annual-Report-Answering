// Glue between the chat and the right-hand source panel.  Two occupants share the one <aside>:
//   - the PDF viewer (js/viewer.js, loaded lazily) when the server holds the document;
//   - the source card (js/sourcecard.js) for the static public demo, which ships its answers without the PDF.
// A static demo never calls /document/file, /document/pages or /locate.

import { api } from "./api.js";
import { SourceCard, isStaticDemo } from "./sourcecard.js";
import { toast, showError } from "./toast.js";

let aside = null;
let appEl = null;
let onClosed = () => {};
let viewerModule = null;
let instance = null; // SourcePanel (PDF viewer)
let card = null; // SourceCard
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
    return Boolean(openSid) && Boolean(instance?.isOpen || card?.isOpen);
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
  if (card) {
    card.destroy(); // the viewer takes over the aside
    card = null;
  }
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

/** Static demo: shows citation `ctx.index` of `ctx.cites` on the source card (no document is fetched). */
function showCard(sid, cite, ctx = {}) {
  if (instance) {
    instance.destroy(); // the card takes over the aside
    instance = null;
  }
  if (!card) card = new SourceCard(aside, { onClose: handleClosed });
  const items = ctx.cites?.length ? ctx.cites : [cite];
  setLayoutOpen(true);
  openSid = sid;
  card.open({ items, index: Math.max(0, items.indexOf(cite)), onSelect: ctx.onSelect });
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

/**
 * @param {{filename: string, pageCount: number|null}} doc
 * @param {{cites?: object[], onSelect?: (cite: object, index: number) => void}} [ctx] the other citations of the same answer (source card only)
 */
export function openCitation(sid, cite, doc, ctx = {}) {
  if (isStaticDemo(sid)) return enqueue(async () => showCard(sid, cite, ctx));
  return enqueue(async () => {
    if (!(await ensureOpen(sid, doc))) return;
    const rects = await rectsFor(sid, cite);
    await instance.showCitation({ page: cite.page, rects, quote: cite.quote || null, printedPage: cite.printed_page || null });
  });
}

/**
 * Opens a page without a passage highlight (Sources links, step page links, document pill).
 * On the source card the first cited passage on that page is shown instead (`ctx.cites`), or just the page number.
 */
export function openPage(sid, page, doc, printedPage = null, ctx = {}) {
  if (isStaticDemo(sid)) {
    const cites = ctx.cites?.length ? ctx.cites : [];
    const hit = cites.find((c) => Number(c.page) === Number(page)) || { page, printed_page: printedPage, doc_name: doc.filename, pageOnly: true };
    return enqueue(async () => showCard(sid, hit, { ...ctx, cites: cites.includes(hit) ? cites : [hit] }));
  }
  return enqueue(async () => {
    if (!(await ensureOpen(sid, doc))) return;
    await instance.showCitation({ page, rects: [], quote: null, printedPage, pageOnly: true });
  });
}

export function closePanel() {
  if (instance?.isOpen) instance.close();
  if (card?.isOpen) card.close();
  handleClosed();
}

export function dropSessionCache(sid) {
  pagesCache.delete(sid);
}
