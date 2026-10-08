// Markdown -> sanitised DOM, with [[cN]] citation markers turned into chip buttons.
// Chips are produced by a marked inline extension (so they work inside tables and lists) and the
// whole result passes through DOMPurify; raw HTML written by the model is shown as text, never parsed.

import { Marked } from "../vendor/marked/marked.esm.js";
import { escapeHtml } from "./dom.js";
import { chipLabel } from "./format.js";
import { icon } from "./icons.js";

const DOMPurify = window.DOMPurify;

/** Per-render context read by the chip renderer (marked renders synchronously, so a module variable is safe). */
let ctx = { messageId: "", citations: [], streaming: false, isActive: () => false };

export function findCitation(citations, n) {
  return citations.find((c) => c.index === n) || citations[n - 1] || null;
}

function chipHtml(n) {
  const cite = findCitation(ctx.citations, n);
  if (!cite) {
    // While streaming the citation event may still be in flight: show a placeholder instead of the raw marker.
    return ctx.streaming ? `<span class="cite-chip cite-chip--pending" aria-hidden="true">Source</span>` : "";
  }
  const where = cite.section_path?.length ? `, ${cite.section_path.join(" > ")}` : "";
  const active = ctx.isActive(ctx.messageId, n) ? " is-active" : "";
  const label = `Open source: ${cite.doc_name}, page ${cite.page}${where}`;
  return (
    `<button type="button" class="cite-chip${active}" data-cite="${n}" data-mid="${escapeHtml(ctx.messageId)}" ` +
    `aria-label="${escapeHtml(label)}">${escapeHtml(chipLabel(cite.doc_name, cite.page))}</button>`
  );
}

const marked = new Marked({ gfm: true, breaks: false });
marked.use({
  extensions: [
    {
      name: "cite",
      level: "inline",
      start(src) {
        const i = src.indexOf("[[c");
        return i < 0 ? undefined : i;
      },
      tokenizer(src) {
        const m = /^\[\[c(\d{1,4})\]\]/.exec(src);
        return m ? { type: "cite", raw: m[0], n: Number(m[1]) } : undefined;
      },
      renderer: (token) => chipHtml(token.n),
    },
  ],
  renderer: {
    // Never let model-written HTML reach the page as markup (it could forge chips or buttons).
    html: (token) => escapeHtml(token.text ?? token.raw ?? ""),
  },
});

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.hasAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const SANITIZE = {
  RETURN_DOM_FRAGMENT: true,
  FORBID_TAGS: ["style", "form", "input", "img", "iframe", "object", "embed", "svg", "math"],
  FORBID_ATTR: ["style"],
};

/** Hides a half-received marker ("[[c", "[[c12") and closes an open code fence so streaming never flickers. */
function prepareForDisplay(text, streaming) {
  let t = text;
  if (streaming) {
    t = t.replace(/\[(?:\[(?:c\d*\]?)?)?$/, "");
    if ((t.match(/^```/gm) || []).length % 2 === 1) t += "\n```";
  }
  return t;
}

function wrapTables(fragment) {
  for (const table of Array.from(fragment.querySelectorAll("table"))) {
    const card = document.createElement("div");
    card.className = "table-card";
    card.innerHTML =
      `<div class="table-toolbar">` +
      `<button type="button" class="icon-btn" data-act="table-copy" data-tip="Copy table" aria-label="Copy table">${icon("copy", { size: 14 })}</button>` +
      `<button type="button" class="icon-btn" data-act="table-full" data-tip="View full screen" aria-label="View table full screen">${icon("maximize", { size: 14 })}</button>` +
      `</div><div class="table-scroll" tabindex="0" role="region" aria-label="Table"></div>`;
    table.replaceWith(card);
    card.querySelector(".table-scroll").append(table);
  }
  for (const pre of fragment.querySelectorAll("pre")) {
    pre.tabIndex = 0; // keyboard-scrollable
  }
}

/**
 * @param {string} text assistant markdown containing [[cN]] markers
 * @param {{messageId?: string, citations?: object[], streaming?: boolean, isActive?: Function}} options
 * @returns {DocumentFragment}
 */
export function renderMarkdown(text, options = {}) {
  ctx = {
    messageId: options.messageId || "",
    citations: options.citations || [],
    streaming: Boolean(options.streaming),
    isActive: options.isActive || (() => false),
  };
  const html = marked.parse(prepareForDisplay(text || "", ctx.streaming));
  const fragment = DOMPurify.sanitize(html, SANITIZE);
  wrapTables(fragment);
  return fragment;
}

/** Answer text without citation markers (clipboard, RAGAS-style plain text). */
export function stripMarkers(text) {
  return String(text || "").replace(/[ \t]*\[\[c\d+\]\]/g, "");
}

/** Markdown with markers replaced by "[file.pdf p.89]" so the references survive copy/paste. */
export function markersToReferences(text, citations) {
  return String(text || "").replace(/[ \t]*\[\[c(\d+)\]\]/g, (_, n) => {
    const cite = findCitation(citations, Number(n));
    return cite ? ` [${cite.doc_name} p.${cite.page}]` : "";
  });
}

/** Tab-separated text of a rendered table (pastes into spreadsheets). */
export function tableToTsv(table) {
  return Array.from(table.rows)
    .map((row) => Array.from(row.cells).map((cell) => cell.innerText.replace(/\s+/g, " ").trim()).join("\t"))
    .join("\n");
}
