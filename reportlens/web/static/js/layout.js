// Responsive shell behaviour: sidebar (full / rail / mobile drawer), source-panel resizer and scrim.
//   >= 1024 px  sidebar full (256 px) unless the user collapsed it to the 56 px rail
//   768-1023    sidebar starts as the rail
//   < 768       sidebar is an off-canvas drawer opened from the header
//   < 1100      the source panel overlays the chat instead of sitting beside it (CSS)

import { safeStorage } from "./dom.js";

const MOBILE = window.matchMedia("(max-width: 767px)");
const NARROW = window.matchMedia("(max-width: 1023px)");
const KEY_SIDEBAR = "rl.sidebar";
const KEY_PANEL = "rl.panelFraction";
const MIN_PANEL_PX = 360;
const MAX_PANEL_FRACTION = 0.6;
const DEFAULT_PANEL_FRACTION = 0.42;

export function initLayout({ app, card, resizer, scrim, onSidebarState, onScrimClick }) {
  // ---------- sidebar ----------
  let pref = safeStorage.get(KEY_SIDEBAR); // "open" | "rail" | null (= follow the viewport)
  let drawerOpen = false;

  const railWanted = () => (pref ? pref === "rail" : NARROW.matches);
  const apply = () => {
    const mobile = MOBILE.matches;
    app.classList.toggle("is-mobile", mobile);
    app.classList.toggle("sidebar-rail", !mobile && railWanted());
    app.classList.toggle("drawer-open", mobile && drawerOpen);
    onSidebarState?.({ mobile, rail: !mobile && railWanted(), drawerOpen });
  };
  MOBILE.addEventListener("change", () => {
    drawerOpen = false;
    apply();
  });
  NARROW.addEventListener("change", apply);
  apply();

  const toggleSidebar = () => {
    if (MOBILE.matches) {
      drawerOpen = !drawerOpen;
    } else {
      pref = railWanted() ? "open" : "rail";
      safeStorage.set(KEY_SIDEBAR, pref);
    }
    apply();
  };
  const closeDrawer = () => {
    if (!drawerOpen) return;
    drawerOpen = false;
    apply();
  };
  const openDrawer = () => {
    drawerOpen = true;
    apply();
  };

  // ---------- source panel resizer ----------
  let fraction = Number(safeStorage.get(KEY_PANEL)) || DEFAULT_PANEL_FRACTION;
  const clampFraction = (f) => {
    const width = card.getBoundingClientRect().width || 1;
    return Math.min(MAX_PANEL_FRACTION, Math.max(MIN_PANEL_PX / width, f));
  };
  const setFraction = (f, persist) => {
    fraction = clampFraction(f);
    card.style.setProperty("--panel-w", `${(fraction * 100).toFixed(2)}%`);
    resizer.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
    if (persist) safeStorage.set(KEY_PANEL, fraction.toFixed(4));
  };
  setFraction(fraction, false);
  window.addEventListener("resize", () => setFraction(fraction, false));

  resizer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    resizer.setPointerCapture(e.pointerId);
    app.classList.add("is-resizing");
    const onMove = (ev) => {
      const rect = card.getBoundingClientRect();
      setFraction((rect.right - ev.clientX) / rect.width, false);
    };
    const onUp = () => {
      resizer.removeEventListener("pointermove", onMove);
      resizer.removeEventListener("pointerup", onUp);
      resizer.removeEventListener("pointercancel", onUp);
      app.classList.remove("is-resizing");
      safeStorage.set(KEY_PANEL, fraction.toFixed(4));
    };
    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
    resizer.addEventListener("pointercancel", onUp);
  });
  resizer.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 0.1 : 0.03;
    if (e.key === "ArrowLeft") setFraction(fraction + step, true);
    else if (e.key === "ArrowRight") setFraction(fraction - step, true);
    else if (e.key === "Home") setFraction(MAX_PANEL_FRACTION, true);
    else if (e.key === "End") setFraction(0, true);
    else return;
    e.preventDefault();
  });
  resizer.addEventListener("dblclick", () => setFraction(DEFAULT_PANEL_FRACTION, true));

  scrim.addEventListener("click", () => {
    if (drawerOpen) closeDrawer();
    else onScrimClick?.();
  });

  return { toggleSidebar, openDrawer, closeDrawer, isMobile: () => MOBILE.matches };
}
