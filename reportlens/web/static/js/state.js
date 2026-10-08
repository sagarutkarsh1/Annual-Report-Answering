// Minimal observable store. Views subscribe and re-render the slice they own;
// nothing here knows about the DOM.

const listeners = new Set();

export const state = {
  config: null, // GET /api/config
  health: null, // GET /api/health
  auth: { required: false }, // GET /api/auth: is there an access code (the sidebar shows "Sign out" only then)
  sessions: [], // Session[] (newest first)
  activeId: null,
  detail: null, // SessionDetail of the active session (messages live here while streaming)
};

/** Merges `patch` into the state and notifies subscribers with the changed keys. */
export function setState(patch) {
  Object.assign(state, patch);
  const keys = Object.keys(patch);
  for (const fn of Array.from(listeners)) fn(state, keys);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function sessionById(sid) {
  return state.sessions.find((s) => s.id === sid) || null;
}

/** Replaces (or inserts at the top) a session in the list without re-sorting the rest. */
export function upsertSession(session) {
  const list = state.sessions.slice();
  const i = list.findIndex((s) => s.id === session.id);
  if (i >= 0) list[i] = { ...list[i], ...stripMessages(session) };
  else list.unshift(stripMessages(session));
  setState({ sessions: list });
}

export function removeSession(sid) {
  setState({ sessions: state.sessions.filter((s) => s.id !== sid) });
}

function stripMessages(session) {
  const { messages, ...rest } = session; // list rows never need the transcript
  return rest;
}
