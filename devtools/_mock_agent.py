"""The mock's PageIndex "agent persona" (dev tooling, not part of the product).

The persona is stateless and reads only the conversation it is handed, like a real model would:

  turn 1   no tool output yet                -> get_document_structure(doc_name)       (and further `part`s if paged)
  turn 2   outline seen, nothing read        -> get_page_content(doc_name, "a-b")      best-matching node, <= 4 pages
  turn 3+  pages read                        -> answer if the pages support it, else read the next window (<= 3 reads)
  answer   a short markdown answer built from REAL sentences / table rows found in the pages it was given, each followed by
           <cite doc="NAME" page="N" quote="<verbatim fragment, <= 25 words>"/>.  Nothing relevant ->
           "The report does not appear to state this." with no cite.

Conversation parsing supports both wire formats the PageIndex SDK uses (chat.completions `messages`, Responses `input`).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from devtools._mock_text import STOPWORDS, Unit, content_tokens, has_figure, page_units, tokens

PAGEINDEX_TOOLS = frozenset({"browse_documents", "get_document", "get_document_structure", "get_page_content"})
NOT_FOUND = "The report does not appear to state this."
MAX_PAGES_PER_READ = 4
MAX_READS = 3
GOOD_SCORE = 0.6     # evidence this good ends the search
MIN_SCORE = 0.34     # below this a unit is not an answer
MAX_UNITS = 3

_SYNONYM_WORDS = {   # paraphrases a model bridges without thinking: question word -> words the report uses
    "receive": "proceeds receipts", "received": "proceeds receipts", "spent": "expenditure paid", "spend": "expenditure paid",
    "earn": "profit income", "earned": "profit income", "owe": "debt borrowings", "sales": "revenue", "turnover": "revenue",
    "boss": "chief executive", "ceo": "chief executive", "worth": "equity assets", "salary": "remuneration paid",
    "pay": "remuneration paid", "cost": "expenditure", "invest": "capital expenditure",
}
_SYNONYMS = {tokens(k)[0]: {t for w in v.split() for t in tokens(w)} for k, v in _SYNONYM_WORDS.items()}
_TARGET_RE = re.compile(r"The user has specified document:\s*(.+)")
_PAGENUM_RE = re.compile(r'"pageNum":\s*(\d+)')
_NUMBER_Q = re.compile(r"\b(how (?:much|many)|amount|total|number|value|figure|million|billion|percent|rate|cost|fees?|"
                       r"dividend|revenue|profit|debt|price|pence|cash|capital|equity)\b|[\u00a3$%]", re.I)


# --------------------------------------------------------------------------------------------- data
@dataclass
class Event:
    kind: str                     # "call" | "output"
    call_id: str
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ""


@dataclass
class Convo:
    instructions: str = ""
    users: list[str] = field(default_factory=list)
    question: str = ""
    doc_name: Optional[str] = None
    page_count: Optional[int] = None
    run: list[Event] = field(default_factory=list)   # tool traffic after the last user message


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""           # short summary, emitted only when the request asks for reasoning summaries


# --------------------------------------------------------------------------------------------- parsing
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _args_of(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _finish(convo: Convo, users: list[str], run: list[Event]) -> Convo:
    convo.users = users
    for u in users:
        m = _TARGET_RE.search(u)
        if m and convo.doc_name is None:
            convo.doc_name = m.group(1).strip()
            pm = _PAGENUM_RE.search(u)
            convo.page_count = int(pm.group(1)) if pm else None
    last = users[-1] if users else ""
    convo.question = "" if _TARGET_RE.match(last.lstrip()) else last.strip()
    convo.run = run
    return convo


def convo_from_chat(body: dict) -> Convo:
    convo = Convo()
    msgs = [m for m in body.get("messages", []) if isinstance(m, dict)]
    users: list[str] = []
    run: list[Event] = []
    names: dict[str, str] = {}
    for m in msgs:
        role, text = m.get("role"), _text_of(m.get("content"))
        if role in ("system", "developer"):
            convo.instructions += text + "\n"
        elif role == "user":
            users.append(text)
            run, names = [], {}
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                names[tc.get("id", "")] = fn.get("name", "")
                run.append(Event("call", tc.get("id", ""), fn.get("name", ""), _args_of(fn.get("arguments"))))
        elif role == "tool":
            cid = m.get("tool_call_id", "")
            run.append(Event("output", cid, names.get(cid, ""), {}, text))
    return _finish(convo, users, run)


def convo_from_responses(body: dict) -> Convo:
    convo = Convo()
    convo.instructions = body.get("instructions") if isinstance(body.get("instructions"), str) else ""
    items = body.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    users: list[str] = []
    run: list[Event] = []
    names: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        typ = it.get("type")
        if typ == "function_call":
            names[it.get("call_id", "")] = it.get("name", "")
            run.append(Event("call", it.get("call_id", ""), it.get("name", ""), _args_of(it.get("arguments"))))
        elif typ == "function_call_output":
            out = it.get("output")
            text = out if isinstance(out, str) else _text_of(out)
            run.append(Event("output", it.get("call_id", ""), names.get(it.get("call_id", ""), ""), {}, text))
        elif it.get("role") in ("system", "developer"):
            convo.instructions += _text_of(it.get("content")) + "\n"
        elif it.get("role") == "user":
            text = _text_of(it.get("content"))
            users.append(text)
            run, names = [], {}
    return _finish(convo, users, run)


def _tool_json(text: str) -> dict:
    """A tool result as a dict: the SDK sends the JSON text, sometimes wrapped as {"type":"text","text":"<json>"}."""
    try:
        val = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if isinstance(val, dict) and val.get("type") == "text" and isinstance(val.get("text"), str):
        return _tool_json(val["text"])
    return val if isinstance(val, dict) else {}


def _results(convo: Convo, tool: str) -> list[tuple[dict, dict]]:
    """(arguments, parsed output) of every finished call of `tool` in this run."""
    calls = {e.call_id: e for e in convo.run if e.kind == "call"}
    out = []
    for e in convo.run:
        if e.kind == "output" and e.name == tool:
            out.append((calls[e.call_id].args if e.call_id in calls else {}, _tool_json(e.text)))
    return out


# --------------------------------------------------------------------------------------------- outline ranking
@dataclass
class _Node:
    start: int
    end: int
    title: str
    summary: str
    key_items: list[str]
    score: float = 0.0


def _flatten(nodes: Any, out: list[_Node]) -> None:
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict):
            continue
        s, e = n.get("start_index"), n.get("end_index")
        if isinstance(s, int) and isinstance(e, int) and e >= s >= 1:
            ki = n.get("key_items") if isinstance(n.get("key_items"), list) else []
            out.append(_Node(s, e, str(n.get("title", "")), str(n.get("summary", "")), [str(k) for k in ki]))
        _flatten(n.get("nodes"), out)


def _bigrams(seq: list[str]) -> set[tuple[str, str]]:
    return set(zip(seq, seq[1:]))


class _Query:
    """The question as the persona understands it: content terms weighted by how rare they are across the outline
    (figures and years are weak evidence - '2025/26' is on every page - so they count for a third)."""

    def __init__(self, question: str, nodes: list[_Node]):
        seq = content_tokens(question)
        self.pairs = _bigrams(seq)
        self.terms = list(dict.fromkeys(seq))
        self._node_sets = [(set(tokens(n.title)), set(tokens(" ".join(n.key_items))), set(tokens(n.summary))) for n in nodes]
        n_docs = max(1, len(nodes))
        df = {t: max(1, sum(1 for ti, ki, su in self._node_sets if t in ti or t in ki or t in su)) for t in self.terms}
        self.weights = {t: math.log(1 + n_docs / (1 + df[t])) * (0.3 if t[0].isdigit() else 1.0) for t in self.terms}
        # a few paraphrases a model would bridge without thinking ("receive" -> "proceeds")
        self.alt = {t: _SYNONYMS.get(t, set()) for t in self.terms}
        self.total = sum(self.weights.values()) or 1.0
        self.strong = {t for t in self.terms if not t[0].isdigit()} or set(self.terms)
        self.wants_number = bool(_NUMBER_Q.search(question))

    def rank_nodes(self, nodes: list[_Node]) -> list[_Node]:
        for n, (ti, ki, su) in zip(nodes, self._node_sets):
            where = {t: (2.0 if t in ti else 1.5 if t in ki else 1.0 if t in su else 0.0) for t in self.terms}
            base = sum(self.weights[t] * where[t] for t in self.terms) / (2.0 * self.total)
            node_pairs = _bigrams(content_tokens(f"{n.title} {' '.join(n.key_items)} {n.summary}"))
            phrase = sum(0.5 * (self.weights[a] + self.weights[b]) for a, b in self.pairs & node_pairs) / self.total
            n.score = base + 0.25 * phrase
        best = max((n.score for n in nodes), default=0.0)
        if best <= 0:
            return []
        keep = [n for n in nodes if n.score >= 0.25 * best]
        # prefer narrow nodes: a parent's summary mentions everything below it
        return sorted(keep, key=lambda n: (-(n.score / math.sqrt(n.end - n.start + 1)), n.start))

    def score_units(self, units: list[Unit], page_rel: Optional[dict[int, float]] = None) -> list[tuple[float, Unit, set[str]]]:
        """Units ranked by how well they answer the question; `page_rel` (0..1) favours pages of well-ranked outline nodes."""
        scored = []
        for u in units:
            toks = set(tokens(u.text))
            matched = {t for t in self.terms if t in toks or (self.alt[t] & toks)}
            label = {t for t in toks if not t[0].isdigit() and t not in STOPWORDS} if u.is_row else set()
            label_asked = bool(label) and label <= set(self.terms)   # a table row whose whole label the question names
            if len(matched & self.strong) < min(2, len(self.strong)) and not label_asked:
                continue
            score = (sum(self.weights[t] for t in matched) / self.total
                     + 0.1 * len(self.pairs & _bigrams(content_tokens(u.text))) + (0.15 if label_asked else 0.0))
            if self.wants_number:
                score *= 1.3 if has_figure(u.text) else 0.45
            n = len(u.words)
            score *= 0.8 if n > 70 else (0.9 if n > 45 else 1.0)
            if page_rel:
                score *= 0.7 + 0.5 * page_rel.get(u.page, 0.0)
            scored.append((score, u, matched))
        return sorted(scored, key=lambda x: (-x[0], x[1].page))


def _windows(ranked: list[_Node], done: set[int]) -> list[tuple[int, int]]:
    """Page windows (<= 4 pages) in reading order, skipping pages that were already requested."""
    out: list[tuple[int, int]] = []
    for n in ranked:
        page = n.start
        while page <= n.end:
            if page in done:
                page += 1
                continue
            last = page
            while last + 1 <= n.end and last + 1 not in done and last + 1 - page < MAX_PAGES_PER_READ:
                last += 1
            out.append((page, last))
            page = last + 1
    seen: set[tuple[int, int]] = set()
    return [w for w in out if not (w in seen or seen.add(w))]


def _spec(a: int, b: int) -> str:
    return str(a) if a == b else f"{a}-{b}"


def _parse_pages(spec: str) -> set[int]:
    pages: set[int] = set()
    for part in re.split(r",\s*", str(spec)):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part.strip())
        if m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            pages.update(range(lo, min(hi, lo + 500) + 1))
    return pages


# --------------------------------------------------------------------------------------------- evidence
def _pick(scored: list[tuple[float, Unit, set[str]]]) -> list[tuple[float, Unit, set[str]]]:
    if not scored or scored[0][0] < MIN_SCORE:
        return []
    floor = max(MIN_SCORE, 0.6 * scored[0][0])
    chosen: list[tuple[float, Unit, set[str]]] = []
    for item in scored:
        if item[0] < floor or len(chosen) >= MAX_UNITS:
            break
        toks = set(tokens(item[1].text))
        if any(len(toks & set(tokens(c[1].text))) / max(1, len(toks)) > 0.8 for c in chosen):
            continue
        chosen.append(item)
    return sorted(chosen, key=lambda x: (x[1].page, -x[0]))


def _cite(doc: str, unit: Unit, matched: set[str]) -> str:
    quote = unit.quote_window(matched)
    attrs = f'doc="{doc}" page="{unit.page}"' + (f' quote="{quote}"' if quote else "")
    return f"<cite {attrs}/>"


def compose_answer(doc: str, chosen: list[tuple[float, Unit, set[str]]]) -> str:
    if len(chosen) == 1:
        _, u, m = chosen[0]
        return f"{u.text} {_cite(doc, u, m)}"
    return "\n".join(f"- {u.text} {_cite(doc, u, m)}" for _, u, m in chosen)


# --------------------------------------------------------------------------------------------- the decision
def _topic(question: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", question) if w.lower() not in STOPWORDS]
    return " ".join(words[:5]) or "the question"


def _sequential_windows(page_count: int, done: set[int]) -> list[tuple[int, int]]:
    """Without an outline tool the persona just reads the document from the front."""
    return [(p, min(p + MAX_PAGES_PER_READ - 1, page_count)) for p in range(1, page_count + 1, MAX_PAGES_PER_READ) if p not in done]


def decide(convo: Convo, tools: frozenset[str] = PAGEINDEX_TOOLS) -> Reply:
    """What the persona does next, given everything in the conversation so far and the tools the request offers."""
    q = convo.question
    if not q:
        return Reply(text="Please ask a question about the document.")

    name = convo.doc_name
    if name is None:
        browsed = _results(convo, "browse_documents")
        if not browsed and "browse_documents" in tools:
            return Reply(tool_calls=[ToolCall("browse_documents", {})], reasoning="Checking which documents are available.")
        docs = (browsed[-1][1].get("documents") if browsed else None) or [{}]
        name = str(docs[0].get("name") or "document.pdf")

    outline = "get_document_structure" in tools
    structs = _results(convo, "get_document_structure")
    if outline and not structs:
        return Reply(tool_calls=[ToolCall("get_document_structure", {"doc_name": name})],
                     reasoning=f"Looking at the document outline to find where {_topic(q)} is covered.")
    pag = (structs[-1][1].get("pagination") if structs else None) or {}
    if pag.get("has_more") and isinstance(pag.get("part"), int):
        return Reply(tool_calls=[ToolCall("get_document_structure", {"doc_name": name, "part": pag["part"] + 1})],
                     reasoning="The outline continues; reading the next part.")

    nodes: list[_Node] = []
    for _, out in structs:
        _flatten(out.get("structure"), nodes)
    query = _Query(q, nodes)
    ranked = query.rank_nodes(nodes)

    reads = _results(convo, "get_page_content")
    requested = set().union(*(_parse_pages(a.get("pages", "")) for a, _ in reads)) if reads else set()
    page_text = {c["page"]: c["text"] for _, out in reads for c in (out.get("content") or [])
                 if isinstance(c, dict) and isinstance(c.get("page"), int) and isinstance(c.get("text"), str)}
    units = [u for p in sorted(page_text) for u in page_units(p, page_text[p])]
    page_rel: dict[int, float] = {}
    top = max((n.score for n in ranked), default=0.0) or 1.0
    for n in ranked:
        for p in range(n.start, n.end + 1):
            page_rel[p] = max(page_rel.get(p, 0.0), n.score / top)
    scored = query.score_units(units, page_rel)
    best = scored[0][0] if scored else 0.0

    windows = _windows(ranked, requested) if outline else _sequential_windows(convo.page_count or MAX_PAGES_PER_READ, requested)
    if windows and len(reads) < MAX_READS and best < GOOD_SCORE:
        a, b = windows[0]
        return Reply(tool_calls=[ToolCall("get_page_content", {"doc_name": name, "pages": _spec(a, b)})],
                     reasoning=f"Reading pages {_spec(a, b)}, the sections that look most relevant.")

    chosen = _pick(scored)
    if not chosen:
        return Reply(text=NOT_FOUND, reasoning="The pages I read do not contain the answer.")
    return Reply(text=compose_answer(name, chosen), reasoning="Writing a short answer from the passages I read.")
