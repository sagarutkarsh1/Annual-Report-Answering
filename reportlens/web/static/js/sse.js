// POST-based Server-Sent Events client: fetch + ReadableStream (EventSource is GET-only).
// The stream is never retried automatically: replaying a POST would ask the question twice.

import { noteApiError } from "./api.js";

/** Incremental SSE frame parser. Feed text chunks, get complete frames back. */
export class SSEParser {
  constructor() {
    this.buffer = "";
  }

  /** @returns {{event: string, id: string|null, data: string}[]} frames completed by this chunk */
  push(chunk) {
    // A lone trailing "\r" may be the first half of "\r\n": keep it in the buffer until the next chunk.
    this.buffer = (this.buffer + chunk).replace(/\r\n/g, "\n");
    const frames = [];
    let end;
    while ((end = this.buffer.indexOf("\n\n")) >= 0) {
      const raw = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const frame = parseFrame(raw);
      if (frame) frames.push(frame);
    }
    return frames;
  }
}

function parseFrame(raw) {
  let event = "message";
  let id = null;
  const data = [];
  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank or ": ping" keep-alive comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "id") id = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, id, data: data.join("\n") } : null;
}

/**
 * POSTs `body` as JSON and dispatches every SSE frame to `onEvent(name, payload)`.
 * Failures (HTTP errors before the stream starts, dropped connections) are reported as an
 * "error" event with `{code, message, status?}`; an abort via `signal` ends quietly.
 * @returns {Promise<{aborted: boolean}>} resolves when the stream ends
 */
export async function postSSE(url, body, { signal, onEvent }) {
  const emit = (name, payload) => {
    try {
      onEvent(name, payload);
    } catch (err) {
      console.error("SSE handler failed for", name, err);
    }
  };

  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (signal?.aborted) return { aborted: true };
    emit("error", { code: "network", message: "Could not reach the server." });
    return { aborted: false };
  }

  if (!response.ok) {
    let code = `http_${response.status}`;
    let message = `The server answered with HTTP ${response.status}.`;
    try {
      const payload = await response.json();
      code = payload?.error?.code || code;
      message = payload?.error?.message || message;
    } catch {
      /* non-JSON error body: keep the generic text */
    }
    noteApiError(code);
    emit("error", { code, message, status: response.status });
    return { aborted: false };
  }
  if (!response.body) {
    emit("error", { code: "stream_unsupported", message: "This browser cannot stream responses." });
    return { aborted: false };
  }

  const parser = new SSEParser();
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        let payload;
        try {
          payload = JSON.parse(frame.data);
        } catch {
          console.warn("Ignoring malformed SSE frame", frame);
          continue;
        }
        emit(frame.event, payload);
      }
    }
    for (const frame of parser.push(decoder.decode() + "\n\n")) {
      try {
        emit(frame.event, JSON.parse(frame.data));
      } catch {
        /* truncated final frame: drop it */
      }
    }
  } catch (err) {
    if (signal?.aborted) return { aborted: true };
    emit("error", { code: "stream_interrupted", message: "The connection to the server was lost." });
  }
  return { aborted: Boolean(signal?.aborted) };
}
