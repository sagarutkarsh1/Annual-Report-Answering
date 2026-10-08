// Product name, tagline and credits: one place, so the UI never spells them differently.

import { h } from "./dom.js";

export const APP_NAME = "Annual Report Lens";
export const TAGLINE = "Chat with your annual report, with live answer evaluation";
export const OWNER = { handle: "sagarutkarsh1", url: "https://github.com/sagarutkarsh1" };
export const CLAUDE_CODE_URL = "https://claude.com/claude-code";

/** "© 2026 sagarutkarsh1 · MIT licence · Built with Claude Code" with links (small print for the login screen and the sidebar). */
export function creditLine(cls = "credit") {
  const link = (href, text) => h("a", { href, text, target: "_blank", rel: "noopener noreferrer" });
  return h(
    "p",
    { class: cls },
    h("span", { text: "© 2026 " }),
    link(OWNER.url, OWNER.handle),
    h("span", { text: " · MIT licence · Built with " }),
    link(CLAUDE_CODE_URL, "Claude Code"),
  );
}

/** mailto: link that asks the owner for an access code (subject prefilled). */
export function requestCodeHref(email) {
  const subject = encodeURIComponent(`Access code request: ${APP_NAME}`);
  const body = encodeURIComponent(`Hello,\n\nI would like an access code for ${APP_NAME}.\n\nName:\nWhat I would like to try:\n`);
  return `mailto:${email}?subject=${subject}&body=${body}`;
}
