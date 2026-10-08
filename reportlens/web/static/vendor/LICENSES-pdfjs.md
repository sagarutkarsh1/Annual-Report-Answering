# Third-party notice: PDF.js (vendored)

ReportLens ships an unmodified copy of **PDF.js** (`pdfjs-dist` **6.4.299**, from the npm registry) under
`static/vendor/pdfjs/`. It is loaded lazily, from the same origin, the first time the source panel opens. Nothing is fetched
from a CDN at runtime.

- Project: https://github.com/mozilla/pdf.js
- Copyright: Mozilla Foundation and PDF.js contributors
- Licence: **Apache License 2.0** - full text in `pdfjs/LICENSE` (identical to the file in the npm package) and at
  http://www.apache.org/licenses/LICENSE-2.0

| Path under `vendor/pdfjs/` | What it is | Licence |
|---|---|---|
| `pdf.min.mjs`, `pdf.worker.min.mjs` | PDF.js core (API + worker), unmodified minified build | Apache-2.0 (`LICENSE`) |
| `standard_fonts/` | Substitute fonts for the 14 standard PDF fonts that are not embedded in a file | Liberation Sans: `LICENSE_LIBERATION`; Foxit/PDFium fonts: `LICENSE_FOXIT` (both shipped by PDF.js) |
| `cmaps/` | Adobe CMap resources for non-embedded CJK / CID fonts | Adobe BSD-style licence, see `cmaps/LICENSE` |
| `iccs/` | ICC colour profile used for CMYK/CalGray conversions | see `iccs/LICENSE` |
| `wasm/` | JBIG2, JPEG 2000 (OpenJPEG) and QCMS decoders compiled to WebAssembly, with JS fallbacks | see the `LICENSE_*` files in `wasm/` (PDFium BSD-style, OpenJPEG BSD-2-Clause, PDF.js Apache-2.0 / MIT, qcms CC0) |

Not vendored on purpose: `pdf_viewer.mjs` / `pdf_viewer.css` (ReportLens has its own page layout in `js/viewer.js`),
`pdf.sandbox*.mjs` and `quickjs-eval.*` (PDF JavaScript is never executed), source maps, and the `legacy/` build
(the modern build needs Chrome/Edge 125+, Firefox ESR 140+ or Safari 18+).

## Upgrading

```
npm pack pdfjs-dist@<version>          # in a scratch folder, then extract
copy build/pdf.min.mjs, build/pdf.worker.min.mjs, LICENSE, standard_fonts/, cmaps/, iccs/, wasm/ (without quickjs-eval.*)
```

The `PDFJS_VERSION` constant in `js/viewer.js` is used as a cache-busting query string; bump it with the files.
`css/viewer.css` contains a trimmed copy of the `.textLayer` rules from `web/pdf_viewer.css`; re-diff them on a major upgrade.
