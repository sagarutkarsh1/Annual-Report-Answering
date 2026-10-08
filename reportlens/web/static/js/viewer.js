/**
 * viewer.js - the "source panel": a continuous-scroll PDF viewer with a temporary citation highlight.
 *
 *   const panel = new SourcePanel(document.getElementById("source-panel"), { onClose });
 *   await panel.open({ sessionId, fileUrl, filename, pageCount, pages });   // pages = DocumentPages.pages
 *   await panel.showCitation({ page: 89, rects, quote, printedPage: "87" });
 *
 * Contract: docs/ARCHITECTURE.md section 7.  Design: research/06 (panel header/footer) and research/05 (pdf.js findings).
 *
 * DOM / CSS contract with the caller (the <aside> is owned by the app shell, everything inside it is built here):
 *   .sp-root            always present on rootEl once constructed
 *   .open               present while the panel is open; the shell animates width/transform from it.  close() removes it
 *                       immediately and sets `hidden` ~300 ms later so a close animation can finish.  open() clears `hidden`.
 *   [data-state]        idle | loading | ready | error  (document load state, also drives the spinner / error card)
 *   inert, aria-hidden  set while closed so a collapsed panel is not reachable with Tab.
 *   The root draws no outer border: the shell owns the 1px left border and the width.
 *
 * Behaviour notes
 *   - pdf.js is imported on the first open() only (dynamic import) and is served from ../vendor/pdfjs (same origin, no CDN).
 *   - Pages are absolutely positioned placeholders sized from the server's per-page sizes, so the scrollbar is right before
 *     anything renders; canvases exist only for pages near the viewport (see KEEP_FAR).
 *   - 100 % zoom == fit to width.  Zoom keeps the point at the centre of the viewport anchored.
 *   - Highlight rects are fractions (0..1) of the visible page, origin top-left (models.Rect), drawn as % boxes.
 *   - onClose fires exactly once per open -> closed transition, whether it came from the X button, Esc or close().
 */

const PDFJS_VERSION = "6.4.299"; // cache-busting query for the vendored files; bump with them
const API_BASE = "/api/sessions";

// ---- layout ---------------------------------------------------------------------------------------------------------
const PAD_X = 16; // horizontal gutter around pages at 100 %
const PAD_Y = 12;
const PAGE_GAP = 10;
const MIN_FIT_WIDTH = 160;
const ZOOM_STEPS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 3];
const FIT_ZOOM = 1;
const A4_POINTS = { w: 595.28, h: 841.89 }; // only used when the server sent no page sizes

// ---- rendering ------------------------------------------------------------------------------------------------------
const MAX_CANVAS_PIXELS = 16_000_000; // per canvas, devicePixelRatio is lowered to stay below
const KEEP_FAR = 4; // rendered pages outside the viewport margin that are kept (nearest first)
const LAYOUT_SETTLE_MS = 120; // wait this long after a layout change before re-rendering at the new scale
const RENDER_WAIT_MS = 2500; // showCitation waits at most this long for the target page canvas
const THUMB_PX = 64; // header thumbnail canvas edge (32 css px at 2x)
const RANGE_CHUNK = 256 * 1024;

// ---- citation highlight (spec 8.2) ----------------------------------------------------------------------------------
const HL_FADE_IN = 150;
const HL_HOLD = 2400;
const HL_FADE_OUT = 600;
const HL_REDUCED_MS = 3000; // static outline for users who prefer reduced motion
const HL_TOP_OFFSET = 0.15; // first highlighted line sits this fraction of the viewport below the top edge
const NOTICE_MS = 4000;
const SMOOTH_MAX_VIEWPORTS = 4; // longer jumps are instant: smooth-scrolling across 100 pages only wastes renders
const LOCATE_TIMEOUT_MS = 6000;
const LOCATE_QUOTE_MAX = 800;
const HIDE_DELAY_MS = 300;

const prefersReducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** "National Grid_Annual_Report.pdf" -> "Nationa…Report.pdf" (spec 2.2); full name stays in the tooltip. */
function shortName(name) {
  return name.length > 20 ? `${name.slice(0, 7)}…${name.slice(-10)}` : name;
}

// ---- inline icons (Lucide, ISC licence) -----------------------------------------------------------------------------
const icon = (body, size = 16) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
const ICON = {
  file: icon('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>', 18),
  x: icon('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
  info: icon('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>'),
  addPage: icon('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M12 7v6"/><path d="M9 10h6"/>', 14),
  zoomIn: icon('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6"/><path d="M8 11h6"/>'),
  zoomOut: icon('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/>'),
  prev: icon('<path d="m15 18-6-6 6-6"/>'),
  next: icon('<path d="m9 18 6-6-6-6"/>'),
  alert: icon('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>', 24),
};

// ---- pdf.js loader (once per page load) -----------------------------------------------------------------------------
let pdfjsPromise = null;
function loadPdfjs() {
  if (!pdfjsPromise) {
    const base = new URL("../vendor/pdfjs/", import.meta.url);
    const file = (name) => `${new URL(name, base).href}?v=${PDFJS_VERSION}`;
    pdfjsPromise = import(file("pdf.min.mjs"))
      .then((lib) => {
        lib.GlobalWorkerOptions.workerSrc = file("pdf.worker.min.mjs");
        return { lib, base: base.href };
      })
      .catch((err) => {
        pdfjsPromise = null; // let a later open() / Retry try again
        throw err;
      });
  }
  return pdfjsPromise;
}

/** Map a pdf.js (or network) failure to something a user can act on. */
function describeLoadError(err) {
  const name = err?.name;
  if (name === "PasswordException") {
    return { message: "This PDF is password-protected, so it cannot be previewed.", retryable: false };
  }
  if (name === "InvalidPDFException" || name === "FormatError") {
    return { message: "This file is not a valid PDF, or it is corrupted.", retryable: false };
  }
  if (name === "ResponseException") {
    if (err.missing || err.status === 404) {
      return { message: "The PDF could not be found. It may have been removed from this chat.", retryable: true };
    }
    return { message: `The server could not deliver the PDF (HTTP ${err.status ?? "error"}).`, retryable: true };
  }
  return { message: "The PDF could not be loaded. Check your connection and try again.", retryable: true };
}

/** Per-page sizes in PDF points; tolerant of a missing / short / malformed `pages` array. */
function normalizeSizes(pages, count) {
  const list = Array.isArray(pages) ? pages : [];
  const valid = (p) => Number(p?.width ?? p?.w) > 0 && Number(p?.height ?? p?.h) > 0;
  const first = list.find(valid);
  const fallback = first ? { w: Number(first.width ?? first.w), h: Number(first.height ?? first.h) } : A4_POINTS;
  return Array.from({ length: count }, (_, i) => {
    const p = list[i];
    const printed = p?.printed_page == null || p.printed_page === "" ? null : String(p.printed_page);
    return valid(p)
      ? { w: Number(p.width ?? p.w), h: Number(p.height ?? p.h), printed }
      : { ...fallback, printed };
  });
}

/** Keep only usable rects, clamped to the page. */
function cleanRects(rects) {
  if (!Array.isArray(rects)) return [];
  const out = [];
  for (const r of rects) {
    const [x, y, w, h] = [r?.x, r?.y, r?.w, r?.h].map(Number);
    if (![x, y, w, h].every(Number.isFinite)) continue;
    const x0 = clamp(x, 0, 1);
    const y0 = clamp(y, 0, 1);
    const x1 = clamp(x + w, 0, 1);
    const y1 = clamp(y + h, 0, 1);
    if (x1 > x0 && y1 > y0) out.push({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
  }
  return out;
}

export class SourcePanel {
  #root;
  #onClose;
  #apiBase;
  #ac = new AbortController(); // every listener is registered with this signal
  #el = {};
  #io;
  #ro;
  #destroyed = false;
  #isOpen = false;
  #hideTimer = 0;
  #savedScroll = null;

  // document
  #sessionId = null;
  #fileUrl = null;
  #filename = "";
  #pageCountHint = 0;
  #sizeHints = null;
  #lib = null;
  #task = null;
  #doc = null;
  #state = "idle";
  #loadSeq = 0;
  #loadPromise = Promise.resolve(false);

  // pages / layout
  #pages = []; // {n, w, h, printed, el, scale, top, height, canvas, text, hl, proxy, renderedScale, failedScale, token, task, textTask}
  #visible = new Set();
  #zoom = FIT_ZOOM;
  #laidOnce = false;
  #lastLayoutAt = 0;
  #current = 1;
  #priority = [];
  #pinned = 0;
  #pumping = false;
  #renderTimer = 0;
  #waiters = new Map();
  #rafScroll = 0;
  #scrollSeq = 0;
  #targetLock = null; // {n, fy, until}: keeps the cited spot in place while the layout is still moving
  #wheelAcc = 0;
  #wheelAt = 0;

  // citation
  #citeSeq = 0;
  #hlCleanup = null;
  #printedOverride = null;
  #locateCtl = null;
  #noticeTimer = 0;

  /** @param {HTMLElement} rootEl the <aside>.  @param {{onClose?: () => void, apiBase?: string}} [opts] */
  constructor(rootEl, { onClose, apiBase = API_BASE } = {}) {
    if (!(rootEl instanceof Element)) throw new TypeError("SourcePanel needs a root element");
    this.#root = rootEl;
    this.#onClose = typeof onClose === "function" ? onClose : () => {};
    this.#apiBase = apiBase.replace(/\/$/, "");
    this.#build();
    rootEl.hidden = true; // stays hidden until open()
    this.#applyClosedState();
  }

  get isOpen() {
    return this.#isOpen;
  }

  /** 1-based page currently shown in the footer. */
  get currentPage() {
    return this.#current;
  }

  // =================================================================================================== public API

  /**
   * Show the panel and load the document.  Idempotent for the same session + file.  Resolves true when the document is
   * ready, false when it failed (the panel then shows the error state; nothing is thrown for load failures).
   */
  async open({ sessionId, fileUrl, filename = "", pageCount = 0, pages = null } = {}) {
    if (this.#destroyed) return false;
    if (!sessionId || !fileUrl) throw new TypeError("SourcePanel.open() needs sessionId and fileUrl");
    this.#show();
    const same = sessionId === this.#sessionId && fileUrl === this.#fileUrl && this.#state !== "error";
    if (same) return this.#loadPromise;

    this.#teardownDocument();
    this.#sessionId = sessionId;
    this.#fileUrl = fileUrl;
    this.#filename = filename || "document.pdf";
    this.#pageCountHint = Math.max(0, Math.floor(Number(pageCount)) || (Array.isArray(pages) ? pages.length : 0));
    this.#sizeHints = pages;
    this.#applyHeader();
    this.#buildPages(normalizeSizes(pages, Math.max(1, this.#pageCountHint)));
    this.#loadPromise = this.#load();
    return this.#loadPromise;
  }

  /**
   * Scroll to a cited passage and flash it.  `rects` are fractions of the visible page (top-left origin).  With no rects
   * and a `quote`, the server is asked to locate it; with nothing at all the whole page border flashes and a notice says so.
   * `pageOnly: true` (a plain page link, no passage was ever claimed) flashes the page without that notice.
   * Resolves once the highlight has started; a newer call supersedes an older one.
   */
  async showCitation({ page, rects, quote, printedPage, pageOnly = false } = {}) {
    if (this.#destroyed) return;
    const seq = ++this.#citeSeq;
    this.#clearHighlight();
    if (!this.#sessionId) {
      console.warn("[source-panel] showCitation() called before open()");
      return;
    }
    this.#show();
    const ready = await this.#loadPromise;
    if (seq !== this.#citeSeq || !ready) return; // superseded, or the error state is already on screen

    const count = this.#pages.length;
    const asked = Math.round(Number(page));
    let n = Number.isFinite(asked) ? clamp(asked, 1, count) : this.#current;
    let boxes = cleanRects(rects);

    const needsLocate = !boxes.length && typeof quote === "string" && quote.trim();
    const [located] = await Promise.all([needsLocate ? this.#locate(n, quote, seq) : null, this.#settleLayout()]);
    if (seq !== this.#citeSeq) return;
    if (located) {
      const lp = Math.round(Number(located.page));
      if (Number.isFinite(lp) && lp >= 1 && lp <= count) n = lp;
      boxes = cleanRects(located.rects);
    }

    this.#printedOverride = printedPage ? { page: n, label: String(printedPage) } : null;
    this.#pinned = n;
    this.#requestRender(n);
    await this.#goTo(n, boxes.length ? Math.min(...boxes.map((b) => b.y)) : 0, { smooth: true, topOffset: boxes.length });
    if (seq !== this.#citeSeq) return;
    await Promise.race([this.#renderNow(n), wait(RENDER_WAIT_MS)]);
    if (seq !== this.#citeSeq || this.#destroyed) return;

    if (boxes.length) {
      this.#drawHighlight(n, boxes, false);
      this.#announce(`Showing page ${n}, passage highlighted.`);
    } else {
      this.#drawHighlight(n, [{ x: 0, y: 0, w: 1, h: 1 }], true);
      if (pageOnly) {
        this.#announce(`Showing page ${n}.`);
      } else {
        this.#notify(`Passage not located - showing page ${n}`);
        this.#announce(`Passage not located. Showing page ${n}.`);
      }
    }
  }

  /** Close the panel (keeps the loaded document so re-opening is instant). */
  close() {
    if (this.#destroyed || !this.#isOpen) return;
    this.#isOpen = false;
    this.#savedScroll = { top: this.#el.body.scrollTop, left: this.#el.body.scrollLeft };
    this.#citeSeq++;
    this.#clearHighlight();
    this.#closeInfo();
    this.#locateCtl?.abort();
    this.#root.classList.remove("open");
    this.#applyClosedState();
    this.#hideTimer = setTimeout(() => {
      if (!this.#isOpen) this.#root.hidden = true;
    }, HIDE_DELAY_MS);
    try {
      this.#onClose();
    } catch (err) {
      console.error("[source-panel] onClose handler failed", err);
    }
  }

  /** Move keyboard focus to the panel title (programmatic focus target for the app shell, spec 8.1 step 5). */
  focus() {
    this.#el.title?.focus({ preventScroll: true });
  }

  /** Release the document, observers and listeners.  The instance is unusable afterwards; onClose is not called. */
  destroy() {
    if (this.#destroyed) return;
    this.#destroyed = true;
    this.#isOpen = false;
    this.#ac.abort();
    this.#io.disconnect();
    this.#ro.disconnect();
    clearTimeout(this.#hideTimer);
    clearTimeout(this.#renderTimer);
    clearTimeout(this.#noticeTimer);
    this.#clearHighlight();
    this.#locateCtl?.abort();
    this.#teardownDocument();
    this.#root.replaceChildren();
    this.#root.classList.remove("sp-root", "open");
    this.#root.removeAttribute("data-state");
    this.#root.removeAttribute("aria-hidden");
    this.#root.inert = false;
    this.#el = {};
  }

  // =================================================================================================== construction

  #build() {
    const root = this.#root;
    root.classList.add("sp-root");
    if (!root.hasAttribute("aria-label")) root.setAttribute("aria-label", "Source document");
    root.dataset.state = "idle";
    root.innerHTML = `
      <header class="sp-head">
        <div class="sp-thumb" aria-hidden="true">${ICON.file}</div>
        <div class="sp-title" tabindex="-1">
          <div class="sp-name"></div>
          <div class="sp-sub">Document</div>
        </div>
        <button type="button" class="sp-btn sp-close" aria-label="Close source panel" title="Close (Esc)">${ICON.x}</button>
      </header>
      <div class="sp-viewport">
        <div class="sp-body" tabindex="0" role="region" aria-label="PDF pages"><div class="sp-pages"></div></div>
        <div class="sp-loading" aria-hidden="true"><span class="sp-spinner"></span><span>Loading document…</span></div>
        <div class="sp-error" role="alert">
          <span class="sp-error-icon">${ICON.alert}</span>
          <strong class="sp-error-title">Couldn't open the document</strong>
          <p class="sp-error-msg"></p>
          <button type="button" class="sp-retry">Retry</button>
        </div>
        <div class="sp-notice" role="status" aria-live="polite"></div>
      </div>
      <footer class="sp-foot">
        <div class="sp-foot-left">
          <button type="button" class="sp-btn sp-info-btn" aria-label="Document information" aria-expanded="false">${ICON.info}</button>
          <button type="button" class="sp-btn sp-add" aria-disabled="true" aria-label="Add Page to Chat (coming soon)" data-tip="Coming soon">${ICON.addPage}<span class="sp-add-label">Add Page to Chat</span></button>
        </div>
        <div class="sp-pager">
          <button type="button" class="sp-btn sp-prev" aria-label="Previous page" title="Previous page (PageUp)">${ICON.prev}</button>
          <span class="sp-pager-text">Page</span>
          <input class="sp-page-input" type="text" inputmode="numeric" autocomplete="off" aria-label="Page number" value="1">
          <span class="sp-total">/ 1</span>
          <span class="sp-printed" hidden></span>
          <button type="button" class="sp-btn sp-next" aria-label="Next page" title="Next page (PageDown)">${ICON.next}</button>
        </div>
        <div class="sp-zoom">
          <button type="button" class="sp-btn sp-zoom-out" aria-label="Zoom out" title="Zoom out">${ICON.zoomOut}</button>
          <button type="button" class="sp-zoom-val" title="Reset to fit width" aria-label="Zoom level, press to fit width">100%</button>
          <button type="button" class="sp-btn sp-zoom-in" aria-label="Zoom in" title="Zoom in">${ICON.zoomIn}</button>
        </div>
        <div class="sp-info" role="group" aria-label="Document information" hidden>
          <div class="sp-info-name"></div>
          <div class="sp-info-pages"></div>
          <a class="sp-info-link" target="_blank" rel="noopener">Open PDF in a new tab</a>
        </div>
      </footer>
      <span class="sp-sr" role="status" aria-live="polite"></span>`;

    const q = (sel) => root.querySelector(sel);
    this.#el = {
      title: q(".sp-title"), name: q(".sp-name"), thumb: q(".sp-thumb"),
      body: q(".sp-body"), pages: q(".sp-pages"), errMsg: q(".sp-error-msg"), retry: q(".sp-retry"), notice: q(".sp-notice"),
      infoBtn: q(".sp-info-btn"), info: q(".sp-info"), add: q(".sp-add"),
      prev: q(".sp-prev"), next: q(".sp-next"), input: q(".sp-page-input"), total: q(".sp-total"), printed: q(".sp-printed"),
      zoomOut: q(".sp-zoom-out"), zoomIn: q(".sp-zoom-in"), zoomVal: q(".sp-zoom-val"), sr: q(".sp-sr"),
    };
    const { signal } = this.#ac;
    const on = (target, type, handler, opts = {}) => target.addEventListener(type, handler, { ...opts, signal });
    const e = this.#el;

    on(q(".sp-close"), "click", () => this.close());
    on(e.retry, "click", () => this.#retry());
    on(e.prev, "click", () => this.#goTo(this.#current - 1, 0, { smooth: true }));
    on(e.next, "click", () => this.#goTo(this.#current + 1, 0, { smooth: true }));
    on(e.zoomOut, "click", () => this.#stepZoom(-1));
    on(e.zoomIn, "click", () => this.#stepZoom(1));
    on(e.zoomVal, "click", () => this.#setZoom(FIT_ZOOM));
    on(e.add, "click", (ev) => ev.preventDefault()); // not implemented yet; kept aria-disabled so the tooltip still works
    on(e.infoBtn, "click", () => this.#toggleInfo());
    on(e.input, "focus", () => e.input.select());
    on(e.input, "blur", () => this.#syncFooter());
    on(e.input, "input", () => (e.input.value = e.input.value.replace(/\D/g, "").slice(0, 6)));
    on(e.input, "keydown", (ev) => {
      if (ev.key !== "Enter") return;
      const n = parseInt(e.input.value, 10);
      if (Number.isFinite(n)) this.#goTo(clamp(n, 1, this.#pages.length), 0, { smooth: true });
      else this.#syncFooter();
      e.input.select();
    });
    on(e.body, "scroll", () => this.#onScroll(), { passive: true });
    on(e.body, "wheel", (ev) => this.#onWheel(ev), { passive: false });
    for (const type of ["touchstart", "pointerdown"]) on(e.body, type, () => (this.#targetLock = null), { passive: true });
    on(root, "keydown", (ev) => this.#onKey(ev));
    on(document, "pointerdown", (ev) => {
      if (!e.info.hidden && !e.info.contains(ev.target) && !e.infoBtn.contains(ev.target)) this.#closeInfo();
    });

    this.#ro = new ResizeObserver(() => this.#relayout());
    this.#ro.observe(e.body);
    this.#io = new IntersectionObserver(
      (entries) => {
        for (const en of entries) {
          const n = Number(en.target.dataset.page);
          if (en.isIntersecting) this.#visible.add(n);
          else this.#visible.delete(n);
        }
        this.#scheduleRender();
      },
      { root: e.body, rootMargin: "100% 0px" }, // about one viewport above and below == the +-1 page the spec asks for
    );
  }

  #applyClosedState() {
    this.#root.inert = true;
    this.#root.setAttribute("aria-hidden", "true");
  }

  #show() {
    clearTimeout(this.#hideTimer);
    if (this.#isOpen) return;
    this.#isOpen = true;
    const root = this.#root;
    root.hidden = false;
    root.inert = false;
    root.removeAttribute("aria-hidden");
    void root.offsetWidth; // flush the un-hidden state so the shell's width transition on `.open` actually runs
    root.classList.add("open");
    const { body } = this.#el;
    if (this.#savedScroll && body.scrollTop === 0) {
      body.scrollTop = this.#savedScroll.top; // display:none while closed resets the scroll offset
      body.scrollLeft = this.#savedScroll.left;
    }
    this.#relayout();
  }

  #applyHeader() {
    const e = this.#el;
    e.name.textContent = shortName(this.#filename);
    e.name.title = this.#filename;
    e.thumb.innerHTML = ICON.file;
    e.info.querySelector(".sp-info-name").textContent = this.#filename;
    e.info.querySelector(".sp-info-link").href = this.#fileUrl;
  }

  #setState(state, { message = "", retryable = true } = {}) {
    this.#state = state;
    this.#root.dataset.state = state;
    this.#el.errMsg.textContent = message;
    this.#el.retry.hidden = !retryable;
  }

  // =================================================================================================== document lifecycle

  async #load() {
    const seq = ++this.#loadSeq;
    this.#setState("loading");
    try {
      const { lib, base } = await loadPdfjs();
      if (seq !== this.#loadSeq) return false;
      this.#lib = lib;
      const task = lib.getDocument({
        url: this.#fileUrl,
        rangeChunkSize: RANGE_CHUNK,
        disableStream: true, // disableAutoFetch is only honoured together with this
        disableAutoFetch: true, // never prefetch the rest of a 300 page report
        cMapUrl: `${base}cmaps/`,
        standardFontDataUrl: `${base}standard_fonts/`,
        wasmUrl: `${base}wasm/`,
        iccUrl: `${base}iccs/`,
      });
      this.#task = task;
      const doc = await task.promise;
      if (seq !== this.#loadSeq) return false; // teardown already destroyed this task
      this.#doc = doc;
    } catch (err) {
      if (seq !== this.#loadSeq) return false;
      console.warn("[source-panel] could not load PDF:", err?.message ?? err);
      this.#setState("error", describeLoadError(err));
      return false;
    }
    if (this.#doc.numPages !== this.#pages.length) {
      // the server's page count disagreed with the file: trust the file
      this.#buildPages(normalizeSizes(this.#sizeHints, this.#doc.numPages));
    }
    this.#setState("ready");
    this.#syncFooter();
    this.#relayout();
    this.#scheduleRender();
    this.#renderThumb(seq);
    return true;
  }

  #retry() {
    if (!this.#sessionId) return;
    this.#teardownDocument({ keepPages: true });
    this.#loadPromise = this.#load();
  }

  /** Destroy the pdf.js document and drop every canvas; keeps the DOM chrome.  Safe to call repeatedly. */
  #teardownDocument({ keepPages = false } = {}) {
    this.#loadSeq++; // invalidates any in-flight load / render
    this.#citeSeq++;
    clearTimeout(this.#renderTimer);
    this.#renderTimer = 0;
    this.#clearHighlight();
    for (const rec of this.#pages) this.#release(rec);
    this.#resolveWaiters(true);
    this.#visible.clear();
    this.#priority = [];
    this.#pinned = 0;
    this.#targetLock = null;
    this.#printedOverride = null;
    this.#io?.disconnect();
    if (!keepPages) {
      this.#pages = [];
      this.#el.pages?.replaceChildren();
      this.#laidOnce = false;
      this.#current = 1;
      this.#zoom = FIT_ZOOM;
      if (this.#el.body) this.#el.body.scrollTop = 0;
    } else {
      for (const rec of this.#pages) this.#io?.observe(rec.el);
    }
    const task = this.#task;
    this.#task = null;
    this.#doc = null;
    Promise.resolve(task?.destroy?.()).catch(() => {});
    this.#lib?.TextLayer?.cleanup?.();
    this.#setState("idle");
  }

  async #renderThumb(seq) {
    try {
      const page = await this.#doc.getPage(1);
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: THUMB_PX / Math.min(base.width, base.height) });
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = THUMB_PX; // crops to a square from the top-left of the cover
      await page.render({ canvas, viewport }).promise;
      if (seq === this.#loadSeq) this.#el.thumb.replaceChildren(canvas);
    } catch {
      /* the file icon stays: the thumbnail is decoration */
    }
  }

  // =================================================================================================== layout & zoom

  #buildPages(sizes) {
    this.#io.disconnect();
    this.#visible.clear();
    const frag = document.createDocumentFragment();
    this.#pages = sizes.map((s, i) => {
      const el = document.createElement("div");
      el.className = "sp-page sp-skel";
      el.dataset.page = String(i + 1);
      el.setAttribute("role", "group");
      el.setAttribute("aria-label", `Page ${i + 1}`);
      frag.appendChild(el);
      return {
        n: i + 1, w: s.w, h: s.h, printed: s.printed, el,
        scale: 1, top: 0, height: 0, renderedScale: null, failedScale: null,
        canvas: null, text: null, hl: null, proxy: null, token: null, task: null, textTask: null,
      };
    });
    this.#el.pages.replaceChildren(frag);
    for (const rec of this.#pages) this.#io.observe(rec.el);
    this.#laidOnce = false;
    this.#el.total.textContent = `/ ${sizes.length}`;
    this.#el.info.querySelector(".sp-info-pages").textContent = `${sizes.length} page${sizes.length === 1 ? "" : "s"}`;
    this.#root.style.setProperty("--sp-digits", String(String(sizes.length).length));
    this.#relayout();
  }

  /** Compute placeholder boxes from the current width + zoom.  Returns false while the panel has no width (hidden / animating). */
  #layout() {
    const body = this.#el.body;
    const width = body.clientWidth;
    if (!width || !this.#pages.length) return false;
    const fit = Math.max(MIN_FIT_WIDTH, width - 2 * PAD_X);
    let top = PAD_Y;
    let widest = 0;
    for (const rec of this.#pages) {
      rec.scale = (fit / rec.w) * this.#zoom;
      rec.top = top;
      rec.height = rec.h * rec.scale;
      top += rec.height + PAGE_GAP;
      widest = Math.max(widest, rec.w * rec.scale);
    }
    const contentWidth = Math.max(width, widest + 2 * PAD_X);
    for (const rec of this.#pages) {
      const w = rec.w * rec.scale;
      const s = rec.el.style;
      s.width = `${w}px`;
      s.height = `${rec.height}px`;
      s.top = `${rec.top}px`;
      s.left = `${(contentWidth - w) / 2}px`;
    }
    const s = this.#el.pages.style;
    s.width = `${contentWidth}px`;
    s.height = `${top - PAGE_GAP + PAD_Y}px`;
    this.#laidOnce = true;
    this.#lastLayoutAt = performance.now();
    return true;
  }

  /** Re-layout keeping the content at the centre of the viewport where it was. */
  #relayout() {
    if (this.#destroyed) return;
    const anchor = this.#laidOnce ? this.#captureAnchor() : null;
    if (!this.#layout()) return;
    if (anchor) this.#restoreAnchor(anchor);
    this.#reapplyTarget();
    this.#syncFooter();
    this.#scheduleRender();
  }

  /** Remember what is at the centre of the viewport.  At the very top nothing is anchored: while the panel is still
   *  animating open the layout is tiny, and a centre point past the end of that layout would scroll the page to the bottom. */
  #captureAnchor() {
    const b = this.#el.body;
    const cx = b.scrollWidth > b.clientWidth ? (b.scrollLeft + b.clientWidth / 2) / b.scrollWidth : 0.5;
    if (b.scrollTop <= 0) return { rec: null, frac: 0, cx };
    const yc = b.scrollTop + b.clientHeight / 2;
    const rec = this.#pages[this.#indexAt(yc)];
    return { rec, frac: clamp((yc - rec.top) / rec.height, 0, 1), cx };
  }

  #restoreAnchor({ rec, frac, cx }) {
    const b = this.#el.body;
    b.scrollTop = rec ? rec.top + frac * rec.height - b.clientHeight / 2 : 0;
    b.scrollLeft = cx * b.scrollWidth - b.clientWidth / 2;
  }

  /** Index (0-based) of the page whose box contains y, or the page above when y falls in a gap. */
  #indexAt(y) {
    let lo = 0;
    let hi = this.#pages.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.#pages[mid].top <= y) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  #setZoom(zoom) {
    const z = clamp(zoom, ZOOM_STEPS[0], ZOOM_STEPS.at(-1));
    if (Math.abs(z - this.#zoom) < 1e-6 || !this.#pages.length) return;
    const anchor = this.#laidOnce ? this.#captureAnchor() : null;
    this.#zoom = z;
    this.#targetLock = null;
    if (!this.#layout()) return;
    if (anchor) this.#restoreAnchor(anchor);
    this.#syncFooter();
    this.#scheduleRender();
  }

  #stepZoom(dir) {
    const i = ZOOM_STEPS.indexOf(this.#zoom);
    this.#setZoom(ZOOM_STEPS[clamp((i < 0 ? ZOOM_STEPS.indexOf(FIT_ZOOM) : i) + dir, 0, ZOOM_STEPS.length - 1)]);
  }

  async #settleLayout() {
    const started = performance.now();
    while (performance.now() - started < 800) {
      if (this.#el.body.clientWidth > 0 && this.#laidOnce && performance.now() - this.#lastLayoutAt > 80) return;
      await wait(16);
    }
  }

  // =================================================================================================== navigation

  #onScroll() {
    if (this.#rafScroll) return;
    this.#rafScroll = requestAnimationFrame(() => {
      this.#rafScroll = 0;
      if (this.#destroyed || !this.#pages.length) return;
      if (this.#targetLock && performance.now() < this.#targetLock.until) return; // a programmatic jump owns the indicator
      const b = this.#el.body;
      const n = this.#indexAt(b.scrollTop + b.clientHeight * 0.35) + 1;
      if (n !== this.#current) {
        this.#current = n;
        this.#syncFooter();
      }
    });
  }

  #onWheel(ev) {
    if (!ev.ctrlKey) {
      this.#targetLock = null;
      return;
    }
    ev.preventDefault(); // Ctrl+wheel (and trackpad pinch) zooms the PDF instead of the whole page
    const now = performance.now();
    this.#wheelAcc = now - this.#wheelAt > 300 ? 0 : this.#wheelAcc;
    this.#wheelAcc += ev.deltaY;
    if (Math.abs(this.#wheelAcc) < 40 || now - this.#wheelAt < 90) return;
    this.#stepZoom(this.#wheelAcc < 0 ? 1 : -1);
    this.#wheelAcc = 0;
    this.#wheelAt = now;
  }

  #onKey(ev) {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      if (!this.#el.info.hidden) {
        this.#closeInfo();
        this.#el.infoBtn.focus();
      } else {
        this.close();
      }
      return;
    }
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.target === this.#el.input) return;
    const n = this.#current;
    const last = this.#pages.length;
    const handlers = {
      PageDown: () => this.#goTo(Math.min(n + 1, last), 0, { smooth: true }),
      PageUp: () => this.#goTo(Math.max(n - 1, 1), 0, { smooth: true }),
      Home: () => this.#goTo(1, 0, { smooth: true }),
      End: () => this.#goTo(last, 0, { smooth: true }),
    };
    if (ev.target === this.#el.body) {
      Object.assign(handlers, {
        "+": () => this.#stepZoom(1),
        "=": () => this.#stepZoom(1),
        "-": () => this.#stepZoom(-1),
        0: () => this.#setZoom(FIT_ZOOM),
      });
    }
    const handler = Object.hasOwn(handlers, ev.key) ? handlers[ev.key] : null;
    if (handler && this.#pages.length) {
      ev.preventDefault();
      handler();
    }
  }

  /**
   * Scroll so page `n` is in view.  `fy` is a fraction down the page that should land `topOffset`-ed below the top edge
   * (the citation case) or at the top edge (plain page navigation).  Resolves when the scroll has settled.
   */
  async #goTo(n, fy, { smooth = true, topOffset = false } = {}) {
    if (!this.#pages.length) return;
    n = clamp(Math.round(n), 1, this.#pages.length);
    const ms = (smooth ? 1200 : 400) + (topOffset ? HL_FADE_IN + HL_HOLD + HL_FADE_OUT : 0);
    // with no width yet (panel still opening) the clock starts at the first real layout instead of now
    const pending = !this.#el.body.clientWidth;
    this.#targetLock = { n, fy, topOffset, ms, pending, until: pending ? Infinity : performance.now() + ms };
    this.#current = n;
    this.#syncFooter();
    this.#requestRender(n);
    await this.#scrollTo(this.#targetY(this.#targetLock), smooth);
  }

  #targetY({ n, fy, topOffset }) {
    const rec = this.#pages[n - 1];
    const b = this.#el.body;
    return topOffset
      ? rec.top + fy * rec.height - HL_TOP_OFFSET * b.clientHeight
      : rec.top - (n === 1 ? PAD_Y : PAGE_GAP / 2) + fy * rec.height;
  }

  /** While the layout is still settling (panel animating open, window resized) keep the cited spot where it was put. */
  #reapplyTarget() {
    const lock = this.#targetLock;
    if (!lock || !this.#laidOnce) return;
    if (lock.pending) {
      lock.pending = false;
      lock.until = performance.now() + lock.ms;
    }
    if (performance.now() >= lock.until) return;
    this.#el.body.scrollTop = Math.max(0, this.#targetY(lock));
  }

  #scrollTo(y, smooth) {
    const b = this.#el.body;
    const top = clamp(y, 0, Math.max(0, b.scrollHeight - b.clientHeight));
    const distance = Math.abs(top - b.scrollTop);
    if (distance < 1) return Promise.resolve();
    const animate = smooth && !prefersReducedMotion() && distance < SMOOTH_MAX_VIEWPORTS * b.clientHeight;
    const seq = ++this.#scrollSeq;
    return new Promise((resolve) => {
      const onEnd = () => seq === this.#scrollSeq && done();
      const done = () => {
        b.removeEventListener("scrollend", onEnd);
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(done, animate ? 900 : 60);
      if (animate) b.addEventListener("scrollend", onEnd);
      b.scrollTo({ top, behavior: animate ? "smooth" : "auto" });
    });
  }

  // =================================================================================================== footer

  #printedLabel(n) {
    if (this.#printedOverride?.page === n) return this.#printedOverride.label;
    return this.#pages[n - 1]?.printed ?? null;
  }

  #syncFooter() {
    const e = this.#el;
    if (!e.input) return;
    const count = this.#pages.length;
    if (document.activeElement !== e.input) e.input.value = String(this.#current);
    const printed = this.#printedLabel(this.#current);
    const showPrinted = printed && printed !== String(this.#current);
    e.printed.hidden = !showPrinted;
    e.printed.textContent = showPrinted ? `· printed p. ${printed}` : "";
    e.prev.disabled = this.#current <= 1;
    e.next.disabled = this.#current >= count;
    e.zoomVal.textContent = `${Math.round(this.#zoom * 100)}%`;
    e.zoomOut.disabled = this.#zoom <= ZOOM_STEPS[0] + 1e-6;
    e.zoomIn.disabled = this.#zoom >= ZOOM_STEPS.at(-1) - 1e-6;
  }

  #toggleInfo() {
    const open = this.#el.info.hidden;
    this.#el.info.hidden = !open;
    this.#el.infoBtn.setAttribute("aria-expanded", String(open));
  }

  #closeInfo() {
    this.#el.info.hidden = true;
    this.#el.infoBtn.setAttribute("aria-expanded", "false");
  }

  #announce(text) {
    this.#el.sr.textContent = "";
    // a changed text node is what makes screen readers repeat an identical message
    requestAnimationFrame(() => (this.#el.sr.textContent = text));
  }

  #notify(text) {
    const n = this.#el.notice;
    n.textContent = text;
    n.classList.add("show");
    clearTimeout(this.#noticeTimer);
    this.#noticeTimer = setTimeout(() => n.classList.remove("show"), NOTICE_MS);
  }

  // =================================================================================================== lazy rendering

  #requestRender(n) {
    if (!this.#priority.includes(n)) this.#priority.unshift(n);
    this.#scheduleRender();
  }

  #needsRender(rec) {
    const stale = (s) => s == null || Math.abs(s - rec.scale) > rec.scale * 0.002;
    return stale(rec.renderedScale) && stale(rec.failedScale);
  }

  /** Resolves once page n shows a canvas at the current scale (or immediately if it already does). */
  #renderNow(n) {
    const rec = this.#pages[n - 1];
    if (!rec || !this.#doc || !this.#needsRender(rec)) return Promise.resolve();
    this.#requestRender(n);
    return new Promise((resolve) => {
      const list = this.#waiters.get(n) ?? [];
      list.push(resolve);
      this.#waiters.set(n, list);
    });
  }

  #resolveWaiters(all = false, n = 0) {
    for (const [key, list] of [...this.#waiters]) {
      if (!all && key !== n) continue;
      this.#waiters.delete(key);
      for (const resolve of list) resolve();
    }
  }

  #scheduleRender() {
    if (this.#renderTimer || this.#destroyed || !this.#doc) return;
    const delay = Math.max(0, LAYOUT_SETTLE_MS - (performance.now() - this.#lastLayoutAt));
    this.#renderTimer = setTimeout(() => {
      this.#renderTimer = 0;
      this.#evict();
      this.#pump();
    }, delay);
  }

  #nextToRender() {
    const rec = this.#priority.map((n) => this.#pages[n - 1]).find((r) => r && this.#needsRender(r));
    if (rec) return rec;
    const b = this.#el.body;
    const mid = b.scrollTop + b.clientHeight / 2;
    let best = null;
    let bestDist = Infinity;
    for (const n of this.#visible) {
      const r = this.#pages[n - 1];
      if (!r || !this.#needsRender(r)) continue;
      const dist = Math.abs(r.top + r.height / 2 - mid);
      if (dist < bestDist) [best, bestDist] = [r, dist];
    }
    return best;
  }

  /** One page at a time: pdf.js paints on the main thread, so parallel renders would only delay the page the user is looking at. */
  async #pump() {
    if (this.#pumping) return;
    this.#pumping = true;
    try {
      for (let rec = this.#nextToRender(); rec && this.#doc && !this.#destroyed; rec = this.#nextToRender()) {
        if (performance.now() - this.#lastLayoutAt < LAYOUT_SETTLE_MS) break; // layout moved again: scheduleRender will retry
        await this.#renderPage(rec);
        this.#priority = this.#priority.filter((n) => this.#needsRender(this.#pages[n - 1]) && n !== rec.n);
        this.#evict();
      }
    } finally {
      this.#pumping = false;
    }
    if (this.#nextToRender()) this.#scheduleRender();
  }

  async #renderPage(rec) {
    const doc = this.#doc;
    const seq = this.#loadSeq;
    const token = (rec.token = Symbol("render"));
    const stale = () => rec.token !== token || seq !== this.#loadSeq;
    try {
      const page = await doc.getPage(rec.n);
      if (stale()) return;
      this.#adoptRealSize(rec, page);
      const scale = rec.scale;
      const viewport = page.getViewport({ scale });
      const dpr = Math.min(window.devicePixelRatio || 1, Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.floor(viewport.width * dpr));
      canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
      const task = page.render({ canvas, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined });
      rec.task = task;
      await task.promise;
      if (stale()) return;
      // swap only after the new bitmap is complete so a zoom never flashes an empty page
      rec.canvas?.remove();
      rec.canvas = canvas;
      rec.proxy = page;
      rec.renderedScale = scale;
      rec.failedScale = null;
      rec.el.prepend(canvas);
      rec.el.classList.remove("sp-skel", "sp-page-error");
      this.#buildTextLayer(rec, page, viewport, token);
    } catch (err) {
      if (err?.name === "RenderingCancelledException" || err?.name === "AbortException" || stale()) return;
      console.warn(`[source-panel] page ${rec.n} failed to render:`, err?.message ?? err);
      rec.failedScale = rec.scale; // do not retry in a loop at this scale
      rec.el.classList.add("sp-page-error");
    } finally {
      rec.task = null;
      this.#resolveWaiters(false, rec.n);
    }
  }

  /** The server's size list should equal pdf.js' (rotation + CropBox); when it does not, trust pdf.js and re-flow. */
  #adoptRealSize(rec, page) {
    const v1 = page.getViewport({ scale: 1 });
    if (Math.abs(v1.width - rec.w) <= 1.5 && Math.abs(v1.height - rec.h) <= 1.5) return;
    rec.w = v1.width;
    rec.h = v1.height;
    this.#relayout();
  }

  /** Selectable text.  Runs after the canvas is on screen and never blocks the render pump. */
  async #buildTextLayer(rec, page, viewport, token) {
    const div = document.createElement("div");
    div.className = "textLayer";
    div.style.setProperty("--total-scale-factor", String(viewport.scale));
    div.style.setProperty("--scale-round-x", "1px");
    div.style.setProperty("--scale-round-y", "1px");
    rec.textTask?.cancel();
    try {
      const layer = new this.#lib.TextLayer({ textContentSource: page.streamTextContent(), container: div, viewport });
      rec.textTask = layer;
      await layer.render();
      if (rec.token !== token) return;
      rec.text?.remove();
      rec.text = div;
      rec.el.appendChild(div);
    } catch (err) {
      if (err?.name !== "AbortException" && err?.name !== "AbortError") {
        console.warn(`[source-panel] text layer unavailable for page ${rec.n}:`, err?.message ?? err);
      }
    } finally {
      if (rec.textTask && rec.token === token) rec.textTask = null;
    }
  }

  /** Free the canvas / text layer of a page and make it a placeholder again. */
  #release(rec) {
    rec.token = Symbol("released");
    try {
      rec.task?.cancel();
      rec.textTask?.cancel();
    } catch {
      /* already finished */
    }
    rec.task = rec.textTask = null;
    if (rec.canvas) rec.canvas.width = rec.canvas.height = 0; // returns the bitmap memory immediately
    rec.canvas?.remove();
    rec.text?.remove();
    rec.canvas = rec.text = null;
    rec.renderedScale = rec.failedScale = null;
    rec.el.classList.add("sp-skel");
    rec.el.classList.remove("sp-page-error");
    try {
      rec.proxy?.cleanup();
    } catch {
      /* the document may already be gone */
    }
    rec.proxy = null;
  }

  /** Keep canvases for the visible band, the pinned citation page and the KEEP_FAR nearest others; free the rest. */
  #evict() {
    const b = this.#el.body;
    const mid = b.scrollTop + b.clientHeight / 2;
    const far = this.#pages
      .filter((r) => r.canvas && !this.#visible.has(r.n) && r.n !== this.#pinned && !this.#priority.includes(r.n))
      .sort((a, c) => Math.abs(a.top - mid) - Math.abs(c.top - mid));
    for (const rec of far.slice(KEEP_FAR)) this.#release(rec);
  }

  // =================================================================================================== highlight

  async #locate(page, quote, seq) {
    const ctl = new AbortController();
    this.#locateCtl?.abort();
    this.#locateCtl = ctl;
    const timer = setTimeout(() => ctl.abort(), LOCATE_TIMEOUT_MS);
    try {
      const url = `${this.#apiBase}/${encodeURIComponent(this.#sessionId)}/locate?page=${page}&quote=${encodeURIComponent(quote.trim().slice(0, LOCATE_QUOTE_MAX))}`;
      const res = await fetch(url, { signal: ctl.signal, headers: { Accept: "application/json" } });
      if (!res.ok || seq !== this.#citeSeq) return null;
      const data = await res.json();
      return data && typeof data === "object" ? data : null;
    } catch {
      return null; // locating is best effort: the caller falls back to the page flash
    } finally {
      clearTimeout(timer);
      if (this.#locateCtl === ctl) this.#locateCtl = null;
    }
  }

  #drawHighlight(n, boxes, wholePage) {
    const rec = this.#pages[n - 1];
    if (!rec) return;
    if (!rec.hl) {
      rec.hl = document.createElement("div");
      rec.hl.className = "sp-hl-layer";
      rec.hl.setAttribute("aria-hidden", "true");
      rec.el.appendChild(rec.hl);
    }
    const nodes = boxes.map((b) => {
      const node = document.createElement("div");
      node.className = wholePage ? "sp-hl sp-hl-page" : "sp-hl";
      Object.assign(node.style, { left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%` });
      rec.hl.appendChild(node);
      return node;
    });

    const total = HL_FADE_IN + HL_HOLD + HL_FADE_OUT;
    const animations = [];
    let timer = 0;
    const cleanup = () => {
      clearTimeout(timer);
      for (const a of animations) a.cancel();
      for (const node of nodes) node.remove();
      if (this.#hlCleanup === cleanup) this.#hlCleanup = null;
    };
    this.#hlCleanup = cleanup;
    if (prefersReducedMotion() || typeof nodes[0].animate !== "function") {
      timer = setTimeout(cleanup, HL_REDUCED_MS); // static outline, no fades
      return;
    }
    const frames = [
      { opacity: 0, offset: 0 },
      { opacity: 1, offset: HL_FADE_IN / total },
      { opacity: 1, offset: (HL_FADE_IN + HL_HOLD) / total },
      { opacity: 0, offset: 1 },
    ];
    for (const node of nodes) animations.push(node.animate(frames, { duration: total, easing: "linear", fill: "both" }));
    animations[0].onfinish = cleanup;
  }

  #clearHighlight() {
    this.#hlCleanup?.();
    this.#hlCleanup = null;
    clearTimeout(this.#noticeTimer);
    this.#el.notice?.classList.remove("show");
  }
}
