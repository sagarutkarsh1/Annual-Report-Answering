// Applies SSE events (docs/ARCHITECTURE.md section 6) to a live assistant message object.
// Pure state mutation: no DOM, so the order/edge cases are easy to reason about.

import { humanMessage } from "./api.js";

/** Client-only fields live under `_ui` so they never collide with the server's Message shape. */
export function uiState(msg) {
  if (!msg._ui) msg._ui = { streaming: false, queued: false, eval: { running: false, startedAt: 0, nContexts: 0 } };
  return msg._ui;
}

/** Error codes message.js turns into its own wording (the neutral "Stopped" notice, friendly OpenAI text): keep the code. */
const CODE_ERRORS = new Set(["cancelled", "agent_max_turns"]);
const keepsCode = (code) => CODE_ERRORS.has(code) || (typeof code === "string" && code.startsWith("openai_"));

export function newUserMessage(sessionId, content) {
  return { id: `tmp-u-${Date.now()}`, session_id: sessionId, role: "user", content, status: "answered", created_at: new Date().toISOString() };
}

export function newAssistantMessage(sessionId) {
  const msg = {
    id: `tmp-a-${Date.now()}`,
    session_id: sessionId,
    role: "assistant",
    content: "",
    status: "streaming",
    citations: [],
    sources: [],
    steps: [],
    usage: null,
    elapsed_ms: null,
    evaluation: null,
    error: null,
    created_at: new Date().toISOString(),
  };
  uiState(msg).streaming = true;
  return msg;
}

/**
 * One item of a `batch_start` event -> the user bubble and the assistant placeholder it stands for.
 * The placeholder is "queued" (waiting for a free slot) until its first event arrives.
 * @param {{index: number, question: string, user_message?: object|string, message_id: string}} item
 */
export function batchPair(sessionId, item) {
  const given = item.user_message;
  const userMsg = newUserMessage(sessionId, item.question);
  if (given && typeof given === "object") Object.assign(userMsg, given);
  else if (typeof given === "string" && given) userMsg.id = given;
  else userMsg.id = `tmp-u-${item.index}-${Date.now()}`;
  if (!userMsg.content) userMsg.content = item.question;
  const msg = newAssistantMessage(sessionId);
  msg.id = item.message_id;
  msg.created_at = userMsg.created_at || msg.created_at;
  uiState(msg).queued = true;
  return { userMsg, msg };
}

const upsertBy = (list, item, key = "id") => {
  const i = list.findIndex((x) => x[key] === item[key]);
  if (i >= 0) list[i] = { ...list[i], ...item };
  else list.push(item);
};

const answerFinished = (msg) => msg.status === "answered" || msg.status === "no_sources";

/**
 * @param {object} msg the assistant message being streamed
 * @param {object} userMsg the optimistic user message to be replaced by the server's copy
 * @returns {string[]} names of coarse changes ("steps","body","final","eval","error") for cheap re-rendering
 */
export function applyStreamEvent(msg, userMsg, name, data) {
  const ui = uiState(msg);
  ui.queued = false; // a batch placeholder leaves the queue with its first event
  switch (name) {
    case "message_start":
      if (data.user_message) Object.assign(userMsg, data.user_message);
      msg.id = data.message_id;
      msg.created_at = data.created_at || msg.created_at;
      return ["start"];
    case "step":
      upsertBy(msg.steps, { ...data.step });
      return ["steps"];
    case "step_done": {
      const step = msg.steps.find((s) => s.id === data.step_id);
      if (step) Object.assign(step, { status: "done", elapsed_ms: data.elapsed_ms, label: data.label || step.label, pages: data.pages || step.pages });
      return ["steps"];
    }
    case "token":
      msg.content += data.text || "";
      return ["body"];
    case "citation":
      upsertBy(msg.citations, data.citation);
      return ["body"];
    case "answer_done": {
      const keep = msg._ui;
      Object.assign(msg, data.message);
      msg._ui = keep;
      return ["steps", "body", "final"];
    }
    case "eval_started":
      ui.eval = { running: true, startedAt: Date.now(), nContexts: data.n_contexts || 0 };
      msg.evaluation = { ...(msg.evaluation || {}), status: "running", errors: {} };
      return ["eval"];
    case "eval_result": {
      const ev = (msg.evaluation = msg.evaluation || { status: "running", errors: {} });
      ev.errors = ev.errors || {};
      ev[data.metric] = data.value ?? null;
      if (data.error) ev.errors[data.metric] = data.error;
      return ["eval"];
    }
    case "eval_done":
      msg.evaluation = data.evaluation;
      ui.eval.running = false;
      return ["eval"];
    case "error": {
      ui.eval.running = false;
      ui.streaming = false;
      if (answerFinished(msg)) {
        // The answer is already on screen: a late error belongs to the evaluation, never discards the answer.
        msg.evaluation = { ...(msg.evaluation || {}), status: "failed", errors: { evaluation: humanMessage(data) } };
        return ["eval"];
      }
      msg.status = "error";
      msg.error = keepsCode(data.code) ? data.code : humanMessage(data);
      return ["steps", "body", "final", "error"];
    }
    case "done":
      ui.streaming = false;
      return ["final"];
    default:
      return [];
  }
}

/** Called when the stream ends: nothing may stay "streaming" or "running" forever. */
export function finalizeStream(msg, { aborted }) {
  const ui = uiState(msg);
  ui.streaming = false;
  ui.queued = false;
  for (const step of msg.steps) if (step.status === "running") step.status = "done";
  if (msg.status === "streaming") {
    msg.status = "error";
    msg.error = aborted ? "Stopped before the answer was finished." : "The connection was lost before the answer finished.";
    msg.stopped = aborted;
  }
}
