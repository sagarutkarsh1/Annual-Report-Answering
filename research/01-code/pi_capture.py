"""Helpers (tested offline against the mock servers) that turn a PageIndex local chat run into
(answer, retrieved_contexts, citations) - the three things RAGAS and the UI need."""
from __future__ import annotations

import json
import re
from typing import Any

CITE_RE = re.compile(r'<cite\s+([^<>]*?)\s*/?>')
ATTR_RE = re.compile(r'(\w+)=(["\'])(.*?)\2', re.S)


def _tool_text(output: Any) -> str:
    """tool output -> the JSON string. Answer-lane events: {'type':'text','text':...};
    Responses items: [{'type':'input_text','text':...}, ...]; sometimes a bare str."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return output.get("text", "")
    if isinstance(output, list):
        return "\n".join(o.get("text", "") for o in output if isinstance(o, dict))
    return ""


def page_contexts_from_tool_json(text: str) -> list[dict]:
    """get_page_content result -> [{'page': int, 'text': str}]; anything else -> []."""
    try:
        j = json.loads(text)
    except Exception:
        return []
    if not isinstance(j, dict) or not j.get("success") or "content" not in j:
        return []
    return [{"page": c["page"], "text": c["text"]} for c in j["content"] if isinstance(c, dict) and "page" in c]


def from_events(events) -> dict:
    """Consume client.chat(..., stream=True).events (answer lane)."""
    answer, calls, ctx = [], [], []
    for ev in events:
        t = ev["type"]
        if t == "answer":
            answer.append(ev["delta"])
        elif t == "tool_call":
            calls.append({"name": ev["name"], "arguments": ev["arguments"]})
        elif t == "tool_result" and ev["name"] == "get_page_content":
            ctx += page_contexts_from_tool_json(_tool_text(ev["output"]))
    return {"answer": "".join(answer), "tool_calls": calls, "contexts": ctx}


def from_responses_envelope(r: dict) -> dict:
    """Consume client.chat(..., protocol='responses') (non-stream) envelope."""
    calls, ctx, by_id = [], [], {}
    for it in r["items"]:
        if it.get("type") == "function_call":
            by_id[it["call_id"]] = it["name"]
            try:
                args = json.loads(it["arguments"])
            except Exception:
                args = it["arguments"]
            calls.append({"name": it["name"], "arguments": args})
        elif it.get("type") == "function_call_output" and by_id.get(it["call_id"]) == "get_page_content":
            ctx += page_contexts_from_tool_json(_tool_text(it["output"]))
    answer = "".join(c["text"] for o in r["output"] if o.get("type") == "message"
                     for c in o["content"] if c.get("type") == "output_text")
    return {"answer": answer, "tool_calls": calls, "contexts": ctx, "usage": r.get("usage")}


def parse_cites(answer: str) -> list[dict]:
    """Our own parser: keeps extra attributes (e.g. quote=) that client.get_citations drops."""
    out, seen = [], set()
    for m in CITE_RE.finditer(answer):
        attrs = {k: v for k, _, v in ATTR_RE.findall(m.group(1))}
        try:
            page = int(str(attrs.get("page", "")).split("-")[0])
        except ValueError:
            continue
        key = (attrs.get("doc"), page, attrs.get("quote"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"doc": attrs.get("doc"), "page": page, **{k: v for k, v in attrs.items() if k not in ("doc", "page")}})
    return out
