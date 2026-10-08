"""Model answer -> display text with [[cN]] markers + resolved citations (page, folio, section, verified passage, highlight rects).

The agent writes `<cite doc=".." page="N" quote=".."/>` after each claim.  We never show those tags: they become `[[cN]]`
markers (the browser renders chip N from `citations[N-1]`) and every cite is resolved against the PDF:

    quote found on the cited page (or +-1 / elsewhere)  -> quote_source "model"    (verbatim passage + rects)
    else the claim sentence aligned to the page         -> quote_source "aligned"  (best supporting span + rects)
    else                                                -> page-only               (the browser flashes the whole page)

Nothing here raises on malformed model output: unusable tags are removed from the text and counted in stats["n_dropped"].
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from .locator import MIN_QUOTE_CHARS, LocateResult, PdfiumDoc, align_claim, locate_quote, squash
from .models import Citation, Rect, Source

log = logging.getLogger("reportlens.citations")

MARKER = "[[c{n}]]"
_MARKER_RE = re.compile(r"\[\[c(\d+)\]\]")
CITE_RE = re.compile(r"<cite(?=[\s/>])", re.I)          # the opening of a cite tag; the tolerant scanner below reads the rest
_CLOSE_TAG = "</cite>"
_MAX_TAG_CHARS = 2000       # an unterminated "<cite" this long is prose, not a tag (real tags are well under 1000)
_MAX_CLAIM_CHARS = 500
_VERIFIED = ("exact", "fuzzy", "fragments")

# --------------------------------------------------------------------------------------- tag scanner

_NAME_RE = re.compile(r"[A-Za-z_][\w:.-]*")
_AFTER_VALUE_RE = re.compile(r"\s*(?:/?>|[A-Za-z_][\w:.-]*\s*=)")        # what may follow a quoted value's closing quote
_PARTIAL_AFTER_RE = re.compile(r"\s*(?:/|[A-Za-z_][\w:.-]*\s*)?\Z")      # ... or a prefix of it (more input may complete it)
_OPEN_TO_CLOSE = {'"': '"”', "'": "'’", "“": '”"', "‘": "’'", "”": '”"', "’": "’'"}
_UNQUOTED_RE = re.compile(r"[^\s>]*?(?=\s|/?>|\Z)")
_INCOMPLETE = object()


def _value_end(s: str, start: int, closers: str):
    """Index of the quote closing the value that starts at `start`, `_INCOMPLETE` if more input could still close it, else None.

    A quote character ends the value only when what follows looks like the rest of a tag (`/>`, `>` or another `name=`), so
    apostrophes and even double quotes inside `quote="..."` survive."""
    j = start
    while True:
        hits = [k for k in (s.find(c, j) for c in closers) if k != -1]
        if not hits:
            return _INCOMPLETE
        k = min(hits)
        rest = s[k + 1:]
        if _AFTER_VALUE_RE.match(rest):
            return k
        if _PARTIAL_AFTER_RE.match(rest):
            return _INCOMPLETE
        j = k + 1


def _scan_tag(s: str, i: int):
    """Read the cite tag that starts at s[i] ("<cite").  Returns ("ok", end, attrs) | ("incomplete", 0, {}) | ("bad", 0, {})."""
    n = len(s)
    pos = i + 5
    attrs: dict[str, str] = {}
    while True:
        while pos < n and s[pos].isspace():
            pos += 1
        if pos >= n:
            return "incomplete", 0, attrs
        c = s[pos]
        if c == ">":
            return "ok", pos + 1, attrs
        if c == "/":
            if pos + 1 >= n:
                return "incomplete", 0, attrs
            if s[pos + 1] == ">":
                return "ok", pos + 2, attrs
            pos += 1
            continue
        if c == "<":
            return "bad", 0, attrs               # another tag starts: this one was never closed
        m = _NAME_RE.match(s, pos)
        if not m:
            pos += 1                              # stray character inside the tag
            continue
        name = m.group().lower()
        pos = m.end()
        while pos < n and s[pos].isspace():
            pos += 1
        if pos >= n:
            return "incomplete", 0, attrs
        if s[pos] != "=":
            attrs.setdefault(name, "")            # bare attribute
            continue
        pos += 1
        while pos < n and s[pos].isspace():
            pos += 1
        if pos >= n:
            return "incomplete", 0, attrs
        if s[pos] in _OPEN_TO_CLOSE:
            end = _value_end(s, pos + 1, _OPEN_TO_CLOSE[s[pos]])
            if end is _INCOMPLETE:
                return "incomplete", 0, attrs
            attrs[name] = s[pos + 1:end]
            pos = end + 1
        else:                                     # unquoted value: up to whitespace or the end of the tag
            m = _UNQUOTED_RE.match(s, pos)
            attrs[name] = m.group()
            pos = m.end()


@dataclass(frozen=True)
class RawCite:
    doc: Optional[str]
    page: int                 # as written by the model (first page of "12-13", "p.12" -> 12); >= 1
    quote: Optional[str]
    start: int                # offsets of the whole tag in the raw answer
    end: int


_ENTITIES = (("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"), ("&amp;", "&"))


def _raw_cite(attrs: dict[str, str], start: int, end: int) -> Optional[RawCite]:
    """A RawCite from parsed attributes, or None when the tag names no usable page."""
    m = re.search(r"\d+", attrs.get("page", ""))
    if not m or int(m.group()) < 1:
        return None
    quote = " ".join(attrs.get("quote", "").split())
    for ent, ch in _ENTITIES:
        quote = quote.replace(ent, ch)
    return RawCite(doc=attrs.get("doc", "").strip() or None, page=int(m.group()), quote=quote or None, start=start, end=end)


# --------------------------------------------------------------------------------------- streaming filter


class CiteStreamFilter:
    """Incremental `<cite/>` -> marker conversion for token streaming.

    feed()/flush() return events: ("text", str) | ("cite", RawCite, n) where n is the 1-based marker number (the caller shows
    MARKER.format(n=n)).  A half tag (or half closing tag) is held back until it is complete, so neither a tag nor a marker
    is ever emitted in pieces; flush() releases an unterminated "<cite" as plain text.  Tags without a usable page (and, when
    `page_count` is given, pages outside 1..page_count) are removed without a marker and counted in `dropped`; the whitespace
    they leave behind is collapsed.  The output does not depend on how the input is chunked."""

    def __init__(self, page_count: Optional[int] = None):
        self._page_count = page_count
        self._buf = ""
        self._base = 0              # offset in the whole raw input of self._buf[0]
        self._n = 0
        self._last = ""             # last character handed out
        self._swallow = False       # a tag was just removed: drop the spaces that follow if we already end in whitespace
        self.dropped = 0

    def feed(self, delta: str) -> list[tuple]:
        self._buf += delta
        return self._drain(final=False)

    def flush(self) -> list[tuple]:
        return self._drain(final=True)

    # -- internals
    def _text(self, out: list[tuple], s: str) -> None:
        if self._swallow:
            if self._last in ("", " ", "\t", "\n"):
                s = s.lstrip(" \t")
            if s:
                self._swallow = False
        if not s:
            return
        self._last = s[-1]
        if out and out[-1][0] == "text":
            out[-1] = ("text", out[-1][1] + s)
        else:
            out.append(("text", s))

    def _classify(self, pos: int, final: bool):
        """What starts at self._buf[pos] == "<": ("text",) | ("hold",) | ("close", end) | ("tag", end, attrs)."""
        buf = self._buf
        tail = buf[pos:pos + len(_CLOSE_TAG)].lower()
        if tail == _CLOSE_TAG:
            return ("close", pos + len(_CLOSE_TAG))
        if CITE_RE.match(buf, pos):
            status, end, attrs = _scan_tag(buf, pos)
            if status == "ok":
                return ("tag", end, attrs)
            if status == "incomplete" and not final and len(buf) - pos < _MAX_TAG_CHARS:
                return ("hold",)
            return ("text",)                                      # never closed: prose
        if not final and ("<cite".startswith(tail) or _CLOSE_TAG.startswith(tail)):
            return ("hold",)
        return ("text",)

    def _drain(self, final: bool) -> list[tuple]:
        out: list[tuple] = []
        buf, pos = self._buf, 0
        while pos < len(buf):
            lt = buf.find("<", pos)
            if lt == -1:
                self._text(out, buf[pos:])
                pos = len(buf)
                break
            if lt > pos:
                self._text(out, buf[pos:lt])
                pos = lt
            kind = self._classify(pos, final)
            if kind[0] == "hold":
                break
            if kind[0] == "text":
                self._text(out, "<")
                pos += 1
            elif kind[0] == "close":
                self._swallow = True
                pos = kind[1]
            else:
                raw = _raw_cite(kind[2], self._base + pos, self._base + kind[1])
                pos = kind[1]
                if raw is None or (self._page_count is not None and raw.page > self._page_count):
                    self.dropped += 1
                    self._swallow = True
                else:
                    self._n += 1
                    self._swallow = False
                    self._last = "]"
                    out.append(("cite", raw, self._n))
        self._base += pos
        self._buf = buf[pos:]
        return out


def parse_cites(raw: str) -> list[RawCite]:
    """Every well-formed cite tag of a raw answer, in order.  Attributes may come in any order, `quote` may contain
    apostrophes and double quotes, `page` may be "12", "12-13" or "p.12".  Tags without a usable page are skipped."""
    f = CiteStreamFilter()
    return [ev[1] for ev in f.feed(raw) + f.flush() if ev[0] == "cite"]


def strip_markers(text: str) -> str:
    """Remove [[cN]] markers (history sent back to the agent, text scored by RAGAS) and the space they leave behind."""
    text = re.sub(r"(?m)^([ \t]*)(?:\[\[c\d+\]\][ \t]*)+", r"\1", text)
    return re.sub(r"[ \t]*\[\[c\d+\]\]", "", text)


# --------------------------------------------------------------------------------------- claim extraction

_ABBREVIATIONS = frozenset("e.g i.e vs approx no nos inc ltd plc co corp st mr mrs ms dr prof fig figs est ca cf etc al".split())
_SENT_END_RE = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[A-Z0-9“\"'(\[£$€])")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")
_TAGS_RE = re.compile(r"<[^<>]{1,200}>")


def _plain_text(raw: str) -> str:
    """Raw answer text with every cite tag and marker removed."""
    f = CiteStreamFilter()
    return "".join(ev[1] for ev in f.feed(raw) + f.flush() if ev[0] == "text")


def _strip_markdown(s: str) -> str:
    s = _MARKER_RE.sub("", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = _TAGS_RE.sub(" ", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"^\s{0,3}(?:#{1,6}\s+|>\s*)", "", s)
    return " ".join(s.split())


def _last_sentence(text: str) -> str:
    start = 0
    for m in _SENT_END_RE.finditer(text):
        head = text[start:m.start() + 1].split()
        last = head[-1].rstrip(".!?\"')]”’").lower() if head else ""
        if last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
            continue                                              # "e.g. ", "No. 5", "J. Smith": not a sentence end
        start = m.end()
    return text[start:].strip()


def claim_for(raw_answer: str, cite_start: int) -> str:
    """The sentence, bullet or table row a cite supports: what stands immediately before the tag (markdown, markers and
    tags stripped).  "" when the tag opens the answer."""
    all_lines = _plain_text(raw_answer[:cite_start]).split("\n")
    lines = list(all_lines)
    while lines and not _strip_markdown(lines[-1]).strip(" |-:"):
        lines.pop()                                      # blank lines, table rules: the cite sits under the real text
    if not lines:
        return ""
    line = lines[-1]
    if line.lstrip().startswith("|"):                   # table row: all cells, also those after the tag
        rest = _plain_text(raw_answer[cite_start:]).split("\n", 1)[0] if len(lines) == len(all_lines) else ""
        cells = [_strip_markdown(c) for c in (line + rest).strip().strip("|").split("|")]
        claim = " ".join(c for c in cells if c)
    else:
        is_bullet = bool(_BULLET_RE.match(line))
        text = _strip_markdown(_BULLET_RE.sub("", line))
        claim = text if is_bullet and len(text) <= 350 else _last_sentence(text)
    if len(claim) > _MAX_CLAIM_CHARS:
        claim = claim[-_MAX_CLAIM_CHARS:].split(" ", 1)[-1]
    return claim


# --------------------------------------------------------------------------------------- tree path


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def tree_path_for_page(tree: list[dict], page: int) -> tuple[list[str], Optional[str], Optional[list[int]]]:
    """(breadcrumb of titles, node_id, [start, end]) of the node that best describes `page`.

    Neighbouring nodes share their boundary page (end_index of one == start_index of the next), so a page that OPENS a
    section prefers the deepest node starting on it; otherwise the deepest containing node wins, then the narrowest, then
    the one starting later.  A "<title> (intro)" node reports its parent's title once instead of twice."""
    best: Optional[tuple[tuple, list[str], dict]] = None
    stack: list[tuple[dict, list[str], int]] = [(n, [], 0) for n in reversed(tree or []) if isinstance(n, dict)]
    while stack:
        node, trail, depth = stack.pop()
        title = str(node.get("title") or "").strip()
        path = trail + [title] if title else trail
        for child in reversed(node.get("nodes") or []):
            if isinstance(child, dict):
                stack.append((child, path, depth + 1))
        lo, hi = _int(node.get("start_index")), _int(node.get("end_index"))
        if lo is None or hi is None or hi < lo or not lo <= page <= hi:
            continue
        key = (lo == page, depth, -(hi - lo), lo)
        if best is None or key > best[0]:
            best = (key, path, node)
    if best is None:
        return [], None, None
    _, path, node = best
    if len(path) >= 2 and path[-1].lower().endswith("(intro)") and path[-1][:-7].strip().lower() == path[-2].lower():
        path = path[:-1]
    node_id = node.get("node_id")
    return path, (str(node_id) if node_id not in (None, "") else None), [int(node["start_index"]), int(node["end_index"])]


# --------------------------------------------------------------------------------------- resolving


@dataclass
class CitationContext:
    doc_display_name: str               # shown in chips ("National Grid_Annual_Report.pdf")
    pdf: PdfiumDoc
    tree: list[dict]                    # PageIndex nodes: title,node_id,start_index,end_index,summary,nodes
    printed_labels: list[Optional[str]]
    read_pages: Optional[set[int]] = None   # pages the agent read (a cite to an unread page is flagged in stats)


@dataclass
class BuiltAnswer:
    text: str
    citations: list[Citation]
    sources: list[Source]
    stats: dict


def _find(raw: RawCite, claim: Optional[str], ctx: CitationContext) -> tuple[Optional[LocateResult], str]:
    cited = min(max(raw.page, 1), ctx.pdf.page_count)
    quote = raw.quote if raw.quote and len(squash(raw.quote)) >= MIN_QUOTE_CHARS else None
    area: Optional[LocateResult] = None
    if quote:
        found = locate_quote(ctx.pdf, cited, quote, neighbours=1)
        if found.method in _VERIFIED:
            return found, "model"
        if found.method == "block":
            area = found
    if claim:
        aligned = align_claim(ctx.pdf, cited, claim)
        if aligned is not None:
            return aligned, "aligned"
    return area, "none"


def resolve_cite(raw: RawCite, claim: Optional[str], ctx: CitationContext, n: int) -> Citation:
    """The per-cite pipeline: verify the model's quote on the page, else align the claim, else page-only; then fill folio,
    tree breadcrumb and geometry."""
    pdf = ctx.pdf
    cited = min(max(raw.page, 1), pdf.page_count)
    res, source = _find(raw, claim, ctx)
    page = res.page if res else cited
    path, node_id, node_range = tree_path_for_page(ctx.tree, page)
    width, height = (res.page_width, res.page_height) if res and res.page_width else pdf.page_sizes()[page - 1]
    labels = ctx.printed_labels
    return Citation(
        id=f"c{n}", index=n, doc_name=ctx.doc_display_name, page=page, cited_page=raw.page,
        printed_page=labels[page - 1] if 0 < page <= len(labels) else None,
        section_path=path, node_id=node_id, node_range=node_range,
        quote=res.matched_text if res and res.method != "page" else None, quote_source=source,
        match_method=res.method if res else "page", match_score=round(res.score, 4) if res else 0.0,
        rects=[Rect(**r) for r in res.rects] if res else [], page_width=round(width, 2), page_height=round(height, 2),
        claim=claim or None)


def _page_only(raw: RawCite, claim: Optional[str], ctx: CitationContext, n: int) -> Citation:
    page = min(max(raw.page, 1), ctx.pdf.page_count)
    path, node_id, node_range = tree_path_for_page(ctx.tree, page)
    labels = ctx.printed_labels
    return Citation(id=f"c{n}", index=n, doc_name=ctx.doc_display_name, page=page, cited_page=raw.page,
                    printed_page=labels[page - 1] if 0 < page <= len(labels) else None, section_path=path, node_id=node_id,
                    node_range=node_range, match_method="page", claim=claim or None)


def _sources(citations: list[Citation]) -> list[Source]:
    by_page: dict[int, Source] = {}
    for c in citations:
        src = by_page.get(c.page)
        if src is None:
            by_page[c.page] = Source(page=c.page, printed_page=c.printed_page, refs=1, citation_ids=[c.id], section_path=c.section_path)
        else:
            src.refs += 1
            src.citation_ids.append(c.id)
    return [by_page[p] for p in sorted(by_page)]


def build_answer(raw: str, ctx: CitationContext) -> BuiltAnswer:
    """Convert the model's raw answer: `<cite/>` tags -> [[cN]] markers numbered in answer order (cites with an unusable or
    out-of-range page are removed, numbering stays contiguous), every cite resolved, sources aggregated per page."""
    raw = strip_markers(raw or "")                           # a literal [[cN]] from the model would collide with ours
    f = CiteStreamFilter(page_count=ctx.pdf.page_count)
    events = f.feed(raw) + f.flush()
    parts: list[str] = []
    citations: list[Citation] = []
    for ev in events:
        if ev[0] == "text":
            parts.append(ev[1])
            continue
        _, rc, n = ev
        parts.append(MARKER.format(n=n))
        claim = claim_for(raw, rc.start)
        try:
            citations.append(resolve_cite(rc, claim, ctx, n))
        except Exception:  # noqa: BLE001 - a PDF hiccup must not cost the user the answer
            log.exception("could not resolve citation %d (page %d); falling back to page-only", n, rc.page)
            citations.append(_page_only(rc, claim, ctx, n))
    unread = 0 if ctx.read_pages is None else sum(1 for c in citations if c.cited_page not in ctx.read_pages)
    stats = {
        "n_cites": len(citations),
        "n_quote_verified": sum(c.quote_source == "model" for c in citations),
        "n_aligned": sum(c.quote_source == "aligned" for c in citations),
        "n_page_only": sum(c.quote_source == "none" for c in citations),
        "n_unread_pages": unread,
        "n_dropped": f.dropped,
    }
    return BuiltAnswer(text="".join(parts).strip(), citations=citations, sources=_sources(citations), stats=stats)
