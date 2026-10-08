# 06 - UI design spec derived from the two reference screenshots

Project: annual-report Q&A (PageIndex retrieval + OpenAI + RAGAS scoring) with a ChatGPT / PageIndex-Chat style web UI.
Written: 2026-10-07. Author: UI-spec researcher. Companion assets: `research/06-ui-assets/` (reference images, zoomed crops, layout overlay).

---------------------------------------------------------------------------------------------------

## 0. Provenance, method and legend

### 0.1 What was analysed

| Item | Detail |
|---|---|
| `ref-1` | 1919 x 1036, lossy WebP (converted to PNG for analysis). Answer with bullets + "Why it matters" heading, chips, Sources (19 references), actions, composer, sidebar, PDF panel. |
| `ref-2` | 1919 x 980, lossless PNG. Used for **all colour sampling** (no compression noise). User bubble, agent steps, table block, scroll-to-bottom button, Sources (21 references). |
| Capture | Microsoft Edge on Windows 11, display scale 100 %, page zoom 100 %. Edge draws a 1 px `#ddd` rounded frame at x=3 and x=1916, so the app viewport is **x = 4..1915 (1912 px wide)**; it starts at y=74 in ref-2 (y=71 in ref-1). 1 image pixel = 1 CSS px. |
| Scale check | Body text was shaped with HarfBuzz using Geist 400 at 16 px: predicted ink width 753.5 px vs measured 753 px (error < 0.1 %). Same fit for 14 px (steps/sidebar), 24 px/600 (h2), 14 px/600 (table header). This proves both the typeface (Geist) and the 100 % scale. |
| Colour sampling | Pillow `getdata()` dominant-colour counts per region on ref-2; text colour = darkest pixel in the glyph box (ClearType fringes make "most common" unreliable for text). |
| Geometry | Pixel-run scans (row/column colour transitions), bounding boxes of known-colour regions, ink extents. All numbers below are CSS px. |
| Corroboration | The app's public, pre-login CSS bundle (`https://app.pageindex.ai/_next/static/chunks/*.css`) was read **for design tokens and library fingerprints only**. Hex values measured from the screenshot matched the bundle's custom properties exactly (see 2.1). The logged-in DOM is behind a login wall and was NOT seen, so component structure below is inferred from pixels + CSS class fingerprints. |

### 0.2 Legend used throughout

| Tag | Meaning |
|---|---|
| **VERIFIED** | Seen directly in a screenshot pixel measurement, in the public CSS bundle, in package source (PyPI/npm), or in the PageIndex GitHub repo during this session. |
| **MEASURED** | Derived numerically from the screenshots (still VERIFIED for the screenshot state). |
| **INFERRED** | A reasoned conclusion (e.g. a hover state that no screenshot shows). Treat as a default we can change. |
| **DECISION** | A choice we are making for our product, which may deliberately differ from the reference. |

### 0.3 Library fingerprints found in the reference app's CSS (VERIFIED)

Stack of the reference product: Next.js (Turbopack) + Tailwind CSS v4 + shadcn/ui-style tokens (`--sidebar-*`, `group/sidebar-wrapper`) + `@tailwindcss/typography` (`.prose`) + **Streamdown** streaming-markdown renderer (`--streamdown-caret`, keyframes `sd-fadeIn`, `sd-blurIn`, `sd-slideUp`) + **pdf.js viewer** (`.pdfViewer`, `.textLayer`, annotation-editor CSS = the 170 kB `pdf_viewer.css`) + KaTeX + Radix-style collapsibles (`group/citations`, `group/tool` with `data-[state=open]:rotate-180`). Font: **Geist** (and Geist Mono). We replicate the *look*, not the code.

---------------------------------------------------------------------------------------------------

## 1. Layout grid (all MEASURED from ref-2 unless noted)

Overlay of these boxes on the screenshot: `research/06-ui-assets/layout-overlay-ref2.png`.

### 1.1 Page structure

```
viewport 1912 x ~906+ (x 4..1915)
+-------------+-----------------------------------------------------------------------------------+
| SIDEBAR     |  CARD  (x 260..1907, y 82..977, 1px border #eef4f9, radius 16, margin 8 top/right/ |
| 256 px      |         bottom, 0 left; very soft shadow)                                          |
| white       |  +--------------------------------------+--------------------------------------+  |
|             |  | CHAT PANE  956 px (58 %)             | SOURCE PANEL 692 px (42 %)           |  |
|             |  | bg #f8f8f8                           | bg #fff, border-left 1px #e4e4e7     |  |
|             |  | header 52 px (Share pill)            | header 52 px + 1px border            |  |
|             |  | scroll area (native scrollbar 15 px) | pdf.js viewport (native scrollbar)   |  |
|             |  | composer 768 x 145 (floating)        | footer 48 px + 1px border            |  |
|             |  +--------------------------------------+--------------------------------------+  |
+-------------+-----------------------------------------------------------------------------------+
```

The 58 / 42 split is exact: card width 1648 -> chat 956 (58.0 %) + panel 692 (42.0 %). This is the classic shadcn "sidebar inset" arrangement (sidebar 16 rem, inset card with `m-2 ml-0 rounded-xl`) with a resizable two-pane split inside the card. INFERRED: the divider is a drag handle (1 px line, ~8 px hit area).

### 1.2 Region table

| Region | x range | y range | Size | Notes |
|---|---|---|---|---|
| Outer page bg | 4..1915 | 74.. | - | `#ffffff`; the 8 px gutter around the card shows the card's shadow as `#fbfbfb -> #f3f3f3`. |
| Sidebar | 4..259 | 74..bottom | **256 px** wide | `#fff`; 1 px right edge `#f3f3f3` at x=259. |
| Card (inset) | 260..1907 | 82..977 | 1648 x 896 | Border `#eef4f9`; radius **16** (corner reaches straight edge after 12 px rows; offset 11 px at row 0); bottom shadow ~ `0 1px 2px rgba(0,0,0,.05)`. |
| Chat pane | 261..1215 | 83..976 | **956** wide | bg `#f8f8f8`. |
| Chat header | 261..1215 | 83..135 | **52** tall | Same bg, no border, opaque (scrolling text is hard-clipped at y=136). Holds "Share" pill. |
| Share pill | 277..349 | 93..120 | 73 x 28 | 1 px `#e4e4e7`, white, full radius; icon 13 px + "Share" 12 px `#9f9fa9`. Inset 16 px left, 10 px top. |
| Chat scroll area | 261..1215 | 136..~823 | - | Native OS scrollbar 15 px at x=1201..1215 (thumb `#8b8b8b`, arrows) - app does not style it. |
| **Message column** | 347..1115 | - | **768** wide | = `max-w-3xl` (or 832 px with 32 px padding; the bundle has a `max-w-[832px]` class). Centred on the scroll viewport (centre x = 731 = centre of 261..1200). |
| Composer | 355..1122 | 824..968 | **768 x 145** | 1 px `#e4e4e7`, white, radius **16**, shadow ~ `0 4px 16px rgba(0,0,0,.06)` (bundle has `shadow-[0px_4px_16px_0px_rgba(0..` - INFERRED to be this). Centred on the pane, ignores the scrollbar. Bottom gap to card = 9 px. |
| Composer inner | - | - | padding 16 | Placeholder at x=376 (20 px in), 14 px. Bottom row 36 px: pill left (x=372, inset 16), send button right (x=1070..1105, inset 17). |
| "Select Documents" pill | 372..541 | 916..951 | 170 x 36 | White, 1 px `#e4e4e7`, full radius, `+` 12 px glyph, text 14 px/500. |
| Send button | 1070..1105 | 916..951 | 36 x 36 circle | Disabled = `#afcfef` (= primary at 40 % on white), white up-arrow 10 px glyph. |
| Scroll-to-bottom FAB | 721..757 | 772..807 | **36** circle | Centred at x=739, 17 px above composer top. Fill `#f8f8f8`, 1 px `#e4e4e7`, `arrow-down` 16 px `#09090b`. |
| Source panel | 1216..1907 | 82..977 | **692** wide | bg `#fff`. |
| Panel header | 1217..1906 | 83..134 | **52** + 1 px border (`#ececee`/`#e4e4e7` at y=135) | Thumb 32 x 32 at (1231, 93) (cover-page render, radius ~4); title x=1274 (14 px/600), subtitle "Document" 12 px `#9f9fa9` at y~114..122; close `x` icon centred (1878, 108). |
| Panel viewport | 1217..1906 | 136..927 | 692 x 792 (this window) | pdf.js continuous scroll; native scrollbar at x=1895..1906. Pages are white on white, no visible page gap/shadow. |
| Panel footer | 1217..1906 | 928 (border) 929..976 | **48** + 1 px top border | Left: info icon 16 px at x=1237; "Add Page to Chat" (icon 14 px + 12 px text) x=1279..1396. Centre: "Page [89] / 308" 14 px `#9f9fa9`, page number is an input (x=1544..1560). Right: zoom-out icon x=1790, "100%" 12 px x=1824..1853, zoom-in x=1874. |

"100 %" in the panel label = **fit-to-width** (the A4-ish report page is shown ~640 px wide in a 677 px viewport), not 1.0 x PDF points. INFERRED from rendered page size; pdf.js `currentScaleValue = "page-width"` reproduces it.

### 1.3 Sidebar internals (MEASURED)

| Element | Geometry |
|---|---|
| Horizontal system | Content inset **24 px** (logo, nav icons, footer divider at x=28). List containers inset **16 px** (Recents row x=20..247) with **8 px** inner padding (text at x=28). Right inset on the row is 12 px (4 px less than left - probably a scrollbar gutter). |
| Logo | wordmark 104 x 21 at (28, 100); collapse button = `panel-left` icon 18 px at x=225..242 (right inset ~16). |
| Nav rows | icon 16 px at x=28, label at x=53 (14 px/400). Row centres y=166 / 210 / 254 -> **44 px pitch** (INFERRED: row 36 px + 8 px gap). Items: New Chat, Documents, Explore. |
| "Recents" | 14 px `#8f8f8f` label at x=29 (centre y=314) + `chevron-down` 14 px; collapsible group. |
| Recents row | **228 x 32**, radius ~8-9 (bundle `--radius-md: 8px`), active bg `#f4f4f5`, text 14 px/400 `#09090b`, truncated with "...", first row y=338..369 (8 px below the 32 px header). |
| Footer | 1 px divider `#f2f2f2` at y=891 (x=28..235). Avatar 36 px circle at (26, 906) fill `#00897b` white initial 18 px. Name 14 px/500 at x=70 (truncated "Utkarsh Sag..."), plan "Free" 12 px `#9f9fa9`. "Upgrade" button 70 x 28 at (168, 910): 1 px `#c3d8f5`, white, radius ~8, text 12 px/500 `#3788d8`. Usage bar: track 172 x 8 at (27, 956) `#f4f4f5` radius full, fill `#3788d8` (52 px = 31 %), "31%" 11 px `#71717a` right-aligned to x=237. Bottom padding ~ 24 px. |

### 1.4 Chat column internals (MEASURED)

| Element | Geometry |
|---|---|
| Body line pitch | **28 px** (16 px / 1.75). |
| User bubble | 416 x 48 at x=699..1114 (right edge = column edge 1115), fill `#3788d8`, text white 16/24, padding ~ 12 x 16, radius ~15 (use **16** = `rounded-xl`; bundle `--radius-xl: 16px`). Width hugs content (max width unknown; INFERRED ~80 %). |
| Gap action-row -> bubble | 18 px. Bubble bottom -> first step row ink: 40 px. |
| Agent step row | **36 px pitch** (ink centres 391 / 427 / 463 / 499). Icon 16 px at x=347, text at x=371/372 (14 px/400), chevron-down 14 px ~10 px after the text. |
| H2 | 24 px/600; ink top-to-ink top distance last step -> H2 = 26 px; H2 -> first paragraph line ~ 18-23 px; paragraph -> next H2 ~ 28-36 px (so H2 margin-top ~ 24-28 px, margin-bottom ~ 8-12 px; the stock typography-plugin 2em/1em margins were overridden). |
| Bullet list | `list-style: disc inside` - wrapped lines return to the **column's left edge (x=347)**, under the bullet (VERIFIED in ref-1 "Transmission £686 million)"). Item spacing: single-line items 33 px apart = 28 line + ~4-5 px (`space-y-1`). Bullet at x~350, text starts x~369-370. |
| Chip | **149 x 22** for "Nationa...Report.pdf p.89"; padding ~ 7 px + 1 px border (ink starts 8 px in, ends 7 px before the edge); gap between neighbouring chips ~ 5-6 px; inline within the 28 px line (no line growth). |
| Sources block | header row ink centre y=206 (ref-2): "Sources" 14/500 + gap 8 + "21 references · 1 document" 12 `#71717a` + 1 px hairline `#e8e8eb` filling to x~1092 + chevron-up 16 px at x=1104..1111. File row centre y=245: index "1." 12 px `#9f9fa9` at x=354, mini PDF icon ~ 12 x 16 at x~370, file name 14/400 at x=396..604, then page links `p.49 p.65 p.75 p.76` 12 px with ~16 px gaps, right-aligned "21 refs" 12 px `#71717a` ending x=1107 (8 px inside the column). Action row centre y=279. |
| Action row | three icon buttons, **38 px pitch** (icon centres x=365 / 403 / 441): copy, download, regenerate (`rotate-cw`); icons 16 px `#cbcbd0`/`#b9b9c0`; timestamp "Today at 5:10 PM" 12 px `#9f9fa9` right-aligned to x=1113. |
| Table block | Outer card x=347..1114 (768 wide), top y=714: 1 px `#e4e4e7`, white, radius ~16, 43 px toolbar strip at the top holding three 14 px icons right-aligned (copy x=1033, download x=1059, maximize x=1087; `#9f9fa9`). Inner table at x=356..1105 (8 px card padding), top border y=758, header row 36 px (y=759..794) bg `#f5f5f6`, 1 px `#e4e4e7` below, radius ~8-12; header text 14/600, cells 14/400, left padding 16 px; a chip can sit inside a cell. |
| Outline rail (ref-2 only) | Three 2 px bars at x=273..289, y~474/482/490: grey `#d4d4d8` 10 px wide, **active black 16 px wide** (middle). A conversation/section navigator rail on the left edge of the chat pane (ChatGPT-style). Optional for us. |

### 1.5 Responsive behaviour (INFERRED / DECISION - not in screenshots)

| Viewport | Layout |
|---|---|
| >= 1280 | As above: sidebar 256 + chat + panel side by side (panel default 42 %, min 360 px, max 60 %). |
| 1024-1279 | Panel overlays the chat pane as a right-hand sheet (min(560px, 90vw)) with a scrim; chat does not reflow. |
| 768-1023 | Sidebar collapsed to icon rail (56 px) by default; panel = full-width sheet. |
| < 768 | Sidebar becomes an off-canvas drawer (hamburger in header); panel = full-screen sheet with back button; composer full width with 12 px gutters; textarea 16 px font to avoid iOS zoom. |

---------------------------------------------------------------------------------------------------

## 2. Design tokens

### 2.1 Colour (measured from ref-2 and cross-checked with the reference CSS custom properties - they agree to the hex digit)

| Token (ours) | Hex | Where seen | Reference CSS name (VERIFIED) |
|---|---|---|---|
| `--bg` canvas | `#f8f8f8` | chat pane (100 % of sampled area) | `--background` |
| `--surface` | `#ffffff` | sidebar, composer, panel, table card, pills | `--card`, `--sidebar` |
| `--fg` | `#09090b` | body, headings, active step, panel title | `--foreground` |
| `--fg-muted` (reference) | `#9f9fa9` | completed steps, timestamp, placeholder, panel subtitle, footer text, "Free" | `--muted-foreground` |
| `--fg-secondary` | `#71717a` | "21 references . 1 document", "21 refs", "31%" | `--gray-500` |
| `--fg-recents` | `#8f8f8f` | "Recents" label | `text-[#8f8f8f]` |
| `--border` | `#e4e4e7` | composer, table card, share pill, panel left border, FAB | `--border`, `--input` |
| `--card-border` | `#eef4f9` | card outline | `--surface-border` (active: `#daedff`) |
| `--hairline` | `#e8e8eb` | Sources header rule | - |
| `--hover` / `--muted` | `#f4f4f5` | active Recents row, progress track, table header (`#f5f5f6`) | `--muted`, `--secondary`, `--accent`, `--sidebar-accent` |
| `--accent` (primary) | `#3788d8` | user bubble, usage-bar fill, "Upgrade" text, enabled send button, sample-doc "PDF" badge | `--primary`, `--ring`, `--vblue-500` |
| `--accent-600` | `#2d72cf` | chip text (darkest px `#2d72d8`), page links | `--vblue-600` |
| `--accent-50` | `#f2f7fd` | chip fill | `--vblue-50` |
| `--accent-200` | `#e0ebfa` | chip **hover** fill (ref-1: hovered chip dominant colour `#dfedfa`) | `--vblue-200` |
| `--accent-300` | `#c3d8f5` | chip border, page-link underline (1 px), "Upgrade" border | `--vblue-300` |
| `--accent-400` | `#568ed8` | (dark-mode primary) | `--vblue-400` |
| send disabled | `#afcfef` | = `#3788d8` at 40 % over white | - |
| action icons | `#cbcbd0` | copy/download/regenerate | ~ `--gray-300` at some opacity |
| avatar | `#00897b` | user initial circle (per-user colour, not a token) | - |
| success / warning / destructive | `#16a34a` / `#d97706` / `#dc2626` (+ subtle `#f0fdf4` / `#fffbeb` / `#fef2f2`, border `#bbf7d0` / `#fde68a` / `#fecaca`, strong `#166534` / `#92400e` / `#991b1b`) | not visible in screenshots; exist in bundle; **we use them for RAGAS bars** | `--success*`, `--warning*`, `--destructive*` |
| scrollbar | thumb `#d4d4d8`, hover `#a1a1aa` | (bundle tokens; screenshots show the native Windows scrollbar) | `--scrollbar-thumb*` |

Dark theme exists in the reference bundle (`.dark`: bg `#09090b`, card `#18181b`, border `#ffffff1a`, primary `#568ed8`, success-subtle `#0f1f15`...). Optional for us; keep every colour a CSS variable so dark mode is a 20-line override.

### 2.2 Typography (VERIFIED fit to Geist; sizes are exact CSS values)

Typeface: **Geist** (Vercel, SIL OFL 1.1; variable, wght 100-900). Mono: **Geist Mono**. The reference loads it via `next/font` with an Arial-based fallback (`size-adjust 104.76%`).

Our stack: `font-family: "Geist", ui-sans-serif, system-ui, "Segoe UI", Roboto, Arial, sans-serif; font-feature-settings: normal; letter-spacing: 0;` (no tracking adjustments needed - fit error < 0.1 %). Mono: `"Geist Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.

Self-hosting: `https://cdn.jsdelivr.net/npm/geist@1.7.2/dist/fonts/geist-sans/Geist-Variable.woff2` (69,652 B) and `.../geist-mono/GeistMono-Variable.woff2` (71,368 B) (VERIFIED file list); Google Fonts also serves `family=Geist:wght@100..900` (VERIFIED HTTP 200). `font-display: swap`.

| Role | Size / line-height | Weight | Colour | Evidence |
|---|---|---|---|---|
| Body / prose / bullets | 16 / 28 (1.75) | 400 | `--fg` | fit 15.99 px |
| User bubble text | 16 / 24 | 400 | `#fff` | fit 15.97 |
| H2 (answer headings) | 24 / 32 | 600 | `--fg` | fit 23.97 @600 |
| H3 (not in shots) | 18 / 28 | 600 | `--fg` | DECISION |
| H1 (not in shots) | 28 / 36 | 600 | `--fg` | DECISION (avoid in answers) |
| Agent step line | 14 / 20 | 400 | completed `--fg-muted`, latest `--fg` | fit 14.01 |
| Sidebar nav / Recents row | 14 / 20 | 400 | `--fg` | fit 13.76-14.19 |
| "Recents" label | 14 / 20 | 400 | `#8f8f8f` | |
| Sources title | 14 / 20 | 500 | `--fg` | fit 14.06 @500 |
| Sources meta ("21 references . 1 document") | 12 / 16 | 400 | `#71717a` | fit 11.98 |
| Source file name | 14 / 20 | 400 | `--fg` | fit 13.91 |
| Page link `p.49` | 12 / 16 | 400 | `#2d72cf`, underline 1 px `#c3d8f5`, offset ~3 px | |
| "N refs", timestamp | 12 / 16 | 400 | `#71717a` / `#9f9fa9` | |
| **Citation chip** | 12 / 20 | 400 | `#2d72cf` on `#f2f7fd` | fit 11.93 |
| Composer placeholder / input | 14 / 20 | 400 | `#9f9fa9` / `--fg` | fit 13.92 |
| "Select Documents" | 14 / 20 | 500 | `--fg` | |
| User name | 14 / 20 | 500 | `--fg` | fit 14.02 @500 |
| Plan label ("Free") | 12 / 16 | 400 | `#9f9fa9` | |
| "Upgrade" | 12 / 16 | 500 | `#3788d8` | |
| Usage % | 11 / 16 | 400 | `#71717a` | bundle has `text-[11px]` |
| Table header / cell | 14 / 20 | 600 / 400 | `--fg` | fit 14.09 @600 |
| Panel title | 14 / 20 | 600 | `--fg` | fit 13.88 @600 |
| Panel subtitle, footer text, zoom % | 12 / 16 | 400 | `#9f9fa9` | |
| Footer page indicator | 14 / 20 | 400 | `#9f9fa9` | |

Numerals: use `font-variant-numeric: tabular-nums` for scores, page numbers, counts (DECISION).

File-name truncation rule (VERIFIED visually, ref-1/2): `National Grid_Annual_Report.pdf` (31 chars) -> chip/title label **"Nationa...Report.pdf"** = first 7 chars + "..." + last 10 chars, applied when length > 20. Full name shown in the Sources list and in tooltips.

### 2.3 Spacing, radius, shadows

| Token | Value | Source |
|---|---|---|
| spacing unit | 4 px (`--spacing: .25rem`) | VERIFIED bundle |
| radius scale | sm 4, md 8, lg 12, xl 16, 3xl 24, full 9999 (`--radius-*` overridden in bundle; base `--radius: .75rem`) | VERIFIED bundle |
| Card / composer / table card / bubble | 16 px | MEASURED |
| Sidebar rows, Upgrade button, FAB? | 8 px (rows), FAB/pills/avatar/send = full | MEASURED |
| Chip | 4-5 px (measured arc 5; bundle sm = 4) -> **5 px** | MEASURED |
| Hit areas | icon buttons 32 x 32 (icon 16) | MEASURED pitch 38 = 32 + 6 gap |
| Composer shadow | `0 4px 16px rgba(0,0,0,.06)` | edge darkening f8 -> f1 over ~10 px; INFERRED params |
| Card shadow | `0 1px 2px rgba(0,0,0,.05)` + 1 px border | MEASURED fall-off ~3 px |
| FAB shadow | `0 1px 3px rgba(0,0,0,.08)` | INFERRED |

### 2.4 Motion (VERIFIED keyframes in bundle; usage INFERRED)

| Name | Definition | Use |
|---|---|---|
| `animate-spin` | `spin 1s linear infinite` | spinners |
| `animate-pulse` | `pulse 2s cubic-bezier(.4,0,.6,1) infinite` (opacity 1 -> .5) | skeletons |
| `caret-blink` | `1.25s ease-out infinite`; opacity 1 at 0-20 %, 0 at 20-50 %, 1 at 70-100 % | streaming caret |
| `sd-fadeIn` / `sd-blurIn` / `sd-slideUp` | Streamdown chunk reveal: fade; fade + 4 px blur; fade + 4 px rise | newly streamed text |
| `blink-twice` | `.4s ease-in-out`; opacity 1 -> .2 -> 1 -> .2 -> 1 | attention flash (we reuse for "chip activated" / "copied") |
| `animate-in/out` (tw-animate-css) | `enter` / `exit` .15 s ease | popovers, tooltips, collapsibles |
| easing | `--ease-out: cubic-bezier(0,0,.2,1)`, `--ease-in-out: cubic-bezier(.4,0,.2,1)` | |

### 2.5 Ready-to-paste `tokens.css` (DECISION: variables, with accessible deviations noted)

```css
:root {
  /* surfaces */
  --bg: #f8f8f8; --surface: #fff; --hover: #f4f4f5;
  --border: #e4e4e7; --card-border: #eef4f9; --hairline: #e8e8eb;
  /* text */
  --fg: #09090b;
  --fg-secondary: #71717a;      /* 4.55:1 on #f8f8f8 - use for any text that carries meaning */
  --fg-muted: #9f9fa9;          /* reference colour: 2.47:1 -> decorative only (icons, disabled) */
  --fg-meta: #6e6e78;           /* DECISION: AA replacement for steps/timestamp/placeholder (4.9:1) */
  /* accent ramp */
  --accent-50: #f2f7fd; --accent-200: #e0ebfa; --accent-300: #c3d8f5;
  --accent-400: #568ed8; --accent-500: #3788d8; --accent-600: #2d72cf;
  --chip-fg: #2762b8;           /* DECISION: AA (5.5:1 on fill, 4.9:1 on hover); reference #2d72cf = 4.4 / 3.9 */
  --bubble-bg: #2d72cf;         /* DECISION: white text 4.75:1; reference #3788d8 = 3.7:1 */
  --ring: #3788d8;
  /* status (RAGAS bars) */
  --ok: #16a34a; --ok-bg: #f0fdf4; --ok-strong: #166534;
  --warn: #d97706; --warn-bg: #fffbeb; --warn-strong: #92400e;
  --bad: #dc2626; --bad-bg: #fef2f2; --bad-strong: #991b1b;
  /* geometry */
  --sidebar-w: 256px; --header-h: 52px; --footer-h: 48px; --col-w: 768px; --panel-w: 42%;
  --r-chip: 5px; --r-row: 8px; --r-card: 16px; --r-full: 9999px;
  --shadow-card: 0 1px 2px rgb(0 0 0 / .05);
  --shadow-composer: 0 4px 16px rgb(0 0 0 / .06);
  --font: "Geist", ui-sans-serif, system-ui, "Segoe UI", Roboto, Arial, sans-serif;
  --font-mono: "Geist Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  /* pdf.js text-layer highlight (override of pdf_viewer.css vars) */
  --highlight-bg-color: rgb(255 210 0 / .35);
  --highlight-selected-bg-color: rgb(255 210 0 / .55);
}
@media (prefers-color-scheme: dark) { /* optional: mirror reference .dark tokens */ }
```

---------------------------------------------------------------------------------------------------

## 3. Component inventory (anatomy, states, behaviour)

Icon set: the reference icons are **Lucide** line icons (16 px, ~1.75 stroke). Verified names in `lucide-static@1.52.0` (ISC licence; 2,130 icons; `sprite.svg` 519 kB available): `message-circle` (New Chat), `panel-left` (collapse), `compass` (Explore), `file-text` (Documents - closest), `chevron-down` / `chevron-up`, `lightbulb` (thought), `book-open` (read pages), `copy`, `download`, `rotate-cw` (regenerate), `share-2`, `plus`, `arrow-up` (send), `arrow-down` (FAB), `x`, `info`, `message-square-plus` (Add page), `zoom-in`, `zoom-out`, `maximize` (table fullscreen). Extras we need (all exist): `lock`, `upload`, `file-up`, `loader-circle`, `circle-check`, `triangle-alert`, `refresh-cw`, `trash-2`, `pencil`, `ellipsis`, `search`, `circle-stop`, `check`, `file-search`, `gauge`. DECISION: inline the ~35 icons we use as one `sprite.svg` (`<use href>`), do not hotlink.

### 3.1 App shell
- Sidebar toggle (`panel-left`): collapses sidebar to a 56 px rail (icons only) or hides it; state persisted in `localStorage` (try/catch). Shortcut `Ctrl+B` (INFERRED shadcn convention).
- Card scroll containers: chat scroll area and panel viewport scroll independently.
- Panel open/close: width animates 0 -> 42 % over ~200 ms `ease-out`; chat column re-centres (it is centred in whatever width remains). Close `x` and `Esc` (focus inside) close; closing returns focus to the chip that opened it.

### 3.2 Sidebar

| Component | Default | Hover | Active / selected | Focus | Disabled / loading |
|---|---|---|---|---|---|
| Nav item (New chat, Documents) | transparent, 14 px, icon 16 | bg `--hover` radius 8 (INFERRED) | same as hover + weight 500 | 2 px ring offset 2 | - |
| "Recents" header | `#8f8f8f` + chevron | text darkens to `--fg` | chevron rotates 180 deg when collapsed state toggles (`group-data-[state=open]` pattern) | ring | - |
| Session row | 32 px, text truncate | bg `--hover`; trailing `ellipsis` button appears (rename / delete) | bg `#f4f4f5` (VERIFIED) | ring | while renaming: inline input; while deleting: row fades out 150 ms |
| User card | avatar + name + plan + bar | - | - | - | usage bar `role=progressbar` |

Behaviours: rows ordered newest-first; grouped headings optional ("Today", "Yesterday"); title = first user question truncated at ~28 chars (reference "What is the status of GHG...") or document name until the first question; long-press / right-click opens menu. Keyboard: Up/Down in list, Enter opens, `Delete` asks to confirm.

### 3.3 Header
"Share" pill (VERIFIED). **DECISION:** replace with "Export" (download chat as .md) or omit - sharing needs auth/public links we do not have.

### 3.4 Message list
- Container: `role="log"`, `aria-live="off"` while streaming (announce completion via a separate polite live region).
- Auto-scroll: stick to bottom while streaming **only if** the user is within ~80 px of the bottom; otherwise show the FAB. FAB click smooth-scrolls (respect reduced motion) and re-enables stick-to-bottom. FAB hidden when at bottom.
- Turn order inside an assistant message (VERIFIED from ref-1 + ref-2): agent steps -> markdown answer (+ inline chips) -> Sources block -> action row (+ timestamp). **DECISION:** insert the Evaluation (RAGAS) block between Sources and the action row.
- User message: right-aligned bubble; timestamp not shown on the bubble (the timestamp row in the screenshots belongs to the previous assistant message).

### 3.5 Agent step row (collapsible)
Anatomy: leading icon (16) + label (14/400) + chevron-down (14). Row height 36.

| State | Appearance |
|---|---|
| running | icon replaced by spinner (`loader-circle`, 1 s) or the label gets a left-to-right shimmer (bundle has `bg-[length:250%_100%]` + gradient utilities); colour `--fg`; label present-progressive: "Thinking..." / "Reading pages 88-90 from "doc.pdf"..." |
| done, latest | label in `--fg` (VERIFIED: the last row "Read pages 86-87 ..." is black) |
| done, earlier | label `--fg-muted` (VERIFIED `#9f9fa9`) - DECISION: use `--fg-meta` |
| expanded | chevron rotated 180 deg; body shows: for "Thought" the reasoning summary text (12-13 px, `--fg-secondary`); for "Read pages" the page list + first ~200 chars per page + "Open page" links that behave like chips |
| error | `triangle-alert` icon `--bad-strong`, label "Could not read pages 88-90", expand shows error |

Label formats (VERIFIED strings): `Thought for N seconds` (reference prints "1 seconds" - a bug; **DECISION** pluralise: "1 second"), `Read pages A–B from "FILE.pdf"` (en dash for ranges; single page: `Read page A from "FILE.pdf"`). Add our own kinds: `Searched the document outline` (tree search), `Evaluating answer` (not a step; see 3.9).
Once the answer completes, steps stay visible (collapsed) above the answer - matches the screenshots.

### 3.6 Markdown rendering
- Elements: p, h2/h3, ul/ol (`list-style: disc inside` for ul, VERIFIED quirk), strong, em, code/pre (Geist Mono 13 px), blockquote, hr, links, tables.
- Tables: wrapped in the "table card" (3.1 geometry): toolbar (copy as Markdown/TSV, download CSV, fullscreen dialog) + horizontally scrollable inner table; header row sticky inside the card on fullscreen.
- Streaming: render accumulated markdown at most once per animation frame; append a caret (a 2 x 16 px `--fg` bar or Streamdown's block caret) to the last block while `streaming`; new text uses a 150-200 ms fade/blur-in. Unterminated fences/tables must not flicker - close open constructs when parsing for display only.
- Citation markers: server emits `[[c1]]` sentinels (see 7.3); after Markdown -> sanitised HTML, replace each sentinel with a chip `<button>`; never let a half sentinel (`[[c`) render (hold back the tail).

### 3.7 Citation chip (the core interaction)

Anatomy: `button.cite-chip` = label text "Nationa...Report.pdf p.89" (12 px) - **149 x 22**, inline.

| State | Appearance / behaviour |
|---|---|
| default | fill `#f2f7fd`, 1 px `#c3d8f5`, text `--chip-fg`, radius 5, cursor pointer |
| hover | fill `#e0ebfa` (VERIFIED in ref-1), 150 ms transition; tooltip after 400 ms: "National Grid_Annual_Report.pdf - page 89 - Group cash flow > Summary" |
| focus-visible | 2 px `--ring` outline, offset 2 |
| active (its page/passage is currently shown in the panel) | fill `#e0ebfa` + 1 px `#568ed8` border (INFERRED; reference shows nothing) |
| pressed | opens source panel (see section 8) and plays the highlight flash; plays `blink-twice` on the chip |
| disabled (document missing / page out of range) | grey fill, `aria-disabled`, tooltip explains |
| repeated | the same page cited N times produces N chips (the reference counts "19 refs" = chips, not unique pages) |

### 3.8 Sources block (collapsible, `group/citations` in bundle)
- Header: "Sources" (14/500) + "N references . M document(s)" (12 `#71717a`) + hairline + chevron-up; whole header is a toggle (`aria-expanded`). Default **expanded** in screenshots. Chevron rotates 180 deg when state flips.
- Body: one row per document: `index.` + PDF icon + file name + **unique sorted page links** (`p.86 p.87 p.89 p.90`) + right-aligned `N refs`. Page link hover: darker `#2762b8`, underline becomes 2 px; click = open panel at that page (page-level flash, no passage highlight).
- Our single-document sessions always show exactly 1 row; keep the "N documents" wording for future multi-doc.
- Counting rule (VERIFIED): "references" = number of inline chips in the answer.

### 3.9 Evaluation (RAGAS) card - NEW (not in the reference)
See section 4.5 for the full spec.

### 3.10 Message action row
Buttons (32 x 32, icon 16): **copy** (copies answer markdown with citations rendered as `[doc p.89]`; icon switches to `check` for 1.5 s, SR text "Copied"), **download** (answer as `.md`; optional "include sources + scores"), **regenerate** (`rotate-cw`; disabled while streaming or evaluating; re-runs retrieval+answer+eval and appends a new version). Timestamp: "Today at 5:10 PM" via `Intl.RelativeTimeFormat`/`Intl.DateTimeFormat`; `<time datetime>`.
States: default `#cbcbd0` (decorative; DECISION icon colour `#8b8b95`, 3:1), hover `--fg` + bg `--hover` circle/rounded 8, focus ring, disabled 40 % opacity.

### 3.11 Composer
- Card 768 x 145 (min), radius 16, border + shadow (see 1.2). Textarea auto-grows to max ~ 200 px then scrolls. `Enter` sends, `Shift+Enter` newline, `Esc` blurs. IME-safe (`isComposing`).
- Bottom row: left = document pill; right = send button.
- **Send button states** (36 px circle): disabled (empty or no ready document) `#afcfef` + `aria-disabled`; enabled `--accent-500` hover `--accent-600`; **sending/streaming** -> becomes a stop button (`circle-stop` / filled square) which aborts the stream; pressed = 0.97 scale.
- Placeholder text: reference "Ask a question..." (VERIFIED). We vary it by state (4.2).
- Drag-and-drop: when the session has no document, the whole card pane is a drop target (dashed accent border overlay); once locked, drops are rejected with a toast.

### 3.12 Source panel
Sub-components: header (thumb + title + subtitle + close), pdf.js viewport, footer (info popover, "Add page to chat", page input, zoom). States: `closed` (width 0), `opening`, `loading` (skeleton page + centred spinner), `ready`, `error` (message + retry), `password-protected` (we reject at upload, so n/a). Footer details:
- Info icon: popover with file name, pages, size, indexed-at, index model, tree node count.
- Page indicator: `Page [input] / total`; typing a number + Enter jumps (clamped); `PageUp/PageDown` and arrows scroll; indicator follows scroll (`pagechanging` event).
- Zoom: steps 50, 67, 80, 90, **100 (= fit width)**, 110, 125, 150, 175, 200, 300 %; buttons disabled at the ends; click on the % label resets to fit width; `Ctrl+wheel` zooms.
- "Add Page to Chat" (`message-square-plus`): in the reference it attaches the viewed page as context. **DECISION: v2** - repurpose as "Ask about this page" (adds a scoped chip to the composer; backend limits retrieval to that page).

### 3.13 Dialogs, menus, toasts
Delete-session confirm (destructive), rename inline, error toast (bottom-centre, 4 s, `role="status"`), "Document is locked to this chat" tooltip on the locked pill. Dialogs trap focus; `Esc` closes; return focus to opener.

### 3.14 Empty / skeleton states
Skeleton lines for the markdown (3 grey bars, pulse 2 s), chip skeletons are not needed (chips appear when data arrives), panel skeleton = blank white page outline + spinner.

---------------------------------------------------------------------------------------------------

## 4. Requirements mapped onto the reference UI

### 4.1 Session model

| Requirement | UI mapping |
|---|---|
| Multiple sessions | Sidebar **Recents** list = sessions (one row each, newest first). Row = title + (hover) menu. Persist server-side (SQLite or JSON) so reload restores sessions, messages, citations, steps, scores. |
| One document per session | Session has `document_id` (nullable until upload). |
| Upload only before chat starts | State machine below; server returns **409 `document_locked`** on any upload/replace once a user message exists. UI also removes the upload affordance (defence in depth). |
| New chat | "New chat" is the first nav item in the sidebar, **always visible** (also when the sidebar is collapsed: icon button with tooltip; also in the mobile header). Shortcut `Ctrl+Shift+O`. If the current session is already empty (no document, no messages), the button just focuses it (avoids piles of blank sessions). |

Session state machine:

```
EMPTY (no doc)  --upload-->  INDEXING  --ready-->  READY (doc bound, 0 messages)  --first message-->  LOCKED (chatting)
     ^                         |  \--fail--> INDEX_FAILED (retry / replace doc)          |
     |                         \--cancel--> EMPTY                                          |
     +------------------- "New chat" creates a fresh EMPTY session ----------------------+
```
Replace/remove document is allowed in EMPTY, INDEXING, INDEX_FAILED, READY. Not allowed in LOCKED.

### 4.2 Screens

**A. Empty session (upload-before-chat)** - centred in the message column (768 max):
```
                     [ logo mark ]
            Ask questions about an annual report
   Upload one PDF. It is indexed once, then every answer cites its pages.

   +- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -+
   |        (file-up icon 32px)                                       |
   |   Drop your annual report PDF here, or  [ Choose PDF ]           |
   |   PDF only - up to 100 MB - up to ~1,000 pages                   |
   +- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -+
   (dashed 1.5px --border-strong, radius 16, bg #fff; hover/drag: border + bg accent-50)

   [ composer, disabled ]  placeholder: "Upload a document to start asking questions"
                           pill: [ + Attach PDF ]   send: disabled
```
Limits shown are placeholders (DECISION: confirm with backend research). Validation: MIME `application/pdf` + magic bytes `%PDF-`, size cap, reject encrypted PDFs, page count check; inline error in the dropzone (`role="alert"`).

**B. Indexing** (after upload; chat disabled):
```
   +---------------------------------------------------------------+
   | [thumb 32] National Grid_Annual_Report.pdf        308 pages    |
   |            Indexing... Building the section tree               |
   |   [#########-------------]  42 %           Remove / Cancel     |
   |   1 Uploaded  2 Read pages (132/308)  3 Outline  4 Summaries   |
   +---------------------------------------------------------------+
```
Progress bar reuses the usage-bar style (8 px, `#f4f4f5` track, `#3788d8` fill, radius full); `role="progressbar"` + `aria-valuenow`; stage list with check/spinner icons; live text in `aria-live="polite"`. Source: SSE or polling (see 7.6). Indexing may take minutes for 300 pages: allow navigating to other sessions meanwhile (job continues server-side; sidebar row shows a small spinner). When ready: card collapses to a doc chip, composer enables, optional suggested questions (3 chips, e.g. "What was underlying operating profit?", "Summarise the principal risks", "How did net debt change?" - DECISION, generic).

**C. Ready, no messages**: composer pill = doc chip `[pdf-icon National Grid_Annual_Report.pdf  x]` (x allowed here) ; placeholder "Ask a question about this report..."; right panel closed (optional: peek of cover thumbnail).

**D. Locked / chatting**: pill becomes `[lock-icon  Nationa...Report.pdf]` non-removable; tooltip "This chat is bound to this document. Start a new chat to use a different one." No `+`. The doc chip click opens the source panel at page 1. `New chat` remains visible.

### 4.3 Sessions list under "Recents"
Row text = first question truncated; before first question = document name; before upload = "New chat". Hover shows `ellipsis` -> menu (Rename, Delete). Active row `#f4f4f5`. A tiny status dot/spinner on the row while indexing or while a response is streaming in a background session. "Recents" header collapses the list (chevron).

### 4.4 Right panel behaviour (see section 8 for the algorithm)
Closed by default; opens on first chip/page-link click; remembers width within the session; shows the session's single document, so no tabs needed.

### 4.5 RAGAS score card (per assistant answer)

Placement: after the Sources block, before the action row. Collapsible like Sources (same header pattern): **"Evaluation"** (14/500) + "RAGAS . 3 metrics" (12 `#71717a`) + hairline + chevron. Default **expanded** while running/after completion for the first answer, user toggles persist.

Metric cards (3-column grid inside a 768-wide column: 3 x ~240 px, gap 12; stacks to 1 column < 640 px):

```
 +-----------------------------+ +-----------------------------+ +-----------------------------+
 | Faithfulness            (i) | | Response relevancy      (i) | | Context precision       (i) |
 | 0.92                        | | 0.88                        | | 0.71                        |
 | [#########################-] | | [########################--] | | [###################------] |
 | High                        | | High                        | | Medium                      |
 +-----------------------------+ +-----------------------------+ +-----------------------------+
 footer:  Evaluated in 14.2 s . judge: <model> . 7 retrieved passages (pages 86-90)    [Re-run]
```
- Card: white, 1 px `--border`, radius 12, padding 12; label 12/500 `--fg-secondary`; value 24/600 tabular-nums `--fg`; bar 6 px tall, radius full, track `#f4f4f5`, fill by threshold (**DECISION**): >= 0.80 `--ok`, 0.50-0.79 `--warn`, < 0.50 `--bad`; textual band label (High / Medium / Low) so colour is not the only cue.
- Tooltips (keyboard-focusable `(i)` button, `aria-describedby`, dismiss with Esc, persists on hover - WCAG 1.4.13):
  - Faithfulness: "How many of the statements in the answer are supported by the retrieved passages. Higher means fewer unsupported claims."
  - Response relevancy: "How directly the answer addresses your question. Low scores flag off-topic or evasive answers."
  - Context precision: "How much of the retrieved material was actually relevant, with relevant passages ranked first."
  (INFERRED wording; exact metric definitions belong to the RAGAS research note.)
- States:

| State | Visual |
|---|---|
| `queued` | header subtitle "Evaluation queued"; cards show skeleton |
| `running` ("evaluating...") | three skeleton cards: grey value block + shimmering bar (pulse 2 s), small spinner + text "Evaluating with RAGAS - this can take up to a minute" + elapsed seconds counter; **per-metric results fill in as they arrive** (fade-in 200 ms, bar animates 0 -> value over 400 ms ease-out) |
| `complete` | all three values; footer line; "Re-run" ghost button |
| `partial` | failed metric shows "-" with `triangle-alert` and tooltip with `reason`; others normal; "Retry failed" button |
| `failed` | one inline alert row: "Evaluation failed: <short reason>" + Retry |
| `skipped` | answer was a refusal / no contexts: "Not evaluated (no sources were retrieved)" |
| `stale` | after regenerate: previous values dimmed 50 % with "Out of date" until the new run finishes |
| `n/a` | metric returned NaN/None: show "n/a" not 0 |

- Never block the chat: the user can ask the next question while a previous evaluation runs (queue evaluations; show "Evaluation queued"). Clicking the footer "7 retrieved passages" opens the panel at the first retrieved page.
- Persist scores with the message; reloaded sessions show them immediately.
- Scores are floats 0-1 (display 2 decimals); store full precision.

### 4.6 Citation chip -> panel
Chip click = `openSource(citation)` (section 8). Sources page links = same with `{page}` only. The panel title always shows the **truncated** name, the footer shows the **physical page** index (what the chips say). Printed page labels differ (VERIFIED: chip "p.89", footer "Page 89 / 308", but the printed folio on that page reads 87): chips use the **PDF page index**; show the printed label in the tooltip/footer when it differs (`pdfViewer.currentPageLabel`).

---------------------------------------------------------------------------------------------------

## 5. Things NOT to copy (branding and legal)

| Do not copy | Do instead |
|---|---|
| The "PageIndex" name, logo, wordmark (the stylised `Page(ndex`), favicon, product copy ("Professional AI Co-Reader", "Traceable answers...") | Neutral name + own mark (below). Mention PageIndex only as attribution of the retrieval technique in an About/Settings dialog ("Retrieval powered by PageIndex (VectifyAI), open source") - check the repo licence before shipping (LICENSE file is in the repo; licence text not reviewed here). |
| Their CSS/JS bundles, fonts files, images, or hot-linking `app.pageindex.ai` assets | Self-host Geist (OFL), Lucide (ISC), pdf.js (Apache-2.0). Our CSS is written from the measured numbers above. |
| "Free / Upgrade / usage %" card, "Explore" page, "Share" public links, account/billing | Footer card shows local user ("You") + settings gear + app version; drop "Explore"; "Share" -> "Export". |
| The National Grid sample content / cover thumbnail | Do not bundle any annual report; the user supplies their own PDF. Generate thumbnails from the uploaded file. |
| The exact brand blue as the product's identity | Colours/layout are not trademarks and the user asked for a close replica, so we keep the neutrals and the blue ramp as **CSS variables** (`--accent-*`); if this is ever shown externally, change `--accent-*` hue by ~10-15 deg to differentiate (one-line change). |

Working product name: **ReportLens** (alternatives: *PageTrace*, *Citeline*, *FolioAsk*). DECISION; run your own trademark/domain check before using externally.

Logo treatment (original, simple): 28 x 28 rounded square (radius 8) filled `--accent-600`, white document page with folded corner and a magnifier ring cut in accent colour; wordmark "ReportLens" in Geist 600 18 px, `Report` `--fg` + `Lens` `--accent-600`, letter-spacing -0.01em. SVG starter:

```html
<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
  <rect width="28" height="28" rx="8" fill="#2d72cf"/>
  <path d="M9 6.5h7l4 4V21a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V7.5a1 1 0 0 1 1-1z" fill="#fff"/>
  <circle cx="14.2" cy="15.2" r="3" fill="none" stroke="#2d72cf" stroke-width="1.6"/>
  <path d="M16.4 17.4 18.6 19.6" stroke="#2d72cf" stroke-width="1.6" stroke-linecap="round"/>
</svg>
```

---------------------------------------------------------------------------------------------------

## 6. Front-end stack decision

### 6.1 Verified versions (npm / PyPI queried 2026-10-07)

| Package | Version | Licence | Note |
|---|---|---|---|
| pdfjs-dist | 6.4.299 | Apache-2.0 | `build/pdf.min.mjs` 458,904 B; `build/pdf.worker.min.mjs` 1,264,342 B; `web/pdf_viewer.mjs` 319,973 B; `web/pdf_viewer.css` 168,569 B (jsDelivr `data.jsdelivr.com` file list; CDNs jsDelivr + cdnjs both HTTP 200). |
| marked | 18.1.0 | MIT | markdown -> HTML (GFM tables) |
| dompurify | 3.4.16 | MPL-2.0 OR Apache-2.0 | sanitise before inserting HTML |
| markdown-it | 15.0.2 | MIT | alternative parser |
| lucide-static | 1.52.0 | ISC | icons / `sprite.svg` |
| geist (fonts) | 1.7.2 | OFL | variable woff2 |
| streamdown | 2.7.0 | Apache-2.0 | React streaming markdown (what the reference uses) |
| react / react-dom | 19.3.0 | MIT | |
| vite | 8.3.3 | MIT | |
| react-pdf | 11.0.0 | MIT | pdf.js wrapper |
| @tailwindcss/browser | 4.3.3 | MIT | play CDN (not for production) |
| fastapi | 0.142.2 | - | **has built-in SSE**: `fastapi.sse.EventSourceResponse`, `ServerSentEvent` (VERIFIED in wheel source) |
| starlette | 1.7.0 | - | `FileResponse` supports HTTP **Range** (`accept-ranges: bytes`, multipart byteranges) - pdf.js range loading works |
| sse-starlette | 3.5.0 | - | no longer needed with FastAPI 0.142 |
| streamlit / gradio | 1.65.0 / 6.29.1 | - | considered, rejected |
| uvicorn | 0.54.0 | - | |
| ragas | 0.4.3 (2026-01-13) | - | for the score card; details in RAGAS note |
| pymupdf / pypdfium2 / pdfplumber | 1.28.2 (AGPL-3.0 or commercial) / 5.14.0 (BSD-3/Apache-2.0) / 0.11.10 (MIT) | | relevant to bbox/quote resolution (6.5) |

pdf.js facts (VERIFIED in downloaded 6.4.299 source): `pdf.min.mjs` exports `getDocument`, `GlobalWorkerOptions`, `TextLayer`, `Util`, `OPS`, ... and **sets `globalThis.pdfjsLib`**; `pdf_viewer.mjs` reads `globalThis.pdfjsLib` (so import `pdf.min.mjs` first, then `import()` the viewer) and exports `PDFViewer`, `PDFPageView`, `EventBus`, `PDFLinkService`, `PDFFindController`, `TextLayerBuilder`, ...; `PDFViewer` **requires an absolutely positioned container** (throws otherwise); events `pagesinit`, `pagesloaded`, `pagechanging`, `pagerendered`, `textlayerrendered`, `scalechanging`; `currentPageNumber`, `currentScaleValue` (`"page-width"`, `"page-fit"`, `"auto"` or number string), `scrollPageIntoView({pageNumber, destArray, ...})`, `currentPageLabel`; highlight colours are CSS vars `--highlight-bg-color` / `--highlight-selected-bg-color` in `pdf_viewer.css`.

### 6.2 Comparison

Scores 1 (poor) - 5 (excellent).

| Criterion | (a) Vanilla ES modules + CSS vars, served as static files (FastAPI `StaticFiles`) | (b) React + Vite (+ Tailwind, shadcn, Streamdown, react-pdf) | (c) Streamlit / Gradio |
|---|---|---|---|
| Pixel fidelity to screenshots | **5** - full control of every px/token (Geist, 52 px headers, 768 col, chips) | **5** - same, plus shadcn/Streamdown give near-identical primitives (the reference is this stack) | **1-2** - fixed page chrome, no custom sidebar/card/panel; CSS hacks are brittle |
| PDF side panel + highlight overlay | **5** - pdf.js `PDFViewer` used directly; overlay layers are plain DOM children of `.page`; click-to-scroll across components is trivial | **4** - `react-pdf` hides the page DOM; custom text-layer/overlay needs refs or dropping to raw pdf.js anyway | **1** - cross-component "click chip -> scroll/highlight inside iframe component" needs a custom bidirectional component; Gradio `gr.HTML`/JS hooks are fragile |
| SSE of steps + tokens + scores | **5** - `fetch` + `ReadableStream` parser (~25 lines) | **5** | **2** - Streamlit has token streaming (`st.write_stream`) and `st.status`, but no multi-event protocol; late-arriving RAGAS scores need rerun tricks; Gradio generators stream one output at a time |
| Simplicity on Windows | **5** - `pip install` + `uvicorn`; no Node, no build; vendored libs in `static/vendor` | **3** - Node 22 is present, but `npm install`, `vite dev` (2 processes) / `vite build`; more to explain for UAT users | **5** - `pip install streamlit` |
| Later conversion to FastAPI | **5** - the same static files and the same REST+SSE endpoints are the production shape; only auth/storage swap | **4** - serve `dist/` from FastAPI or separately; API unchanged | **1-2** - UI is throw-away; Streamlit is its own server |
| Long-term maintainability | **3** - hand-rolled state/render (~2-3 k LOC for this scope) | **5** | **2** |
| **Total (equal weight)** | **28** | **26** | **12-13** |

### 6.3 Recommendation (DECISION)

**Use (a): vanilla ES modules + CSS custom properties + vendored pdf.js/marked/DOMPurify, served by a thin FastAPI shell.**
Reasons: (1) fastest route to pixel fidelity with the exact numbers above; (2) pdf.js highlight overlay is a DOM problem, and vanilla gives direct access; (3) FastAPI 0.142 already ships SSE (`EventSourceResponse`, 15 s `: ping` keep-alives, `Cache-Control: no-cache`, `X-Accel-Buffering: no`), so the streaming protocol in section 7 is the *final* production shape - nothing to rewrite at UAT sign-off; (4) no Node toolchain for the user on Windows; (5) the user said the core will later become a FastAPI endpoint - the shell is ~150 lines now and the UI never changes.
Keep the **core** (index, ask, evaluate) as a plain Python package that yields typed events (generator); the FastAPI shell only adapts events to SSE. Escape hatch: if the UI grows (auth, multi-document, collaboration) port to (b) reusing `tokens.css`, the SSE protocol, and the REST API unchanged.

Suggested static layout (no bundler; `<script type="module">` + `<script type="importmap">` for vendor aliases):

```
app/static/
  index.html
  css/   tokens.css  base.css  layout.css  components.css  markdown.css  pdf-panel.css
  js/    main.js  state.js  api.js  sse.js  markdown.js  citations.js  format.js
         components/ sidebar.js  chat.js  message.js  steps.js  sources.js  evalcard.js
                     composer.js  uploader.js  sourcepanel.js  toast.js
  vendor/ pdfjs/{pdf.min.mjs, pdf.worker.min.mjs, pdf_viewer.mjs, pdf_viewer.css}
          marked.esm.js   purify.es.mjs
  fonts/ Geist-Variable.woff2  GeistMono-Variable.woff2
  icons/ sprite.svg
```
Weight: pdf.js ~2.2 MB total (worker 1.26 MB lazy-loaded only when the panel first opens) + ~0.2 MB others; load pdf.js lazily via dynamic `import()`.

### 6.4 pdf.js wiring sketch (vanilla)

```js
// sourcepanel.js - lazy, called on first chip click
let pdfjs, viewerNS;
async function loadPdfJs() {
  if (pdfjs) return;
  pdfjs = await import('/static/vendor/pdfjs/pdf.min.mjs');   // side effect: globalThis.pdfjsLib
  pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
  viewerNS = await import('/static/vendor/pdfjs/pdf_viewer.mjs'); // must come AFTER pdf.min.mjs
}
export async function openDocument(url /* /api/documents/{id}/file (Range-enabled) */) {
  await loadPdfJs();
  const container = document.querySelector('#pdfContainer');       // position:absolute; inset:0; overflow:auto
  const viewer = document.querySelector('#pdfViewer');             // <div class="pdfViewer">
  const eventBus = new viewerNS.EventBus();
  const linkService = new viewerNS.PDFLinkService({ eventBus });
  const pdfViewer = new viewerNS.PDFViewer({ container, viewer, eventBus, linkService, textLayerMode: 1 });
  linkService.setViewer(pdfViewer);
  const doc = await pdfjs.getDocument({ url, rangeChunkSize: 1 << 18 }).promise;
  pdfViewer.setDocument(doc); linkService.setDocument(doc);
  eventBus.on('pagesinit', () => { pdfViewer.currentScaleValue = 'page-width'; });
  eventBus.on('pagechanging', e => updatePageIndicator(e.pageNumber, doc.numPages));
  eventBus.on('pagerendered', e => reattachHighlight(e.pageNumber)); // overlays are lost on re-render
  return { pdfViewer, eventBus, doc };
}
```
Note: keep one `PDFViewer` per session document and reuse it across chip clicks.

---------------------------------------------------------------------------------------------------

## 7. SSE event protocol for the chat stream (DECISION; compatible with PageIndex's own event types)

### 7.1 Transport
- `POST /api/sessions/{session_id}/messages` with JSON `{ "text": "...", "client_message_id": "uuid" }` -> response `text/event-stream`. Works with POST (FastAPI's `EventSourceResponse` accepts any HTTP method - VERIFIED). Browser `EventSource` is GET-only, so the client uses `fetch()` + `ReadableStream` (parser in 7.7).
- Every event: SSE `event:` = name, `id:` = `"{message_id}:{seq}"` (seq monotonic from 1), `data:` = one JSON object (FastAPI JSON-encodes `data`; strings get quoted - always send objects). `retry: 3000`. Keep-alive `: ping` every 15 s is automatic in FastAPI 0.142 (`_PING_INTERVAL = 15.0`, VERIFIED) - RAGAS can take > 30 s, so the stream **stays open until `done`**.
- Persist every event's effect (steps, citations, final text, eval) so `GET /api/sessions/{id}` and `GET /api/messages/{id}` reproduce the same UI after reload; if the stream drops during evaluation the client polls `GET /api/messages/{id}` every 3 s until `eval.status` is terminal.
- Cancel: client aborts the fetch (`AbortController`) and calls `POST /api/messages/{id}/cancel`; aborting before `answer_done` skips evaluation; aborting during evaluation lets the job finish in the background.
- Alignment with the PageIndex library (VERIFIED in `pageindex/chat_stream.py`, `local_chat.py` on GitHub main): its typed process events are `{"type":"thinking"|"answer","delta":...}`, `{"type":"tool_call","call_id","name","arguments"}`, `{"type":"tool_result","call_id","name","output"}`; tool names `get_document_structure(doc_name, part)` and `get_page_content(doc_name, pages="37-40")`; citation tags `<cite doc="..." page="37" block="p37_table_6"/>`. Our server maps: `thinking` -> `step(kind=thought)`/`step_delta`/`step_done`; `tool_call get_page_content` -> `step(kind=read_pages)`; `answer` deltas -> `token`; `<cite .../>` -> `citations` + `[[cN]]` sentinel.

### 7.2 Event catalogue

| `event:` | When | `data` JSON |
|---|---|---|
| `message_start` | user message persisted, work begins | `{message_id, session_id, user_message_id, created_at, models:{answer, judge}}` |
| `step` | a step begins | `{message_id, step_id, kind: "thought"\|"search_tree"\|"read_pages"\|"tool", label, doc_id?, document?, pages?: [88,89,90], t_ms}` |
| `step_delta` (optional) | stream of a thought summary for the expandable body | `{message_id, step_id, delta}` |
| `step_done` | step ends | `{message_id, step_id, status: "ok"\|"error", label, duration_ms, pages?, detail?: {tool, arguments, chars, snippet}}` - `label` is the **final** line e.g. `Thought for 3 seconds`, `Read pages 86–87 from "National Grid_Annual_Report.pdf"` |
| `token` | answer markdown delta | `{message_id, delta}` - may contain `[[c3]]` sentinels; no half sentinels |
| `citations` | one or more times; each carries **new** citations; the last has `final:true` | `{message_id, citations:[Citation], final:false}`; final: `{message_id, citations:[...], sources:[SourceGroup], reference_count, final:true}` |
| `answer_done` | answer text complete | `{message_id, content, finish_reason, usage:{prompt_tokens, completion_tokens, total_tokens}, timings_ms:{retrieval, generation}, contexts:[{page, node_id, chars}]}` |
| `eval_started` | RAGAS run queued/started | `{message_id, metrics:["faithfulness","answer_relevancy","context_precision"], judge_model, n_contexts, started_at}` |
| `eval_result` | each metric as it completes, then the aggregate | per metric: `{message_id, metric, status:"ok"\|"error"\|"skipped", score: 0.92\|null, reason?, elapsed_ms, final:false}`; aggregate: `{message_id, status:"complete"\|"partial"\|"failed"\|"skipped", scores:{faithfulness, answer_relevancy, context_precision}, elapsed_ms, final:true}` |
| `title` | auto-title for the session (first answer) | `{session_id, title}` |
| `error` | any failure | `{message_id?, stage:"upload"\|"retrieval"\|"generation"\|"evaluation", code, message, retryable, detail?}` (errors in `evaluation` must not discard the answer) |
| `done` | end of stream (always sent last) | `{message_id, status:"ok"\|"error"\|"cancelled"}` |

Metric keys follow RAGAS naming (`faithfulness`, `answer_relevancy`/Response Relevancy, `context_precision`); UI labels: Faithfulness, Response relevancy, Context precision.

### 7.3 Types

```jsonc
// Citation  (one per inline chip occurrence)
{
  "id": "c1",                                   // matches [[c1]] in the text
  "doc_id": "doc_01", "document": "National Grid_Annual_Report.pdf",
  "label": "Nationa...Report.pdf p.89",         // server may omit; client can build it
  "page": 89,                                   // 1-based PHYSICAL PDF page (what the viewer shows)
  "page_label": "87",                           // printed folio when different (optional)
  "section": ["Financial review", "Group cash flow"],   // tree-node title path = the cited "area"
  "node_id": "0042",
  "block_id": null,                             // PageIndex block id when available
  "bbox": [112, 340, 905, 620], "bbox_scale": 1000,     // [x0,y0,x1,y1], top-left origin (PageIndex convention); nullable
  "quote": "Cash generated from continuing operations was £6,991 million"  // verbatim span; nullable
}
// SourceGroup (drives the Sources block)
{ "doc_id": "doc_01", "document": "National Grid_Annual_Report.pdf", "pages": [86, 87, 89, 90], "refs": 19 }
```
`bbox`/`block_id` conventions are VERIFIED from PageIndex's SDK: bbox = `[x0,y0,x1,y1]` in a **0-1000 coordinate space from the top-left** (`pageindex/imaging.py::highlight_region`), `get_citations()` returns `document, doc_id, page, block_id, bbox`, and page-only citations may have no bbox. Block-level boxes come from PageIndex Cloud layout blocks; a self-hosted OpenAI route will usually have **page + quote only** unless our backend resolves the quote to rectangles (see 8.3).

### 7.4 Example transcript

```
event: message_start
id: m_7f3:1
data: {"message_id":"m_7f3","session_id":"s_21","user_message_id":"m_7f2","created_at":"2026-10-07T17:10:03Z","models":{"answer":"<openai-model>","judge":"<openai-model>"}}

event: step
id: m_7f3:2
data: {"message_id":"m_7f3","step_id":"st1","kind":"thought","label":"Thinking","t_ms":0}

event: step_done
id: m_7f3:3
data: {"message_id":"m_7f3","step_id":"st1","status":"ok","label":"Thought for 1 second","duration_ms":1180}

event: step
id: m_7f3:4
data: {"message_id":"m_7f3","step_id":"st2","kind":"read_pages","label":"Reading pages 88–90","doc_id":"doc_01","document":"National Grid_Annual_Report.pdf","pages":[88,89,90],"t_ms":1210}

event: step_done
id: m_7f3:5
data: {"message_id":"m_7f3","step_id":"st2","status":"ok","label":"Read pages 88–90 from \"National Grid_Annual_Report.pdf\"","duration_ms":240,"pages":[88,89,90],"detail":{"tool":"get_page_content","arguments":{"pages":"88-90"},"chars":11890}}

event: citations
id: m_7f3:6
data: {"message_id":"m_7f3","final":false,"citations":[{"id":"c1","doc_id":"doc_01","document":"National Grid_Annual_Report.pdf","page":89,"section":["Financial review","Cash flow"],"quote":"Cash generated from continuing operations"}]}

event: token
id: m_7f3:7
data: {"message_id":"m_7f3","delta":"## Core operating cash flow\n\nCash generated from continuing operations was £6,991 million in 2024/25 [[c1]]."}

event: answer_done
id: m_7f3:8
data: {"message_id":"m_7f3","content":"## Core operating cash flow\n\n...[[c1]].","finish_reason":"stop","usage":{"prompt_tokens":38210,"completion_tokens":412,"total_tokens":38622},"timings_ms":{"retrieval":2400,"generation":3100}}

event: citations
id: m_7f3:9
data: {"message_id":"m_7f3","final":true,"citations":[],"sources":[{"doc_id":"doc_01","document":"National Grid_Annual_Report.pdf","pages":[89],"refs":1}],"reference_count":1}

event: eval_started
id: m_7f3:10
data: {"message_id":"m_7f3","metrics":["faithfulness","answer_relevancy","context_precision"],"judge_model":"<openai-model>","n_contexts":3,"started_at":"2026-10-07T17:10:09Z"}

event: eval_result
id: m_7f3:11
data: {"message_id":"m_7f3","metric":"faithfulness","status":"ok","score":0.92,"elapsed_ms":9100,"final":false}

event: eval_result
id: m_7f3:12
data: {"message_id":"m_7f3","status":"complete","scores":{"faithfulness":0.92,"answer_relevancy":0.88,"context_precision":0.71},"elapsed_ms":14230,"final":true}

event: done
id: m_7f3:13
data: {"message_id":"m_7f3","status":"ok"}
```

### 7.5 Client state machine for one assistant message
`pending -> streaming (steps/tokens/citations) -> answered -> evaluating -> complete`; `error` and `cancelled` are terminal side exits; `answered` shows the action row immediately (copy/download enabled; regenerate disabled until `done`). `eval_result.final` flips the score card from skeleton to values.

### 7.6 REST surface the UI needs (keep identical when migrating to the final FastAPI service)

| Method / path | Purpose |
|---|---|
| `POST /api/sessions` -> `{id}` ; `GET /api/sessions` ; `PATCH /api/sessions/{id}` (rename) ; `DELETE /api/sessions/{id}` | Recents list |
| `GET /api/sessions/{id}` | session + messages (steps, citations, content, eval) for reload |
| `POST /api/sessions/{id}/document` (multipart `file`) -> `202 {document_id, status}`; **`409 document_locked`** if any message exists | upload before chat |
| `DELETE /api/sessions/{id}/document` | remove/replace while not locked |
| `GET /api/documents/{doc_id}/events` (SSE) or `GET /api/documents/{doc_id}` (poll) | indexing progress: `index_progress {stage, pct, detail, page?, total?}`, `index_ready {pages, nodes}`, `index_error {code, message}` |
| `GET /api/documents/{doc_id}/file` | the PDF (Starlette `FileResponse`, Range-capable) for pdf.js |
| `GET /api/documents/{doc_id}/thumb` | cover thumbnail (32 x 32 header + larger) |
| `POST /api/sessions/{id}/messages` (SSE) | chat turn (7.2) |
| `GET /api/messages/{id}` ; `POST /api/messages/{id}/cancel` ; `POST /api/messages/{id}/evaluate` (retry) ; `POST /api/messages/{id}/regenerate` | hydrate/poll, cancel, re-run |

FastAPI shell sketch (VERIFIED API shape of 0.142.2):

```python
from fastapi import FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent

@app.post("/api/sessions/{sid}/messages", response_class=EventSourceResponse)
async def send_message(sid: str, body: NewMessage):
    async for ev in chat_service.run(sid, body.text):          # core generator of typed events
        yield ServerSentEvent(event=ev.name, data=ev.data, id=f"{ev.message_id}:{ev.seq}", retry=3000)
```

### 7.7 Minimal client parser (no libraries)

```js
export async function* sse(url, init) {
  const res = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' } });
  if (!res.ok) throw Object.assign(new Error('HTTP ' + res.status), { status: res.status, body: await res.text() });
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value.replace(/\r\n/g, '\n');
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      let event = 'message', id = null; const data = [];
      for (const line of raw.split('\n')) {
        if (line.startsWith(':')) continue;                 // keep-alive comment
        const [k, ...r] = line.split(':'); const v = r.join(':').replace(/^ /, '');
        if (k === 'event') event = v; else if (k === 'id') id = v; else if (k === 'data') data.push(v);
      }
      if (data.length) yield { event, id, data: JSON.parse(data.join('\n')) };
    }
  }
}
```

---------------------------------------------------------------------------------------------------

## 8. Citation -> source panel behaviour (spec)

### 8.1 Click flow
1. `openSource({page, bbox?, quote?, bbox_scale?})`.
2. If the panel is closed: open it (width transition ~200 ms). If the viewer is not created: show skeleton, lazy-load pdf.js, open `/api/documents/{id}/file`.
3. Scroll: if `bbox` -> `pdfViewer.scrollPageIntoView({pageNumber: page, destArray: [null, {name: 'XYZ'}, left, top, null]})` with `top` = bbox top minus ~15 % of the viewport (so the passage sits in the upper third); else scroll to page top. (The reference's screenshot shows the viewer positioned so a mid-page table is in view, i.e. it scrolls to the passage, not the page top.)
4. After the page's `textlayerrendered` / `pagerendered` event, apply the highlight (8.2). Update the footer (`Page 89 / 308`, tooltip with printed label).
5. The clicked chip enters the *active* state; focus moves to the panel header (programmatic focus on the title, `tabindex=-1`) unless the user triggered the click with the mouse (then keep focus); `Esc` closes and restores focus to the chip.

### 8.2 Highlight overlay (temporary)
- Layer: one `div.cite-hl-layer` appended to the target page's `.page` element (position absolute, inset 0, `pointer-events:none`, `aria-hidden="true"`); re-created on `pagerendered` because pdf.js may rebuild page children on zoom.
- Rects use **percentages** of the page box so they survive zoom: `left = x0/scale*100%`, `top = y0/scale*100%`, `width = (x1-x0)/scale*100%`, `height = (y1-y0)/scale*100%` (`scale` = `bbox_scale`, default 1000, top-left origin).
- Style (matches PageIndex's own helper: yellow fill ~25 % + orange outline): `background: rgb(255 210 0 / .35); outline: 2px solid #ff8c00; border-radius: 3px; mix-blend-mode: multiply`.
- Timeline (DECISION): fade in 150 ms -> hold **2.4 s** -> fade out 600 ms (total ~3.2 s), then remove. `prefers-reduced-motion`: no fades, static outline for 3 s. Clicking the same chip again replays it; clicking another chip cancels the previous highlight immediately.
- Persistent affordance after the flash: the chip stays active and the footer shows a small "highlighted: p.89" tag, so the user can re-trigger it.

### 8.3 Where the rectangle comes from (priority order)
1. `bbox` from the backend (PageIndex block bbox, or our own resolver).
2. `quote` -> client text-layer match: wait for the text layer, build normalised text (lower-case, collapse whitespace, strip soft hyphens/hyphenation, unify quotes and `£`), find the quote (try the full quote, then a 6-8-word sliding window), map the match back to the covering `.textLayer span`s and draw one rect per span line (`getClientRects` relative to the page box).
3. Backend resolver (recommended for reliability): when the answer is produced, run `page.search_for(quote)` (PyMuPDF; **AGPL-3.0 or commercial licence - decide before production**) or pypdfium2 text-page search (BSD-3/Apache-2.0) to turn the quote into rectangles and normalise to 0-1000 top-left; send as `bbox`. Tables may need several rects (one per row) - allow `bbox` to be an array of boxes in a later version.
4. Fallback: page-level only -> flash the whole page outline (2 px accent ring, 1.5 s) and the footer page number.
Never fabricate a highlight: if nothing matches, show a one-line notice "Exact passage not located - showing page 89" (`role="status"`).

### 8.4 "Area" in citations
The user asked for "pages and area". Provide three levels: (1) page (chip label), (2) section path from the PageIndex tree (`section: [...]` in the tooltip and in the Sources expanded row), (3) passage highlight (bbox/quote). Tooltip format: `File.pdf - p.89 - Financial review > Cash flow`.

---------------------------------------------------------------------------------------------------

## 9. Accessibility basics to keep

| Topic | Requirement |
|---|---|
| Focus rings | Every interactive element: `:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px }` (`#3788d8` on `#f8f8f8` = 3.5:1 >= 3:1). On filled blue (bubble/send) use `outline: 2px solid #fff; box-shadow: 0 0 0 4px #2d72cf`. Never `outline: none` without a replacement. |
| Contrast (measured) | Reference chip text `#2d72cf` on `#f2f7fd` = **4.41:1** and on hover `#e0ebfa` = **3.94:1** - both below the 4.5:1 AA minimum for 12 px text. DECISION: `--chip-fg #2762b8` -> **5.53:1** (default) / **4.94:1** (hover). Reference user bubble white on `#3788d8` = 3.70:1 -> use `#2d72cf` (4.75:1). Reference muted `#9f9fa9` on `#f8f8f8` = **2.47:1** (steps, timestamp, placeholder, footer) -> use `--fg-meta #6e6e78` (~4.9:1) or `#71717a` (4.55:1). "Recents" `#8f8f8f` on white = 3.23:1 -> `#71717a`. Page links `#2d72cf` on `#f8f8f8` = 4.47:1 -> `--chip-fg`. Status text uses the `*-strong` tokens (`#166534` on `#f0fdf4` = 6.8:1, `#92400e` on `#fffbeb` = 6.8:1, `#991b1b` on `#fef2f2` = 7.6:1). Non-text UI (borders/icons) >= 3:1 where they convey state; decorative hairlines exempt. |
| Names / roles | Chip: `<button type="button" aria-label="Open source: National Grid_Annual_Report.pdf, page 89, Financial review">` (visible label is the truncated text; the accessible name must contain the visible text - WCAG 2.5.3). Sources/steps/eval toggles: `<button aria-expanded aria-controls>`. Panel: `<aside role="complementary" aria-label="Source document">`. Message list: `role="log"`. Progress bars: `role="progressbar"` + `aria-valuenow/min/max`. Score bars: `role="meter"` with `aria-valuemin=0 aria-valuemax=1 aria-valuenow aria-labelledby`. Icon-only buttons: `aria-label` (Copy answer, Download answer, Regenerate, Close source panel, Zoom in, Zoom out, Collapse sidebar, New chat). |
| Live regions | Don't announce every token. `aria-live="off"` on the streaming message; a visually-hidden `role="status"` announces: "Answer ready, 19 references." and "Evaluation complete: faithfulness 0.92, response relevancy 0.88, context precision 0.71." and "Showing page 89, passage highlighted." Errors via `role="alert"`. |
| Keyboard | Full flow without a mouse: Tab order sidebar -> header -> messages -> composer -> panel. `Enter` send / `Shift+Enter` newline; `Esc` closes panel/dialog/tooltip; `Ctrl+Shift+O` new chat; arrows in Recents; panel footer page input accepts Enter; chips are real buttons. Skip link "Skip to composer". |
| Targets | Hit area >= 24 x 24 (WCAG 2.5.8); our icon buttons are 32 x 32. |
| Motion | Honour `prefers-reduced-motion`: disable caret blink/shimmer/slide, highlight fade becomes static outline, smooth-scroll off. No flashing > 3 times/second (`blink-twice` is 2 flashes in 0.4 s: keep it to a single-shot and disable under reduced motion). |
| Colour independence | RAGAS bands have text labels + numbers; error/ok never colour-only. |
| Forms | File input has a visible label/button; drag-and-drop is an enhancement; errors tied with `aria-describedby`. |
| Tooltips | Hover **and** focus; dismissible (Esc), hoverable, persistent (WCAG 1.4.13). |
| PDF viewer | pdf.js text layer is selectable text (keep `textLayerMode: 1`), highlight layer is `aria-hidden`; give the viewport `tabindex=0` + `aria-label="PDF pages"`; offer "Open PDF in new tab" in the info popover. |
| Language / structure | `<html lang="en">`, one `<h1>` (visually hidden app title), landmark regions (`nav`, `main`, `aside`), document titles per session (`<title>` = session title). |

---------------------------------------------------------------------------------------------------

## 10. Gaps, risks and open questions

1. **States not in the screenshots** (hover/focus for nav rows, the exact panel open animation, dark mode in the product, mobile layout, empty state, error states, tooltip styles): specified here as INFERRED defaults - confirm with the user at UAT.
2. **Reference DOM not observed** (login wall): component anatomy is reconstructed; no scraping or credential use was attempted.
3. **Bug-for-bug items we deliberately fix**: "Thought for 1 seconds"; contrast failures (section 9); regenerate icon with no label; timestamp placement ambiguous.
4. **Highlight accuracy**: PageIndex's block-level `bbox` is a Cloud-layout feature (cookbook `pageindex-citation.ipynb`); on a self-hosted OpenAI path we must resolve quotes ourselves. Needs a decision with the backend note (library choice and the AGPL implication of PyMuPDF).
5. **Physical vs printed page numbers**: chips = PDF page index; verify PageIndex's `physical_index` semantics in the retrieval note so the chip number equals the pdf.js page number (reference shows 89 = printed 87).
6. **RAGAS latency**: 3 metrics = many LLM calls; the UI is designed for 10-90 s. Cost/latency toggles ("Evaluate automatically" on/off, evaluate on demand) are worth adding in Settings (DECISION).
7. **Indexing duration** for a 300+ page report is unknown until the PageIndex note is read; progress stages above are generic placeholders.
8. **Size limits** (MB/pages) in the dropzone copy are placeholders.
9. **Streaming markdown edge cases** (partial tables, code fences, `[[` sentinel across chunks) need unit tests; a hold-back buffer on the server side is the simplest fix.
10. **Licences**: Geist (OFL), Lucide (ISC), pdf.js (Apache-2.0), marked (MIT), DOMPurify (MPL-2.0/Apache-2.0) are all permissive; PyMuPDF is AGPL/commercial; PageIndex repo licence text not reviewed in this note.

---------------------------------------------------------------------------------------------------

## 11. Sources

Primary / measured
- Reference screenshots: `C:\Users\ayush\AppData\Local\Temp\claude\C--Users-ayush-Annual-Report-Answering\536cb16d-2d66-4e30-9660-8cbaf6c92128\images\1.webp` and `2.png` (copies + crops in `research/06-ui-assets/`).
- Reference app public CSS bundle (tokens, fingerprints; read-only inspection): `https://app.pageindex.ai/_next/static/chunks/3aqlhko9hhtu_.css` (tokens, Tailwind v4 theme, prose, keyframes), `.../144_gmal_6oqc.css` (pdf.js viewer CSS), `.../1zf-2lqzfja-c.css` (Geist `@font-face`).
- PageIndex GitHub (raw, `main`, fetched 2026-10-07): `https://raw.githubusercontent.com/VectifyAI/PageIndex/main/pageindex/chat_stream.py`, `.../pageindex/local_chat.py` (process events, `show_process`), `.../pageindex/agent_tools.py` (tool names, `get_page_content`, citation prompt formats), `.../pageindex/imaging.py` (`highlight_region`, bbox 0-1000 top-left), `.../pageindex/__init__.py`, `.../cookbook/pageindex-citation.ipynb` (`<cite doc page block/>`, `client.get_citations`, `get_page_image`).
- pdf.js 6.4.299 source: `https://cdn.jsdelivr.net/npm/pdfjs-dist@6.4.299/build/pdf.min.mjs`, `.../web/pdf_viewer.mjs`, `.../web/pdf_viewer.css`; file list `https://data.jsdelivr.com/v1/packages/npm/pdfjs-dist@6.4.299?structure=flat`.
- FastAPI 0.142.2 wheel (`fastapi/sse.py`, `fastapi/routing.py`) and Starlette 1.7.0 wheel (`starlette/responses.py` FileResponse range support), downloaded with `pip download --no-deps`.
- Registries: `https://registry.npmjs.org/<pkg>/latest`, `https://pypi.org/pypi/<pkg>/json`.
- Lucide: `https://data.jsdelivr.com/v1/packages/npm/lucide-static@1.52.0?structure=flat`; Geist fonts: `https://data.jsdelivr.com/v1/packages/npm/geist@1.7.2?structure=flat`; Google Fonts CSS `https://fonts.googleapis.com/css2?family=Geist:wght@100..900`.

Appendix A - raw vertical rhythm of ref-2 chat column (ink extents, x 340..1120)

| y range | height | Element |
|---|---|---|
| 202-211 | 10 | "Sources" header text |
| 240-251 | 12 | source file row text |
| 273-284 | 12 | timestamp / action row |
| 297-344 | 48 | user bubble |
| 384-397 | 14 | step 1 (Thought for 1 seconds) |
| 420-433 | 14 | step 2 (Read pages 88-90) |
| 456-469 | 14 | step 3 (Thought for 3 seconds) |
| 492-505 | 14 | step 4 (Read pages 86-87) - latest, dark |
| 531-552 | 22 | H2 "Core operating cash flow" |
| 570-584 | 15 | paragraph line 1 |
| 593-614 | 22 | chip box (line 2) |
| 623-637 | 15 | paragraph line 3 |
| 673-693 | 21 | H2 "Where the cash originates" |
| 714 | - | table card top border |
| 758 / 795 | - | inner table top border / header bottom border |
| 772-807 | 36 | scroll-to-bottom FAB |
| 824-968 | 145 | composer |

Appendix B - font-fit log (Geist via HarfBuzz; fit size = measured ink width / natural width per px): body 15.99, bubble 15.97, h2 23.97 (@600), steps 14.01, filename 13.91, Recents row 14.01, table header 14.09 (@600), chip 11.93, sources meta 11.98, Select Documents 13.92 (@500), username 14.02 (@500), panel title 13.88 (@600). Inter and Segoe UI fit worse (Inter body 15.36 / needs -1.9 % tracking), so Geist is the font.
