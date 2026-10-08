// The demo's usage budget as the browser sees it: a fraction from GET /api/config, never dollar amounts.

import { state } from "./state.js";

export const LOW_BUDGET_BANNER = "This demo has used most of its usage budget and may stop accepting new questions soon.";
export const EXHAUSTED_BANNER = "The demo's usage budget has been used up. New uploads and questions are paused; your existing chats stay readable. Please contact the owner.";
export const EXHAUSTED_COMPOSER = "The usage budget is used up - questions are paused";

/** @returns {"off" | "ok" | "low" | "exhausted"} */
export function usageLevel() {
  const usage = state.config?.usage_budget;
  if (!usage?.enabled) return "off";
  if (usage.used_fraction >= 1) return "exhausted";
  return usage.used_fraction > 0.8 ? "low" : "ok";
}
