# 05 - Citation -> source side panel: PDF rendering, text location and highlighting

Date of research: 2026-10-07. Machine: Windows 11, Python 3.13.3, Node 22.15. Everything marked **VERIFIED** was observed this session
(in source code, by running code, or in a real browser). **INFERRED** = reasoned, not observed. Nothing here used a paid API.

Deliverables produced with this note (all in `C:\Users\ayush\Annual Report Answering\research\`):

| File | What |
|---|---|
| `quote_locator_prototype.py` | Standalone quote locator (3 backends, rapidfuzz core). ~680 lines. |
| `quote_locator_test.py` | Test script (backend agreement, 124 quote-variant checks x 3 backends, geometry, timing). |
| `test_assets/` | `gen_test_pdf.py`, `add_landscape.py`, `gen_big_pdf.py`, `test5.pdf` (5 pages), `test5_geom.pdf`, `test5.truth.json`, `big300.pdf` (300 pages), `threshold_experiment.py` |
| `citation_viewer_demo/` | Working demo: `server.py` (FastAPI) + `static/citation_panel.js`, `panel.css`, `index.html`, `pdfviewer_probe.html` |

---------------------------------------------------------------------------------------------------

## 0. Decision summary (read this first)

| Question | Recommendation | Why (evidence section) |
|---|---|---|
| Who renders the page? | **pdf.js 6.4.299 in the browser** (canvas, + optional TextLayer for selectable text). Do NOT render page images on the server. | Server-side raster costs CPU per view and gives no text selection (s.2, s.6.4). |
| Which viewer shell? | **Option 1 (verified end-to-end): thin custom continuous viewer on pdf.js *core* only** (`pdf.min.mjs` + worker, ~0.46 MB + 1.26 MB), pages laid out from **server-supplied page sizes**. **Option 2 (probe-verified): stock `PDFViewer`** from `pdf_viewer.mjs` + a percentage-positioned overlay re-added on `pagerendered`. | Option 1 has no jump on load, no dependence on PDFViewer internals that changed in v5/v6; Option 2 gives page labels/zoom presets for free (s.2.6). |
| Where is the passage located? | **On the server**, when the answer is produced (eager, background) with lazy fallback on click. Browser only draws rectangles. | Client-side exact search fails on stream-order traps, has no fuzzy matching; PDFFindController scans the whole doc sequentially (s.2.5, s.2.7). |
| Which PDF library for geometry? | **pypdfium2 (Apache-2.0 / BSD-3-Clause)** for words + boxes. PyMuPDF only if katonic.ai buys the Artifex commercial licence (it is AGPL-3.0 otherwise). | Same accuracy in all tests (<=1 pt agreement); licence-clean for an API (s.5). |
| What does the browser receive? | `{page, method, score, rects:[{x,y,w,h}] (0..1 of the *visible* page), boxes_1000, matched_text, ...}` (s.6.2). Same rects work with pdf.js canvas, PNG, any zoom. | s.3.4, s.6 |
| What if the quote cannot be found? | `method:"page"`, `rects:[]` -> flash the whole page + toast "Exact passage not located". Never invent a highlight. | s.4 |

---------------------------------------------------------------------------------------------------

## 1. Environment and exact versions (VERIFIED)

| Package | Version | Licence (from installed metadata) | Notes |
|---|---|---|---|
| pdfjs-dist (npm) | **6.4.299** (published 2026-10-03T16:47Z; `dist-tags.latest`) | Apache-2.0 | 1566 versions on npm; 6.0.227 = 2026-05-30 (major), 6.1.200 06-27, 6.2.108 07-28, 6.3.289 08-29. `engines.node >=22.13.0 \|\| >=24` (Node use only). |
| pymupdf | **1.28.2** | "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License" | `import fitz` still works but prints a deprecation warning ("use `import pymupdf`"). |
| pypdfium2 | **5.14.0** (PDFium 156.0.8076.0) | BSD-3-Clause, Apache-2.0, + bundled dependency licences (`licenses/data/windows_x64/BUILD_LICENSES/*`) | cp313 Windows wheel installs fine. |
| pdfplumber / pdfminer.six | 0.11.10 / 20260107 | MIT / MIT | |
| rapidfuzz | 3.14.6 | MIT | only hard dependency of the locator core |
| reportlab | 5.0.1 | BSD | used to generate test PDFs |
| fastapi / starlette / uvicorn | 0.142.2 / **1.7.0** / 0.54.0 | | Starlette 1.7.0 `FileResponse` has built-in Range support |
| PyPDF2 / pypdf | 3.0.1 / 6.19.0 | | PageIndex's default parser is PyPDF2 (see s.6.6) |
| Browser used for tests | Chrome **152.0.7977.130** (Claude desktop embedded browser) | | |

Throw-away venv: `C:\Users\ayush\AppData\Local\Temp\claude\C--Users-ayush-Annual-Report-Answering\536cb16d-2d66-4e30-9660-8cbaf6c92128\scratchpad\pdf-venv`
(`pdf-research\pdfjs\package` = unpacked `pdfjs-dist-6.4.299.tgz`, read for all pdf.js claims below).

---------------------------------------------------------------------------------------------------

## 2. Approach A - client-side with pdf.js

### 2.1 Exact URLs (all return HTTP 200 + `access-control-allow-origin: *`; VERIFIED with `curl -I`)

| File | jsDelivr (preferred: immutable, 1y cache) | unpkg | cdnjs |
|---|---|---|---|
| ESM core (459 KB min) | `https://cdn.jsdelivr.net/npm/pdfjs-dist@6.4.299/build/pdf.min.mjs` | `https://unpkg.com/pdfjs-dist@6.4.299/build/pdf.min.mjs` | `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/6.4.299/pdf.min.mjs` |
| Worker (1.26 MB min) | `.../build/pdf.worker.min.mjs` | same path on unpkg | `.../pdf.js/6.4.299/pdf.worker.min.mjs` |
| Viewer components (320 KB) | `.../web/pdf_viewer.mjs` | `https://unpkg.com/pdfjs-dist@6.4.299/web/pdf_viewer.mjs` | `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/6.4.299/pdf_viewer.mjs` (all four viewer URLs return 200 on unpkg/cdnjs); cdnjs has **`pdf_viewer.css`**, while `pdf_viewer.min.css` returns 404 |
| Viewer CSS (168 KB, includes annotation/editor CSS) | `.../web/pdf_viewer.css` | `https://unpkg.com/pdfjs-dist@6.4.299/web/pdf_viewer.css` | `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/6.4.299/pdf_viewer.css` |
| CMaps / standard fonts / wasm / ICC | `.../cmaps/`, `.../standard_fonts/`, `.../wasm/` (jbig2, openjpeg), `.../iccs/` | | |
| Legacy build (older browsers) | `.../legacy/build/pdf.min.mjs`, `.../legacy/build/pdf.worker.min.mjs`, `.../legacy/web/pdf_viewer.mjs` | | |

Package sizes: build 12 MB (incl. maps), web 1.6 MB, cmaps 1.5 MB, standard_fonts 820 KB, wasm 1.6 MB, iccs 20 KB.
Browser support (pdf.js wiki FAQ): modern build = latest Firefox/Chrome; legacy build = Firefox ESR, Chrome 125+, Safari 18+ (use `legacy/` for anything older; v6 source uses `RegExp.escape`, `URL.parse`, `Promise.withResolvers`, CSS `round()`).
**Recommendation for production:** self-host `build/pdf.min.mjs`, `build/pdf.worker.min.mjs`, `standard_fonts/` (and `wasm/`, `cmaps/` only if needed) under `/static/vendor/pdfjs/6.4.299/` - avoids CDN/CSP/offline problems and keeps `worker-src 'self'` (no blob wrapper needed for a same-origin worker; see s.8.3).

### 2.2 Loading code that works in 6.4.299 (VERIFIED in Chrome 152)

```js
const V = "6.4.299", CDN = `https://cdn.jsdelivr.net/npm/pdfjs-dist@${V}`;
const pdfjsLib = await import(`${CDN}/build/pdf.min.mjs`);          // ESM only; also sets globalThis.pdfjsLib
pdfjsLib.GlobalWorkerOptions.workerSrc = `${CDN}/build/pdf.worker.min.mjs`;   // REQUIRED (default "./pdf.worker.mjs" only in Node)
const task = pdfjsLib.getDocument({
  url: "/api/sessions/abc/document/pdf",
  rangeChunkSize: 262144,          // default 65536
  disableStream: true,             // see s.2.4: needed so that disableAutoFetch really works
  disableAutoFetch: true,          // do not prefetch the whole 300-page file
  cMapUrl: `${CDN}/cmaps/`, standardFontDataUrl: `${CDN}/standard_fonts/`, wasmUrl: `${CDN}/wasm/`, iccUrl: `${CDN}/iccs/`,
});
const pdf = await task.promise;     // task.destroy() to cancel; PDFDocumentProxy.destroy() was REMOVED in 6.0 ([api-major] #21245)
```

`DocumentInitParameters` (read from `types/src/display/api.d.ts`, VERIFIED): `url, data, httpHeaders, withCredentials, password, range, rangeChunkSize (65536), worker, verbosity, docBaseUrl, cMapUrl, cMapPacked (true), iccUrl, useSystemFonts, standardFontDataUrl, wasmUrl, useWorkerFetch, useWasm (true), stopAtErrors, maxImageSize (-1), isOffscreenCanvasSupported, isImageDecoderSupported, canvasMaxAreaInBytes, disableFontFace, fontExtraProperties, enableXfa, ownerDocument, disableRange, disableStream, disableAutoFetch, pdfBug, CanvasFactory, FilterFactory, BinaryDataFactory, enableHWA, pagesMapper`. Calling `getDocument()` without an object is removed in 6.0.

### 2.3 Rendering a page + text layer (the v6 API) (VERIFIED, this is what `citation_panel.js` does)

```js
const page = await pdf.getPage(n);
const viewport = page.getViewport({ scale });            // scale = CSS px per PDF point; applies /Rotate and CropBox
canvas.width = Math.floor(viewport.width * dpr); canvas.height = Math.floor(viewport.height * dpr);
canvas.style.width = "100%"; canvas.style.height = "100%";
await page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr,0,0,dpr,0,0] : null }).promise;
// ("canvas" param is the recommended one in 6.x; "canvasContext" still works: build source does `canvas = canvasContext.canvas`)

textDiv.style.setProperty("--total-scale-factor", viewport.scale);   // MUST be set BEFORE render()
textDiv.style.setProperty("--scale-round-x", "1px"); textDiv.style.setProperty("--scale-round-y", "1px");
const tl = new pdfjsLib.TextLayer({ textContentSource: page.streamTextContent(), container: textDiv, viewport });
await tl.render();                 // tl.update({viewport}), tl.cancel(), tl.textDivs, tl.textContentItemsStr, TextLayer.cleanup()
```

* **`renderTextLayer()` / `updateTextLayer()` no longer exist** (0 occurrences in `build/pdf.mjs`); `TextLayer` is a class exported from `pdf.mjs` (`export { ..., TextLayer, TextLayerImages, ... }`). VERIFIED.
* v6 positions every span purely with CSS variables (`--font-height`, `--scale-x`, `--rotate`, `--total-scale-factor`, `--min-font-size`) and `setLayerDimensions()` uses the CSS `round()` function with `--scale-round-x/y`. If you do not load the full `pdf_viewer.css` you must copy the `.textLayer` block (lines ~648-720 of `web/pdf_viewer.css`) - done in `panel.css`.
* **Rotated pages (`/Rotate 90/180/270`) need these three global rules or the text layer is mis-placed** (observed: spans came out vertical and ~400 px off on a landscape page until added; they live at the END of `pdf_viewer.css`): 
  `[data-main-rotation="90"]{transform:rotate(90deg) translateY(-100%)}  [data-main-rotation="180"]{transform:rotate(180deg) translate(-100%,-100%)}  [data-main-rotation="270"]{transform:rotate(270deg) translateX(-100%)}`
  (`setLayerDimensions(div, viewport)` only sets the attribute `data-main-rotation`; the layer is laid out in the *unrotated* page box.) VERIFIED.
* `PageViewport` (6.4.299): fields `viewBox, userUnit, scale, rotation, offsetX, offsetY, transform, width, height, rawDims`; methods `clone()`, `convertToViewportPoint(x,y)`, `convertToPdfPoint(x,y)`. **`convertToViewportRectangle` does not exist any more** (0 hits). `Util.applyTransform(p, m)` now **mutates `p` in place and returns `undefined`** (this broke my first client-side locator; use `viewport.convertToViewportPoint`). VERIFIED.
* Lazy per-page rendering: `IntersectionObserver` on page placeholders with `rootMargin: "150% 0"`, a serial render queue ordered by distance to the viewport centre, `renderTask.cancel()` + `canvas.width = 0` to evict pages farther than ~10 pages. Implemented; with a 300-page doc, jumping to page 150 renders pages 148-152 only (rendered = [150,151,149,152,148]). VERIFIED.
* Canvas cap: limit `dpr` so `width*height*dpr^2 <= 16 M` pixels (done).

### 2.4 HTTP Range / streaming behaviour (VERIFIED with a 300-page, 1.5 MB, non-linearised PDF, `Cache-Control: no-store`, server log of every request)

| `getDocument` options | Requests seen by the server |
|---|---|
| defaults (stream on) | `GET` (no Range) -> **200, full 1,516,336 bytes**; `Range 0-262143` -> 206; `Range 1310720-` (trailer/xref) -> 206. Whole file arrived within ~40 ms on localhost, so the pages needed no further requests. pdf.js *starts* with an un-ranged GET; on a slow link it probably aborts that stream once it knows ranges work (INFERRED - on localhost the body had already arrived). |
| `disableStream:true, disableAutoFetch:true, rangeChunkSize:262144` | `GET` (no Range) probe -> **aborted (0 body bytes)**; `Range 0-262143`; `Range 1310720-`; and for page 150 exactly one more chunk `Range 786432-1048575`. Total ~730 KB of 1.5 MB for "open + render page 150". **Use this for 300-page reports.** |

pdf.js needs `Accept-Ranges: bytes` + `Content-Length` and no `Content-Encoding`; the server must answer 206 with `Content-Range`. Note: when the response is cacheable (`max-age`) Chrome answers pdf.js' range requests from the cached full 200 and the server logs only one request - use `Cache-Control: no-store` (or `?nocache=1` in the demo) when you want to watch real Range traffic.

### 2.5 Computing bounding boxes from text items (approach A done by hand) (VERIFIED against server rects)

`getTextContent()` -> `{items:[{str, dir, transform:[a,b,c,d,e,f], width, height, fontName, hasEOL}], styles:{fontName:{ascent, descent, vertical, fontFamily}}, lang}`.
`transform` maps text space -> PDF user space *including font size*; `width` is in user-space units. For a sub-range [fa, fb] (fractions of the item's characters) of an item:

```js
const fs  = Math.hypot(it.transform[0], it.transform[1]);       // font size
const wEm = it.width / fs;                                       // advance in em
const corners = [[wEm*fa,desc],[wEm*fb,desc],[wEm*fb,asc],[wEm*fa,asc]].map(([u,v]) =>
   vp.convertToViewportPoint(it.transform[0]*u + it.transform[2]*v + it.transform[4],
                             it.transform[1]*u + it.transform[3]*v + it.transform[5]));   // handles rotation
// bounding box of the 4 corners / vp.width,vp.height -> normalised rect
```
(`clientLocate()` in `citation_panel.js`, ~35 lines, exact squashed-substring match over the concatenated items, per-character proportional interpolation inside an item.)
Measured against the server (PDFium) rects for the same exact quotes, on test5.pdf, viewport scale 1: edge differences **0.2-0.8 pt** for plain text, cross-column text and the rotated landscape page; **3.3 pt** at the right edge of a table row (proportional interpolation inside the cell item); client-side produced **one rect per text item** (4 rects where the server merged to 1 row band); and on the stream-order trap page it found **nothing (0 rects)** because pdf.js' items are in the same stream order as MuPDF/PDFium. Time: 2-48 ms per page. Conclusion: geometry from pdf.js items is accurate enough, but you would have to port the whole alignment/fuzzy/column-order logic to JS - not worth it.

### 2.6 `PDFViewer` + `PDFFindController` (stock component) - what I tested (VERIFIED, `pdfviewer_probe.html`)

Wiring that works in 6.4.299 (`pdf.min.mjs` MUST be imported first - `pdf_viewer.mjs` does `const {...} = globalThis.pdfjsLib`):

```js
const pdfjsLib   = await import(`${CDN}/build/pdf.min.mjs`);
const viewerLib  = await import(`${CDN}/web/pdf_viewer.mjs`);   // exports EventBus, PDFLinkService, PDFFindController, PDFViewer, PDFSinglePageViewer, PDFPageView, TextLayerBuilder, FindState, ...
const eventBus = new viewerLib.EventBus();
const linkService = new viewerLib.PDFLinkService({ eventBus });
const findController = new viewerLib.PDFFindController({ eventBus, linkService });
const viewer = new viewerLib.PDFViewer({ container, eventBus, linkService, findController });  // container = overflow:auto, position:absolute; child <div class="pdfViewer">
linkService.setViewer(viewer);
viewer.setDocument(pdf); linkService.setDocument(pdf, null);
eventBus.on("pagesinit", () => { viewer.currentScaleValue = "page-width"; });   // "page-width" == the "100%" of the reference UI (INFERRED from sibling spec)
eventBus.dispatch("find", { source, type:"", query, caseSensitive:false, entireWord:false, highlightAll:true, findPrevious:false, matchDiacritics:false });
eventBus.on("updatefindmatchescount", e => e.matchesCount /* {current,total} */);
eventBus.on("updatefindcontrolstate", e => e.state /* FindState: FOUND=0 NOT_FOUND=1 WRAPPED=2 PENDING=3 */);
```
`PDFFindController` behaviour (read in `web/pdf_viewer.mjs` ~l.880-1200 and measured):

| Query (test5.pdf p1) | Result |
|---|---|
| curly quotes exact | FOUND |
| **straight** quotes vs curly in PDF | FOUND (normalised) |
| hyphen-broken word joined: "infrastructure programme" (PDF has `infra-` / `structure`) | **FOUND** (it de-hyphenates) |
| as printed "infra- structure" | NOT_FOUND |
| ASCII "efficient financial workflow" vs ligatures ﬃ ﬁ ﬂ | FOUND |
| cross-line and cross-column phrase (stream order contiguous) | FOUND |
| "1588 million" vs "(1,588) million" | NOT_FOUND (number punctuation not ignored); "(1,588) million" -> FOUND (2 matches) |

Why it is the wrong tool for citations: (1) exact phrase only - no fuzzy, no dropped/changed words; (2) **document-wide**: `#extractText()` chains `getTextContent` for pages 1..N *sequentially* (`deferred = deferred.then(...)`), so the first hit on page 150 needs pages 1-149 extracted first: **33.9 s** to FOUND on the 300-page test file in this (loaded) environment, vs 20-60 ms for the server locator; (3) cannot be limited to the cited page; (4) 250 ms debounce (`delay`) before every non-"again" find; (5) stream-order dependent exactly like any text search.
Verdict: use it only as a convenience search box, not for citations.

Option-2 overlay on the stock viewer (VERIFIED): overlay `div` with percentage-positioned children appended to `viewer.getPageView(n-1).div`; overlay rects matched text-layer span boxes within 1-2 px; scrolling with
`viewer.scrollPageIntoView({pageNumber, destArray:[null,{name:"XYZ"}, null, topPdf, null]})` where `topPdf = viewBoxTop - minY*pageHeightPt + 0.3*containerHeight/cssPerPt` (PDF user space, origin bottom-left) put the passage at ~30% of the viewport. **PDFPageView removes unknown children on re-render** (zoom change: overlay count went 4 -> 0), so the overlay must be re-created on the `pagerendered` event (`e.pageNumber`).

### 2.7 Summary of approach A

| | Pros | Cons |
|---|---|---|
| pdf.js text items / TextLayer for *locating* | no server work; accurate boxes (<1 pt) | need to port normalisation + fuzzy + column-order to JS; same stream-order problem; per-item rects; PDFFindController scans whole doc |
| pdf.js for *rendering* | best fidelity, text selection, zoom crisp, offloads CPU to client, Range loading | worker 1.26 MB; v6 needs modern browser (legacy build otherwise); CSP needs `worker-src blob:` for CDN worker |

---------------------------------------------------------------------------------------------------

## 3. Approach B - server-side location (PyMuPDF and alternatives)

### 3.1 PyMuPDF 1.28.2 facts (VERIFIED by running)

* `page.search_for(text, *, clip=None, quads=False, flags=None, textpage=None) -> list[Rect|Quad]`. Default `flags` = `TEXT_DEHYPHENATE | TEXT_PRESERVE_WHITESPACE | TEXT_PRESERVE_LIGATURES | TEXT_MEDIABOX_CLIP`. Case-insensitive. **No hit limit parameter any more.**
* A hit spanning several lines returns **one Rect per line**; matches continue across line breaks and across columns when the text is contiguous in the content stream (query "subject to shareholder approval at the AGM" returned 2 rects: end of left column + middle of right column).
* **Whitespace / case tolerant, nothing else is**:

| Case on test PDF | search_for result |
|---|---|
| quote across 2 lines | 2 rects OK |
| straight `"Climate Leadership"` vs curly in PDF | **0 hits** |
| ASCII "efficient" vs ligature glyphs, default flags (PRESERVE_LIGATURES) | **0 hits**; with `flags=pymupdf.TEXT_MEDIABOX_CLIP` (ligatures expanded) -> 1 hit |
| hyphenated line end `infra-`/`structure`: "infrastructure", "infra-structure" | **0 hits**; only literal `"infra- structure"` hits; `TEXT_DEHYPHENATE` (even via `textpage=`) had **no effect** |
| "(1,588)" -> hit, "1,588" -> hit, "1588" -> **0** | number formatting must match |
| ellipsis, dropped words, typos | 0 hits |

  => `search_for` is fine for byte-identical quotes only; LLM quotes need the normalising locator (s.4).
* `page.get_text("words")` -> tuples `(x0, y0, x1, y1, word, block_no, line_no, word_no)`; default order = content-stream/block order (NOT sorted). `get_text("text", sort=True)` interleaves columns (bad). Default `get_text("text")` keeps ligature code points (ﬃ) - normalise with NFKC.
* **Coordinates / rotation / CropBox** (VERIFIED on a page with CropBox (30,100,500,600) + `/Rotate 90`, checked visually by drawing on a pixmap): all text APIs return coordinates in the *unrotated*, CropBox-relative, top-left space. `page.rect` is the visible (rotated, cropped) rect = `Rect(0,0,500,470)`, `page.rotation_matrix = Matrix(0,1,-1,0,500,0)`. **visible = raw * page.rotation_matrix**; normalise by `page.rect.width/height`. Using raw coordinates puts the highlight in the wrong place (blue box in `geom_check.png`). PyMuPDF only returns words inside the CropBox (the title above the crop was excluded: 29 words vs 35 for unfiltered PDFium/pdfplumber; removing `TEXT_MEDIABOX_CLIP` did not change that). The PDFium/pdfplumber backends therefore drop words whose centre is outside the visible box.
* Speed (300-page synthetic doc, 1069 words/page, quiet machine): `get_text("words")` **12 ms/page** (3.6 s for 300 pages), `get_text("text")` 9 ms/page, `search_for` 8.3 ms/page. (Later runs were 10-30x slower because the machine was loaded by other processes - treat absolute numbers as +-10x, ratios as stable.)
* Pixmap render (`page.get_pixmap(dpi=144, alpha=False)`): ~100-260 ms/page when idle (1191x1684 px, ~540 KB PNG/JPEG). Fine as a fallback "server-rendered image + overlay" design, but it costs server CPU per page view and gives no selectable text.

### 3.2 pypdfium2 5.14.0 facts (VERIFIED)

* `pdf = pdfium.PdfDocument(path_or_bytes)`; `page = pdf[i]`; `page.get_size()` = **visible** size (rotation + CropBox applied); `page.get_rotation()`, `get_cropbox()`, `get_mediabox()` (all PDF user space, origin bottom-left, UNrotated).
* `tp = page.get_textpage()`: `count_chars()`, `get_text_range(index=0,count=-1)`, `get_charbox(i, loose=False)` -> `(l,b,r,t)`, `count_rects(index,count)` + `get_rect(k)` (PDFium-merged line rects for a char range), `get_text_bounded(...)`, `search(text, match_case, match_whole_word, consecutive)` -> searcher with `.get_next()` -> `(char_index, char_count)`, `get_index(x,y,x_tol,y_tol)`, `get_textobj`.
* PDFium text quirks: newlines are `\r\n`; ligatures are **already expanded** ("efficient", "official"); a line-end hyphen is **replaced by U+FFFE and the line break dropped** (`infra\ufffestructure`, `net\ufffework`) - i.e. PDFium joins hyphenated words for free, but you must split words at U+FFFE yourself; table rows come out as ONE line ("Net interest paid (1,588) (1,479) (7%)") which is good for row quotes. `tight` char boxes vary per glyph (x-height vs ascender); **`loose=True` gives uniform line-height boxes** -> use these.
* `tp.search()` is also not tolerant (quote style, hyphenation, numbers -> None); same conclusion as PyMuPDF.
* Coordinate conversion to visible top-left space (implemented + verified against PyMuPDF to <=0.8 pt, rotated/cropped/landscape pages):
  `rot 0: (px-cl, ct-py)`, `rot 90: (py-cb, px-cl)`, `rot 180: (cr-px, py-cb)`, `rot 270: (ct-py, cr-px)` with crop box `(cl,cb,cr,ct)` clipped to the media box.
* Reading order: PDFium keeps stream order on normal pages (right-column-first trap preserved), but on an *artificial* portrait page with vertical text it re-ordered lines by position; real landscape `/Rotate 90` pages (text upright after rotation) matched PyMuPDF exactly.
* Speed: text only **9 ms/page** (2.7 s / 300 pages); per-char `get_charbox` in Python ~**31 ms/page** (267 k chars in 30 pages -> 0.94 s) -> single cold page extract+locate **32-57 ms** (vs 14-24 ms PyMuPDF). Fine for an on-click or per-answer call.
* **PDFium is not thread-safe, not even across documents** (pypdfium2 README, "Incompatibility with Threading"). FastAPI runs sync endpoints in a thread pool: use ONE process-wide lock (`PDFIUM_LOCK` in the module) or a single-worker executor. PyMuPDF has the same advice for multithreading.
* Render: `page.render(scale=2).to_pil()` ~0.5-1.4 s/page on the loaded machine; JPEG q82 ~560 KB at 144 dpi.

### 3.3 pdfplumber 0.11.10 facts (VERIFIED)

* `page.extract_words()` -> dicts `x0, x1, top, bottom, text, upright, direction, ...`. **Default re-sorts words line-by-line across the whole page => interleaves the columns** ("billion", "Cash", "generated" ...). Must pass **`use_text_flow=True`**.
* On a page whose glyphs are physically rotated (my vertical-text page) `use_text_flow=True` splits into **single characters** (143 "words"); retry without text flow (done in the backend).
* `page.width/height/bbox` **ignore the CropBox** (visible size = `cropbox` size; `page.bbox` still the rotated media box). Words are in rotated-mediabox space; subtract `(cropbox.x0, page.height - cropbox.y1)` (done; verified equal to PyMuPDF within 0.2 pt).
* Speed: **405 ms/page** (10 pages in 4.04 s) = 33x slower than PyMuPDF; 300 pages ~2 min. OK only for lazy single-page location.

### 3.4 Normalised rectangles (what travels to the browser)

Output rect = `{x, y, w, h}` fractions (0..1) of the **visible** page (after /Rotate and CropBox), origin top-left. In the browser: `left:x*100%; top:y*100%; width:w*100%; height:h*100%` inside a `position:relative` page box. Works at any zoom, DPR, and for canvas or `<img>` rendering. The page-size list (`/api/.../document` -> `pages:[{w,h}]` from `page.get_size()`) equals pdf.js' viewport size within 1.5 pt on all five test pages (portrait, landscape-by-/Rotate, rotated+cropbox) - no console mismatch warnings. Compatibility: also returned as `boxes_1000` = `[x0,y0,x1,y1]` on a 0-1000 top-left grid, the same convention as PageIndex Cloud's `bbox` (see sibling note 06 s.8 / 02 s.6).

---------------------------------------------------------------------------------------------------

## 4. The quote locator (`quote_locator_prototype.py`)

### 4.1 Pipeline

```
backend(page) -> PageWords[Word(text,x0,y0,x1,y1)]  (visible space)
for ordering in (stream order, column order [only if stream is not exact]):
    squash: NFKC -> casefold -> keep only letters/digits -> drop spaces, hyphens, quotes, symbols
            "(1,588)"->"1588"  "e\ufb03cient"->"efficient"  "Group\u2019s"->"groups"  "infra-"+"structure"->"infrastructure"
    1 exact      squashed-substring; prefer token-aligned hit; n_matches = #occurrences        score 1.00
    2 fuzzy      rapidfuzz partial_ratio_alignment (rough) -> coordinate ascent over (start,end) token boundaries
                 maximising  score = coverage * sqrt(precision)  with LCS from rapidfuzz Indel.distance        score = that
    3 fragments  quote split on "...", "…", "[...]": each fragment exact/fuzzy; groups merged separately
    4 block      densest weighted bag-of-tokens window (digits/long tokens weigh more), expanded to full lines   score <= 0.5 (area only)
    5 page       nothing usable -> rects = []                                                                  score 0
locate(): also tries page +-1 (printed-vs-physical off-by-one) and, if given per-page squashed text, a whole-document exact search.
```
* **Rect building**: consecutive matched words are merged into one rect per text line (vertical-overlap test, horizontal gap < 50% page width so a whole table row becomes one band; orientation decided by majority vote so stacked lines never merge; vertical text handled); each ellipsis fragment is merged on its own so a rect never bridges un-quoted text; +-1 pt padding; clamped to the page.
* **Column order** (`column_order`): lines -> segments split at gaps > max(2.2*h, 3% width); segments wider than 60% of the page are full-width bands; inside a band segments are clustered into columns by left edge; reading order = band, column, line. Needed because stream order is not reading order in real layouts (my page 4: right column is drawn first, a sentence flows left-bottom -> right-top; the exact quote is found ONLY in column order, order=columns).
* Document wrapper: `PdfiumDoc(bytes)` (opens from **bytes** => no Windows file lock; global PDFium lock; LRU of 32 page-word lists; memoised `squashed_pages()` ~11 ms/page, 2.28 M chars for 300 pages; whole-doc substring over 300 pages = 19 ms).

Usage:
```python
from quote_locator_prototype import PdfiumDoc, locate
doc = PdfiumDoc(Path("report.pdf").read_bytes())
res = locate(doc.page_words, doc.page_count, page=89, quote="Cash generated from continuing operations was £6,991 million", radius=1,
             doc_squash=doc.squashed_pages)          # callable, only invoked if the +-1 neighbourhood fails
res.to_json()   # {page, hinted_page, method, score, rects, boxes_1000, matched_text, quote, n_matches, order, page_width, page_height, notes}
```
Backends: `pymupdf_page_words(page)`, `pdfium_page_words(pdf, index0)`, `pdfplumber_page_words(page)` - all return the same `PageWords` (verified identical to <=1 pt).

### 4.2 Thresholds (experiment `test_assets/threshold_experiment.py`, VERIFIED)

540 perturbed true spans (6 perturbation types x 90: exact, 2 words replaced, 2 words dropped, 5 junk words added, swap, half) vs 180 negatives (random text built from the SAME 70-word vocabulary = worst case, and real text from other pages) on the 300-page synthetic doc:

| | min | p5 | median | max |
|---|---|---|---|---|
| positives: replace2 | 0.69 | 0.76 | 0.91 | 0.98 |
| positives: drop2 | 0.86 | 0.88 | 0.96 | 0.99 |
| positives: +5 junk words | 0.64 | 0.70 | 0.84 | 0.94 |
| negatives (all) | 0.00 | | 0.00 | **0.49** |

Accept at 0.70: 98.5% of positives accepted, **0% false positives**; at 0.80: 93% / 0%. Chosen `FUZZY_MIN = 0.70`, `BLOCK_MIN_COVERAGE = 0.55` (block score capped at 0.5). Fuzzy stage time: mean 0.66 ms, max 6.7 ms. UI rule used in the demo: `approx = method in (block, page) or score < 0.90` -> dashed outline + toast "Close match (90%)" / "Approximate area".

### 4.3 Test results (`python quote_locator_test.py`, VERIFIED)

* Backend agreement (word boxes, visible space, vs PyMuPDF): pdfium mean |dx0| 0.08 pt, |dx1| 0.15 pt (max 3.6 pt at word ends: loose boxes), |dy_centre| <= 0.5 pt; pdfplumber <= 0.01 pt in x, <= 1.1 pt in y; on portrait, rotated+cropped (500x470 visible) and landscape-by-/Rotate (841.9x595.3) pages.
* **124 quote-variant checks per backend (8 passages x ~15 perturbations): 123/124 OK for pymupdf, pdfium and pdfplumber** (0.13-0.23 s total). Variants: verbatim, collapsed whitespace/newlines, straight quotes/apostrophes, UPPER, em-dash->hyphen, thousands commas stripped, 2 middle words dropped, 2 words replaced, head/tail truncated, junk before/after, ellipsis, first half, **wrong page hint (+-1)**, unmatchable paraphrase (-> `page`). Passages cover: hyphenated line ends (`infra-`/`structure`, `net-`/`work`), ligatures, curly quotes, em dashes, GBP, table row `Net interest paid (1,588) (1,479) (7%)`, a one-token ambiguous cell `(1,588)` (n_matches=2), a passage **crossing from left column to right column**, the stream-order trap page, and a landscape `/Rotate 90` page. The one miss: a 6-word table row quoted with two cells dropped -> only the row label is highlighted (score 0.94).
* Geometry vs ground truth line boxes (`gen_test_pdf.py` writes `*.truth.json`): line recall 0.98-1.00, rect precision 1.00 on exact matches.
* Rotated(90)+CropBox page: all three backends return the same rect (359-371.5 x 89-314.4 pt) as PyMuPDF `search_for * rotation_matrix`.
* Independent visual check: overlays from the PDFium rects drawn on a PDFium render (`test_assets/visual_check.png` made by `test_assets/visual_check.py`; `geom_check.png` = rotated+crop raw-vs-visible rects; 7 cases: cross-column, hyphenation, table row band, column-order, landscape, rotated+crop, ellipsis fragments) - all correct.
* **Realistic input**: 60 quotes (8-50 words) cut out of **PyPDF2** page text (what PageIndex gives the LLM) of random pages of the 300-page doc: **60/60 exact on the right page**, mean 41 ms cold per quote (machine loaded).
* Whole-document fallback: quote from page 200 with hint page 160 -> found page 200 in 291 ms (incl. building squashed text once); steady-state substring over 300 pages 19 ms.
* Timing, one cold page, correct hint (quiet machine): PyMuPDF **13-24 ms**, PDFium **32-41 ms** (extract + locate); warm worst case (miss: all stages) 6-10 ms.

### 4.4 Known limits (honest list)

* Tested on **synthetic** PDFs (reportlab/PyMuPDF) - the real 308-page National Grid report was not available offline (downloading it needs your approval). Run the same harness on it before UAT; extend `P` in the test with 20 real quotes.
* No OCR: scanned / outlined-text pages (no text layer) -> `page` fallback. PageIndex local mode is text-only anyway.
* `block` fallback highlights whole text *lines* around the densest window, not true paragraphs/table cells.
* Multi-occurrence quotes (`n_matches > 1`) pick the first aligned occurrence in reading order; the UI can show "1 of N" (not implemented).
* Numbers differing by the LLM ("6.99 billion" vs "6,991 million") cannot match - the sibling note 03 already requires a numeric guard.
* Column order is a heuristic (3-column pages, sidebars, rotated text blocks untested on real files).
* `UserUnit` != 1 PDFs not tested.

---------------------------------------------------------------------------------------------------

## 5. Licensing and backend recommendation

| | PyMuPDF 1.28.2 | pypdfium2 5.14.0 + PDFium | pdfplumber 0.11.10 / pdfminer.six |
|---|---|---|---|
| Licence | **AGPL-3.0 or Artifex commercial** (installed metadata; docs page says "read the full text of the AGPL ... else contact Artifex"; Artifex = exclusive commercial licensing agent for MuPDF) | **BSD-3-Clause / Apache-2.0** (+ BSD-style PDFium and bundled third-party licences listed in the wheel) | MIT |
| Network/SaaS use | AGPL s.13 (remote network interaction) is the problem for an API: the service (derivative work) would have to be offered as source, or you buy a commercial licence. Not legal advice - ask counsel. The docs page does not explicitly address SaaS. | no copyleft | none |
| Word boxes | native `get_text("words")` (+ blocks/lines) | build from `get_charbox(loose=True)` (done, 80 lines) | native `extract_words` |
| Speed / page | 12 ms words | 9 ms text, ~31 ms with char boxes | **405 ms** |
| Quality here | reference; clips to CropBox; clean multi-column stream order | equal to PyMuPDF on all tests; free hyphen join + ligature expansion; one-line table rows; not thread-safe | equal after rotation/crop fixes; needs `use_text_flow=True` (columns) and a fallback for rotated text |
| Extras | `find_tables`, pixmaps, `search_for` | render, thumbnails (`render(scale=...)`) | table extraction |

**Recommendation:** pypdfium2 for the locator, thumbnails and page sizes (all permissive). Keep the PyMuPDF backend in the module only for UAT comparison or if a commercial licence is purchased. pdfplumber = acceptable lazy single-page fallback, never for bulk. **Whole-pipeline caveat:** sibling note 03 plans PyMuPDF for page text / `find_tables`; if the API ships, make ONE licensing decision for the whole pipeline (PageIndex's default parser `PyPDF2` and `pypdf` are permissive). Mixing "PDFium for geometry, PyMuPDF for text" does not remove the AGPL exposure.

---------------------------------------------------------------------------------------------------

## 6. Target architecture and API (compatible with the later FastAPI service)

### 6.1 Flow
1. **Upload** (before chat): save bytes (`doc.pdf`), compute once: page count, `pages:[{w,h}]` (visible sizes), first-page thumbnail (pypdfium2 `render(scale=0.4)` JPEG), `squashed_pages()` (cache to disk/memory), PageIndex tree.
2. **Answer** (PageIndex agent + OpenAI): the model returns evidence items `{source_id/page, quote}` (sibling note 03: verbatim quotes, 8-300 chars, fragments joined with " … ") - these are the locator inputs. PageIndex local mode only emits page-level `<cite page=.../>`; cloud-only block bboxes are not available without an API key, so **our locator is the source of the "area"**.
3. **Resolve eagerly** after the answer is complete: for each citation run `locate(...)` in a worker (20-60 ms each, serialise through the PDFium lock), store the JSON next to the citation, send `citations:[{cid, page, quote, locate:{...}}]` (or let the browser fetch lazily).
4. **Click**: chip -> `panel.goToCitation(locate)` - no network round-trip if precomputed.

### 6.2 Endpoints (names per the task; all session-scoped, session owns exactly one document)

```
GET  /api/sessions/{sid}/document
     -> {doc_id, name, page_count, pages:[{w,h}], pdf_url:"/api/sessions/{sid}/document/pdf", thumbnail_url, labels?:[...printed page labels]}
GET|HEAD /api/sessions/{sid}/document/pdf          FileResponse (Range/206, ETag, Last-Modified)       (s.8)
GET  /api/sessions/{sid}/citations/{cid}/locate
     -> {
          "cid": "c3", "page": 89, "hinted_page": 89,
          "method": "exact" | "fuzzy" | "fragments" | "block" | "page",
          "score": 0.97,                                  // exact=1.0, fuzzy = coverage*sqrt(precision), block<=0.5, page=0
          "rects": [{"x":0.0823,"y":0.2728,"w":0.1982,"h":0.0137}],   // 0..1 of the VISIBLE page, top-left origin, one per text line
          "boxes_1000": [[82,273,280,287]],               // same, PageIndex-cloud style 0-1000 grid
          "matched_text": "Cash generated from continuing operations was",
          "quote": "...", "n_matches": 1, "order": "stream"|"columns",
          "page_width": 595.28, "page_height": 841.89,    // points, visible
          "notes": ["page corrected from 90 to 89"]
        }
POST /api/sessions/{sid}/locate   {page, quote}   (ad-hoc / tooltips / tests)
```
Reasoning for server-side + normalised rects: independent of renderer and zoom; the same JSON feeds pdf.js now, a PNG preview or a PDF export later; tests run without a browser; works for FastAPI unchanged (`ql.locate` is plain Python, `to_json()` is the response body).

FastAPI sketch (the demo `server.py` is the working version):
```python
@router.api_route("/sessions/{sid}/document/pdf", methods=["GET", "HEAD"])      # HEAD must be registered explicitly
def pdf(sid: str): return FileResponse(path_for(sid), media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="report.pdf"', "Cache-Control": "private, max-age=3600"})
@router.get("/sessions/{sid}/citations/{cid}/locate")
def loc(sid: str, cid: str):
    c = store.citation(sid, cid)
    if c.locate is None: c.locate = ql.locate(docs[sid].page_words, docs[sid].page_count, c.page, c.quote, radius=1, doc_squash=docs[sid].squashed_pages).to_json()
    return c.locate
```

### 6.3 Fallback chain (UI side, implemented in `goToCitation`)
`exact` -> solid highlight, pulses ~4.2 s (`animationend` removes nodes); `fuzzy` with score < 0.9 or `block` -> dashed outline + toast; `page` (no rects) -> scroll to page, `.flash` ring around the page (2.6 s) + toast "Showing page N. The exact passage could not be located."

### 6.4 Why not server-rendered page images
Pros: pixel-identical geometry, no pdf.js. Cons: 100-260 ms (idle) to ~1 s (loaded) CPU per page view, ~0.5 MB/page, no text selection, blurry at zoom unless re-rendered. Only worth it for a mobile/fallback mode.

### 6.5 Printed page labels vs physical page
PageIndex everything is **physical 1-based** (`<physical_index_N>`, `physical_index`). The reference UI shows "p.89" chips for physical pages although the printed folio differs (sibling note 06). The locator tries +-1 page (and whole-doc exact) to survive off-by-one citations; show printed labels in tooltips (pdf.js `PDFViewer.currentPageLabel`, or `pdf.getPageLabels()`).

### 6.6 PageIndex facts relevant here (VERIFIED in `VectifyAI/PageIndex` main, `pageindex/utils.py`)
`get_page_tokens(pdf_path, model=None, pdf_parser="PyPDF2")` - default text parser is **PyPDF2** (option `"PyMuPDF"`); page numbers in prompts are `<physical_index_N>`. So LLM quotes carry PyPDF2 extraction quirks (ligature code points, `infra-\nstructure`, no space repair). The squash normalisation covers those; tested 60/60 (s.4.3).

---------------------------------------------------------------------------------------------------

## 7. UX details: what the demo implements and what was verified

Reference (user's PageIndex screenshots, crops `a_panel_head.png`, `a_panel_bottom.png`, `2rgb.png`): header = cover thumbnail + truncated name ("Nationa...Report.pdf") + "Document" + X; body = **continuous scroll** of pages; footer = info icon, "Add Page to Chat", "Page [89] / 308", zoom-out, "100%", zoom-in. Sibling spec 06: header 52 px, footer 48 px, panel ~42% width (min 360, max 60%), highlight timeline 150 ms in / 2.4 s hold / 600 ms out.

| Feature | Status in `citation_panel.js` / `panel.css` | Verified how |
|---|---|---|
| Open from chip, scroll to the passage (first line at ~30% of viewport), smooth scroll | yes | JS + screenshots |
| Pulsing highlight, ~4.2 s then removed (`@keyframes cp-pulse`, CSS box-shadow ring + multiply fill); reduced-motion -> plain fade | yes (timeline is 1 CSS block; change to the 150/2400/600 ms spec in one place) | rects confirmed; animation paused for measurement |
| Approximate highlight (dashed) + toast | yes | |
| Page-only fallback: page ring flash + toast | yes | |
| Loading skeleton per page (shimmer) until canvas rendered | yes | |
| Page nav: prev/next buttons, editable page input (Enter), "/ total", indicator follows scroll (binary search over page tops; locked during programmatic smooth scroll) | yes | page input -> page 4 OK |
| Zoom - / % / + (50..300%, 100% = fit-width), reset by clicking %, re-layout keeps the same spot of the same page; visible pages are released and re-rendered crisp (skeleton shimmer meanwhile; keeping the old canvas CSS-stretched until the new one is ready would be a nicer touch, not done) | yes | 110%/150% steps, key `0`, key `-` |
| Panel resize by dragging the left edge (clamped 320 px..75vw, saved in `localStorage` in try/catch, double-click resets) + `ResizeObserver` re-fit | yes | CSS var change -> page width 542 -> 373 -> 542 |
| Close (X, Esc) | yes | Esc test |
| Keyboard (when the panel has focus): `[` `]` prev/next page, `+` `-` `0` zoom, `g` focus page box, Home/End, Esc | yes | |
| Selectable text (TextLayer), correct on /Rotate pages | yes (needs the 3 rotation CSS rules) | |
| Per-page fit-width (landscape page 5 fits too) | yes | |
| Memory control: only ~10 pages keep canvases | yes | |
| Mobile (<820 px): panel becomes full-screen sheet | CSS only | not tested |
| Header thumbnail from page 1; "Add Page to Chat", info icon | placeholder gradient / not implemented | |

End-to-end browser test (Chrome 152, demo page): 9 citation cases (plain, hyphenated+curly quotes, cross-column, table row, stream-order trap, landscape, fuzzy, wrong page hint, unmatchable): overlay rects vs the pdf.js text-layer spans under them - **head and tail of every quote found in the covered spans for all 8 locatable cases**; unmatchable case -> `flash` + toast. On the rotated(90)+CropBox file the overlay (1097,268,13x244) coincided with the text span (1098,269,11x242).

---------------------------------------------------------------------------------------------------

## 8. Serving the PDF from FastAPI/Starlette

### 8.1 Range support (VERIFIED, starlette 1.7.0 source + curl + browser)
`FileResponse` natively handles `Range` (single and multiple ranges, `max_ranges=100`), `If-Range`, HEAD, `Accept-Ranges: bytes`, `ETag`, `Last-Modified`, 416 with `Content-Range: bytes */N`. curl results: `HEAD` 200 with `content-length: 131456`; `Range: bytes=0-99` -> `206`, `content-range: bytes 0-99/131456`, `content-length: 100`; `bytes=131000-` -> 206; `bytes=999999-` -> 416. In FastAPI use `@app.api_route(path, methods=["GET","HEAD"])` - plain `@app.get` does not register HEAD (I saw 404 because of my root static mount; it would be 405 otherwise). pdf.js itself only sends GET.

### 8.2 Headers / CORS
Same-origin UI -> no CORS needed. If the UI is on another origin: `Access-Control-Allow-Origin: <ui>`, `Access-Control-Allow-Headers: Range, If-Range`, `Access-Control-Expose-Headers: Accept-Ranges, Content-Range, Content-Length, ETag` (pdf.js sends a `Range` header -> preflight), do not enable gzip for `/pdf`. Add `Content-Disposition: inline`, `X-Content-Type-Options: nosniff`. Cache: `private, max-age=3600` + ETag is fine (the browser then serves later range requests from its cached copy). Auth: PDFs are per-session; check ownership in the route (cookies; `withCredentials` in `getDocument` only for cross-origin).

### 8.3 CSP (VERIFIED with 3 header variants in the browser)
* pdf.js from jsDelivr with a cross-origin worker: pdf.js wraps it in a Blob (`new Worker(URL.createObjectURL(new Blob(['await import("<cdn url>");'])), {type:"module"})`, source `PDFWorker._createCDNWrapper`) => CSP needs **`worker-src blob:`** and the CDN in `script-src` (the blob imports it). Tested working policy (demo `DEMO_CSP=1`, no console errors): `default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; worker-src blob: https://cdn.jsdelivr.net; connect-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: blob:; font-src 'self' data: https://cdn.jsdelivr.net` (`'unsafe-inline'` only because the demo page has an inline module script; `worker-src blob:` alone, `DEMO_CSP=2`, also works).
* Without `blob:` pdf.js logs the CSP violation then **"Warning: Setting up fake worker."** and continues on the main thread (works but janky).
* Without `font-src https://cdn.jsdelivr.net`, non-embedded standard fonts (e.g. Helvetica, used by the demo PDF's first title) log "Loading the font ... LiberationSans-Regular.ttf violates ... font-src" and fall back to a system font. Self-hosting `standard_fonts/` same-origin removes both issues.
* Self-hosted same-origin worker (`/static/vendor/pdfjs/...`): `worker-src 'self'` is enough (no blob wrapper because `_isSameOrigin`).

### 8.4 Windows file-lock gotchas (VERIFIED on Windows 11)
`pymupdf.open(path)`, `pdfium.PdfDocument(path)`, `pdfplumber.open(path)` and any unclosed `open(path)` keep the file open: `os.remove` -> `PermissionError [WinError 32]`; `shutil.rmtree` of the session folder fails the same way. **Open from bytes** (`pymupdf.open(stream=data)`, `pdfium.PdfDocument(data)`) - deletion works. Always `close()` documents on "New chat"/session delete; Starlette's `FileResponse` opens the file only for the duration of the response (a long-running range download can still block a delete -> retry or soft-delete). Also: port 8766 was already bound by another local process during testing (`WinError 10048`) - make the port configurable.

### 8.5 Memory / concurrency
Keep one `PdfiumDoc` (bytes in memory, 5-30 MB) per active session in an LRU (e.g. 8) and close evicted ones; guard all PDFium calls with `PDFIUM_LOCK` (not thread-safe); cache `locate` JSON per `(page, quote)`.

---------------------------------------------------------------------------------------------------

## 9. Gotchas collected (all hit or verified this session)

1. pdfjs-dist 6.x is ESM-only (`.mjs`); `pdf_viewer.mjs` reads `globalThis.pdfjsLib`, so import `pdf.min.mjs` first.
2. `renderTextLayer` is gone - use `new TextLayer({...}).render()`; set `--total-scale-factor` before `render()`.
3. `Util.applyTransform(p, m)` mutates and returns `undefined`; `convertToViewportRectangle` is gone; `PDFDocumentProxy.destroy()` is gone (use `loadingTask.destroy()`).
4. `[data-main-rotation]` CSS rules are mandatory for `/Rotate` pages when you do not ship the full `pdf_viewer.css`.
5. `disableAutoFetch` only works together with `disableStream: true`; pdf.js always probes with an un-ranged GET first.
6. Cacheable PDF + Chrome = range requests answered from cache (server sees one request) - fine in production, confusing in tests.
7. CDN worker needs `worker-src blob:`; otherwise "fake worker". Fonts need `font-src`.
8. PDFFindController: sequential whole-document text extraction, exact-phrase only, 250 ms debounce - not a citation tool.
9. `PDFPageView` wipes foreign children on re-render - re-add overlays on `pagerendered`.
10. PyMuPDF: raw coordinates are unrotated/cropbox-relative -> multiply by `page.rotation_matrix`; `TEXT_DEHYPHENATE` does not help `search_for`; ASCII-vs-ligature needs flags without `TEXT_PRESERVE_LIGATURES`; `search_for` has no tolerance for quotes/numbers; `import fitz` is deprecated.
11. PDFium: coordinates bottom-left & unrotated; line-end hyphen becomes U+FFFE; use `loose=True` char boxes; not thread-safe.
12. pdfplumber: default `extract_words` interleaves columns (use `use_text_flow=True`); rotated glyphs -> single characters; `bbox` ignores CropBox; slow.
13. Stream order != reading order (my page 4): always keep a geometric column order as the second pass.
14. The built-in test browser stops `requestAnimationFrame`/IntersectionObserver/ResizeObserver while the pane is hidden (renders took 10x longer, IO never fired) - take a screenshot first when testing in it; not an app bug.
15. Chrome caches static module JS heuristically; use `Cache-Control: no-cache` in dev (the demo server does) or version query strings.
16. Static assets: `index.html` imports `./citation_panel.js?v=5` (cache-bust string).

---------------------------------------------------------------------------------------------------

## 10. How to run / reproduce

```powershell
cd "C:\Users\ayush\Annual Report Answering\research"
python -m venv .pdfvenv ; .\.pdfvenv\Scripts\pip install pymupdf pypdfium2 pdfplumber rapidfuzz fastapi uvicorn reportlab pypdf2
python quote_locator_test.py                        # uses test_assets\test5.pdf, test5_geom.pdf, big300.pdf
python test_assets\threshold_experiment.py test_assets\big300.pdf
set DEMO_PDF=test_assets\test5.pdf ; python citation_viewer_demo\server.py     # http://127.0.0.1:8765/   (PORT env to change)
#   ?nocache=1 -> unique PDF URL (watch Range traffic), &nostream=1 -> disableStream:true ; pdfviewer_probe.html = stock PDFViewer probe
#   DEMO_CSP=1|2|3 -> CSP variants, DEMO_PDF_CACHE=no-store
python test_assets\gen_test_pdf.py test4.pdf ; python test_assets\add_landscape.py       # regenerate test PDFs (run in the same folder)
```
Set `PYTHONUTF8=1` on Windows (console is cp1252; the tests print ligatures/pound signs).

---------------------------------------------------------------------------------------------------

## 11. Open points / risks / not verified

* Real 308-page National Grid PDF **not tested** (no permission to download; no local copy). Highest risk: odd stream order, text as outlines, 3-column layouts, tables with floating numbers, very large images (pdf.js render time - the 250-300 ms/page numbers come from a loaded machine and dense synthetic text).
* Absolute timings are +-10x noisy (machine shared with other agents); ratios and algorithm costs are reliable.
* Safari/Firefox not tested (Chrome 152 only). v6 needs the legacy build for old browsers.
* Mobile layout, touch pinch-zoom, accessibility (focus management, `aria-live` toast is in place) not tested.
* `PDFViewer` Option 2: overlay + scroll verified, but zoom/resize re-add logic is described not implemented.
* Printed page labels: not implemented (pdf.js `getPageLabels()` available).
* `UserUnit`, encrypted PDFs (`password` option exists), XFA, optional-content layers not tested.
* Licence statements are my reading of the metadata/docs - not legal advice.

---------------------------------------------------------------------------------------------------

## 12. Sources (all opened this session)

* pdf.js release notes (GitHub API): https://github.com/mozilla/pdf.js/releases (v6.4.299 2026-10-03, v6.0.227 2026-05-30 `[api-major]` list); FAQ https://github.com/mozilla/pdf.js/wiki/Frequently-Asked-Questions ; examples https://github.com/mozilla/pdf.js/tree/master/examples (`learning/helloworld.html`, `components/simpleviewer.mjs`); docs index https://mozilla.github.io/pdf.js/examples/ (outdated: still shows `canvasContext`, no TextLayer example).
* pdfjs-dist 6.4.299 package contents read locally: `build/pdf.mjs` (PageViewport l.806, TextLayer l.15006, PDFWorker l.16200+, setLayerDimensions l.1517), `web/pdf_viewer.mjs` (PDFFindController l.~850-1200, TextHighlighter l.~6030), `web/pdf_viewer.css` (.textLayer l.648, rotation rules l.6292), `types/src/display/api.d.ts`, `text_layer.d.ts`, `page_viewport.d.ts`, `types/web/pdf_find_controller.d.ts`. npm registry: https://registry.npmjs.org/pdfjs-dist ; cdnjs API https://api.cdnjs.com/libraries/pdf.js/6.4.299 ; jsDelivr data API https://data.jsdelivr.com/v1/packages/npm/pdfjs-dist@6.4.299.
* PyMuPDF licensing/docs: https://pymupdf.readthedocs.io/en/latest/about.html ; installed `pymupdf.Page.search_for` source (defaults/flags).
* pypdfium2 README (licences, threading): https://raw.githubusercontent.com/pypdfium2-team/pypdfium2/main/README.md ; API https://pypdfium2.readthedocs.io/en/stable/python_api.html
* Starlette `FileResponse` source (installed 1.7.0).
* VectifyAI/PageIndex `pageindex/utils.py` (parser default, physical_index): https://github.com/VectifyAI/PageIndex
* Sibling notes in this folder: `02-pageindex-cloud-docs-and-api.md` s.6 (citation formats, bbox 0-1000), `03-pageindex-retrieval-patterns-and-prompts.md` (evidence quotes, normalisation, PyMuPDF/AGPL use), `06-ui-design-spec-from-screenshots.md` s.8 (panel behaviour spec; its s.8.3 priority "bbox -> client text match -> backend resolver" should be reordered: **backend resolver first**, client text match only as a last resort).
