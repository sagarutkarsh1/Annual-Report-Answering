# Third-party front-end assets (vendored, no CDN at runtime)

| Asset | Version | Licence | Files | Source |
|---|---|---|---|---|
| marked | 18.1.0 | MIT | `marked/marked.esm.js`, `marked/LICENSE` | https://github.com/markedjs/marked (npm `marked`) |
| DOMPurify | 3.4.16 | MPL-2.0 OR Apache-2.0 | `dompurify/purify.min.js`, `dompurify/LICENSE` | https://github.com/cure53/DOMPurify (npm `dompurify`, `dist/purify.min.js`) |
| Geist Sans / Geist Mono (variable) | 1.7.2 | SIL Open Font License 1.1 | `fonts/Geist-Variable.woff2`, `fonts/GeistMono-Variable.woff2`, `fonts/OFL.txt` | https://github.com/vercel/geist-font (npm `geist`) |
| Lucide icons | 1.52.0 | ISC | inlined as SVG path data in `../js/icons.js` (about 40 icons) | https://lucide.dev (npm `lucide-static`) |
| Swagger UI | 5.32.15 | Apache-2.0 | `swagger-ui/swagger-ui-bundle.js`, `swagger-ui/swagger-ui.css`, `swagger-ui/LICENSE`, `swagger-ui/NOTICE`, `swagger-ui/swagger-ui-bundle.js.LICENSE.txt` | https://github.com/swagger-api/swagger-ui (npm `swagger-ui-dist`); serves the API reference at `/docs` |

Notes
* The `//# sourceMappingURL` comments were removed from the vendored marked, DOMPurify and Swagger UI files so browsers do not request
  source maps that are not shipped.
* `pdfjs/` (Apache-2.0) is vendored and documented by the source-panel owner in `pdfjs/` / `LICENSES-pdfjs.md`.
* The Annual Report Lens logo mark (`logoMark()` in `../js/icons.js`) is an original drawing; no third-party brand assets are used.
