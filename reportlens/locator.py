"""Where on the page is this quote / claim?  pdfium word boxes + rapidfuzz -> highlight rectangles.

Pipeline for a quote (productionised from research/quote_locator_prototype.py):

    page words (visible space) --squash--> exact -> fuzzy -> ellipsis fragments -> densest block -> page only
    each tried in PDF stream order, then in a column-aware geometric order (stream order is not reading order in
    multi-column annual reports); +-N neighbour pages and a whole-document exact search rescue wrong page numbers.

Numeric guard: a quote whose numbers are not all present in the matched span is rejected, however similar the text is
("(1,588)" must never highlight "(1,688)", and "588 million" must not highlight "1,588 million").

Rects are fractions (0..1) of the visible page (after /Rotate and CropBox), origin top-left: the browser applies them as
CSS percentages over any renderer.
"""
from __future__ import annotations

import bisect
import logging
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from rapidfuzz import fuzz
from rapidfuzz.distance import Indel

from .pdfutil import PDFIUM_LOCK, SOFT_HYPHENS, open_document, page_chars, page_geometry, read_pdf_bytes

log = logging.getLogger("reportlens.locator")

# --------------------------------------------------------------------------------------- tunables

FUZZY_MIN = 0.78            # fuzzy window accepted from here (score = coverage * sqrt(precision)); >= 0.90 is a clean match
FRAGMENT_MIN = 0.6          # share of an ellipsis quote's characters that must be located
BLOCK_MIN_COVERAGE = 0.55   # weighted token coverage for the "block" (area only) fallback
BLOCK_MAX_SCORE = 0.5       # a block is an area, never a match
MIN_QUOTE_CHARS = 4         # shorter squashed quotes are too ambiguous
DOC_SEARCH_MIN_CHARS = 24   # whole-document exact search only for quotes this distinctive
DOC_SEARCH_COLD_MAX_PAGES = 150   # ... and only when it is cheap (<~1.5 s to squash the document) or already squashed
ALIGN_MIN_COVERAGE = 0.6    # claim alignment: weighted share of the claim's tokens found in one window
ALIGN_MIN_WEIGHT = 8.0      # ... and at least this much absolute weight (a single number + a word is enough, a lone word is not)
DEFAULT_CACHE_PAGES = 48    # word lists kept per document (LRU)

# --------------------------------------------------------------------------------------- normalisation (shared with citations.py)

_ELLIPSIS_RE = re.compile(r"\s*(?:\[\s*(?:\.{2,}|…)\s*\]|\.{3,}|…)\s*")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")


def norm_token(s: str) -> str:
    """NFKC (expands ligatures, super/subscripts, full-width), casefold, keep letters and digits only.

    "(1,588)" -> "1588"   "eﬃcient" -> "efficient"   "Group’s" -> "groups"   "net-" -> "net"
    """
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if ch.isalnum())


def squash(text: str) -> str:
    """Whitespace-, punctuation- and case-free form of a text, for matching quotes against page text."""
    return "".join(norm_token(t) for t in text.split())


def numbers_in(text: str) -> list[str]:
    """Numbers of a text in comparable form: thousands commas dropped, decimals kept ("£6,991m" -> "6991", "3.5%" -> "3.5")."""
    text = unicodedata.normalize("NFKC", text)
    return [m.replace(",", "") for m in _NUMBER_RE.findall(text)]


# --------------------------------------------------------------------------------------- data model


@dataclass(slots=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float                      # visible space, origin top-left, PDF points
    glue: bool = False             # no space before this word (second half of a hyphen-joined word)

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
    score: float                   # 0..1: exact 1.0, fuzzy coverage*sqrt(precision), block <= 0.5, aligned = token coverage
    rects: list[dict]              # normalised {x,y,w,h}; [] when method == "page"
    matched_text: str              # the page words that were highlighted
    quote: str = ""
    n_matches: int = 1             # exact occurrences on the page (ambiguity indicator)
    order: str = "stream"          # reading order that produced the match: stream | columns | lines
    page_width: float = 0.0
    page_height: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------- word extraction

_WORD_BREAKS = frozenset(" \t\r\n\x0b\x0c\u00a0\u2007\u2009\u200b\u202f\u3000")

_Box = tuple[float, float, float, float]
_Run = list[tuple[str, _Box]]


def _geometric_split(run: _Run) -> list[_Run]:
    """Cut a whitespace-free run of chars where the boxes jump: onto another line, or back to the left on the same line
    (two text objects PDFium did not separate with a space).  Vertical text continues, it does not jump."""
    out: list[_Run] = []
    cur: _Run = []
    for ch, box in run:
        if cur:
            pb = cur[-1][1]
            ph, pw = max(pb[3] - pb[1], 1e-6), max(pb[2] - pb[0], 1e-6)
            new_line = abs((box[1] + box[3]) / 2 - (pb[1] + pb[3]) / 2) > 0.6 * ph
            moved_back = not new_line and box[0] < pb[0] - 0.5 * pw - 0.5 * ph
            if (new_line and abs((box[0] + box[2]) / 2 - (pb[0] + pb[2]) / 2) > 0.6 * pw) or moved_back:
                out.append(cur)
                cur = []
        cur.append((ch, box))
    if cur:
        out.append(cur)
    return out


def _build_words(runs: list[tuple[bool, _Run]], geometric: bool) -> list[Word]:
    words: list[Word] = []
    for glue, run in runs:
        for k, part in enumerate(_geometric_split(run) if geometric else [run]):
            words.append(Word("".join(c for c, _ in part), min(b[0] for _, b in part), min(b[1] for _, b in part),
                              max(b[2] for _, b in part), max(b[3] for _, b in part), glue and k == 0))
    return words


def _extract_words(pdf, index: int) -> PageWords:
    """Words with visible-space boxes for one page.  Caller holds PDFIUM_LOCK.  Words are built from loose char boxes
    (uniform line heights) and converted to the visible, top-left space by hand, honouring /Rotate and CropBox."""
    page = pdf[index]
    try:
        geo = page_geometry(page)
        tp = page.get_textpage()
        try:
            text = page_chars(tp)
            runs: list[tuple[bool, _Run]] = []        # (glued to the previous word, chars) split at whitespace / soft hyphens
            cur: _Run = []
            glue = False
            get_box = tp.get_charbox
            for k, ch in enumerate(text):
                if ch in SOFT_HYPHENS or ch in _WORD_BREAKS or ch < " ":
                    if cur:
                        runs.append((glue, cur))
                        cur = []
                    glue = ch in SOFT_HYPHENS
                    continue
                cur.append((ch, geo.rect_to_visible(*get_box(k, loose=True))))
            if cur:
                runs.append((glue, cur))
        finally:
            tp.close()
    finally:
        page.close()
    words = _build_words(runs, geometric=True)
    if len(words) > 30 and sum(len(w.text) for w in words) / len(words) < 1.6:
        # text that reads right-to-left or bottom-to-top on the visible page (/Rotate 180 or 270 over upright content) makes
        # every glyph look like a jump: trust the whitespace PDFium reported instead
        words = _build_words(runs, geometric=False)
    vw, vh = geo.width, geo.height
    words = [w for w in words if 0 <= (w.x0 + w.x1) / 2 <= vw and 0 <= (w.y0 + w.y1) / 2 <= vh]   # drop text outside the CropBox
    return PageWords(index + 1, vw, vh, words)


# --------------------------------------------------------------------------------------- reading orders


def _cluster_lines(words: Sequence[Word]) -> list[list[Word]]:
    """Group words into text lines by y-centre proximity (each line sorted left to right)."""
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
    for ln in lines:
        ln.sort(key=lambda w: w.x0)
    return lines


def line_order(words: Sequence[Word]) -> list[Word]:
    """Top-to-bottom, left-to-right: keeps a table row together whatever order the PDF drew its cells in."""
    return [w for ln in sorted(_cluster_lines(words), key=lambda ln: sum((w.y0 + w.y1) / 2 for w in ln) / len(ln)) for w in ln]


def column_order(words: Sequence[Word], page_w: float, page_h: float) -> list[Word]:
    """Geometric reading order: running header, then the body, then the running footer.  In the body, full-width bands
    run top->bottom and inside a band columns run left->right.

    Lines are split into segments at big gaps; segments wider than 60 % of the page are full-width bands that cut the
    page; inside a band, segments are clustered into columns by their left edge.  Header/footer zones are read apart so a
    page number never interrupts a sentence that flows from one column to the next."""
    segs: list[list[Word]] = []
    for ln in _cluster_lines(words):
        cur = [ln[0]]
        for w in ln[1:]:
            if w.x0 - cur[-1].x1 > max(1.6 * w.h, 0.025 * page_w):
                segs.append(cur)
                cur = [w]
            else:
                cur.append(w)
        segs.append(cur)
    head = [s for s in segs if _seg_y(s) < _HEADER_ZONE * page_h]
    foot = [s for s in segs if _seg_y(s) > _FOOTER_ZONE * page_h]
    body = [s for s in segs if _HEADER_ZONE * page_h <= _seg_y(s) <= _FOOTER_ZONE * page_h]
    return _read_bands(head, page_w) + _read_bands(body, page_w) + _read_bands(foot, page_w)


_HEADER_ZONE, _FOOTER_ZONE = 0.06, 0.93      # share of the page height


def _seg_y(s: list[Word]) -> float:
    return sum((w.y0 + w.y1) / 2 for w in s) / len(s)


def _read_bands(segs: list[list[Word]], page_w: float) -> list[Word]:
    def sx0(s): return s[0].x0
    def sx1(s): return s[-1].x1

    segs = sorted(segs, key=_seg_y)
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
            for s in sorted(c["segs"], key=_seg_y):
                out.extend(s)
    return out


class _Index:
    """Squashed page text of the words in one reading order + the map from squashed offsets back to word indices."""

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
        self._claim_view: Optional[tuple[list[str], list[int]]] = None

    def claim_view(self) -> tuple[list[str], list[int]]:
        """(tokens, word index of each token) in this order, for claim alignment; built once."""
        if self._claim_view is None:
            toks: list[str] = []
            owner: list[int] = []
            for wi, w in enumerate(self.words):
                for t in _claim_tokens(w.text):
                    toks.append(t)
                    owner.append(wi)
            self._claim_view = (toks, owner)
        return self._claim_view

    def k_at(self, pos: int) -> int:
        """Index of the kept token containing squashed offset `pos`."""
        return max(0, bisect.bisect_right(self.starts, pos) - 1)

    def span_to_words(self, a: int, b: int) -> list[int]:
        """Word indices covered by the squashed span [a, b)."""
        if not self.S or b <= a:
            return []
        # every word between the first and last token, so punctuation-only words ("—") stay in the highlight and the text
        return list(range(self.kept[self.k_at(a)], self.kept[self.k_at(max(a, b - 1))] + 1))

    def aligned(self, a: int, b: int) -> bool:
        k0, k1 = self.k_at(a), self.k_at(b - 1)
        return self.starts[k0] == a and self.starts[k1] + len(self.tok[k1]) == b


class _PageEntry:
    """Words of one page plus the reading-order indexes built from them on demand."""

    def __init__(self, pw: PageWords):
        self.pw = pw
        self._indexes: dict[str, _Index] = {}

    def index(self, order: str) -> _Index:
        idx = self._indexes.get(order)
        if idx is None:
            words = self.pw.words
            if order == "columns":
                words = column_order(words, self.pw.width, self.pw.height)
            elif order == "lines":
                words = line_order(words)
            idx = self._indexes[order] = _Index(words)
        return idx


# --------------------------------------------------------------------------------------- rect building


def _is_vertical(words: Sequence[Word]) -> bool:
    """Majority vote: words (>=3 chars) much taller than wide => physically rotated text."""
    votes = [w.h > 1.6 * w.w for w in words if len(w.text) >= 3]
    return bool(votes) and sum(votes) > len(votes) / 2


def _mergeable(cur: list[float], w: Word, vertical: bool, page_w: float, page_h: float) -> bool:
    """Can word `w` (next in reading order) join the running line rect `cur`?  Horizontal text: strong vertical overlap and
    a gap below half the page width, in either direction (a whole table row becomes one band; text that reads right-to-left
    after /Rotate 180 still merges).  Vertical text: the same on the other axis."""
    if not vertical:
        vo = min(cur[3], w.y1) - max(cur[1], w.y0)
        return vo >= 0.55 * min(cur[3] - cur[1], w.h) and _close(w.x0 - cur[2], cur[0] - w.x1, 0.5 * page_w)
    ho = min(cur[2], w.x1) - max(cur[0], w.x0)
    return ho >= 0.55 * min(cur[2] - cur[0], w.w) and _close(w.y0 - cur[3], cur[1] - w.y1, 0.5 * page_h)


def _close(gap_after: float, gap_before: float, limit: float) -> bool:
    return -3 <= gap_after < limit or -3 <= gap_before < limit


def rects_for_groups(groups: Sequence[Sequence[Word]], page_w: float, page_h: float, pad: float = 1.0) -> list[dict]:
    """Merge consecutive word boxes into one rect per text line and normalise to 0..1 of the visible page.  Every group
    (e.g. one ellipsis fragment) merges on its own, so a rect never bridges the un-quoted gap between two fragments."""
    merged: list[list[float]] = []
    for words in groups:
        vertical = _is_vertical(words)
        cur: Optional[list[float]] = None
        for w in words:
            if cur is not None and _mergeable(cur, w, vertical, page_w, page_h):
                cur = [min(cur[0], w.x0), min(cur[1], w.y0), max(cur[2], w.x1), max(cur[3], w.y1)]
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


def _join_words(words: Sequence[Word]) -> str:
    out: list[str] = []
    for w in words:
        out.append(w.text if (w.glue or not out) else " " + w.text)
    return "".join(out)


# --------------------------------------------------------------------------------------- matching core


@dataclass
class _Cand:
    method: str
    score: float
    word_idx: list[int]
    n_matches: int = 1
    notes: list[str] = field(default_factory=list)
    groups: Optional[list[list[int]]] = None      # fragment groups (quote order); None => single group


def _numbers_present(needed: Sequence[str], words: Sequence[Word]) -> bool:
    """Numeric guard: every number of the quote also occurs among the matched words."""
    if not needed:
        return True
    have = set(numbers_in(" ".join(w.text for w in words)))
    return all(n in have for n in needed)


def _guard_ok(idx: _Index, word_idx: Sequence[int], needed: Sequence[str]) -> bool:
    return _numbers_present(needed, [idx.words[i] for i in word_idx])


def _exact(idx: _Index, q: str, needed: Sequence[str]) -> Optional[_Cand]:
    if not q:
        return None
    occ, i = [], idx.S.find(q)
    while i != -1 and len(occ) < 200:
        occ.append(i)
        i = idx.S.find(q, i + 1)
    ok = [o for o in occ if _guard_ok(idx, idx.span_to_words(o, o + len(q)), needed)]
    if not ok:
        return None
    aligned = [o for o in ok if idx.aligned(o, o + len(q))]
    chosen = (aligned or ok)[0]
    cand = _Cand("exact", 1.0 if aligned else 0.97, idx.span_to_words(chosen, chosen + len(q)), n_matches=len(aligned or ok))
    if not aligned:
        cand.notes.append("match starts/ends inside a word")
    return cand


def _fuzzy(idx: _Index, q: str, needed: Sequence[str]) -> Optional[_Cand]:
    """Approximate match of `q` inside the squashed page text.

    1. rapidfuzz partial_ratio_alignment -> rough location (~0.3 ms for 9k chars)
    2. snap to token boundaries, then coordinate ascent over (start token, end token) maximising
           score(a, b) = coverage * sqrt(precision),  LCS from the bit-parallel Indel distance
       with coverage = LCS/len(quote), precision = LCS/len(span)."""
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
            if e > a and (v := sc(a, e)) > best + 1e-9:
                best, b, improved = v, e, True
        for st in starts:
            if st < b and (v := sc(st, b)) > best + 1e-9:
                best, a, improved = v, st, True
        if not improved:
            break
    # characters of junk words can match by chance and drag the window over them: drop edge words the quote does not contain
    ka, kb = idx.k_at(a), idx.k_at(b - 1)
    while ka < kb and idx.tok[ka] not in q:
        ka += 1
    while kb > ka and idx.tok[kb] not in q:
        kb -= 1
    a, b = idx.starts[ka], idx.starts[kb] + len(idx.tok[kb])
    best = max(best, sc(a, b))
    words = idx.span_to_words(a, b)
    if best < FUZZY_MIN or not _guard_ok(idx, words, needed):
        return None
    return _Cand("fuzzy", best, words)


_STOP = frozenset("the of and to in a for on by is was at as with from that an or be are this it its were has have had which "
                  "will would also than their there been not but our we us may can such other more".split())


def _density(idx: _Index, quote_tokens: list[str], needed: Sequence[str]) -> Optional[_Cand]:
    """Densest window of page tokens w.r.t. the quote's tokens (weighted by length / digits) - an approximate area."""
    qset = {t for t in quote_tokens if t not in _STOP and len(t) > 1}
    if not qset or not idx.tok:
        return None
    wt = {t: min(len(t), 8) + (3 if any(c.isdigit() for c in t) else 0) for t in qset}
    total = sum(wt.values())
    win = max(8, int(1.5 * len(quote_tokens)))
    cnt: dict[str, int] = {}
    cur = best = 0.0
    best_range = (0, 0)
    toks = idx.tok
    for r, t in enumerate(toks):
        if t in qset:
            cnt[t] = cnt.get(t, 0) + 1
            if cnt[t] == 1:
                cur += wt[t]
        left = r - win
        if left >= 0 and toks[left] in qset:
            cnt[toks[left]] -= 1
            if cnt[toks[left]] == 0:
                cur -= wt[toks[left]]
        if cur > best:
            best, best_range = cur, (max(0, r - win + 1), r)
    if best <= 0 or best / total < BLOCK_MIN_COVERAGE:
        return None
    lo, hi = best_range
    while lo < hi and toks[lo] not in qset:
        lo += 1
    while hi > lo and toks[hi] not in qset:
        hi -= 1
    cand = _expand_to_lines(idx, _Cand("block", BLOCK_MAX_SCORE * best / total, [idx.kept[k] for k in range(lo, hi + 1)]))
    # an area that lacks the quote's amounts is not where the quote sits
    return cand if _guard_ok(idx, cand.word_idx, [n for n in needed if len(n) >= 2]) else None


def _expand_to_lines(idx: _Index, cand: _Cand) -> _Cand:
    """Extend a window to the whole text lines it touches (so it reads as an 'area')."""
    words = idx.words
    lo, hi = min(cand.word_idx), max(cand.word_idx)

    def same_line(a: Word, b: Word) -> bool:
        return min(a.y1, b.y1) - max(a.y0, b.y0) >= 0.5 * min(a.h, b.h)

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
    needed = numbers_in(quote)
    if cand := _exact(idx, q, needed):
        return cand
    if cand := _fuzzy(idx, q, needed):
        return cand
    frags = [f for f in _ELLIPSIS_RE.split(quote) if len(squash(f)) >= 8]
    if len(frags) >= 2:
        parts, total, acc = [], 0, 0.0
        for f in frags:
            fq, fneeded = squash(f), numbers_in(f)
            fc = _exact(idx, fq, fneeded) or _fuzzy(idx, fq, fneeded)
            total += len(fq)
            if fc:
                parts.append(fc)
                acc += fc.score * len(fq)
        if parts and acc / total >= FRAGMENT_MIN:
            groups = [p.word_idx for p in parts]
            res = _Cand("fragments", acc / total, [i for g in groups for i in g], groups=groups)
            res.notes.append(f"{len(parts)}/{len(frags)} fragments located")
            return res
    return _density(idx, [t for t in (norm_token(x) for x in quote.split()) if t], needed)


_RANK = {"exact": 4, "fuzzy": 3, "fragments": 2, "block": 1, "page": 0}
_VERIFIED = ("exact", "fuzzy", "fragments")
_QUOTE_ORDERS = ("stream", "columns")


def _result_from(entry: _PageEntry, cand: _Cand, order: str, quote: str, hinted: int) -> LocateResult:
    pw = entry.pw
    idx = entry.index(order)
    groups = [[idx.words[i] for i in g] for g in (cand.groups or [cand.word_idx])]
    return LocateResult(
        page=pw.page, hinted_page=hinted, method=cand.method, score=cand.score,
        rects=rects_for_groups(groups, pw.width, pw.height),
        matched_text=" ... ".join(_join_words(g) for g in groups), quote=quote, n_matches=cand.n_matches, order=order,
        page_width=pw.width, page_height=pw.height, notes=list(cand.notes))


def _page_miss(entry: _PageEntry, quote: str, hinted: int, note: str) -> LocateResult:
    pw = entry.pw
    return LocateResult(page=pw.page, hinted_page=hinted, method="page", score=0.0, rects=[], matched_text="", quote=quote,
                        n_matches=0, page_width=pw.width, page_height=pw.height, notes=[note])


def _locate_in_page(entry: _PageEntry, quote: str, hinted: int) -> LocateResult:
    """Locate `quote` on one page.  Stream order first, then column order unless the stream match was exact."""
    best: Optional[tuple[_Cand, str]] = None
    for order in _QUOTE_ORDERS:
        cand = _locate_core(entry.index(order), quote)
        if cand and (best is None or (_RANK[cand.method], cand.score) > (_RANK[best[0].method], best[0].score)):
            best = (cand, order)
        if best and best[0].method == "exact":
            break
    if best is None:
        return _page_miss(entry, quote, hinted, "no usable match; highlight whole page")
    return _result_from(entry, best[0], best[1], quote, hinted)


# --------------------------------------------------------------------------------------- claim alignment


def _weight(tok: str) -> float:
    """How much a claim token says about WHERE its support is: amounts dominate, long words count, filler counts zero."""
    if tok[0].isdigit():
        digits = sum(c.isdigit() for c in tok)
        return 2.0 if digits == 1 else 4.0 + min(digits, 8)
    return 0.0 if tok in _STOP or len(tok) < 2 else min(len(tok), 10) * 0.5


def _claim_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    return [t.replace(",", "") if t[0].isdigit() else t for t in _TOKEN_RE.findall(text)]


def _is_year(tok: str) -> bool:
    return len(tok) == 4 and tok.isdigit() and 1900 <= int(tok) <= 2100


def _key_numbers(tokens: Sequence[str]) -> list[str]:
    """Numbers a supporting span must contain: amounts and decimals, not bare years or tiny counts."""
    return list(dict.fromkeys(t for t in tokens if t[0].isdigit() and not _is_year(t) and (len(t.replace(".", "")) >= 3 or "." in t)))


def _align_in_index(idx: _Index, claim_tokens: Sequence[str], weights: dict[str, float], numbers: Sequence[str],
                    key_numbers: Sequence[str]) -> Optional[tuple[float, list[int]]]:
    """Best (coverage, word indices) window of the page's tokens for the claim, or None."""
    page_toks, page_word = idx.claim_view()
    if not page_toks:
        return None
    total = sum(weights.values())
    win = max(10, int(1.5 * len(claim_tokens)) + 4)
    cnt: dict[str, int] = {}
    cur = best = 0.0
    best_range = (0, 0)
    for r, t in enumerate(page_toks):
        if t in weights:
            cnt[t] = cnt.get(t, 0) + 1
            if cnt[t] == 1:
                cur += weights[t]
        left = r - win
        if left >= 0 and page_toks[left] in weights:
            cnt[page_toks[left]] -= 1
            if cnt[page_toks[left]] == 0:
                cur -= weights[page_toks[left]]
        if cur > best:
            best, best_range = cur, (max(0, left + 1), r)
    if best < ALIGN_MIN_WEIGHT or best / total < ALIGN_MIN_COVERAGE:
        return None
    lo, hi = best_range
    while lo < hi and page_toks[lo] not in weights:
        lo += 1
    while hi > lo and page_toks[hi] not in weights:
        hi -= 1
    have = set(page_toks[lo:hi + 1])
    if key_numbers:
        if sum(n in have for n in key_numbers) < -(-len(key_numbers) // 2):
            return None
    elif numbers and not any(n in have for n in numbers):
        return None
    return best / total, list(range(page_word[lo], page_word[hi] + 1))


def _align_on_page(entry: _PageEntry, claim: str, hinted: int) -> Optional[LocateResult]:
    tokens = _claim_tokens(claim)
    weights = {t: w for t in tokens if (w := _weight(t)) > 0}
    if len(weights) < 2:
        return None
    numbers = [t for t in dict.fromkeys(tokens) if t[0].isdigit()]
    key_numbers = _key_numbers(tokens)
    best: Optional[tuple[float, list[int], str]] = None
    for order in ("stream", "lines", "columns"):
        hit = _align_in_index(entry.index(order), tokens, weights, numbers, key_numbers)
        if hit and (best is None or hit[0] > best[0] + 1e-9):
            best = (hit[0], hit[1], order)
    if best is None:
        return None
    score, word_idx, order = best
    cand = _Cand("block", score, word_idx, notes=["aligned from the answer sentence (no usable quote)"])
    return _result_from(entry, cand, order, claim, hinted)


# --------------------------------------------------------------------------------------- the document


class PdfiumDoc:
    """A PDF opened from bytes (no Windows file lock) with a bounded per-page word cache.  Thread-safe: all PDFium access
    is serialised through PDFIUM_LOCK, the cache through an internal lock."""

    def __init__(self, data: "bytes | Path", *, cache_pages: int = DEFAULT_CACHE_PAGES):
        raw = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else read_pdf_bytes(Path(data))
        self._pdf = open_document(raw)            # raises PdfError
        with PDFIUM_LOCK:
            self.page_count: int = len(self._pdf)
        self._max = max(1, cache_pages)
        self._cache: "OrderedDict[int, _PageEntry]" = OrderedDict()
        self._lock = threading.Lock()
        self._sizes: Optional[list[tuple[float, float]]] = None
        self._squashed: Optional[list[str]] = None
        self._derive_lock = threading.Lock()      # the whole-document values below are built once even when questions run in parallel
        self._closed = False

    # -- lifecycle
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
        with PDFIUM_LOCK:
            self._pdf.close()

    def __enter__(self) -> "PdfiumDoc":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _check(self, page: Optional[int] = None) -> None:
        if self._closed:
            raise RuntimeError("PdfiumDoc is closed")
        if page is not None and not 1 <= page <= self.page_count:
            raise IndexError(f"page {page} outside 1..{self.page_count}")

    # -- pages
    def _entry(self, page: int) -> _PageEntry:
        self._check(page)
        with self._lock:
            entry = self._cache.get(page)
            if entry is not None:
                self._cache.move_to_end(page)
                return entry
        with PDFIUM_LOCK:
            self._check()
            pw = _extract_words(self._pdf, page - 1)
        entry = _PageEntry(pw)
        with self._lock:
            entry = self._cache.setdefault(page, entry)
            self._cache.move_to_end(page)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return entry

    def page_words(self, page: int) -> PageWords:
        """Words of a page (1-based) in visible space; cached."""
        return self._entry(page).pw

    def page_sizes(self) -> list[tuple[float, float]]:
        """Visible page sizes in points, for laying out placeholders before pages render."""
        with self._derive_lock:
            if self._sizes is None:
                sizes = []
                with PDFIUM_LOCK:
                    self._check()
                    for i in range(self.page_count):
                        page = self._pdf[i]
                        try:
                            geo = page_geometry(page)
                        finally:
                            page.close()
                        sizes.append((geo.width, geo.height))
                self._sizes = sizes
            return self._sizes

    @property
    def squashed_ready(self) -> bool:
        return self._squashed is not None

    def squashed_pages(self) -> list[str]:
        """Per-page squashed text for the whole-document exact search (~10 ms/page, memoised; warm it at index time)."""
        if self._squashed is not None:
            return self._squashed
        with self._derive_lock:                   # parallel questions: one computes, the others wait and reuse it
            if self._squashed is None:
                out = []
                for i in range(self.page_count):
                    with PDFIUM_LOCK:
                        self._check()
                        page = self._pdf[i]
                        try:
                            tp = page.get_textpage()
                            try:
                                out.append(squash(page_chars(tp)))
                            finally:
                                tp.close()
                        finally:
                            page.close()
                self._squashed = out
            return self._squashed


# --------------------------------------------------------------------------------------- public API


def locate_quote(doc: PdfiumDoc, page: int, quote: str, *, neighbours: int = 1) -> LocateResult:
    """Where is `quote` on `page`?  Neighbour pages (+-`neighbours`) and then the whole document are searched only when the
    hinted page yields no exact / fuzzy / fragment match (printed-vs-physical page confusion, off-by-one cites)."""
    page = min(max(1, page), doc.page_count)
    quote = quote or ""
    first = _locate_in_page(doc._entry(page), quote, page)
    if first.method in _VERIFIED:
        return first
    for d in range(1, neighbours + 1):
        for p in (page - d, page + d):
            if 1 <= p <= doc.page_count:
                res = _locate_in_page(doc._entry(p), quote, page)
                if res.method in _VERIFIED:
                    res.notes.append(f"page corrected from {page} to {p}")
                    log.info("quote cited on page %d found on page %d", page, p)
                    return res
    q = squash(quote)
    if len(q) >= DOC_SEARCH_MIN_CHARS and (doc.squashed_ready or doc.page_count <= DOC_SEARCH_COLD_MAX_PAGES):
        hits = [i + 1 for i, s in enumerate(doc.squashed_pages()) if q in s]
        for p in sorted(hits, key=lambda h: abs(h - page)):
            res = _locate_in_page(doc._entry(p), quote, page)
            if res.method in _VERIFIED:
                res.notes.append(f"page corrected from {page} to {p} via whole-document search ({len(hits)} hit pages)")
                log.info("quote cited on page %d found on page %d by whole-document search", page, p)
                return res
    log.debug("quote not verified near page %d (%s, score %.2f): %.60r", page, first.method, first.score, quote)
    return first


def align_claim(doc: PdfiumDoc, page: int, claim: str, *, neighbours: int = 0) -> Optional[LocateResult]:
    """No quote available: the span of `page` that best supports an answer sentence.  Amounts weigh heavily and a claim
    whose amounts are absent from the span is rejected; None when the support is weak."""
    page = min(max(1, page), doc.page_count)
    best: Optional[LocateResult] = None
    for p in [page] + [q for d in range(1, neighbours + 1) for q in (page - d, page + d) if 1 <= q <= doc.page_count]:
        res = _align_on_page(doc._entry(p), claim or "", page)
        if res and (best is None or res.score > best.score + 1e-9):
            best = res
    if best is not None and best.page != page:
        best.notes.append(f"page corrected from {page} to {best.page}")
    return best
