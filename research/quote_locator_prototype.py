"""
quote_locator_prototype.py
==========================

Given a PDF page number + a (verbatim-ish) quote produced by an LLM, find WHERE on that page the quote
sits and return normalised (0..1) line-level rectangles that a browser can overlay on the rendered page.

Pipeline (backend independent core):

    PDF page --(backend)--> PageWords  [word, x0,y0,x1,y1 in *visible* top-left PDF points]
                                |
                      normalise + "squash"   (NFKC, casefold, keep only letters/digits, drop spaces)
                                |
        1. exact squashed substring match        score 1.00   method "exact"
        2. fuzzy window (rapidfuzz partial_ratio + Indel refinement)   method "fuzzy"
        3. ellipsis fragments ("... " / "[...]")  each fragment located separately   method "fragments"
        4. densest bag-of-tokens window (lines)   method "block"   (approximate area)
        5. nothing usable                         method "page"    (rects == [], highlight whole page)

    Each step is tried on the stream reading order first, then on a column-aware geometric order
    (stream order is NOT always reading order in multi-column annual reports).

Backends (all return PageWords in the same coordinate space):

    pymupdf_page_words(page)            PyMuPDF  (AGPL-3.0 / commercial)
    pdfium_page_words(pdf, index)       pypdfium2 (Apache-2.0 / BSD-3)   <- recommended for a licence-clean API
    pdfplumber_page_words(page)         pdfplumber / pdfminer.six (MIT)  (slow, ~0.4 s/page)

Coordinates: output rects are {x, y, w, h} fractions of the *visible* page (after /Rotate and CropBox),
origin top-left, so they can be applied as CSS percentages on top of any renderer (pdf.js canvas, PNG).

Only dependency of the core: rapidfuzz (MIT).  Backends import their libraries lazily.
"""
from __future__ import annotations

import bisect
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from rapidfuzz import fuzz
from rapidfuzz.distance import Indel

# --------------------------------------------------------------------------------------- data model


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float                      # visible space, origin top-left, PDF points
    block: int = 0                 # optional (backend specific)
    line: int = 0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def w(self) -> float:
        return self.x1 - self.x0


@dataclass
class PageWords:
    page: int                      # 1-based
    width: float                   # visible page size in points (after rotation / cropbox)
    height: float
    words: list[Word]


@dataclass
class LocateResult:
    page: int                      # page the passage was actually found on (1-based)
    hinted_page: int               # page the caller asked for
    method: str                    # exact | fuzzy | fragments | block | page
    score: float                   # 0..1 similarity between quote and matched text (see notes in docs)
    rects: list[dict]              # normalised {x,y,w,h}; [] when method == "page"
    matched_text: str              # the page words that were highlighted (for tooltips / debugging)
    quote: str
    n_matches: int = 1             # number of exact occurrences on the page (ambiguity indicator)
    order: str = "stream"          # which reading order produced the match: stream | columns
    page_width: float = 0.0
    page_height: float = 0.0
    notes: list[str] = field(default_factory=list)

    def boxes_1000(self) -> list[list[int]]:
        """Same rects as [x0, y0, x1, y1] on a 0-1000 top-left grid (PageIndex Cloud's bbox convention)."""
        return [[round(r["x"] * 1000), round(r["y"] * 1000), round((r["x"] + r["w"]) * 1000), round((r["y"] + r["h"]) * 1000)]
                for r in self.rects]

    def to_json(self) -> dict:
        return {
            "page": self.page, "hinted_page": self.hinted_page, "method": self.method,
            "score": round(self.score, 4), "rects": self.rects, "matched_text": self.matched_text,
            "boxes_1000": self.boxes_1000(),
            "quote": self.quote, "n_matches": self.n_matches, "order": self.order,
            "page_width": round(self.page_width, 2), "page_height": round(self.page_height, 2),
            "notes": self.notes,
        }


# --------------------------------------------------------------------------------------- normalisation

_ELLIPSIS_RE = re.compile(r"\s*(?:\[\s*(?:\.{2,}|…)\s*\]|\.{3,}|…)\s*")


def norm_token(s: str) -> str:
    """NFKC (expands ligatures, super/subscripts, full-width), casefold, keep letters+digits only.

    "(1,588)" -> "1588"   "eﬃcient" -> "efficient"   "Group’s" -> "groups"   "net-" -> "net"
    """
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if ch.isalnum())


def squash(text: str) -> str:
    return "".join(norm_token(t) for t in text.split())


class _Index:
    """Squashed page text + mapping squashed-char offsets -> word indices (in a given word order)."""

    def __init__(self, words: Sequence[Word]):
        self.words = list(words)
        self.tok: list[str] = []
        self.starts: list[int] = []
        self.kept: list[int] = []           # kept[k] -> index into self.words
        parts: list[str] = []
        pos = 0
        for i, w in enumerate(self.words):
            t = norm_token(w.text)
            if not t:
                continue
            self.tok.append(t)
            self.starts.append(pos)
            self.kept.append(i)
            parts.append(t)
            pos += len(t)
        self.S = "".join(parts)

    def k_at(self, pos: int) -> int:
        """kept-token index containing squashed offset pos."""
        return max(0, bisect.bisect_right(self.starts, pos) - 1)

    def span_to_words(self, a: int, b: int) -> list[int]:
        """squashed [a,b) -> list of word indices (inclusive range of kept tokens)."""
        if not self.S or b <= a:
            return []
        k0 = self.k_at(a)
        k1 = self.k_at(max(a, b - 1))
        return [self.kept[k] for k in range(k0, k1 + 1)]

    def aligned(self, a: int, b: int) -> bool:
        k0 = self.k_at(a)
        k1 = self.k_at(b - 1)
        return self.starts[k0] == a and self.starts[k1] + len(self.tok[k1]) == b


# --------------------------------------------------------------------------------------- reading orders


def column_order(words: Sequence[Word], page_w: float) -> list[Word]:
    """Geometric reading order: full-width bands top->bottom; inside a band columns left->right.

    1. cluster words into lines (y-centre proximity), 2. split lines at big gaps into segments,
    3. segments wider than 60% of the page are 'full width' and cut the page into bands,
    4. inside a band, segments are clustered into columns by their left edge.
    """
    ws = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    lines: list[list[Word]] = []
    for w in ws:
        yc = (w.y0 + w.y1) / 2
        for ln in reversed(lines[-6:]):
            lyc = sum((x.y0 + x.y1) / 2 for x in ln) / len(ln)
            lh = sum(x.h for x in ln) / len(ln)
            if abs(yc - lyc) <= 0.5 * min(lh, max(w.h, 1e-6)) + 0.5:
                ln.append(w)
                break
        else:
            lines.append([w])
    segs: list[list[Word]] = []
    for ln in lines:
        ln.sort(key=lambda w: w.x0)
        cur = [ln[0]]
        for w in ln[1:]:
            gap = w.x0 - cur[-1].x1
            if gap > max(2.2 * w.h, 0.03 * page_w):
                segs.append(cur)
                cur = [w]
            else:
                cur.append(w)
        segs.append(cur)

    def sx0(s): return s[0].x0
    def sx1(s): return s[-1].x1
    def sy(s): return sum((w.y0 + w.y1) / 2 for w in s) / len(s)

    segs.sort(key=sy)
    bands: list[list[list[Word]]] = [[]]
    for s in segs:
        if sx1(s) - sx0(s) > 0.6 * page_w:
            if bands[-1]:
                bands.append([])
            bands[-1].append(s)
            bands.append([])
        else:
            bands[-1].append(s)
    out: list[Word] = []
    for band in bands:
        if not band:
            continue
        if len(band) == 1 and sx1(band[0]) - sx0(band[0]) > 0.6 * page_w:
            out.extend(band[0])
            continue
        cols: list[dict] = []
        for s in sorted(band, key=sx0):
            for c in cols:
                if abs(sx0(s) - c["x0"]) <= 0.025 * page_w or (
                        min(sx1(s), c["x1"]) - max(sx0(s), c["x0"]) > 0.5 * (sx1(s) - sx0(s))):
                    c["segs"].append(s)
                    c["x1"] = max(c["x1"], sx1(s))
                    break
            else:
                cols.append({"x0": sx0(s), "x1": sx1(s), "segs": [s]})
        cols.sort(key=lambda c: c["x0"])
        for c in cols:
            for s in sorted(c["segs"], key=sy):
                out.extend(s)
    return out


# --------------------------------------------------------------------------------------- rect building


def _union(a: list[float], w: Word) -> list[float]:
    return [min(a[0], w.x0), min(a[1], w.y0), max(a[2], w.x1), max(a[3], w.y1)]


def _mergeable(cur: list[float], w: Word, vertical: bool, page_w: float, page_h: float) -> bool:
    """Can word `w` (next in reading order) be merged into the running line rect `cur`?

    horizontal text: strong vertical overlap + forward gap < 50% page width (whole table rows become ONE band;
                     gutter between two columns is harmless because the words are consecutive in the match).
    vertical text  (unrotated pages whose words are taller than wide): same logic on the other axis.
    Orientation is decided once per group from the first word so that stacked horizontal lines never merge."""
    if not vertical:
        vo = min(cur[3], w.y1) - max(cur[1], w.y0)
        return vo >= 0.55 * min(cur[3] - cur[1], w.h) and -3 <= (w.x0 - cur[2]) < 0.5 * page_w and w.x1 > cur[0]
    ho = min(cur[2], w.x1) - max(cur[0], w.x0)
    return ho >= 0.55 * min(cur[2] - cur[0], w.w) and -3 <= (w.y0 - cur[3]) < 0.5 * page_h and w.y1 > cur[1]


def _is_vertical(words: Sequence[Word]) -> bool:
    """Majority vote: words (>=3 chars) that are much taller than wide => physically rotated text."""
    votes = [w.h > 1.6 * w.w for w in words if len(w.text) >= 3]
    return bool(votes) and sum(votes) > len(votes) / 2


def rects_for_words(words: Sequence[Word], page_w: float, page_h: float, pad: float = 1.0) -> list[dict]:
    """Merge consecutive word boxes into line rects and normalise to 0..1 of the visible page."""
    return rects_for_groups([words], page_w, page_h, pad)


def rects_for_groups(groups: Sequence[Sequence[Word]], page_w: float, page_h: float, pad: float = 1.0) -> list[dict]:
    """Like rects_for_words, but every group (e.g. one ellipsis fragment) is merged on its own, so rects never
    bridge the un-quoted gap between two fragments."""
    merged: list[list[float]] = []
    for words in groups:
        vertical = _is_vertical(words)
        cur: Optional[list[float]] = None
        for w in words:
            if cur is not None and _mergeable(cur, w, vertical, page_w, page_h):
                cur = _union(cur, w)
            else:
                if cur is not None:
                    merged.append(cur)
                cur = [w.x0, w.y0, w.x1, w.y1]
        if cur is not None:
            merged.append(cur)
    out = []
    for x0, y0, x1, y1 in merged:
        x0, y0, x1, y1 = max(0.0, x0 - pad), max(0.0, y0 - pad), min(page_w, x1 + pad), min(page_h, y1 + pad)
        out.append({"x": round(x0 / page_w, 5), "y": round(y0 / page_h, 5),
                    "w": round((x1 - x0) / page_w, 5), "h": round((y1 - y0) / page_h, 5)})
    return out


# --------------------------------------------------------------------------------------- matching core

FUZZY_MIN = 0.70            # accept fuzzy window as "fuzzy" at/above this score (cov*sqrt(prec)); see threshold experiment
BLOCK_MIN_COVERAGE = 0.55   # accept density window as "block" at/above this weighted token coverage
MIN_QUOTE_CHARS = 4         # shorter squashed quotes are too ambiguous


@dataclass
class _Cand:
    method: str
    score: float
    word_idx: list[int]
    n_matches: int = 1
    notes: list[str] = field(default_factory=list)
    groups: Optional[list[list[int]]] = None      # fragment groups (quote order); None => single group


def _exact(idx: _Index, q: str) -> Optional[_Cand]:
    if not q or q not in idx.S:
        return None
    occ, i = [], idx.S.find(q)
    while i != -1 and len(occ) < 200:
        occ.append(i)
        i = idx.S.find(q, i + 1)
    aligned = [o for o in occ if idx.aligned(o, o + len(q))]
    chosen = (aligned or occ)[0]
    c = _Cand("exact", 1.0 if aligned else 0.97, idx.span_to_words(chosen, chosen + len(q)), n_matches=len(aligned or occ))
    if not aligned:
        c.notes.append("match starts/ends inside a word")
    return c


def _fuzzy(idx: _Index, q: str) -> Optional[_Cand]:
    """Approximate match of q inside the squashed page text.

    1. rapidfuzz partial_ratio_alignment -> rough location (very fast, ~0.3 ms for 9k chars)
    2. snap to token boundaries, then coordinate ascent over (start token, end token) maximising
           score(a, b) = coverage * sqrt(precision)
       with  LCS = Indel-LCS(quote, page[a:b]);  coverage = LCS/len(quote);  precision = LCS/len(span)
       (Indel.distance is bit-parallel => a pass over ~100 candidate boundaries costs a few ms)."""
    S, n = idx.S, len(q)
    if n < 12 or len(S) < 12 or n > len(S):
        return None
    al = fuzz.partial_ratio_alignment(q, S)
    if al is None or al.score < 55:
        return None
    lo_k = idx.k_at(max(0, al.dest_start - n))
    hi_k = idx.k_at(min(len(S) - 1, al.dest_end + n))
    starts = idx.starts[lo_k:hi_k + 1]
    ends = [idx.starts[k] + len(idx.tok[k]) for k in range(lo_k, hi_k + 1)]

    def sc(a: int, b: int) -> float:
        if b <= a:
            return 0.0
        lcs = (n + (b - a) - Indel.distance(q, S[a:b])) // 2
        return (lcs / n) * (lcs / (b - a)) ** 0.5

    a = idx.starts[idx.k_at(al.dest_start)]
    kb = idx.k_at(max(al.dest_start, al.dest_end - 1))
    b = idx.starts[kb] + len(idx.tok[kb])
    best = sc(a, b)
    for _ in range(4):
        improved = False
        for e in ends:
            if e > a:
                v = sc(a, e)
                if v > best + 1e-9:
                    best, b, improved = v, e, True
        for st in starts:
            if st < b:
                v = sc(st, b)
                if v > best + 1e-9:
                    best, a, improved = v, st, True
        if not improved:
            break
    if best < 0.3:
        return None
    return _Cand("fuzzy", best, idx.span_to_words(a, b))


_STOP = {"the", "of", "and", "to", "in", "a", "for", "on", "by", "is", "was", "at", "as", "with", "from", "that", "an", "or", "be", "are", "this", "it"}


def _density(idx: _Index, quote_tokens: list[str]) -> Optional[_Cand]:
    """Densest window of page tokens w.r.t. quote tokens (weighted by token length / digits)."""
    qset = {t for t in quote_tokens if t not in _STOP and len(t) > 1}
    if not qset or not idx.tok:
        return None
    wt = {t: min(len(t), 8) + (3 if any(c.isdigit() for c in t) else 0) for t in qset}
    total = sum(wt.values())
    W = max(8, int(1.5 * len(quote_tokens)))
    cnt: dict[str, int] = {}
    cur = best = 0.0
    best_range = (0, 0)
    toks = idx.tok
    for r, t in enumerate(toks):
        if t in qset:
            cnt[t] = cnt.get(t, 0) + 1
            if cnt[t] == 1:
                cur += wt[t]
        l = r - W
        if l >= 0 and toks[l] in qset:
            cnt[toks[l]] -= 1
            if cnt[toks[l]] == 0:
                cur -= wt[toks[l]]
        if cur > best:
            best, best_range = cur, (max(0, r - W + 1), r)
    if best <= 0:
        return None
    lo, hi = best_range
    while lo < hi and toks[lo] not in qset:
        lo += 1
    while hi > lo and toks[hi] not in qset:
        hi -= 1
    return _Cand("block", 0.5 * best / total, [idx.kept[k] for k in range(lo, hi + 1)])   # score capped at 0.5: area only


def _expand_to_lines(idx: _Index, cand: _Cand) -> _Cand:
    """Extend a density window to the whole text lines it touches (so it reads as an 'area')."""
    words = idx.words
    sel = set(cand.word_idx)
    lo, hi = min(sel), max(sel)
    def same_line(a: Word, b: Word) -> bool:
        return min(a.y1, b.y1) - max(a.y0, b.y0) >= 0.5 * min(a.h, b.h)
    # walk outwards while neighbouring words (in order) are on the same line as the edge words
    while lo > 0 and same_line(words[lo - 1], words[lo]) and words[lo - 1].x1 <= words[lo].x0 + 1:
        lo -= 1
    while hi < len(words) - 1 and same_line(words[hi + 1], words[hi]) and words[hi + 1].x0 >= words[hi].x1 - 1:
        hi += 1
    cand.word_idx = list(range(lo, hi + 1))
    return cand


def _locate_core(idx: _Index, quote: str) -> Optional[_Cand]:
    q = squash(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return None
    c = _exact(idx, q)
    if c:
        return c
    c = _fuzzy(idx, q)
    if c and c.score >= FUZZY_MIN:
        return c
    # ellipsis / multi fragment quotes
    frags = [f for f in _ELLIPSIS_RE.split(quote) if len(squash(f)) >= 8]
    if len(frags) >= 2:
        parts, total, acc = [], 0, 0.0
        for f in frags:
            fq = squash(f)
            fc = _exact(idx, fq) or _fuzzy(idx, fq)
            total += len(fq)
            if fc and fc.score >= FUZZY_MIN:
                parts.append(fc)
                acc += fc.score * len(fq)
        if parts:
            groups = [p.word_idx for p in parts]
            res = _Cand("fragments", acc / total, [i for g in groups for i in g], groups=groups)
            res.notes.append(f"{len(parts)}/{len(frags)} fragments located")
            if res.score >= 0.5:
                return res
    d = _density(idx, [norm_token(t) for t in quote.split() if norm_token(t)])
    if d and d.score >= 0.5 * BLOCK_MIN_COVERAGE:
        d = _expand_to_lines(idx, d)
        if c and c.score > 0.0:
            d.notes.append(f"best fuzzy window scored {c.score:.2f}")
        return d
    if c:  # weak fuzzy: keep as 'block' only if density agreed enough, else nothing
        return None
    return None


_RANK = {"exact": 4, "fuzzy": 3, "fragments": 2, "block": 1, "page": 0}


def locate_in_page(pw: PageWords, quote: str, *, hinted_page: Optional[int] = None) -> LocateResult:
    """Locate `quote` on one page. Tries stream order, then column order (only if stream is not exact)."""
    hinted = hinted_page or pw.page
    best: Optional[tuple[_Cand, str, _Index]] = None
    orders: list[tuple[str, Callable[[], list[Word]]]] = [
        ("stream", lambda: pw.words),
        ("columns", lambda: column_order(pw.words, pw.width)),
    ]
    for name, getter in orders:
        idx = _Index(getter())
        cand = _locate_core(idx, quote)
        if cand and (best is None or (_RANK[cand.method], cand.score) > (_RANK[best[0].method], best[0].score)):
            best = (cand, name, idx)
        if best and best[0].method == "exact":
            break
    if best is None:
        return LocateResult(page=pw.page, hinted_page=hinted, method="page", score=0.0, rects=[], matched_text="",
                            quote=quote, n_matches=0, page_width=pw.width, page_height=pw.height,
                            notes=["no usable match; highlight whole page"])
    cand, order, idx = best
    groups = [[idx.words[i] for i in g] for g in (cand.groups or [cand.word_idx])]
    return LocateResult(
        page=pw.page, hinted_page=hinted, method=cand.method, score=cand.score,
        rects=rects_for_groups(groups, pw.width, pw.height),
        matched_text=" ... ".join(" ".join(w.text for w in g) for g in groups),
        quote=quote, n_matches=cand.n_matches, order=order, page_width=pw.width, page_height=pw.height,
        notes=cand.notes)


def locate(get_page_words: Callable[[int], PageWords], page_count: int, page: int, quote: str, *,
           radius: int = 1, doc_squash: Optional[Callable[[], Sequence[str]]] = None) -> LocateResult:
    """Locate with page-number slack (printed vs physical page / off-by-one) and optional whole-doc fallback.

    get_page_words(page_no_1based) -> PageWords
    doc_squash() -> list of per-page squashed strings (pre-computed at index time) enabling an instant
                    whole-document exact search when the neighbourhood fails.
    """
    page = min(max(1, page), page_count)
    first = locate_in_page(get_page_words(page), quote, hinted_page=page)
    if first.method in ("exact", "fuzzy", "fragments") or radius <= 0 and doc_squash is None:
        return first
    best = first
    for d in range(1, radius + 1):
        for p in (page - d, page + d):
            if 1 <= p <= page_count:
                r = locate_in_page(get_page_words(p), quote, hinted_page=page)
                if (_RANK[r.method], r.score) > (_RANK[best.method], best.score):
                    best = r
                    best.notes.append(f"page corrected from {page} to {p}")
    if best.method in ("exact", "fuzzy", "fragments") or doc_squash is None:
        return best
    q = squash(quote)
    if len(q) >= 12:
        pages = doc_squash()
        hits = [i + 1 for i, s in enumerate(pages) if q in s]
        if hits:
            p = min(hits, key=lambda h: abs(h - page))
            r = locate_in_page(get_page_words(p), quote, hinted_page=page)
            if (_RANK[r.method], r.score) > (_RANK[best.method], best.score):
                r.notes.append(f"page corrected from {page} to {p} via whole-document search ({len(hits)} hit pages)")
                best = r
    return best


# --------------------------------------------------------------------------------------- backends


def pymupdf_page_words(page) -> PageWords:
    """PyMuPDF backend.  Raw coordinates are in the *unrotated*, cropbox-relative space; multiply by
    page.rotation_matrix to get visible space (verified on a rotated + cropped test page)."""
    import pymupdf
    rm = page.rotation_matrix
    r = page.rect                               # visible (rotated + cropped) rect, origin (0,0)
    out: list[Word] = []
    for x0, y0, x1, y1, text, b, l, _w in page.get_text("words"):
        rr = pymupdf.Rect(x0, y0, x1, y1) * rm
        out.append(Word(text, rr.x0, rr.y0, rr.x1, rr.y1, b, l))
    return PageWords(page.number + 1, r.width, r.height, out)


_WS = set(" \t\r\n ￾​")


def pdfium_page_words(pdf, index: int) -> PageWords:
    """pypdfium2 backend (Apache-2.0/BSD-3).  Builds words from per-char boxes (loose boxes give uniform
    line heights) and converts PDF user space (bottom-left) -> visible top-left space by hand, honouring
    /Rotate and CropBox.  `pdf` is a pypdfium2.PdfDocument; `index` is 0-based."""
    page = pdf[index]
    try:
        rot = page.get_rotation() % 360
        cl, cb, cr, ct = page.get_cropbox()
        mb = page.get_mediabox()
        # clip crop box to media box (PDFium does the same when rendering)
        cl, cb, cr, ct = max(cl, mb[0]), max(cb, mb[1]), min(cr, mb[2]), min(ct, mb[3])
        if rot in (0, 180):
            vw, vh = cr - cl, ct - cb
        else:
            vw, vh = ct - cb, cr - cl

        def to_vis(px: float, py: float) -> tuple[float, float]:
            if rot == 0:
                return px - cl, ct - py
            if rot == 90:
                return py - cb, px - cl
            if rot == 180:
                return cr - px, py - cb
            return ct - py, cr - px            # 270

        tp = page.get_textpage()
        try:
            n = tp.count_chars()
            text = tp.get_text_range()
            words: list[Word] = []
            cur: list[tuple[str, tuple[float, float, float, float]]] = []

            def flush():
                if cur:
                    xs0 = min(b[0] for _, b in cur); ys0 = min(b[1] for _, b in cur)
                    xs1 = max(b[2] for _, b in cur); ys1 = max(b[3] for _, b in cur)
                    words.append(Word("".join(c for c, _ in cur), xs0, ys0, xs1, ys1))
                    cur.clear()

            for k in range(min(n, len(text))):
                ch = text[k]
                if ch in _WS:
                    flush()
                    continue
                l, b, r, t = tp.get_charbox(k, loose=True)
                (x0, y0), (x1, y1) = to_vis(l, t), to_vis(r, b)
                box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                if cur:
                    pb = cur[-1][1]
                    ph = max(pb[3] - pb[1], 1e-6); pw_ = max(pb[2] - pb[0], 1e-6)
                    vflow = abs((box[1] + box[3]) / 2 - (pb[1] + pb[3]) / 2) > 0.6 * ph   # new line
                    back = box[0] < pb[0] - 0.5 * pw_ - 0.5 * ph and not vflow
                    if (vflow and abs((box[0] + box[2]) / 2 - (pb[0] + pb[2]) / 2) > 0.6 * pw_) or back:
                        flush()
                cur.append((ch, box))
            flush()
        finally:
            tp.close()
    finally:
        page.close()
    words = [w for w in words if 0 <= (w.x0 + w.x1) / 2 <= vw and 0 <= (w.y0 + w.y1) / 2 <= vh]   # drop text outside CropBox
    return PageWords(index + 1, vw, vh, words)


def pdfplumber_page_words(page) -> PageWords:
    """pdfplumber backend (MIT).  Verified gotchas:
      * use_text_flow=True is REQUIRED for multi-column pages (default re-sorts word-by-word line-by-line and
        interleaves the columns), BUT on pages whose text is physically rotated it splits into single characters
        -> we detect that (mean word length < 1.6) and retry with use_text_flow=False.
      * page.bbox ignores the CropBox; we subtract the (rotated) crop offset ourselves and drop words outside it.
      * ~0.4 s / dense page (30x slower than pdfium / mupdf)."""
    ws = page.extract_words(use_text_flow=True, keep_blank_chars=False)
    if len(ws) > 30 and sum(len(w["text"]) for w in ws) / len(ws) < 1.6:
        ws = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    cb = page.cropbox            # (x0, y0_bottomleft, x1, y1_bottomleft) in rotated-mediabox space
    ox, oy = cb[0], page.height - cb[3]
    vw, vh = cb[2] - cb[0], cb[3] - cb[1]
    out = []
    for w in ws:
        x0, y0, x1, y1 = w["x0"] - ox, w["top"] - oy, w["x1"] - ox, w["bottom"] - oy
        if 0 <= (x0 + x1) / 2 <= vw and 0 <= (y0 + y1) / 2 <= vh:
            out.append(Word(w["text"], x0, y0, x1, y1))
    return PageWords(page.page_number, vw, vh, out)


# PDFium is NOT thread-safe - not even across different documents (pypdfium2 README, "Incompatibility with Threading").
# FastAPI runs sync endpoints in a thread pool, so every PDFium call in the process must go through this one lock
# (or be pushed to a single-worker executor).  The lock is re-entrant so helpers can nest.
PDFIUM_LOCK = threading.RLock()


class PdfiumDoc:
    """Convenience wrapper: keeps a PdfDocument open from BYTES (no Windows file lock, see notes) with a small
    page-words cache.  All PDFium access is serialised through PDFIUM_LOCK."""

    def __init__(self, data: bytes, cache: int = 32):
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:
            self._pdf = pdfium.PdfDocument(data)
            self.page_count = len(self._pdf)
        self._cache: dict[int, PageWords] = {}
        self._max = cache
        self._squashed: Optional[list[str]] = None

    def page_words(self, page_no: int) -> PageWords:
        with PDFIUM_LOCK:
            if page_no not in self._cache:
                if len(self._cache) >= self._max:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[page_no] = pdfium_page_words(self._pdf, page_no - 1)
            return self._cache[page_no]

    def page_sizes(self) -> list[tuple[float, float]]:
        """Visible (rotated + cropped) page sizes in points - lets the browser lay out placeholders instantly."""
        out = []
        with PDFIUM_LOCK:
            for i in range(self.page_count):
                p = self._pdf[i]
                out.append(p.get_size())
                p.close()
        return out

    def squashed_pages(self) -> list[str]:
        """Per-page squashed text for whole-document exact search (~10 ms/page, memoised; call it at index time)."""
        if self._squashed is not None:
            return self._squashed
        out = []
        with PDFIUM_LOCK:
            for i in range(self.page_count):
                page = self._pdf[i]
                tp = page.get_textpage()
                out.append(squash(tp.get_text_range()))
                tp.close()
                page.close()
        self._squashed = out
        return out

    def close(self):
        with PDFIUM_LOCK:
            self._pdf.close()
