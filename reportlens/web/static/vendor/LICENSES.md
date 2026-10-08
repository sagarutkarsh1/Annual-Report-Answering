# Third-party front-end assets (vendored, no CDN at runtime)

| Asset | Version | Licence | Files | Source |
|---|---|---|---|---|
| marked | 18.1.0 | MIT | `marked/marked.esm.js`, `marked/LICENSE` | https://github.com/markedjs/marked (npm `marked`) |
| DOMPurify | 3.4.16 | MPL-2.0 OR Apache-2.0 | `dompurify/purify.min.js`, `dompurify/LICENSE` | https://github.com/cure53/DOMPurify (npm `dompurify`, `dist/purify.min.js`) |
| Geist Sans / Geist Mono (variable) | 1.7.2 | SIL Open Font License 1.1 | `fonts/Geist-Variable.woff2`, `fonts/GeistMono-Variable.woff2`, `fonts/OFL.txt` | https://github.com/vercel/geist-font (npm `geist`) |
| Lucide icons | 1.52.0 | ISC | inlined as SVG path data in `../js/icons.js` (about 40 icons) | https://lucide.dev (npm `lucide-static`) |

Notes
* The `//# sourceMappingURL` comments were removed from the vendored marked and DOMPurify files so browsers do not request
  source maps that are not shipped.
* `pdfjs/` (Apache-2.0) is vendored and documented by the source-panel owner in `pdfjs/` / `LICENSES-pdfjs.md`.
* The ReportLens logo mark (`logoMark()` in `../js/icons.js`) is an original drawing; no third-party brand assets are used.
