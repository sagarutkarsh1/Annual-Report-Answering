// The "Enter access code" screen, shown when the server answers 401 auth_required (a public deployment with ACCESS_CODE set).

import { api, humanMessage } from "./api.js";
import { h } from "./dom.js";
import { icon, logoMark } from "./icons.js";

let pending = null;

/**
 * Shows the access-code screen over the app and resolves once the server has accepted a code (it then holds the login cookie).
 * Calls made while the screen is already open share it.
 * @returns {Promise<void>}
 */
export function promptLogin() {
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
    const form = h(
      "form",
      { class: "login__card", novalidate: true },
      h("span", { class: "login__logo", html: logoMark(40) }),
      h("h1", { class: "login__title", text: "Enter access code" }),
      h("p", { id: "login-hint", class: "login__sub", text: "This demo is private. Type the access code you were given to continue." }),
      h("label", { class: "login__label", for: "login-code", text: "Access code" }),
      input,
      error,
      submit,
    );
    const screen = h("div", { id: "login", class: "login" }, form);

    const fail = (message) => {
      error.replaceChildren(h("span", { html: icon("circle-alert", { size: 14 }) }), h("span", { text: message }));
      input.setAttribute("aria-invalid", "true");
      input.focus();
      input.select();
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
      screen.remove();
      pending = null;
      resolve();
    });

    (document.getElementById("app") || document.body).append(screen);
    input.focus();
  });
  return pending;
}
