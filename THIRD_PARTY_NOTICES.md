# Third-party software

Annual Report Lens is MIT-licensed (see `LICENSE`). It builds on the open-source projects below, each under its own licence.
Python packages are installed from PyPI at build time (`requirements-deploy.txt`, `pyproject.toml`) and are not part of this
repository; the front-end assets in `reportlens/web/static/vendor/` are vendored copies with their licence files next to them
(`reportlens/web/static/vendor/LICENSES.md`, `LICENSES-pdfjs.md`).

## Python packages (main ones)

| Package | Licence | Used for |
|---|---|---|
| [PageIndex](https://github.com/VectifyAI/PageIndex) (`pageindex`) | MIT | Vectorless, reasoning-based indexing (the section tree) and the answering agent |
| [RAGAS](https://github.com/explodinggradients/ragas) (`ragas`) | Apache-2.0 | Faithfulness, answer relevancy and context precision scores |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) (`openai-agents`) | MIT | The agent loop behind PageIndex chat |
| [OpenAI Python](https://github.com/openai/openai-python) (`openai`) | Apache-2.0 | Model calls on every OpenAI-compatible provider |
| [LiteLLM](https://github.com/BerriAI/litellm) (`litellm`) | MIT | Optional routing to other providers (loaded only when needed) |
| [instructor](https://github.com/instructor-ai/instructor) | MIT | Structured judge outputs inside RAGAS |
| [FastAPI](https://github.com/fastapi/fastapi) / [Starlette](https://github.com/encode/starlette) | MIT / BSD-3-Clause | Web server and REST API |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Data models |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) / PDFium | Apache-2.0 or BSD-3-Clause | PDF text, layout and the quote locator |
| [PyPDF2](https://github.com/py-pdf/pypdf) | BSD-3-Clause | Used by PageIndex |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | Fuzzy quote matching |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause | HTTP client |
| [tiktoken](https://github.com/openai/tiktoken) | MIT | Token counting while indexing |
| [python-multipart](https://github.com/Kludex/python-multipart), [python-dotenv](https://github.com/theskumar/python-dotenv) | Apache-2.0, BSD-3-Clause | Uploads, `.env` files |

## Front-end assets (vendored)

| Asset | Licence |
|---|---|
| [pdf.js](https://github.com/mozilla/pdf.js) | Apache-2.0 |
| [marked](https://github.com/markedjs/marked) | MIT |
| [DOMPurify](https://github.com/cure53/DOMPurify) | MPL-2.0 or Apache-2.0 |
| [Swagger UI](https://github.com/swagger-api/swagger-ui) | Apache-2.0 |
| [Geist / Geist Mono](https://github.com/vercel/geist-font) fonts | SIL Open Font License 1.1 |
| [Lucide](https://lucide.dev) icons (inlined SVG paths) | ISC |

## Documents

The demo chat a deployment may show (`demo/`, not part of this repository) quotes a third-party document, for example a
company's published annual report. That document belongs to its publisher and is shown for demonstration only, with
attribution; it is not covered by this project's MIT licence.
