/* citation_panel.js - reusable "citation -> source" PDF side panel (research prototype)
 *
 * Stack:  pdf.js core only (pdfjs-dist/build/pdf.min.mjs + worker; ~450 KB + 1.2 MB worker)
 *         - own continuous-scroll page list with lazy rendering (IntersectionObserver)
 *         - pdf.js TextLayer for selectable text (optional)
 *         - highlight overlay driven by NORMALISED rects computed on the server (0..1 of the visible page)
 *
 *   const panel = new CitationPanel(rootEl, { pdfjsLib, onClose });
 *   await panel.load({ url: "/api/pdf", name: "Report.pdf", pages: [{w,h},...] });
 *   panel.goToCitation({ page: 89, rects: [{x,y,w,h}], method: "exact", score: 1, quote: "..." });
 */

const DPR_CAP_PIXELS = 16_000_000;           // per canvas
const KEEP_RENDERED = 10;                    // pages kept alive around the viewport
const PAGE_GAP = 12;                         // px between pages (also used for offset maths)
const PAD_X = 16;                            // body side padding
const ZOOMS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];

export class CitationPanel {
  constructor(root, { pdfjsLib, onClose = () => {}, textLayer = true } = {}) {
    this.root = root;
    this.pdfjs = pdfjsLib;
    this.onClose = onClose;
    this.useTextLayer = textLayer;
    this.zoom = 1;
    this.pages = [];            // [{w,h,el,canvas,tl,hl,state}]
    this.rendered = new Map();  // pageNo -> {task, textLayer}
    this.visible = new Set();
    this.currentPage = 1;
    this._build();
  }

  /* ---------------------------------------------------------------- DOM */
  _build() {
    this.root.classList.add("cp");
    this.root.innerHTML = `
      <div class="cp-resize" title="Drag to resize (double-click to reset)"></div>
      <header class="cp-head">
        <div class="cp-thumb" aria-hidden="true"></div>
        <div class="cp-title"><div class="cp-name"></div><div class="cp-sub">Document</div></div>
        <button class="cp-close" aria-label="Close source panel" title="Close (Esc)">&times;</button>
      </header>
      <div class="cp-body" tabindex="0"><div class="cp-pages"></div><div class="cp-toast" role="status" aria-live="polite"></div></div>
      <footer class="cp-foot">
        <div class="cp-nav">
          <button class="cp-prev" aria-label="Previous page" title="Previous page ( [ )">&#8249;</button>
          <span>Page</span>
          <input class="cp-page-input" type="text" inputmode="numeric" aria-label="Page number" value="1">
          <span class="cp-total">/ 1</span>
          <button class="cp-next" aria-label="Next page" title="Next page ( ] )">&#8250;</button>
        </div>
        <div class="cp-zoom">
          <button class="cp-zout" aria-label="Zoom out" title="Zoom out ( - )">&minus;</button>
          <button class="cp-zval" title="Reset zoom ( 0 )">100%</button>
          <button class="cp-zin" aria-label="Zoom in" title="Zoom in ( + )">+</button>
        </div>
      </footer>`;
    const q = (s) => this.root.querySelector(s);
    this.body = q(".cp-body");
    this.list = q(".cp-pages");
    this.toast = q(".cp-toast");
    this.input = q(".cp-page-input");
    this.totalEl = q(".cp-total");
    this.zval = q(".cp-zval");
    q(".cp-close").onclick = () => this.close();
    q(".cp-prev").onclick = () => this.goToPage(this.currentPage - 1);
    q(".cp-next").onclick = () => this.goToPage(this.currentPage + 1);
    q(".cp-zout").onclick = () => this.stepZoom(-1);
    q(".cp-zin").onclick = () => this.stepZoom(+1);
    this.zval.onclick = () => this.setZoom(1);
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { this.goToPage(parseInt(this.input.value, 10) || this.currentPage); this.body.focus(); }
      e.stopPropagation();
    });
    this.input.addEventListener("focus", () => this.input.select());
    this.body.addEventListener("scroll", () => this._onScroll(), { passive: true });
    this.root.addEventListener("keydown", (e) => this._onKey(e));
    new ResizeObserver(() => this._onResize()).observe(this.body);
    this._initResize(q(".cp-resize"));
    this.io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const n = +en.target.dataset.page;
        if (en.isIntersecting) this.visible.add(n); else this.visible.delete(n);
      }
      this._schedule();
    }, { root: this.body, rootMargin: "150% 0px 150% 0px" });
  }

  _initResize(handle) {
    let startX, startW;
    const move = (e) => {
      const w = Math.min(Math.max(startW + (startX - e.clientX), 320), window.innerWidth * 0.75);
      this.root.style.setProperty("--cp-w", w + "px");
    };
    const up = () => {
      window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
      document.body.classList.remove("cp-resizing");
      try { localStorage.setItem("cp-w", getComputedStyle(this.root).getPropertyValue("--cp-w")); } catch { /* ignore */ }
    };
    handle.addEventListener("pointerdown", (e) => {
      startX = e.clientX; startW = this.root.getBoundingClientRect().width;
      document.body.classList.add("cp-resizing");
      window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); e.preventDefault();
    });
    handle.addEventListener("dblclick", () => {
      this.root.style.removeProperty("--cp-w");
      try { localStorage.removeItem("cp-w"); } catch { /* ignore */ }
    });
    try { const w = localStorage.getItem("cp-w"); if (w) this.root.style.setProperty("--cp-w", w); } catch { /* ignore */ }
  }

  /* ---------------------------------------------------------------- load */
  async load({ url, name, pages, pdfOptions = {} }) {
    this.close(false);
    this.root.querySelector(".cp-name").textContent = name;
    this.root.querySelector(".cp-name").title = name;
    this._destroyPages();
    this.sizes = pages;   // server-provided visible sizes in PDF points -> instant, jump-free layout
    this.totalEl.textContent = `/ ${pages.length}`;
    this.list.replaceChildren();
    this.pages = pages.map((p, i) => {
      const el = document.createElement("div");
      el.className = "cp-pg skeleton";
      el.dataset.page = i + 1;
      el.innerHTML = `<canvas></canvas><div class="textLayer"></div><div class="cp-hl-layer"></div><span class="cp-pgno">${i + 1}</span>`;
      this.list.appendChild(el);
      this.io.observe(el);
      return { w: p.w, h: p.h, el, canvas: el.firstChild, tl: el.querySelector(".textLayer"),
               hl: el.querySelector(".cp-hl-layer"), state: "idle", scale: 1, rect: null };
    });
    this._layout();
    // pdf.js document (HTTP Range + streaming, no full prefetch of a 300 page file)
    this.loadingTask?.destroy();
    this.loadingTask = this.pdfjs.getDocument({
      url, rangeChunkSize: 262144, disableAutoFetch: true, disableStream: false,
      ...pdfOptions,
    });
    this.pdf = await this.loadingTask.promise;
    this.open();
    this._schedule();
    return this.pdf;
  }

  open() {
    this.root.classList.add("open"); this.root.hidden = false;
    this._ensureLayout();                       // panel was display:none -> widths were 0; re-measure synchronously
  }
  _ensureLayout() {
    if (this.pages.length && Math.abs(this.body.clientWidth - (this._laidW || 0)) > 1) {
      const cur = this.pages[this.currentPage - 1];
      const frac = cur && this._laidW ? (this.body.scrollTop - cur.top) / cur.height : 0;
      this._layout();
      if (cur) this.body.scrollTop = cur.top + frac * cur.height;
      for (const [n, r] of this.rendered) this._release(n, r);
      this._schedule();
    }
  }
  close(notify = true) {
    this.root.classList.remove("open");
    if (notify) { this.onClose(); }
  }
  _destroyPages() {
    for (const [, r] of this.rendered) { try { r.task?.cancel(); r.textLayer?.cancel(); } catch { /* ignore */ } }
    this.rendered.clear(); this.visible.clear(); this.io.disconnect();
  }

  /* ---------------------------------------------------------------- layout & zoom */
  _fitWidth() { return Math.max(200, this.body.clientWidth - 2 * PAD_X); }

  _layout() {
    this._laidW = this.body.clientWidth;
    const fit = this._fitWidth();
    let top = PAGE_GAP;
    for (const p of this.pages) {
      p.scale = (fit / p.w) * this.zoom;          // CSS px per PDF point (per-page fit-width * zoom)
      const w = p.w * p.scale, h = p.h * p.scale;
      p.el.style.width = w + "px"; p.el.style.height = h + "px"; p.el.style.top = top + "px";
      p.top = top; p.height = h; top += h + PAGE_GAP;
    }
    this.list.style.height = top + "px";
    this.zval.textContent = Math.round(this.zoom * 100) + "%";
  }

  setZoom(z) {
    z = Math.min(Math.max(z, ZOOMS[0]), ZOOMS.at(-1));
    if (Math.abs(z - this.zoom) < 1e-6) return;
    // keep the same spot of the same page at the top of the viewport
    const cur = this.pages[this.currentPage - 1];
    const frac = cur ? (this.body.scrollTop - cur.top) / cur.height : 0;
    this.zoom = z;
    this._layout();
    if (cur) this.body.scrollTop = cur.top + frac * cur.height;
    for (const [n, r] of this.rendered) this._release(n, r);   // re-render crisp at the new scale
    this._schedule();
  }
  stepZoom(dir) {
    const i = ZOOMS.findIndex((z) => Math.abs(z - this.zoom) < 1e-6);
    const base = i >= 0 ? i : ZOOMS.findIndex((z) => z >= this.zoom);
    this.setZoom(ZOOMS[Math.min(Math.max(base + dir, 0), ZOOMS.length - 1)]);
  }
  _onResize() {
    if (!this.pages.length) return;
    clearTimeout(this._rz);
    this._rz = setTimeout(() => this._ensureLayout(), 120);
  }

  /* ---------------------------------------------------------------- navigation */
  _pageAtOffset(y) {                      // binary search over precomputed tops
    let lo = 0, hi = this.pages.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (this.pages[m].top <= y) lo = m; else hi = m - 1; }
    return lo + 1;
  }
  _onScroll() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      if (performance.now() < (this._navLock || 0)) return;   // a programmatic smooth scroll is in flight: keep the target page number
      const n = this._pageAtOffset(this.body.scrollTop + this.body.clientHeight * 0.35);
      if (n !== this.currentPage) { this.currentPage = n; if (document.activeElement !== this.input) this.input.value = n; }
    });
  }
  _lockNav(ms = 900) {                       // prevents the page indicator flickering back while smooth-scrolling
    this._navLock = performance.now() + ms;
    clearTimeout(this._navT);
    this._navT = setTimeout(() => this._onScroll(), ms + 30);
  }
  goToPage(n, { smooth = true, offsetY = 0 } = {}) {
    n = Math.min(Math.max(1, n | 0), this.pages.length);
    const p = this.pages[n - 1];
    this.currentPage = n; this.input.value = n; this._lockNav(smooth ? 900 : 0);
    this.body.scrollTo({ top: Math.max(0, p.top - PAGE_GAP + offsetY), behavior: smooth ? "smooth" : "auto" });
  }

  /* ---------------------------------------------------------------- citation -> highlight */
  goToCitation({ page, rects = [], method = "exact", score = 1, note = "" }) {
    this.open();
    const n = Math.min(Math.max(1, page | 0), this.pages.length);
    const p = this.pages[n - 1];
    this.clearHighlights();
    if (!rects.length) {                       // page-only fallback
      this.goToPage(n);
      p.el.classList.remove("flash"); void p.el.offsetWidth; p.el.classList.add("flash");
      this._toast(`Showing page ${n}. The exact passage could not be located.`);
      return;
    }
    const minY = Math.min(...rects.map((r) => r.y));
    // put the first highlighted line ~30% down the viewport
    const target = p.top + minY * p.height - this.body.clientHeight * 0.3;
    this.currentPage = n; this.input.value = n; this._lockNav(1100);
    this.body.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
    const approx = method === "block" || method === "page" || score < 0.9;
    for (const r of rects) {
      const d = document.createElement("div");
      d.className = "cp-hl" + (approx ? " approx" : "");
      d.style.cssText = `left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%`;
      p.hl.appendChild(d);
      d.addEventListener("animationend", () => d.remove(), { once: true });   // temporary highlight (~4s)
    }
    if (approx) this._toast(method === "block" ? "Approximate area (exact wording not found)" : `Close match (${Math.round(score * 100)}%)`);
    else if (note) this._toast(note);
  }
  clearHighlights() {
    for (const p of this.pages) { p.hl.replaceChildren(); p.el.classList.remove("flash"); }
  }
  _toast(msg) {
    this.toast.textContent = msg; this.toast.classList.add("show");
    clearTimeout(this._tt); this._tt = setTimeout(() => this.toast.classList.remove("show"), 3500);
  }

  /* ---------------------------------------------------------------- lazy rendering */
  _schedule() {
    if (this._pump) return;
    this._pump = true;
    queueMicrotask(async () => {
      try {
        while (true) {
          const mid = this.body.scrollTop + this.body.clientHeight / 2;
          const todo = [...this.visible].filter((n) => !this.rendered.has(n))
            .sort((a, b) => Math.abs(this.pages[a - 1].top - mid) - Math.abs(this.pages[b - 1].top - mid));
          if (!todo.length || !this.pdf) break;
          await this._render(todo[0]);
          this._evict();
        }
      } finally { this._pump = false; }
    });
  }
  _evict() {
    if (this.rendered.size <= KEEP_RENDERED) return;
    const mid = this.body.scrollTop + this.body.clientHeight / 2;
    const far = [...this.rendered.keys()].filter((n) => !this.visible.has(n))
      .sort((a, b) => Math.abs(this.pages[b - 1].top - mid) - Math.abs(this.pages[a - 1].top - mid));
    while (this.rendered.size > KEEP_RENDERED && far.length) { const n = far.shift(); this._release(n, this.rendered.get(n)); }
  }
  _release(n, r) {
    try { r.task?.cancel(); r.textLayer?.cancel(); } catch { /* ignore */ }
    const p = this.pages[n - 1];
    p.canvas.width = p.canvas.height = 0; p.tl.replaceChildren(); p.el.classList.add("skeleton");
    this.rendered.delete(n);
  }
  async _render(n) {
    const p = this.pages[n - 1];
    const entry = {}; this.rendered.set(n, entry);
    try {
      const page = await this.pdf.getPage(n);
      if (this.rendered.get(n) !== entry) return;                       // released meanwhile
      const viewport = page.getViewport({ scale: p.scale });
      // sanity: server-side visible size must equal pdf.js' (rotation + cropbox) -> normalised rects line up
      if (Math.abs(viewport.width / p.scale - p.w) > 1.5 || Math.abs(viewport.height / p.scale - p.h) > 1.5)
        console.warn(`page ${n}: size mismatch server ${p.w}x${p.h} vs pdf.js ${viewport.width / p.scale}x${viewport.height / p.scale}`);
      let dpr = window.devicePixelRatio || 1;
      dpr = Math.min(dpr, Math.sqrt(DPR_CAP_PIXELS / (viewport.width * viewport.height)));
      const c = p.canvas;
      c.width = Math.floor(viewport.width * dpr); c.height = Math.floor(viewport.height * dpr);
      c.style.width = "100%"; c.style.height = "100%";
      const ctx = c.getContext("2d");
      entry.task = page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null });
      await entry.task.promise;
      p.el.classList.remove("skeleton");
      if (this.useTextLayer && this.rendered.get(n) === entry) {
        p.tl.replaceChildren();
        p.tl.style.setProperty("--total-scale-factor", viewport.scale);
        p.tl.style.setProperty("--scale-round-x", "1px"); p.tl.style.setProperty("--scale-round-y", "1px");
        entry.textLayer = new this.pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: p.tl, viewport });
        await entry.textLayer.render();
      }
      page.cleanup?.();
    } catch (e) {
      if (e?.name === "RenderingCancelledException" || e?.name === "AbortException") return;
      console.error("render failed", n, e);
      this.rendered.delete(n);
    }
  }

  /* ---------------------------------------------------------------- keyboard */
  _onKey(e) {
    if (e.target === this.input || e.ctrlKey || e.metaKey || e.altKey) return;
    switch (e.key) {
      case "Escape": this.close(); break;
      case "]": this.goToPage(this.currentPage + 1); e.preventDefault(); break;
      case "[": this.goToPage(this.currentPage - 1); e.preventDefault(); break;
      case "+": case "=": this.stepZoom(+1); e.preventDefault(); break;
      case "-": case "_": this.stepZoom(-1); e.preventDefault(); break;
      case "0": this.setZoom(1); e.preventDefault(); break;
      case "g": this.input.focus(); e.preventDefault(); break;
      case "Home": this.goToPage(1); e.preventDefault(); break;
      case "End": this.goToPage(this.pages.length); e.preventDefault(); break;
    }
  }

  /* ---------------------------------------------------------------- approach A helper (client-side locate) */
  /** Exact squashed-substring locate using pdf.js text items.  Returns normalised rects (0..1) or []. */
  async clientLocate(pageNo, quote) {
    const page = await this.pdf.getPage(pageNo);
    const vp = page.getViewport({ scale: 1 });
    const tc = await page.getTextContent();
    const norm = (s) => [...s.normalize("NFKC").toLowerCase()].filter((ch) => /[\p{L}\p{N}]/u.test(ch)).join("");
    let S = ""; const map = [];                     // squashed char -> {item, frac}
    tc.items.forEach((it, ii) => {
      if (!("str" in it)) return;
      const chars = [...it.str];
      chars.forEach((ch, ci) => { const t = norm(ch); for (const _ of t) { S += _; map.push({ ii, a: ci / chars.length, b: (ci + 1) / chars.length }); } });
    });
    const q = norm(quote), at = S.indexOf(q);
    if (!q || at < 0) return [];
    const per = new Map();                          // item -> [fracStart, fracEnd]
    for (let k = at; k < at + q.length; k++) {
      const m = map[k]; const cur = per.get(m.ii);
      per.set(m.ii, cur ? [Math.min(cur[0], m.a), Math.max(cur[1], m.b)] : [m.a, m.b]);
    }
    const rects = [];
    for (const [ii, [fa, fb]] of per) {
      const it = tc.items[ii], st = tc.styles[it.fontName] || {};
      const fs = Math.hypot(it.transform[0], it.transform[1]) || 1;           // font size in user space
      const wEm = it.width / fs;                                              // advance width in em
      const asc = st.ascent ?? 0.9, desc = st.descent ?? -0.2;
      // text-space box -> user space (item.transform) -> viewport (scale 1) ; bounding box of 4 corners
      const corners = [[wEm * fa, desc], [wEm * fb, desc], [wEm * fb, asc], [wEm * fa, asc]].map(([u, v]) => {
        const x = it.transform[0] * u + it.transform[2] * v + it.transform[4] * 1;
        const y = it.transform[1] * u + it.transform[3] * v + it.transform[5] * 1;
        return vp.convertToViewportPoint(x, y);   // v6: Util.applyTransform(p, m) mutates p in place and returns undefined
      });
      const xs = corners.map((c) => c[0]), ys = corners.map((c) => c[1]);
      rects.push({ x: Math.min(...xs) / vp.width, y: Math.min(...ys) / vp.height,
                   w: (Math.max(...xs) - Math.min(...xs)) / vp.width, h: (Math.max(...ys) - Math.min(...ys)) / vp.height });
    }
    return rects;
  }
}
