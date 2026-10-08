// Main-pane cards for sessions without a ready document: upload (empty), indexing progress, failed.

import { humanMessage } from "./api.js";
import { h } from "./dom.js";
import { formatBytes, formatClock, plural, truncName } from "./format.js";
import { icon, logoMark } from "./icons.js";
import { state } from "./state.js";

export const UPLOAD_NOTE = "One document per chat. Upload before you start - it can't be changed after your first question";

/** Client-side checks that mirror the server's, so obvious mistakes fail fast without a round trip. */
export function validateFile(file, maxUploadMb) {
  const looksPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  if (!looksPdf) return "Only PDF files are supported.";
  if (file.size === 0) return "That file is empty.";
  if (file.size > maxUploadMb * 1024 * 1024) return `That file is ${formatBytes(file.size)}, which is over the ${maxUploadMb} MB limit.`;
  return null;
}

/** Dropzone + hidden file input. `pick()` opens the OS picker (also used by the composer's Attach pill). */
export class UploadCard {
  /** @param {{maxUploadMb: number, onFile: (file: File) => void, onCancelUpload: () => void, heading?: object}} opts */
  constructor(opts) {
    this.opts = opts;
    this.input = h("input", {
      type: "file",
      accept: "application/pdf,.pdf",
      class: "sr-only",
      tabindex: "-1",
      "aria-hidden": "true",
      on: {
        change: () => {
          const file = this.input.files?.[0];
          this.input.value = "";
          if (file) opts.onFile(file);
        },
      },
    });
    this.zone = h("button", { type: "button", class: "dropzone", "aria-describedby": "upload-note upload-error", on: { click: () => this.pick() } });
    this.errorEl = h("p", { class: "upload-error", id: "upload-error", role: "alert", hidden: true });
    this.el = h(
      "div",
      { class: "state-card state-card--upload" },
      h("div", { class: "state-card__logo", html: logoMark(40) }),
      opts.heading || [
        h("h2", { class: "state-card__title", text: "Ask questions about an annual report" }),
        h("p", { class: "state-card__sub", text: "Upload one PDF. It is indexed once, then every answer cites its pages." }),
      ],
      this.zone,
      this.errorEl,
      h("p", { class: "upload-note", id: "upload-note" }, h("span", { html: icon("lock", { size: 14 }) }), UPLOAD_NOTE),
      this.input,
    );
    this.showIdle();
  }

  pick() {
    this.input.click();
  }

  showIdle() {
    this.zone.disabled = false;
    this.zone.classList.remove("is-busy");
    this.zone.innerHTML =
      `<span class="dropzone__icon">${icon("file-up", { size: 32 })}</span>` +
      `<span class="dropzone__title">Drop your annual report PDF here, or <span class="dropzone__link">choose a file</span></span>` +
      `<span class="dropzone__hint">PDF only · up to ${this.opts.maxUploadMb} MB</span>`;
  }

  /** Shows the upload in flight with a determinate bar. */
  showUploading(file, fraction = 0) {
    this.clearError();
    this.zone.classList.add("is-busy");
    this.zone.disabled = true;
    const pct = Math.round(fraction * 100);
    this.zone.innerHTML =
      `<span class="dropzone__icon">${icon("loader-circle", { size: 28, cls: "spin" })}</span>` +
      `<span class="dropzone__title"></span>` +
      `<span class="bar" role="progressbar" aria-label="Upload progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span class="bar__fill" style="width:${pct}%"></span></span>` +
      `<span class="dropzone__hint tnum">${formatBytes(file.size)} · ${pct}%</span>`;
    this.zone.querySelector(".dropzone__title").textContent = `Uploading ${truncName(file.name, 40)}`;
    if (!this.cancelBtn) {
      this.cancelBtn = h("button", { type: "button", class: "btn btn-sm upload-cancel", text: "Cancel upload", on: { click: () => this.opts.onCancelUpload() } });
      this.zone.after(this.cancelBtn);
    }
  }

  endUpload() {
    this.cancelBtn?.remove();
    this.cancelBtn = null;
    this.showIdle();
  }

  showError(text) {
    this.errorEl.hidden = false;
    this.errorEl.textContent = text;
  }

  showApiError(err) {
    this.showError(humanMessage(err));
  }

  clearError() {
    this.errorEl.hidden = true;
    this.errorEl.textContent = "";
  }
}

const SLOW_HOST_NOTE = "This demo runs on a small free server, so indexing can take several minutes (a 300-page report 10 minutes or more). Keep this tab open.";

const STAGES = [
  ["validating", "Validating the PDF"],
  ["extracting_text", "Extracting text"],
  ["building_tree", "Building the outline"],
  ["summarizing", "Summarising sections"],
  ["finalizing", "Finalising"],
];

/** Live indexing card. `update(session)` is called by the poller every second; the DOM is patched in place. */
export class IndexingCard {
  /** @param {{onCancel: () => void}} opts */
  constructor({ onCancel }) {
    this.startedLocal = Date.now();
    this.createdAt = 0;
    this.statusEl = h("p", { class: "indexing__status", "aria-live": "polite" });
    this.nameEl = h("div", { class: "indexing__name" });
    this.metaEl = h("div", { class: "indexing__meta" });
    this.pctEl = h("span", { class: "tnum indexing__pct" });
    this.clockEl = h("span", { class: "tnum indexing__clock", "aria-hidden": "true" });
    this.fill = h("span", { class: "bar__fill" });
    this.bar = h("div", { class: "bar", role: "progressbar", "aria-label": "Indexing progress", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" }, this.fill);
    this.list = h("ol", { class: "stages" });
    // On a small free host (Render free: 0.1 CPU) indexing a long report really does take many minutes; say so up front.
    this.noteEl = state.config?.low_memory ? h("p", { class: "state-card__sub indexing__note", text: SLOW_HOST_NOTE }) : null;
    this.stageItems = STAGES.map(([key, label]) => {
      const li = h("li", { class: "stage", dataset: { stage: key } }, h("span", { class: "stage__mark" }), h("span", { class: "stage__label", text: label }));
      this.list.append(li);
      return li;
    });
    this.el = h(
      "div",
      { class: "state-card state-card--indexing" },
      h("div", { class: "indexing__file" }, h("span", { class: "indexing__icon", html: icon("file-text", { size: 22 }) }), h("div", { class: "indexing__titles" }, this.nameEl, this.metaEl)),
      this.statusEl,
      this.noteEl,
      h("div", { class: "indexing__bar" }, this.bar, this.pctEl),
      h("div", { class: "indexing__foot" }, h("span", { class: "indexing__elapsed" }, "Elapsed ", this.clockEl), h("button", { type: "button", class: "btn btn-sm", text: "Cancel", on: { click: onCancel } })),
      this.list,
    );
    this.tick = setInterval(() => this.renderClock(), 1000);
    this.stage = null;
  }

  update(session) {
    const doc = session.document;
    if (!doc) return;
    const created = Date.parse(doc.created_at);
    this.createdAt = Number.isFinite(created) && Date.now() - created >= 0 && Date.now() - created < 864e5 ? created : 0;
    this.nameEl.textContent = doc.filename;
    this.nameEl.title = doc.filename;
    const meta = [formatBytes(doc.size_bytes)];
    if (doc.page_count) meta.unshift(plural(doc.page_count, "page"));
    this.metaEl.textContent = meta.filter(Boolean).join(" · ");

    const pct = Math.round(Math.min(1, Math.max(0, doc.progress || 0)) * 100);
    this.fill.style.width = `${pct}%`;
    this.bar.setAttribute("aria-valuenow", String(pct));
    this.pctEl.textContent = `${pct}%`;

    const current = STAGES.findIndex(([key]) => key === doc.stage);
    this.stageItems.forEach((li, i) => {
      const state = doc.stage === "ready" || (current >= 0 && i < current) ? "done" : i === current ? "current" : "todo";
      li.className = `stage is-${state}`;
      li.firstChild.innerHTML = state === "done" ? icon("circle-check", { size: 16 }) : state === "current" ? icon("loader-circle", { size: 16, cls: "spin" }) : icon("circle", { size: 16 });
      if (state === "current") li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    });
    if (doc.stage !== this.stage) {
      this.stage = doc.stage;
      this.statusEl.textContent = current >= 0 ? `Indexing: ${STAGES[current][1].toLowerCase()}` : doc.stage === "queued" ? "Waiting to start indexing" : "Finishing up";
    }
    this.renderClock();
  }

  renderClock() {
    const since = this.createdAt || this.startedLocal;
    this.clockEl.textContent = formatClock((Date.now() - since) / 1000);
  }

  destroy() {
    clearInterval(this.tick);
  }
}

/** Failed indexing: explains what went wrong and offers the dropzone again (re-upload is allowed in this state). */
export function failedHeading(session) {
  const doc = session.document;
  return [
    h("h2", { class: "state-card__title", text: "We couldn't index this document" }),
    h(
      "div",
      { class: "alert alert--error", role: "alert" },
      h("span", { html: icon("triangle-alert", { size: 16 }) }),
      h("div", {}, h("div", { class: "alert__title", text: doc?.filename || "The uploaded PDF" }), h("div", { text: doc?.error || "Indexing failed for an unknown reason." })),
    ),
    h("p", { class: "state-card__sub", text: "Upload a different file to try again." }),
  ];
}
