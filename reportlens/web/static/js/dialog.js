// Modal dialogs built on <dialog>: the browser provides focus trapping, Esc and focus restore.

import { h } from "./dom.js";
import { icon } from "./icons.js";

function open(dialog) {
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
}

/** @returns {Promise<boolean>} true when the user confirmed */
export function confirmDialog({ title, text, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const confirm = h("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}`, text: confirmLabel });
    const cancel = h("button", { type: "button", class: "btn", text: "Cancel" });
    const dialog = h(
      "dialog",
      { class: "dlg", "aria-labelledby": "dlg-title" },
      h("div", { class: "dlg__body" }, h("h2", { class: "dlg__title", id: "dlg-title", text: title }), h("p", { class: "dlg__text", text })),
      h("div", { class: "dlg__actions" }, cancel, confirm),
    );
    let result = false;
    confirm.addEventListener("click", () => {
      result = true;
      dialog.close();
    });
    cancel.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => resolve(result), { once: true });
    open(dialog);
    cancel.focus(); // safe default for destructive confirms
  });
}

/** Full-screen view of a table (clone, so the chat copy is untouched). */
export function tableDialog(table) {
  const close = h("button", { type: "button", class: "icon-btn", aria: { label: "Close" }, html: icon("x") });
  const copy = table.cloneNode(true);
  const dialog = h(
    "dialog",
    { class: "dlg dlg--wide", "aria-label": "Table" },
    h("div", { class: "dlg__head" }, h("span", { text: "Table" }), close),
    h("div", { class: "dlg__body" }, h("div", { class: "table-card table-card--full" }, h("div", { class: "table-scroll", tabindex: "0" }, copy))),
  );
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close(); // backdrop click
  });
  open(dialog);
}
