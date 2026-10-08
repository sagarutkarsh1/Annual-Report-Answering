// The visitor's own model provider and API key, kept in THIS browser only (sessionStorage by default: gone when the tab
// closes; localStorage when "Remember on this device" is ticked) and sent with each of their requests in one header.
// The server uses it for that request and never stores or logs it (reportlens/providers.py).

const STORE_KEY = "arl.llm.v1";
let memory = null; // fallback when the browser blocks storage (private mode, strict settings)

function stores() {
  const out = [];
  for (const name of ["sessionStorage", "localStorage"]) {
    try {
      if (window[name]) out.push(window[name]);
    } catch {
      /* storage blocked */
    }
  }
  return out;
}

/** @returns {{provider: string, api_key?: string, base_url?: string, chat_model?: string, index_model?: string,
 *            judge_model?: string, embedding_model?: string} | null} */
export function loadLLM() {
  for (const store of stores()) {
    try {
      const raw = store.getItem(STORE_KEY);
      if (raw) return JSON.parse(raw);
    } catch {
      /* unreadable entry: ignore */
    }
  }
  return memory;
}

export function hasLLM() {
  return !!loadLLM()?.provider;
}

export function isRemembered() {
  try {
    return !!window.localStorage?.getItem(STORE_KEY);
  } catch {
    return false;
  }
}

export function saveLLM(choice, remember) {
  clearLLM();
  memory = choice;
  const target = remember ? "localStorage" : "sessionStorage";
  try {
    window[target].setItem(STORE_KEY, JSON.stringify(choice));
  } catch {
    /* kept in memory for this page only */
  }
}

export function clearLLM() {
  memory = null;
  for (const store of stores()) {
    try {
      store.removeItem(STORE_KEY);
    } catch {
      /* ignore */
    }
  }
}

/** base64url of UTF-8 JSON (headers must be ASCII). */
export function encodeChoice(choice) {
  const bytes = new TextEncoder().encode(JSON.stringify(choice));
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Headers for a request: the visitor's choice when they have one (or `choice` when given, for "Test connection"). */
export function llmHeaders(choice) {
  const c = choice || loadLLM();
  return c?.provider ? { "X-LLM-Config": encodeChoice(c) } : {};
}
