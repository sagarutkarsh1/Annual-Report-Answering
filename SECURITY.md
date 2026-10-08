# Security

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. E-mail **sagarutkarsh1@gmail.com** with what you found, how to
reproduce it and what an attacker could gain. You will get an answer within a few days; fixes are credited unless you prefer
otherwise.

## How the app protects a public deployment

A short summary; the threat model with every limit is in [docs/DEPLOY.md](docs/DEPLOY.md) (part C).

| Concern | Protection |
|---|---|
| Strangers using the owner's model credit | Access code (constant-time check, 10 wrong tries per address per 10 minutes, then a lockout), a spend budget, a cap on chats and a per-address question limit (`PUBLIC_MODE`) |
| Visitors seeing each other's chats | Private chats: every browser gets a signed visitor cookie; another visitor's chat answers 404, exactly like one that does not exist |
| The read-only demo | Anyone may read it, nobody may change it (checked in the web layer and again in the service) |
| A visitor's own API key | Kept in their browser only (tab storage unless they choose "remember"), sent with their own requests in one header, used for that request and dropped: never written to the process environment, a file, the database or a log; child processes for a visitor's job do not inherit the owner's key; key-shaped strings are redacted from logs |
| Server-side request forgery through a custom model URL | Public deployments accept only the listed providers' fixed URLs; custom URLs (Ollama, private gateways) need `ALLOW_CUSTOM_LLM_URL=1` |
| Cross-site requests | State-changing requests with a foreign `Origin` are refused; cookies are `HttpOnly`, `SameSite=Lax`, `Secure` behind https |
| Host-header attacks | `ALLOWED_HOSTS` allow-list |
| Malicious uploads | Streamed with a size cap before reading, PDF magic check, page cap, the client's file name never becomes a path |
| Script injection in answers | Model output is rendered through DOMPurify; a strict Content-Security-Policy allows no inline scripts and no third-party origins |
| Supply chain | Front-end libraries are vendored (no CDN at runtime); known-malicious litellm releases are refused at build time |

## Data handling

Document text, questions and answers are sent to the model provider that answers (the server's, or the visitor's own).
Uploaded files, indexes and chats live on the server's disk only (temporary on Render's free tier) and are not encrypted at
rest. Do not upload confidential documents to a public demo; run the app on your own machine for those.
