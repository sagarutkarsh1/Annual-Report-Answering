"""Responders for the non-agent prompts the mock recognises (dev tooling, not part of the product):

  * PageIndex indexing prompts (leaf / parent summary, tree expansion, document description) - extractive, built only from
    the text inside the prompt itself;
  * RAGAS 0.4.3 judge prompts (statement generation, NLI verdicts, question generation, context-precision verdict) -
    schema-valid JSON decided by lexical overlap, so scores are plausible but not meaningful.

`classify_prompt` is the single place that knows the prompt markers; the list is repeated in devtools/mock_openai.py's docstring.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

from devtools._mock_text import content_tokens, page_units, tokens

# marker substring -> kind.  Order matters only for overlapping markers (none today).
PROMPT_MARKERS: tuple[tuple[str, str], ...] = (
    ("splitting an over-long section of a PDF", "index_expand"),
    ("You are given a text chunk from a document", "index_leaf"),
    ("Subsection Titles and Summaries:", "index_parent"),
    ("generate a one-sentence description for the document", "index_description"),
    ("generate a description of the partial document", "index_leaf_classic"),
    ("Break down each sentence into one or more fully understandable statements", "ragas_statements"),
    ("judge the faithfulness of a series of statements", "ragas_nli"),
    ("Generate a question for the given answer and identify if the answer is noncommittal", "ragas_question"),
    ("verify if the context was useful in arriving at the given answer", "ragas_precision"),
    ("Rewrite the user's last question as a standalone question", "rewrite_question"),      # reportlens.qa.rewrite_question
)


def classify_prompt(text: str) -> Optional[str]:
    for marker, kind in PROMPT_MARKERS:
        if marker in text:
            return kind
    return None


# --------------------------------------------------------------------------------------------- indexing prompts
def _words_cap(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]).rstrip(",;:") + "."


def _max_words(prompt: str, default: int = 150) -> int:
    m = re.search(r"within (\d+) words", prompt)
    return int(m.group(1)) if m else default


_RUNNING_CHROME = re.compile(r"annual report(?: and accounts)?\s*(?:\d{4}(?:/\d{2,4})?)?", re.I)


def _label_lines(text: str) -> list[str]:
    """Lines that name things without stating anything: table row labels and headings (no digits, no full stop)."""
    out = []
    for ln in (x.strip() for x in text.splitlines()):
        words = ln.split()
        if 2 <= len(words) <= 14 and not re.search(r"\d", ln) and not re.search(r"[.!?:;,]$", ln) and ln[:1].isupper():
            out.append(ln)
    return out


def extractive_summary(text: str, max_words: int) -> str:
    """The first sentences of the narrative, then what the page's tables and headings are about. Uses only `text`."""
    text = _RUNNING_CHROME.sub(" ", text)
    units = page_units(0, text)
    prose = [u.text for u in units if not u.is_row and len(u.text.split()) >= 5 and re.search(r"[a-z]", u.text)]
    labels = [u.label for u in units if u.is_row and len(u.label.split()) >= 2] + _label_lines(text)
    budget = min(max_words, 110)
    parts: list[str] = []
    used = 0
    for sent in prose:
        n = len(sent.split())
        if used and used + n > budget - (24 if labels else 0):
            break
        parts.append(sent)
        used += n
    if labels:
        parts.append("Covers: " + "; ".join(dict.fromkeys(labels[:20])) + ".")
    summary = " ".join(parts) or " ".join(text.split()[:budget])
    return _words_cap(summary, max_words) or "No text."


def _title_from(text: str) -> str:
    first = next((u.text for u in page_units(0, text) if not u.is_row), " ".join(text.split()[:12]))
    return " ".join(first.split()[:12]).rstrip(".,;:")


def leaf_summary(prompt: str) -> str:
    m = re.search(r"Given Text:(.*?)\n\s*Reply strictly in the following JSON format", prompt, re.S)
    text = m.group(1) if m else prompt
    obj: dict[str, Any] = {"summary": extractive_summary(text, _max_words(prompt))}
    if "Also return a short title" in prompt:
        obj = {"title": _title_from(text), **obj}
    return json.dumps(obj)


def leaf_summary_classic(prompt: str) -> str:
    m = re.search(r"Partial Document Text:(.*?)\n\s*Directly return", prompt, re.S)
    return extractive_summary(m.group(1) if m else prompt, 100)


def parent_summary(prompt: str) -> str:
    title = (re.search(r"Section Title:\s*(.*)", prompt) or [None, "This section"])[1].strip()
    opening = re.search(r"Opening Text:(.*?)\n\s*Subsection Titles and Summaries:", prompt, re.S)
    listing = re.search(r"Subsection Titles and Summaries:\s*(\[.*\])\s*\n\s*Reply strictly", prompt, re.S)
    children: list[dict] = []
    if listing:
        try:
            children = json.loads(listing.group(1))
        except ValueError:
            children = []
    lead = next((u.text for u in page_units(0, opening.group(1)) if not u.is_row), "") if opening else ""
    titles = [str(c.get("title", "")) for c in children if isinstance(c, dict) and c.get("title")]
    parts = [f"{title}."] if not lead else [lead]
    if titles:
        parts.append("Subsections: " + "; ".join(titles) + ".")
    if children and isinstance(children[0], dict) and children[0].get("summary"):
        parts.append(str(children[0]["summary"]).split(". ")[0].rstrip(".") + ".")
    return json.dumps({"summary": _words_cap(" ".join(parts), _max_words(prompt))})


def doc_description(prompt: str) -> str:
    m = re.search(r"Document Structure:\s*(.*?)\n\s*Directly return", prompt, re.S)
    titles: list[str] = []
    if m:
        try:
            structure = ast.literal_eval(m.group(1).strip())
            titles = [str(n.get("title", "")) for n in structure if isinstance(n, dict)]
        except (ValueError, SyntaxError):
            titles = re.findall(r"""['"]title['"]:\s*['"]([^'"]+)['"]""", m.group(1))[:8]
    titles = [t for t in dict.fromkeys(titles) if t and t.lower() != "preface"][:5]
    if not titles:
        return "A long PDF document."
    listed = ", ".join(titles[:-1]) + (" and " if len(titles) > 1 else "") + titles[-1]
    return f"An annual report covering {listed.lower() if len(titles) > 1 else listed}."


# --------------------------------------------------------------------------------------------- RAGAS prompts
def _last_input_json(prompt: str) -> dict:
    """RAGAS prompts end with 'input: {json}\\nOutput: ' after the few-shot examples (which use 'Input:')."""
    i = prompt.rfind("input: {")
    j = prompt.rfind("Output:")
    if i < 0 or j < i:
        return {}
    try:
        val = json.loads(prompt[i + len("input: "):j].strip())
        return val if isinstance(val, dict) else {}
    except ValueError:
        return {}


_MARKUP = re.compile(r"<cite[^>]*/>|\[\[c\d+\]\]|\[\d+(?:,\s*\d+)*\]|^\s*(?:[-*\u2022]|\d+[.)])\s+", re.M)


def _statements(answer: str) -> list[str]:
    plain = _MARKUP.sub("", answer)
    out: list[str] = []
    for line in plain.splitlines():
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\u201c(\u00a3\d])", line.strip()):
            s = re.sub(r"\s+", " ", s).strip()
            if len(s.split()) >= 3:
                out.append(s)
    return list(dict.fromkeys(out))[:12] or ([plain.strip()] if plain.strip() else [])


def _numbers(text: str) -> set[str]:
    """Figures a statement must back up. Years and one/two-digit numbers are ignored: they come from column headers and
    list numbering, which a judge would not hold against a statement."""
    return {t for t in tokens(text) if t[0].isdigit() and (len(t) > 4 or "." in t or (len(t) == 4 and not t.startswith(("19", "20"))))}


def _coverage(needle: str, hay: str) -> float:
    """Share of the needle's substantive words and figures that also occur in the haystack."""
    figures = _numbers(needle)
    words = {t for t in content_tokens(needle) if not t[0].isdigit() or t in figures}
    return len(words & set(tokens(hay))) / len(words) if words else 0.0


def ragas_statements(prompt: str) -> dict:
    return {"statements": _statements(str(_last_input_json(prompt).get("answer", "")))}


def ragas_nli(prompt: str) -> dict:
    data = _last_input_json(prompt)
    ctx = str(data.get("context", ""))
    out = []
    for st in data.get("statements", []):
        st = str(st)
        ok = _coverage(st, ctx) >= 0.65 and _numbers(st) <= _numbers(ctx)
        out.append({"statement": st, "verdict": 1 if ok else 0,
                    "reason": "The key terms and figures of the statement appear in the context." if ok
                    else "The context does not contain the key terms or figures of the statement."})
    return {"statements": out}


_EVASIVE = re.compile(r"does not appear to state|do(?:es)? not (?:state|say|mention)|cannot find|not (?:sure|available)|don't know|do not know", re.I)


_PREDICATE = frozenset({
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "will", "would", "did", "does", "do", "rose", "fell", "grew",
    "increased", "decreased", "declined", "reached", "totalled", "totaled", "stood", "remained", "amounted", "reported", "paid"})
_PERIOD = re.compile(r"^(?:19|20)\d\d(?:/\d{2,4})?$")


def _question_about(answer: str) -> str:
    """A question this answer could be the answer to (the real judge asks the model for one): the answer's leading noun phrase
    plus the years it mentions, digits kept.  Answers that open with a figure or a verb fall back to their first words."""
    first = next(iter(_statements(answer)), "") or answer
    words = first.split()
    phrase: list[str] = []
    for w in words[:8]:
        bare = w.strip(".,;:()\"'“”‘’")
        if not bare or bare.lower() in _PREDICATE or bare[0] in "£$€" or (bare[0].isdigit() and not _PERIOD.match(bare)):
            break                                          # the subject ends where the verb or the figure starts
        phrase.append(bare)
        if w.endswith(":"):
            break
    periods = [p for p in dict.fromkeys(w.strip(".,;:()") for w in words) if _PERIOD.match(p) and p not in phrase][:2]
    if len(phrase) < 2:                                    # opens with a figure / a bare verb: use the words and figures it has
        phrase = [w.strip(".,;:()") for w in words[:10] if w.strip(".,;:()").lower() not in _PREDICATE]
    topic = " ".join(phrase + periods) or "this"
    return f"What was {topic}?"


def ragas_question(prompt: str) -> dict:
    plain = _MARKUP.sub("", str(_last_input_json(prompt).get("response", "")))
    return {"question": _question_about(plain), "noncommittal": 1 if _EVASIVE.search(plain) else 0}


def ragas_precision(prompt: str) -> dict:
    data = _last_input_json(prompt)
    answer, ctx = str(data.get("answer", "")), str(data.get("context", ""))
    shared_numbers = _numbers(answer) & _numbers(ctx)
    ok = _coverage(answer, ctx) >= 0.25 or len(shared_numbers) >= 2
    return {"verdict": 1 if ok else 0,
            "reason": "The context contains information used in the answer." if ok else "The context is not used in the answer."}


# --------------------------------------------------------------------------------------------- schema fallback
def instance_from_schema(schema: dict, defs: Optional[dict] = None, depth: int = 0) -> Any:
    """A minimal valid instance of a JSON schema (for judge prompts the mock does not know)."""
    defs = defs if defs is not None else {**schema.get("$defs", {}), **schema.get("definitions", {})}
    if depth > 6:
        return None
    if "$ref" in schema:
        return instance_from_schema(defs.get(schema["$ref"].split("/")[-1], {}), defs, depth + 1)
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            return instance_from_schema(schema[key][0], defs, depth + 1)
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {k: instance_from_schema(v, defs, depth + 1) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [instance_from_schema(schema.get("items", {}), defs, depth + 1)]
    return {"string": "ok", "integer": 1, "number": 0.5, "boolean": True, "null": None}.get(t, "ok")


def schema_from_system_prompt(system: str) -> Optional[dict]:
    """The JSON schema instructor embeds in its system message ('...match the following json_schema: {...}')."""
    i = system.find("json_schema")
    j = system.find("{", i if i >= 0 else 0)
    if j < 0:
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(system[j:])
    except ValueError:
        return None
    return val if isinstance(val, dict) else None


def rewrite_reply(prompt: str) -> str:
    """The question-rewrite call (reportlens.qa.QAEngine.rewrite_question): hand back the follow-up exactly as typed, the text
    after 'Last question: ' (the mock cannot resolve references, but a real question beats the generic reply)."""
    text = prompt.rsplit("Last question:", 1)[-1].split("\n\nStandalone question:", 1)[0].strip()
    return text or "What does the report say?"


def ragas_reply(kind: str, prompt: str) -> str:
    fn = {"ragas_statements": ragas_statements, "ragas_nli": ragas_nli,
          "ragas_question": ragas_question, "ragas_precision": ragas_precision}[kind]
    return json.dumps(fn(prompt))


def index_reply(kind: str, prompt: str) -> str:
    if kind == "index_expand":
        return json.dumps({"subsections": []})
    return {"index_leaf": leaf_summary, "index_leaf_classic": leaf_summary_classic,
            "index_parent": parent_summary, "index_description": doc_description}[kind](prompt)
