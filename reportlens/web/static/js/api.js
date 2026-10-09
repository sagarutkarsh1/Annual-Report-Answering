// Thin fetch wrappers for the REST API (docs/ARCHITECTURE.md section 5).
// Every failure becomes an ApiError(code, message, status) so callers handle one shape.

import { APP_NAME } from "./brand.js";
import { llmHeaders } from "./llmstore.js";

export class ApiError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

/** Limits from GET /api/config, used to word a few error messages. */
const limits = { maxUploadMb: 100, maxPages: 1200, publicMode: false, maxBatch: 10 };
export function setLimits(next) {
  Object.assign(limits, next);
}

/** Callbacks for the errors that change what the whole page shows (set once by main.js). */
const hooks = { authRequired: null, budgetExhausted: null, ownKeyRequired: null };
export function setErrorHooks(next) {
  Object.assign(hooks, next);
}

/** Every failed response passes through here: a lost login or a used-up budget is handled once, wherever it surfaced. */
export function noteApiError(code) {
  if (code === "auth_required") hooks.authRequired?.();
  else if (code === "budget_exhausted") hooks.budgetExhausted?.();
  else if (code === "own_key_required") hooks.ownKeyRequired?.();
}

/** Human wording per error code (the server's own message is the fallback). */
const HUMAN = {
  document_locked: () => "The document is locked after the first question. Start a new chat to use a different one.",
  document_already_uploaded: () => "This chat already has a document. Start a new chat to use a different one.",
  document_not_ready: () => "The document is still being indexed. You can ask once it is ready.",
  document_not_found: () => "The document file could not be found on the server.",
  file_too_large: () => `That file is larger than the ${limits.maxUploadMb} MB limit.`,
  scanned_pdf: () => `This PDF looks scanned (it has no selectable text). ${APP_NAME} needs a text-based PDF.`,
  encrypted_pdf: () => "This PDF is password-protected. Remove the password and upload it again.",
  invalid_pdf: () => "That file is not a valid PDF.",
  too_many_pages: () => `This PDF has too many pages (limit ${limits.maxPages}).`,
  openai_not_configured: () => "OpenAI API key is not configured - add OPENAI_API_KEY to .env and restart",
  openai_auth: () => "OpenAI rejected the API key. Check OPENAI_API_KEY in .env and restart.",
  openai_rate_limit: () => "OpenAI rate limit reached. Wait a moment and try again.",
  openai_model: () => "The configured OpenAI model is not available for this API key.",
  agent_max_turns: () => "The agent ran out of reasoning steps before answering. Try a narrower question.",
  agent_failed: (m) => (m && m !== "agent_failed" ? m : "The agent could not finish this answer."),
  auth_required: () => "Enter the access code to continue.",
  invalid_code: () => "That access code is not correct.",
  too_many_attempts: (m) => m || "Too many wrong access codes. Wait a few minutes and try again.",
  budget_exhausted: () => "The demo's usage budget has been used up. Please contact the owner.",
  rate_limited: (m) => m || "You have reached the hourly question limit. Please try again later.",
  session_limit: (m) => m || "This demo is at its limit of chats. Delete one to start another.",
  session_busy: () => "The previous question is still being answered. Wait for it to finish or press Stop.",
  // On a free public host the whole server sleeps after ~15 idle minutes and comes back empty: say so instead of "no longer exists".
  session_not_found: () => (limits.publicMode ? "The demo restarted (free hosting sleeps). Please start a new chat and upload again." : "That chat no longer exists."),
  empty_question: () => "Type a question first.",
  too_many_questions: () => `You can run at most ${limits.maxBatch} questions at once.`,
  cancelled: () => "Stopped before the answer was finished.",
  network: () => `Cannot reach the ${APP_NAME} server. Check that it is still running.`,
  demo_read_only: () => "The demo chat is read-only. Sign in and start your own chat to ask questions.",
  invalid_llm_config: (m) => m || "Your model settings are not valid. Open 'Model & API key' to fix them.",
  own_key_required: () => "This server runs on your own API key. Open 'Model & API key' to add one.",
  own_key_disabled: () => "This server does not accept your own API key. Remove it under 'Model & API key'.",
  stream_interrupted: () => "The connection to the server was lost before the answer finished.",
};

export function humanMessage(err) {
  const code = err?.code;
  const message = err?.message;
  return (HUMAN[code] && HUMAN[code](message)) || message || "Something went wrong.";
}

async function request(method, path, { json, signal, llm } = {}) {
  let response;
  try {
    const base = json === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" };
    response = await fetch(path, {
      method,
      headers: { ...base, ...llmHeaders(llm) },
      body: json === undefined ? undefined : JSON.stringify(json),
      signal,
    });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError("network", "Cannot reach the server.", 0);
  }
  if (response.status === 204) return null;
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (!response.ok) {
    const e = payload?.error;
    noteApiError(e?.code);
    throw new ApiError(e?.code || `http_${response.status}`, e?.message || `Request failed (HTTP ${response.status}).`, response.status);
  }
  return payload;
}

const enc = encodeURIComponent;

export const api = {
  auth: () => request("GET", "/api/auth"),
  demo: () => request("GET", "/api/demo"),
  login: (code) => request("POST", "/api/login", { json: { code } }),
  logout: () => request("POST", "/api/logout"),
  health: () => request("GET", "/api/health"),
  config: () => request("GET", "/api/config"),
  listSessions: () => request("GET", "/api/sessions"),
  createSession: (fromSession) => request("POST", "/api/sessions", { json: fromSession ? { from_session: fromSession } : {} }),
  getSession: (sid, opts) => request("GET", `/api/sessions/${enc(sid)}`, opts),
  renameSession: (sid, title) => request("PATCH", `/api/sessions/${enc(sid)}`, { json: { title } }),
  deleteSession: (sid) => request("DELETE", `/api/sessions/${enc(sid)}`),
  documentPages: (sid) => request("GET", `/api/sessions/${enc(sid)}/document/pages`),
  documentOutline: (sid) => request("GET", `/api/sessions/${enc(sid)}/document/outline`),
  locate: (sid, { page, quote, claim }) => {
    const q = new URLSearchParams({ page: String(page) });
    if (quote) q.set("quote", quote);
    if (claim) q.set("claim", claim);
    return request("GET", `/api/sessions/${enc(sid)}/locate?${q}`);
  },
  getMessage: (sid, mid) => request("GET", `/api/sessions/${enc(sid)}/messages/${enc(mid)}`),
  evaluate: (sid, mid) => request("POST", `/api/sessions/${enc(sid)}/messages/${enc(mid)}/evaluate`),
  /** "Test connection" for a provider choice that may not be saved yet. */
  checkLLM: (choice) => request("POST", "/api/llm/check", { llm: choice }),
  documentFileUrl: (sid) => `/api/sessions/${enc(sid)}/document/file`,
  messagesUrl: (sid) => `/api/sessions/${enc(sid)}/messages`,
  batchUrl: (sid) => `/api/sessions/${enc(sid)}/batch`,
};

/**
 * Multipart upload via XHR (fetch has no upload progress). Resolves with the Session (state "indexing").
 * @param {{onProgress?: (fraction: number) => void, signal?: AbortSignal}} opts
 */
export function uploadDocument(sid, file, { onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/sessions/${enc(sid)}/document`);
    xhr.setRequestHeader("Accept", "application/json");
    for (const [name, value] of Object.entries(llmHeaders())) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new ApiError("network", "Cannot reach the server.", 0));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    xhr.onload = () => {
      let payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        /* non-JSON body */
      }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(payload);
      const e = payload?.error;
      noteApiError(e?.code);
      reject(new ApiError(e?.code || `http_${xhr.status}`, e?.message || `Upload failed (HTTP ${xhr.status}).`, xhr.status));
    };
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}
