// The access-code screen, shown when the server answers 401 auth_required (a deployment with ACCESS_CODE set).
// It also offers the read-only demo chat (no code needed) and, when the owner configured one, an e-mail to ask for a code.

import { api, humanMessage } from "./api.js";
import { APP_NAME, TAGLINE, creditLine, requestCodeHref } from "./brand.js";
import { h } from "./dom.js";
import { icon, logoMark } from "./icons.js";

let pending = null;

/**
 * Shows the access-code screen over the app.  Resolves with "login" once the server has accepted a code (it then holds the
 * login cookie), "demo" when the visitor chose the demo chat, or "cancel" (only when `cancellable`: Escape / Back).
 * Calls made while the screen is already open share it.
 * @param {{requestEmail?: string|null, demo?: {available: boolean, title?: string, filename?: string}|null, cancellable?: boolean}} [opts]
 * @returns {Promise<"login" | "demo" | "cancel">}
 */
export function promptLogin({ requestEmail = null, demo = null, cancellable = false } = {}) {
  if (pending) return pending;
  pending = new Promise((resolve) => {
    const input = h("input", {
      id: "login-code",
      class: "login__input",
      type: "password",
      name: "access-code",
      autocomplete: "off",
      autocapitalize: "off",
      spellcheck: "false",
      required: true,
      maxlength: "200",
      aria: { describedby: "login-hint login-error" },
    });
    const error = h("p", { id: "login-error", class: "login__error", role: "status", aria: { live: "polite" } });
    const submit = h("button", { type: "submit", class: "btn btn-primary login__submit", text: "Continue" });
    const finish = (result) => {
      screen.remove();
      document.removeEventListener("keydown", onKey);
      pending = null;
      resolve(result);
    };
    const request = requestEmail
      ? h(
          "p",
          { class: "login__request" },
          h("span", { html: icon("mail", { size: 14 }) }),
          h("span", { text: "No code yet? " }),
          h("a", { href: requestCodeHref(requestEmail), text: `Email ${requestEmail}` }),
          h("span", { text: " to request one." }),
        )
      : null;
    const demoBlock = demo?.available
      ? h(
          "div",
          { class: "login__alt" },
          h("div", { class: "login__divider", role: "separator" }, h("span", { text: "or" })),
          h(
            "button",
            { type: "button", class: "btn login__demo", on: { click: () => finish("demo") } },
            h("span", { html: icon("eye", { size: 16 }) }),
            h("span", { text: "View a demo chat" }),
          ),
          h("p", { class: "login__demo-sub", text: `A real conversation about the ${(demo.title || "").replace(/^Demo:\s*/i, "") || "an annual report"}: cited answers and their live scores. No code needed.` }),
        )
      : null;
    const back = cancellable ? h("button", { type: "button", class: "btn btn-ghost login__back", text: "Back to the demo", on: { click: () => finish("cancel") } }) : null;
    const form = h(
      "form",
      { class: "login__card", novalidate: true },
      h("span", { class: "login__logo", html: logoMark(40) }),
      h("h1", { class: "login__title", text: APP_NAME }),
      h("p", { class: "login__tagline", text: TAGLINE }),
      h("p", { id: "login-hint", class: "login__sub", text: "This app is invite-only. Type the access code you were given to start your own chats." }),
      h("label", { class: "login__label", for: "login-code", text: "Access code" }),
      input,
      error,
      submit,
      request,
      demoBlock,
      back,
      creditLine("login__credit"),
    );
    const screen = h("div", { id: "login", class: "login" }, form);

    const fail = (message) => {
      error.replaceChildren(h("span", { html: icon("circle-alert", { size: 14 }) }), h("span", { text: message }));
      input.setAttribute("aria-invalid", "true");
      input.focus();
      input.select();
    };
    const onKey = (e) => {
      if (cancellable && e.key === "Escape") finish("cancel");
    };

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const code = input.value.trim();
      if (!code) return fail("Enter the access code.");
      submit.disabled = true;
      submit.textContent = "Checking...";
      error.replaceChildren();
      input.removeAttribute("aria-invalid");
      try {
        await api.login(code);
      } catch (err) {
        fail(humanMessage(err));
        return;
      } finally {
        submit.disabled = false;
        submit.textContent = "Continue";
      }
      finish("login");
    });

    document.addEventListener("keydown", onKey);
    (document.getElementById("app") || document.body).append(screen);
    input.focus();
  });
  return pending;
}
