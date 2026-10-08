// Agent steps timeline: "Thinking...", "Read pages 88-90 from "report.pdf"", expandable rows.

import { h } from "./dom.js";
import { enDashRanges, formatDuration, plural, thoughtLabel } from "./format.js";
import { icon } from "./icons.js";

const TOOL_ICON = { get_page_content: "book-open", get_document_structure: "list-tree" };

function stepIcon(step) {
  if (step.status === "running") return "loader-circle";
  if (step.kind === "thinking") return "lightbulb";
  return TOOL_ICON[step.tool] || "file-search";
}

export function stepLabel(step, docName) {
  if (step.kind === "thinking") return step.status === "running" ? "Thinking..." : thoughtLabel(step.elapsed_ms);
  const base = enDashRanges(step.label || "Working");
  if (step.status === "running") return `${base.replace(/^Read /, "Reading ")}...`;
  const hasDoc = /\bfrom\b/.test(base);
  return step.tool === "get_page_content" && step.pages?.length && !hasDoc ? `${base} from "${docName}"` : base;
}

const isExpandable = (step) => step.status === "done" && (step.pages?.length > 0 || Number.isFinite(step.elapsed_ms));

/**
 * @param {object[]} steps
 * @param {{docName: string, streaming: boolean, expanded: Set<string>, collapsed: boolean,
 *          onToggleRow: (id: string) => void, onToggleAll: () => void, onOpenPage: (page: number) => void}} opts
 */
export function renderSteps(steps, opts) {
  const root = h("div", { class: "steps" });
  if (!steps.length) return root;
  const collapsible = !opts.streaming && steps.length >= 3;

  if (collapsible) {
    const total = steps.reduce((sum, s) => sum + (s.elapsed_ms || 0), 0);
    root.append(
      h(
        "button",
        {
          type: "button",
          class: "steps__summary",
          aria: { expanded: String(!opts.collapsed) },
          on: { click: opts.onToggleAll },
        },
        h("span", { text: `${plural(steps.length, "step")}${total ? ` · ${formatDuration(total)}` : ""}` }),
        h("span", { class: "steps__chev", html: icon("chevron-down", { size: 14 }) }),
      ),
    );
    if (opts.collapsed) return root;
  }

  const list = h("ol", { class: "steps__list" });
  steps.forEach((step, i) => {
    const latest = i === steps.length - 1;
    const open = opts.expanded.has(step.id) && isExpandable(step);
    const label = stepLabel(step, opts.docName);
    const rowId = `step-${step.id}`;
    const head = isExpandable(step)
      ? h(
          "button",
          { type: "button", class: "step__head", aria: { expanded: String(open), controls: `${rowId}-body` }, on: { click: () => opts.onToggleRow(step.id) } },
          h("span", { class: "step__icon", html: icon(stepIcon(step), { cls: step.status === "running" ? "spin" : "" }) }),
          h("span", { class: "step__label", text: label }),
          h("span", { class: "step__chev", html: icon("chevron-down", { size: 14 }) }),
        )
      : h(
          "div",
          { class: "step__head" },
          h("span", { class: "step__icon", html: icon(stepIcon(step), { cls: step.status === "running" ? "spin" : "" }) }),
          h("span", { class: "step__label", text: label }),
        );
    const li = h("li", { class: `step${latest ? " is-latest" : ""}${step.status === "running" ? " is-running" : ""}${open ? " is-open" : ""}` }, head);
    if (open) li.append(stepBody(step, rowId, opts));
    list.append(li);
  });
  root.append(list);
  return root;
}

function stepBody(step, rowId, opts) {
  const body = h("div", { class: "step__body", id: `${rowId}-body` });
  if (Number.isFinite(step.elapsed_ms)) body.append(h("div", { class: "step__detail", text: `Took ${formatDuration(step.elapsed_ms)}` }));
  if (step.pages?.length) {
    const links = h("div", { class: "step__pages" }, h("span", { class: "step__detail", text: step.pages.length === 1 ? "Page" : "Pages" }));
    for (const page of step.pages.slice(0, 24)) {
      links.append(h("button", { type: "button", class: "page-link", text: `p.${page}`, on: { click: () => opts.onOpenPage(page) } }));
    }
    if (step.pages.length > 24) links.append(h("span", { class: "step__detail", text: `+${step.pages.length - 24} more` }));
    body.append(links);
  }
  return body;
}
