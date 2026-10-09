// Source card: what a citation opens in the right-hand panel when the server has no PDF to show.
// The public demo ships its answers without the (third-party) report, so there is nothing to render or highlight:
// the card shows the page, the section, the verified quote and the claim it supports instead.
// It sits in the same <aside> and uses the same chrome and open/close animation as the PDF viewer (viewer.css, sp-*).
// Every string reaches the DOM through textContent (h() "text" / text nodes): nothing from the data is parsed as HTML.

import { h } from "./dom.js";
import { icon } from "./icons.js";
import { state } from "./state.js";

const HIDE_DELAY_MS = 300; // the shell's width/transform transition is 200 ms
export const NO_PDF_NOTE = "The report PDF is not bundled with this public demo. Upload your own report to see highlights on the real page.";

/** True when `sid` is the packaged demo chat and this server holds no PDF for it (GET /api/demo -> has_document === false). */
export function isStaticDemo(sid) {
  const demo = state.demo;
  return Boolean(sid) && demo?.available === true && demo.has_document === false && demo.session_id === sid;
}

const safeHref = (url) => {
  try {
    const u = new URL(url, location.href);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
};

const pageLabel = (cite) => {
  const printed = cite.printed_page && String(cite.printed_page) !== String(cite.page) ? ` (printed p. ${cite.printed_page})` : "";
  return `p. ${cite.page}${printed}`;
};

export class SourceCard {
  #root;
  #onClose;
  #el = {};
  #isOpen = false;
  #hideTimer = 0;
  #items = [];
  #index = 0;
  #onSelect = null;
  #destroyed = false;
  #ac = new AbortController();

  /** @param {HTMLElement} rootEl the <aside>.  @param {{onClose?: () => void}} [opts] */
  constructor(rootEl, { onClose } = {}) {
    this.#root = rootEl;
    this.#onClose = typeof onClose === "function" ? onClose : () => {};
    this.#build();
    rootEl.hidden = true;
    this.#applyClosedState();
  }

  get isOpen() {
    return this.#isOpen;
  }

  /**
   * Shows citation `index` of `items` (citation objects of one answer; a `{page, pageOnly: true}` entry stands for a plain
   * page link with no cited passage).  `onSelect(cite, index)` is called when the reader steps to another citation.
   */
  open({ items, index = 0, onSelect = null }) {
    if (this.#destroyed) return;
    this.#items = Array.isArray(items) && items.length ? items : [];
    this.#onSelect = typeof onSelect === "function" ? onSelect : null;
    this.#show();
    this.#render(Math.min(Math.max(0, index), Math.max(0, this.#items.length - 1)), false);
  }

  close() {
    if (this.#destroyed || !this.#isOpen) return;
    this.#isOpen = false;
    this.#root.classList.remove("open");
    this.#applyClosedState();
    this.#hideTimer = setTimeout(() => {
      if (!this.#isOpen) this.#root.hidden = true;
    }, HIDE_DELAY_MS);
    try {
      this.#onClose();
    } catch (err) {
      console.error("[source-card] onClose handler failed", err);
    }
  }

  /** Releases the aside (the PDF viewer, or a later card, takes it over). onClose is not called. */
  destroy() {
    if (this.#destroyed) return;
    this.#destroyed = true;
    this.#isOpen = false;
    this.#ac.abort();
    clearTimeout(this.#hideTimer);
    this.#root.replaceChildren();
    this.#root.classList.remove("sp-root", "sc-root", "open");
    this.#root.removeAttribute("data-state");
    this.#root.removeAttribute("aria-hidden");
  }

  // ----------------------------------------------------------------------------------------------- internals
  #build() {
    const root = this.#root;
    root.classList.add("sp-root", "sc-root");
    root.dataset.state = "ready";
    if (!root.hasAttribute("aria-label")) root.setAttribute("aria-label", "Source");
    const el = this.#el;
    el.name = h("div", { class: "sp-name" });
    el.sub = h("div", { class: "sp-sub tnum" });
    el.title = h("div", { class: "sp-title", tabindex: "-1" }, el.name, el.sub);
    el.close = h("button", { type: "button", class: "sp-btn sp-close", "aria-label": "Close source panel", title: "Close (Esc)", html: icon("x", { size: 16 }) });
    el.body = h("div", { class: "sc-body", tabindex: "0", role: "region", "aria-label": "Cited passage" });
    el.prev = h("button", { type: "button", class: "sp-btn sc-prev", "aria-label": "Previous citation", title: "Previous citation (Left arrow)", html: icon("chevron-left", { size: 16 }) });
    el.next = h("button", { type: "button", class: "sp-btn sc-next", "aria-label": "Next citation", title: "Next citation (Right arrow)", html: icon("chevron-right", { size: 16 }) });
    el.count = h("span", { class: "sc-count tnum" });
    el.nav = h("div", { class: "sc-nav" }, el.prev, el.count, el.next);
    el.foot = h("footer", { class: "sp-foot sc-foot", hidden: true }, el.nav);
    el.sr = h("span", { class: "sr-only", role: "status", "aria-live": "polite" });
    root.replaceChildren(
      h("header", { class: "sp-head" }, h("div", { class: "sp-thumb", "aria-hidden": "true", html: icon("file-text", { size: 18 }) }), el.title, el.close),
      h("div", { class: "sp-viewport" }, el.body),
      el.foot,
      el.sr,
    );
    const { signal } = this.#ac;
    el.close.addEventListener("click", () => this.close(), { signal });
    el.prev.addEventListener("click", () => this.#step(-1), { signal });
    el.next.addEventListener("click", () => this.#step(1), { signal });
    root.addEventListener("keydown", (e) => this.#onKey(e), { signal });
  }

  #onKey(e) {
    if (e.key === "Escape") {
      e.stopPropagation();
      this.close();
    } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      e.preventDefault();
      this.#step(e.key === "ArrowLeft" ? -1 : 1);
    }
  }

  #step(delta) {
    const next = this.#index + delta;
    if (next < 0 || next >= this.#items.length) return;
    this.#render(next, true);
    const el = delta < 0 ? this.#el.prev : this.#el.next;
    if (el.disabled) this.#el.body.focus({ preventScroll: true }); // the button just became unavailable: keep focus in the card
  }

  #render(index, notify) {
    const items = this.#items;
    const cite = items[index];
    if (!cite) return;
    this.#index = index;
    const el = this.#el;
    const demo = state.demo || {};
    const name = cite.doc_name || demo.filename || "document.pdf";
    el.name.textContent = name;
    el.name.title = name;
    el.sub.textContent = pageLabel(cite);

    const nodes = [];
    if (cite.section_path?.length) {
      nodes.push(h("nav", { class: "sc-crumb", "aria-label": "Section" }, h("span", { class: "sc-crumb__label", text: "In" }), h("span", { class: "sc-crumb__path", text: cite.section_path.join(" › ") })));
    }
    if (cite.quote) {
      const heading = cite.quote_source === "aligned" ? "Closest matching passage" : "Verified quote";
      nodes.push(h("h3", { class: "sc-label", text: heading }), h("blockquote", { class: "sc-quote", text: cite.quote }));
    } else if (cite.pageOnly) {
      nodes.push(h("p", { class: "sc-plain", text: "No passage from this page is cited in the demo answers." }));
    } else {
      nodes.push(h("p", { class: "sc-plain", text: "The exact passage could not be located on this page." }));
    }
    if (cite.claim) nodes.push(h("h3", { class: "sc-label", text: "Supports" }), h("p", { class: "sc-claim", text: cite.claim }));
    nodes.push(h("div", { class: "sc-note", role: "note" }, h("span", { class: "sc-note__icon", html: icon("info", { size: 16 }) }), h("span", { text: NO_PDF_NOTE })));
    if (demo.attribution) {
      const href = demo.attribution_url ? safeHref(demo.attribution_url) : null;
      nodes.push(
        h("p", { class: "sc-attr" }, h("span", { text: `Source: ${demo.attribution} ` }), href ? h("a", { href, target: "_blank", rel: "noopener noreferrer", html: `Publisher's site ${icon("external-link", { size: 12 })}` }) : null),
      );
    }
    el.body.replaceChildren(...nodes);
    el.body.scrollTop = 0;

    const many = items.length > 1;
    el.foot.hidden = !many;
    el.prev.disabled = index <= 0;
    el.next.disabled = index >= items.length - 1;
    el.count.textContent = many ? `Citation ${index + 1} of ${items.length}` : "";
    el.sr.textContent = `${many ? `Citation ${index + 1} of ${items.length}, ` : ""}page ${cite.page}${cite.quote ? ", verified quote shown" : ""}.`;
    if (notify && this.#onSelect) {
      try {
        this.#onSelect(cite, index);
      } catch (err) {
        console.error("[source-card] onSelect failed", err);
      }
    }
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
  }
}
