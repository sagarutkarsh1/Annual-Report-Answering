// Tiny DOM helpers shared by all views (no framework).

/**
 * Hyperscript-style element factory.
 *   h("button", { class: "btn", on: { click: fn }, aria: { label: "Close" }, dataset: { id: 1 } }, "Text", child)
 * `false`, `null` and `undefined` props/children are skipped; `html` sets innerHTML (trusted strings only).
 */
export function h(tag, props = null, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === false || value === null || value === undefined) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key === "html") el.innerHTML = value;
      else if (key === "on") for (const [type, fn] of Object.entries(value)) el.addEventListener(type, fn);
      else if (key === "aria") for (const [name, v] of Object.entries(value)) el.setAttribute(`aria-${name}`, String(v));
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  return el;
}

function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** Calls `fn` at most once per animation frame, with the latest arguments. */
export function rafThrottle(fn) {
  let frame = 0;
  let lastArgs = null;
  const run = () => {
    frame = 0;
    fn(...lastArgs);
  };
  const throttled = (...args) => {
    lastArgs = args;
    if (!frame) frame = requestAnimationFrame(run);
  };
  throttled.cancel = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
  };
  throttled.flush = () => {
    if (!frame) return;
    cancelAnimationFrame(frame);
    run();
  };
  return throttled;
}

/** localStorage that never throws (private windows, blocked storage). */
export const safeStorage = {
  get(key, fallback = null) {
    try {
      const value = window.localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* storage unavailable: preference simply is not remembered */
    }
  },
};

export const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Keeps already-rendered children that did not change, so hover cards, text selection and
 * chip focus survive every streaming re-render. `fragment` is consumed.
 */
export function patchChildren(container, fragment) {
  const next = Array.from(fragment.childNodes);
  const current = Array.from(container.childNodes);
  next.forEach((node, i) => {
    const old = current[i];
    if (!old) container.append(node);
    else if (!old.isEqualNode(node)) container.replaceChild(node, old);
  });
  for (let i = current.length - 1; i >= next.length; i -= 1) container.removeChild(current[i]);
}

/** Clipboard write with a textarea fallback for non-secure contexts. @returns {Promise<boolean>} */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = h("textarea", { class: "sr-only", "aria-hidden": "true", tabindex: "-1" });
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
    return ok;
  }
}
