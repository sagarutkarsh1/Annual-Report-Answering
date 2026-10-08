// "Sources" block: N references, one row per document, unique page links and ref count.

import { h } from "./dom.js";
import { plural } from "./format.js";
import { icon } from "./icons.js";

/**
 * @param {object} msg assistant message (sources[], citations[])
 * @param {{docName: string, open: boolean, onToggle: () => void, onOpenPage: (page: number, source: object) => void}} opts
 * @returns {HTMLElement|null} null when there is nothing to list
 */
export function renderSources(msg, opts) {
  const sources = (msg.sources || []).slice().sort((a, b) => a.page - b.page);
  if (!sources.length) return null;
  // "References" counts inline chips (the reference UI does the same), not unique pages.
  const references = sources.reduce((sum, s) => sum + (s.refs || 0), 0) || msg.citations?.length || sources.length;
  const bodyId = `src-${msg.id}`;

  const links = sources.map((source) => {
    const printed = source.printed_page && String(source.printed_page) !== String(source.page) ? `, printed p. ${source.printed_page}` : "";
    const section = source.section_path?.length ? ` — ${source.section_path.join(" > ")}` : "";
    return h("button", {
      type: "button",
      class: "page-link",
      text: `p.${source.page}`,
      "data-tip": `Page ${source.page}${printed}${section}`,
      "aria-label": `Open page ${source.page}${printed}`,
      on: { click: () => opts.onOpenPage(source.page, source) },
    });
  });

  return h(
    "section",
    { class: `sources${opts.open ? " is-open" : ""}`, "aria-label": "Sources" },
    h(
      "button",
      { type: "button", class: "block-head", aria: { expanded: String(opts.open), controls: bodyId }, on: { click: opts.onToggle } },
      h("span", { class: "block-head__title", text: "Sources" }),
      h("span", { class: "block-head__meta", text: `${plural(references, "reference")} · ${plural(1, "document")}` }),
      h("span", { class: "block-head__rule" }),
      h("span", { class: "block-head__chev", html: icon("chevron-up", { size: 16 }) }),
    ),
    opts.open
      ? h(
          "div",
          { class: "sources__body", id: bodyId },
          h(
            "div",
            { class: "source-row" },
            h("span", { class: "source-row__index", text: "1." }),
            h("span", { class: "source-row__icon", html: icon("file-text", { size: 14 }) }),
            h("span", { class: "source-row__name", text: opts.docName, title: opts.docName }),
            h("span", { class: "source-row__pages" }, links),
            h("span", { class: "source-row__refs tnum", text: `${references} ${references === 1 ? "ref" : "refs"}` }),
          ),
        )
      : null,
  );
}
