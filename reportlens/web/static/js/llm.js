// "Model & API key": the visitor picks a provider, pastes their own key and model ids, tests the connection and saves.
// Storage and the request header live in llmstore.js; the server side is reportlens/providers.py.

import { api, humanMessage } from "./api.js";
import { h } from "./dom.js";
import { icon } from "./icons.js";
import { clearLLM, isRemembered, loadLLM, saveLLM } from "./llmstore.js";
import { setState, state } from "./state.js";
import { toast } from "./toast.js";

/** "Gemini · your key" / "Server's model" for the sidebar. */
export function llmSummary() {
  const choice = loadLLM();
  if (!choice?.provider) return null;
  const provider = (state.config?.llm?.providers || []).find((p) => p.id === choice.provider);
  return { label: provider?.label || choice.provider, model: choice.chat_model || state.config?.llm?.openai_defaults?.chat_model || "" };
}

function field(id, label, input, hint) {
  return h("div", { class: "llm__field" }, h("label", { class: "llm__label", for: id, text: label }), input, hint ? h("p", { class: "llm__hint", id: `${id}-hint` }, hint) : null);
}

/** Opens the dialog; resolves when it closes (true when the settings changed). */
export function openLLMDialog() {
  return new Promise((resolve) => {
    const catalogue = state.config?.llm || { providers: [], openai_defaults: {} };
    const providers = catalogue.providers || [];
    const saved = loadLLM() || { provider: providers[0]?.id || "openai" };
    let changed = false;

    const provider = h("select", { id: "llm-provider", class: "llm__input" }, providers.map((p) => h("option", { value: p.id, text: p.label })));
    provider.value = providers.some((p) => p.id === saved.provider) ? saved.provider : providers[0]?.id || "openai";
    const key = h("input", { id: "llm-key", class: "llm__input", type: "password", autocomplete: "off", spellcheck: "false", maxlength: "512", value: saved.api_key || "" });
    const showKey = h("button", { type: "button", class: "icon-btn llm__show", "aria-label": "Show the key", html: icon("eye", { size: 16 }) });
    const baseUrl = h("input", { id: "llm-base", class: "llm__input", type: "url", spellcheck: "false", maxlength: "300", value: saved.base_url || "" });
    const chat = h("input", { id: "llm-chat", class: "llm__input", type: "text", spellcheck: "false", maxlength: "160", value: saved.chat_model || "" });
    const index = h("input", { id: "llm-index", class: "llm__input", type: "text", spellcheck: "false", maxlength: "160", value: saved.index_model || "" });
    const judge = h("input", { id: "llm-judge", class: "llm__input", type: "text", spellcheck: "false", maxlength: "160", value: saved.judge_model || "" });
    const embed = h("input", { id: "llm-embed", class: "llm__input", type: "text", spellcheck: "false", maxlength: "160", value: saved.embedding_model || "" });
    const remember = h("input", { id: "llm-remember", type: "checkbox" });
    remember.checked = isRemembered();
    const keyLinks = h("p", { class: "llm__hint" });
    const embedHint = h("p", { class: "llm__hint" });
    const status = h("div", { class: "llm__status", role: "status", "aria-live": "polite" });
    const baseField = field("llm-base", "Base URL", baseUrl, "Your provider's OpenAI-compatible endpoint, for example http://localhost:11434/v1 for Ollama.");

    const test = h("button", { type: "button", class: "btn", html: `${icon("refresh-cw", { size: 14 })}<span>Test connection</span>` });
    const save = h("button", { type: "submit", class: "btn btn-primary", text: "Save" });
    const remove = h("button", { type: "button", class: "btn btn-ghost llm__remove", text: saved.api_key || loadLLM() ? "Remove my key" : "Use the server's model" });
    const close = h("button", { type: "button", class: "icon-btn", "aria-label": "Close", html: icon("x") });

    const form = h(
      "form",
      { class: "llm", novalidate: true },
      h("div", { class: "dlg__head" }, h("span", { html: `${icon("key-round", { size: 16 })}` }), h("h2", { class: "dlg__title", id: "llm-title", text: "Model & API key" }), close),
      h(
        "div",
        { class: "dlg__body llm__body" },
        h("p", { class: "llm__intro", text: "Use your own model provider and key. Your key stays in this browser and is sent only with your own requests; the server uses it for that request and never stores or logs it. You pay your provider directly." }),
        field("llm-provider", "Provider", provider),
        h("div", { class: "llm__field" }, h("label", { class: "llm__label", for: "llm-key", text: "API key" }), h("div", { class: "llm__keyrow" }, key, showKey), keyLinks),
        baseField,
        field("llm-chat", "Answer model", chat, "The model that reads the report and answers (it must support tool calls)."),
        h(
          "details",
          { class: "llm__more" },
          h("summary", { text: "More models (optional)" }),
          field("llm-index", "Indexing model", index, "Writes the section summaries when a report is indexed. Default: the answer model."),
          field("llm-judge", "Judge model", judge, "Scores each answer with RAGAS. Default: the answer model."),
          h("div", { class: "llm__field" }, h("label", { class: "llm__label", for: "llm-embed", text: "Embedding model" }), embed, embedHint),
        ),
        h("label", { class: "llm__check" }, remember, h("span", { text: "Remember on this device (otherwise it is forgotten when you close the tab)" })),
        status,
      ),
      h("div", { class: "dlg__actions llm__actions" }, remove, h("span", { class: "llm__spacer" }), test, save),
    );
    const dialog = h("dialog", { class: "dlg dlg--llm", "aria-labelledby": "llm-title" }, form);

    const current = () => providers.find((p) => p.id === provider.value) || {};
    const choice = () => {
      const p = current();
      const out = { provider: p.id };
      const pairs = [["api_key", key], ["chat_model", chat], ["index_model", index], ["judge_model", judge], ["embedding_model", embed]];
      for (const [name, el] of pairs) if (el.value.trim()) out[name] = el.value.trim();
      if (!p.base_url || p.self_hosted_only) out.base_url = baseUrl.value.trim() || p.base_url || "";
      if (!out.base_url) delete out.base_url;
      return out;
    };
    const say = (kind, text) => {
      status.className = `llm__status llm__status--${kind}`;
      status.replaceChildren(h("span", { html: icon(kind === "ok" ? "circle-check" : kind === "bad" ? "triangle-alert" : "info", { size: 14 }) }), h("span", { text }));
    };
    const problem = () => {
      const p = current();
      if (p.key_required && !key.value.trim()) return "Enter your API key.";
      if (p.id !== "openai" && !chat.value.trim()) return `Enter the answer model id, exactly as ${p.label} names it.`;
      if ((!p.base_url || p.self_hosted_only) && !(baseUrl.value.trim() || p.base_url)) return "Enter the base URL.";
      return "";
    };
    const refresh = () => {
      const p = current();
      const defaults = catalogue.openai_defaults || {};
      const isOpenAI = p.id === "openai";
      baseField.hidden = !(p.self_hosted_only || !p.base_url);
      if (!baseUrl.value && p.base_url) baseUrl.value = p.base_url;
      key.placeholder = p.key_required ? "Paste your key" : "Not needed for this provider";
      chat.placeholder = isOpenAI ? `Default: ${defaults.chat_model || ""}` : "Model id, required";
      index.placeholder = isOpenAI ? `Default: ${defaults.index_model || ""}` : "Default: the answer model";
      judge.placeholder = isOpenAI ? `Default: ${defaults.judge_model || ""}` : "Default: the answer model";
      embed.placeholder = isOpenAI ? `Default: ${defaults.embedding_model || ""}` : p.embeddings ? "Optional" : "Not offered by this provider";
      keyLinks.replaceChildren(
        ...(p.key_url ? [h("a", { href: p.key_url, target: "_blank", rel: "noopener noreferrer", text: "Get a key" })] : []),
        ...(p.key_url && p.models_url ? [h("span", { text: " · " })] : []),
        ...(p.models_url ? [h("a", { href: p.models_url, target: "_blank", rel: "noopener noreferrer", text: "Model ids" })] : []),
      );
      embedHint.textContent = p.embeddings
        ? "Used only for the answer-relevancy score. Leave empty to skip that score."
        : "This provider has no embeddings API, so the answer-relevancy score is skipped (faithfulness and context precision still run).";
      status.replaceChildren();
      status.className = "llm__status";
    };

    provider.addEventListener("change", () => {
      baseUrl.value = "";
      refresh();
    });
    showKey.addEventListener("click", () => {
      key.type = key.type === "password" ? "text" : "password";
      showKey.setAttribute("aria-label", key.type === "password" ? "Show the key" : "Hide the key");
    });
    test.addEventListener("click", async () => {
      const issue = problem();
      if (issue) return say("bad", issue);
      test.disabled = true;
      say("info", "Testing: one tiny request to your provider...");
      try {
        const result = await api.checkLLM(choice());
        if (result.note) return say("ok", result.note);
        const failed = Object.entries(result.checks || {}).filter(([, c]) => !c.ok);
        if (!failed.length) say("ok", `Connected: ${result.answer_model} answered.`);
        else say("bad", failed.map(([name, c]) => `${name.replace("_", " ")}: ${c.status ? `HTTP ${c.status} - ` : ""}${c.message}`).join(" · "));
      } catch (err) {
        say("bad", humanMessage(err));
      } finally {
        test.disabled = false;
      }
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const issue = problem();
      if (issue) return say("bad", issue);
      saveLLM(choice(), remember.checked);
      changed = true;
      setState({ llm: llmSummary() });
      toast(`Using your ${current().label} key`);
      dialog.close();
    });
    remove.addEventListener("click", () => {
      clearLLM();
      changed = true;
      setState({ llm: null });
      toast("Your key was removed from this browser");
      dialog.close();
    });
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(changed);
    }, { once: true });

    refresh();
    document.body.append(dialog);
    dialog.showModal();
    (saved.api_key ? chat : key).focus();
  });
}
