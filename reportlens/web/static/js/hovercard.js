// Floating UI: simple tooltips ([data-tip]) and the citation hover card.
// Both open on hover (after a delay) and on keyboard focus, stay open while hovered (WCAG 1.4.13)
// and close on Esc. One element of each kind is reused for the whole app.

import { h } from "./dom.js";
import { truncName } from "./format.js";
import { icon } from "./icons.js";

const TIP_DELAY = 400;
const CARD_DELAY = 150;
const HIDE_GRACE = 140;

let tipEl;
let cardEl;
let hooks;
let showTimer = 0;
let hideTimer = 0;
let current = null; // { kind: "tip" | "card", anchor }

export function initFloating(options) {
  hooks = options; // { getCitation(mid, n), getPageCount(), onOpen(cite, chipEl) }
  tipEl = h("div", { class: "float tip", role: "tooltip", id: "float-tip" });
  cardEl = h("div", { class: "float hover-card", role: "tooltip", id: "float-card" });
  document.body.append(tipEl, cardEl);

  document.addEventListener("mouseover", (e) => onEnter(e.target, CARD_DELAY, TIP_DELAY));
  document.addEventListener("focusin", (e) => onEnter(e.target, 0, 0));
  document.addEventListener("mouseout", (e) => onLeave(e.target, e.relatedTarget));
  document.addEventListener("focusout", (e) => onLeave(e.target, e.relatedTarget));
  document.addEventListener("click", (e) => {
    if (current && targetFor(e.target)?.anchor === current.anchor) hide(); // the click already opened the page
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && current) hide();
  });
  document.addEventListener(
    "scroll",
    (e) => current && !(e.target instanceof Node && (cardEl.contains(e.target) || tipEl.contains(e.target))) && hide(),
    true,
  );
  window.addEventListener("resize", () => current && hide());
  for (const el of [tipEl, cardEl]) {
    el.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    el.addEventListener("mouseleave", () => scheduleHide());
  }
}

function targetFor(node) {
  if (!(node instanceof Element)) return null;
  const chip = node.closest(".cite-chip[data-cite]");
  if (chip) return { kind: "card", anchor: chip };
  const tip = node.closest("[data-tip]");
  return tip ? { kind: "tip", anchor: tip } : null;
}

function onEnter(node, cardDelay, tipDelay) {
  const target = targetFor(node);
  if (!target) return;
  clearTimeout(hideTimer);
  if (current && current.anchor === target.anchor) return;
  clearTimeout(showTimer);
  showTimer = setTimeout(() => show(target), target.kind === "card" ? cardDelay : tipDelay);
}

function onLeave(node, related) {
  const target = targetFor(node);
  if (!target) return;
  if (related instanceof Node && (target.anchor.contains(related) || tipEl.contains(related) || cardEl.contains(related))) return;
  clearTimeout(showTimer);
  scheduleHide();
}

function scheduleHide() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hide, HIDE_GRACE);
}

function show({ kind, anchor }) {
  if (!anchor.isConnected) return;
  const el = kind === "card" ? cardEl : tipEl;
  if (kind === "card") {
    const cite = hooks.getCitation(anchor.dataset.mid, Number(anchor.dataset.cite));
    if (!cite) return;
    cardEl.replaceChildren(...cardContent(cite, anchor));
  } else {
    tipEl.textContent = anchor.dataset.tip;
  }
  hide();
  current = { kind, anchor };
  anchor.setAttribute("aria-describedby", el.id);
  place(el, anchor);
  el.classList.add("is-visible");
}

function hide() {
  clearTimeout(showTimer);
  clearTimeout(hideTimer);
  tipEl.classList.remove("is-visible");
  cardEl.classList.remove("is-visible");
  if (current) {
    current.anchor.removeAttribute("aria-describedby");
    current = null;
  }
}

function place(el, anchor) {
  const r = anchor.getBoundingClientRect();
  el.style.left = "0px";
  el.style.top = "0px";
  const w = el.offsetWidth;
  const hgt = el.offsetHeight;
  let top = r.bottom + 8;
  if (top + hgt > window.innerHeight - 8) top = Math.max(8, r.top - hgt - 8);
  const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
}

function cardContent(cite, chipEl) {
  const pageCount = hooks.getPageCount();
  const printed = cite.printed_page && String(cite.printed_page) !== String(cite.page) ? ` · printed p. ${cite.printed_page}` : "";
  const meta = `Page ${cite.page}${pageCount ? ` of ${pageCount}` : ""}${printed}`;
  const nodes = [
    h("div", { class: "hover-card__file", title: cite.doc_name }, h("span", { html: icon("file-text", { size: 14 }) }), truncName(cite.doc_name, 36)),
    h("div", { class: "hover-card__meta tnum", text: meta }),
  ];
  if (cite.section_path?.length) nodes.push(h("div", { class: "hover-card__crumb", text: cite.section_path.join(" › ") }));
  if (cite.quote) {
    const heading = cite.quote_source === "aligned" ? "Closest matching passage" : "Highlighted passage";
    const quote = cite.quote.length > 420 ? `${cite.quote.slice(0, 420).trimEnd()}…` : cite.quote;
    nodes.push(h("div", { class: "hover-card__note", text: heading }), h("blockquote", { class: "hover-card__quote", text: quote }));
  } else {
    nodes.push(h("div", { class: "hover-card__note", text: "The exact passage could not be located, so the whole page is shown." }));
  }
  nodes.push(
    h("button", {
      type: "button",
      class: "btn btn-sm hover-card__open",
      text: "Open page",
      on: { click: () => { hide(); hooks.onOpen(cite, chipEl); } },
    }),
  );
  return nodes;
}
