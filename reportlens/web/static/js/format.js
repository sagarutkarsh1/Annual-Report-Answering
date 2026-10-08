// Pure formatting helpers (no DOM).

/** "National Grid_Annual_Report.pdf" -> "Nationa...Report.pdf" (head 7 + "..." + tail 10, only when longer than 20). */
export function truncName(name, max = 20) {
  const text = String(name || "document.pdf");
  return text.length > max ? `${text.slice(0, 7)}...${text.slice(-10)}` : text;
}

export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

/** 12345 -> "12.3k" */
export function compactNumber(n) {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** Milliseconds -> "850 ms", "4.2 s", "1 min 05 s". */
export function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${String(Math.round(s % 60)).padStart(2, "0")} s`;
}

/** Whole seconds -> "0:07", "1:05" for live timers. */
export function formatClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** "Thought for 1 second" / "Thought for 3 seconds" (the reference prints "1 seconds"). */
export function thoughtLabel(elapsedMs) {
  if (!Number.isFinite(elapsedMs)) return "Thought";
  const seconds = Math.round(elapsedMs / 1000);
  return seconds < 1 ? "Thought for less than a second" : `Thought for ${plural(seconds, "second")}`;
}

/** Chip label: "Nationa...Report.pdf p.89". */
export function chipLabel(docName, page) {
  return `${truncName(docName)} p.${page}`;
}

/** "Today at 5:10 PM" | "Yesterday at ..." | "Oct 5 at ...". */
export function formatTimestamp(iso, now = new Date()) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(now) - startOfDay(date)) / 86400000);
  if (days === 0) return `Today at ${time}`;
  if (days === 1) return `Yesterday at ${time}`;
  const day = date.toLocaleDateString([], { month: "short", day: "numeric", ...(days > 300 ? { year: "numeric" } : {}) });
  return `${day} at ${time}`;
}

/** "Read pages 88-90" -> "Read pages 88–90" (en dash between numbers, like the reference). */
export function enDashRanges(text) {
  return String(text).replace(/(\d)-(\d)/g, "$1–$2");
}

/** Cost estimate -> "~$0.043"; null/invalid -> "" (the caller omits the cost). */
export function formatCost(usd) {
  if (usd === null || usd === undefined || !Number.isFinite(usd)) return "";
  return `~$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(3)}`;
}

/** Scores are stored at full precision and shown with 2 decimals. */
export function formatScore(value) {
  return Number.isFinite(value) ? value.toFixed(2) : "n/a";
}

/** RAGAS band: >= 0.80 high, 0.50-0.79 medium, < 0.50 low. Text label accompanies colour. */
export function scoreBand(value) {
  if (!Number.isFinite(value)) return null;
  if (value >= 0.8) return { key: "high", label: "High" };
  if (value >= 0.5) return { key: "medium", label: "Medium" };
  return { key: "low", label: "Low" };
}
