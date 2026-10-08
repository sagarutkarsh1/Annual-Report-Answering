// Sidebar: brand, New chat, Recents (sessions) with inline rename, delete and "new chat with this document".

import { h } from "./dom.js";
import { icon, logoMark } from "./icons.js";
import { state, subscribe } from "./state.js";

const DEFAULT_TITLE = "New chat";

/** Row label: the server title, or the document name until the first question gives the chat a real title. */
export function sessionLabel(session) {
  if (session.title && session.title !== DEFAULT_TITLE) return session.title;
  return session.document?.filename || DEFAULT_TITLE;
}

const STATE_ICON = {
  indexing: { name: "loader-circle", cls: "spin", tip: "Indexing document" },
  locked: { name: "lock", cls: "", tip: "Document locked to this chat" },
  failed: { name: "triangle-alert", cls: "is-bad", tip: "Indexing failed" },
};

export class Sidebar {
  /**
   * @param {HTMLElement} root
   * @param {{onSelect: (sid: string) => void, onNewChat: () => void, onRename: (sid: string, title: string) => void,
   *          onDelete: (sid: string) => void, onNewFromDocument: (sid: string) => void, onToggle: () => void,
   *          onSignOut: () => void}} handlers
   */
  constructor(root, handlers) {
    this.root = root;
    this.handlers = handlers;
    this.rows = new Map();
    this.recentsOpen = true;
    this.renamingId = null;
    this.menu = null;
    this.build();
    subscribe((_, keys) => {
      if (keys.some((k) => k === "sessions" || k === "activeId" || k === "config" || k === "auth")) this.render();
    });
    document.addEventListener("click", (e) => {
      if (this.menu && !this.menu.el.contains(e.target) && !e.target.closest(".sess__menu")) this.closeMenu();
    });
  }

  build() {
    this.newChatBtn = h(
      "button",
      { type: "button", class: "nav-item", "data-tip": "New chat (Ctrl+Shift+O)", on: { click: () => this.handlers.onNewChat() } },
      h("span", { class: "nav-item__icon", html: icon("message-circle") }),
      h("span", { class: "nav-item__label", text: "New chat" }),
    );
    this.toggleBtn = h("button", {
      type: "button",
      class: "icon-btn sidebar__toggle",
      "aria-label": "Collapse sidebar",
      "data-tip": "Toggle sidebar (Ctrl+B)",
      html: icon("panel-left", { size: 18 }),
      on: { click: () => this.handlers.onToggle() },
    });
    this.recentsBtn = h(
      "button",
      { type: "button", class: "recents__head", aria: { expanded: "true", controls: "recents-list" }, on: { click: () => this.toggleRecents() } },
      h("span", { text: "Recents" }),
      h("span", { class: "recents__chev", html: icon("chevron-down", { size: 14 }) }),
    );
    this.list = h("ul", { class: "recents__list", id: "recents-list" });
    this.empty = h("p", { class: "recents__empty", text: "Your chats will appear here.", hidden: true });
    this.foot = h("div", { class: "sidebar__foot" });
    this.root.append(
      h("div", { class: "sidebar__top" }, h("a", { class: "brand", href: "#/", "aria-label": "ReportLens home", html: `${logoMark(28)}<span class="brand__word">Report<span>Lens</span></span>` }), this.toggleBtn),
      h("nav", { class: "sidebar__nav", "aria-label": "Chats" }, this.newChatBtn),
      h("div", { class: "recents" }, this.recentsBtn, this.list, this.empty),
      this.foot,
    );
    this.list.addEventListener("keydown", (e) => this.onListKey(e));
  }

  toggleRecents() {
    this.recentsOpen = !this.recentsOpen;
    this.recentsBtn.setAttribute("aria-expanded", String(this.recentsOpen));
    this.list.hidden = !this.recentsOpen;
    this.root.classList.toggle("recents-collapsed", !this.recentsOpen);
  }

  /** Reconciles the list in place so focus, hover and an open rename input survive updates. */
  render() {
    const sessions = state.sessions;
    const seen = new Set();
    sessions.forEach((session, i) => {
      seen.add(session.id);
      let row = this.rows.get(session.id);
      if (!row) {
        row = this.createRow(session);
        this.rows.set(session.id, row);
      }
      this.updateRow(row, session);
      if (this.list.children[i] !== row.li) this.list.insertBefore(row.li, this.list.children[i] || null);
    });
    for (const [id, row] of this.rows) {
      if (!seen.has(id)) {
        row.li.remove();
        this.rows.delete(id);
      }
    }
    this.empty.hidden = sessions.length > 0;
    this.renderFoot();
  }

  renderFoot() {
    const cfg = state.config;
    this.foot.replaceChildren();
    if (!cfg) return;
    if (state.auth?.required) {
      this.foot.append(
        h("button", { type: "button", class: "nav-item sidebar__signout", on: { click: () => this.handlers.onSignOut() } },
          h("span", { class: "nav-item__icon", html: icon("log-out") }), h("span", { class: "nav-item__label", text: "Sign out" })),
      );
    }
    if (cfg.demo_mock) this.foot.append(h("div", { class: "badge badge--demo", text: "Demo mode · mock model" }));
    this.foot.append(h("div", { class: "sidebar__models", text: `Answers: ${cfg.chat_model}`, title: `Answer model ${cfg.chat_model}\nIndex model ${cfg.index_model}\nJudge ${cfg.judge_model}` }));
  }

  createRow(session) {
    const main = h("button", { type: "button", class: "sess__main", on: { click: () => this.handlers.onSelect(session.id) } });
    const title = h("span", { class: "sess__title" });
    const status = h("span", { class: "sess__state" });
    main.append(title, status);
    const menuBtn = h("button", {
      type: "button",
      class: "sess__menu",
      "aria-label": "Chat options",
      "aria-haspopup": "menu",
      html: icon("ellipsis", { size: 16 }),
      on: { click: (e) => (e.stopPropagation(), this.openMenu(session.id, menuBtn)) },
    });
    const li = h("li", { class: "sess", dataset: { sid: session.id } }, main, menuBtn);
    li.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      this.openMenu(session.id, menuBtn);
    });
    return { li, main, title, status, menuBtn };
  }

  updateRow(row, session) {
    const label = sessionLabel(session);
    const active = session.id === state.activeId;
    row.li.classList.toggle("is-active", active);
    if (active) row.main.setAttribute("aria-current", "page");
    else row.main.removeAttribute("aria-current");
    row.title.textContent = label;
    row.main.title = label;
    const st = STATE_ICON[session.state];
    row.status.innerHTML = st ? icon(st.name, { size: 14, cls: st.cls }) : "";
    row.status.title = st?.tip || "";
    row.status.className = `sess__state${st ? ` sess__state--${session.state}` : ""}`;
    row.main.setAttribute("aria-label", `${label}${st ? `, ${st.tip.toLowerCase()}` : ""}`);
  }

  /** Arrow keys move between rows, F2 renames, Delete removes (with confirmation). */
  onListKey(e) {
    const main = e.target.closest(".sess__main");
    if (!main || this.renamingId) return;
    const mains = Array.from(this.list.querySelectorAll(".sess__main"));
    const i = mains.indexOf(main);
    const sid = main.closest(".sess").dataset.sid;
    if (e.key === "ArrowDown") (mains[i + 1] || mains[i]).focus();
    else if (e.key === "ArrowUp") (mains[i - 1] || mains[i]).focus();
    else if (e.key === "Home") mains[0].focus();
    else if (e.key === "End") mains[mains.length - 1].focus();
    else if (e.key === "F2") this.startRename(sid);
    else if (e.key === "Delete") this.handlers.onDelete(sid);
    else return;
    e.preventDefault();
  }

  // ----- context menu -----
  openMenu(sid, anchor) {
    this.closeMenu();
    const session = state.sessions.find((s) => s.id === sid);
    if (!session) return;
    const items = [
      { label: "Rename", icon: "pencil", run: () => this.startRename(sid) },
      session.document?.status === "ready" && { label: "New chat with this document", icon: "message-square-plus", run: () => this.handlers.onNewFromDocument(sid) },
      { label: "Delete", icon: "trash-2", danger: true, run: () => this.handlers.onDelete(sid) },
    ].filter(Boolean);
    const el = h(
      "div",
      { class: "menu", role: "menu", "aria-label": "Chat options" },
      items.map((item) =>
        h("button", {
          type: "button",
          role: "menuitem",
          class: `menu__item${item.danger ? " is-danger" : ""}`,
          html: `${icon(item.icon)}<span>${item.label}</span>`,
          on: { click: () => (this.closeMenu(false), item.run()) },
        }),
      ),
    );
    document.body.append(el);
    const r = anchor.getBoundingClientRect();
    el.style.top = `${Math.min(r.bottom + 4, window.innerHeight - el.offsetHeight - 8)}px`;
    el.style.left = `${Math.min(r.left, window.innerWidth - el.offsetWidth - 8)}px`;
    el.addEventListener("keydown", (e) => {
      const btns = Array.from(el.querySelectorAll("button"));
      const i = btns.indexOf(document.activeElement);
      if (e.key === "ArrowDown") btns[(i + 1) % btns.length].focus();
      else if (e.key === "ArrowUp") btns[(i - 1 + btns.length) % btns.length].focus();
      else if (e.key === "Escape" || e.key === "Tab") this.closeMenu();
      else return;
      e.preventDefault();
    });
    this.menu = { el, anchor };
    anchor.setAttribute("aria-expanded", "true");
    el.querySelector("button").focus();
  }

  closeMenu(restoreFocus = true) {
    if (!this.menu) return;
    const { el, anchor } = this.menu;
    el.remove();
    anchor.removeAttribute("aria-expanded");
    this.menu = null;
    if (restoreFocus && anchor.isConnected) anchor.closest(".sess").querySelector(".sess__main").focus();
  }

  // ----- inline rename -----
  startRename(sid) {
    const row = this.rows.get(sid);
    const session = state.sessions.find((s) => s.id === sid);
    if (!row || !session) return;
    this.renamingId = sid;
    const input = h("input", { class: "sess__input", type: "text", maxlength: "120", value: sessionLabel(session), "aria-label": "Chat name" });
    row.li.classList.add("is-renaming");
    row.main.hidden = true; // the input must not live inside the row button (Enter would activate it)
    row.li.prepend(input);
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const value = input.value.trim();
      this.renamingId = null;
      row.li.classList.remove("is-renaming");
      input.remove();
      row.main.hidden = false;
      if (save && value && value !== sessionLabel(session)) this.handlers.onRename(sid, value);
      row.main.focus();
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key !== "Enter" && e.key !== "Escape") return;
      e.preventDefault(); // otherwise the follow-up keypress would "click" the row button that regains focus
      finish(e.key === "Enter");
    });
    input.addEventListener("blur", () => finish(true));
    input.focus();
    input.select();
  }

  setCollapsed(collapsed) {
    this.toggleBtn.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  }
}
