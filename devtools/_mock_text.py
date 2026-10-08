"""Text helpers for the mock OpenAI server (dev tooling, not part of the product).

* tokenising / light stemming shared by the agent persona, the judge responders and the embeddings;
* hashed bag-of-words embeddings (dim 64, L2-normalised);
* splitting a PDF page's text into answerable units (sentences and table rows) plus verbatim quote windows.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

EMBED_DIM = 64

STOPWORDS = frozenset(
    "a an the of in on at to for from by with about as and or but nor so if then than that this these those it its "
    "is are was were be been being am do does did done have has had having will would shall should can could may might must "
    "what which who whom whose when where why how much many more most any all some each per into over under between "
    "report annual company tell me please state stated says say said mention mentioned according s t d ll re ve "
    "there their they them he she his her we our us you your i not no yes also".split()
)

_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"
_TOKEN_RE = re.compile(rf"{_NUM}|[A-Za-z]+(?:['\u2019][A-Za-z]+)?")
_SUFFIXES = ("ations", "ation", "ating", "ated", "ates", "ions", "ion", "ings", "ing", "ies", "ied", "ed", "es", "s")


def stem(word: str) -> str:
    """Crude stemming (suffix strip, then keep 5 letters: reduction/reducing -> reduc), only to make question and page
    words comparable. Not linguistically correct."""
    if word[:1].isdigit():
        return word
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            word = word[: -len(suf)]
            break
    return word[:5]


def tokens(text: str) -> list[str]:
    """Lower-cased tokens: numbers lose thousands separators, possessives lose 's, words are stemmed."""
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        t = m.group(0)
        if t[0].isdigit():
            out.append(t.replace(",", ""))
            continue
        t = re.sub(r"['\u2019]s$", "", t).replace("'", "").replace("\u2019", "")
        out.append(stem(t))
    return out


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOPWORDS and len(t) > 1]


# --------------------------------------------------------------------------------------------- embeddings
def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic hashed bag-of-words vector (signed buckets, sub-linear tf), L2-normalised.
    Texts sharing content words have high cosine similarity; empty text maps to a fixed unit vector."""
    vec = [0.0] * dim
    counts: dict[str, int] = {}
    for t in content_tokens(text):
        counts[t] = counts.get(t, 0) + 1
    for t, c in counts.items():
        h = int.from_bytes(hashlib.md5(t.encode("utf-8")).digest()[:8], "little")
        vec[h % dim] += (1.0 if (h >> 32) & 1 else -1.0) * (1.0 + math.log(c))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


# --------------------------------------------------------------------------------------------- page units
_SENT_END = re.compile(r"[.!?][\)\]\u201d\u2019\"']*$")
_ABBREV = frozenset({"no.", "nos.", "vs.", "etc.", "approx.", "inc.", "ltd.", "co.", "st.", "dr.", "mr.", "mrs.", "ms."})
_UNSAFE_CHARS = ("\ufffe", "\u00ad", '"', "<", ">", "\\", "\x02")
_NUMERIC_WORD = re.compile(r"^[\(\u00a3$\u20ac-]*\d[\d,\.]*[%\)]*$|^n/m$|^-$")


def _is_figure(w: str) -> bool:
    return bool(_NUMERIC_WORD.match(w))


_YEAR_PAIR = re.compile(r"^\d{4}/\d{2}$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def has_figure(text: str) -> bool:
    """True when the text states an amount (a year or a day number alone does not count)."""
    words = [w.strip(".,;:") for w in text.split()]
    return any(_is_figure(w) and not _YEAR.match(w) and len(w) > 2 for w in words)


def _looks_like_row(line: str) -> bool:
    """A table row: a short label followed by two or more figures, e.g. 'Capital expenditure (2,596) (2,288)'."""
    words = line.split()
    tail = 0
    for w in reversed(words):
        if not _is_figure(w):
            break
        tail += 1
    return 2 <= tail < len(words) <= 18 and len(words) - tail <= 12


def _looks_like_column_header(line: str) -> bool:
    """'\u00a3m 2025/26 2024/25' style lines: they label columns, they are not statements."""
    return sum(1 for w in line.split() if _YEAR_PAIR.match(w)) >= 2 and len(line.split()) <= 8


def _looks_like_heading(line: str) -> bool:
    words = line.split()
    return (1 <= len(words) <= 7 and line[:1].isupper() and not line.rstrip().endswith((".", ",", ";", ":", "-", "?", "!", ")"))
            and not any(_is_figure(w) for w in words))


def _clean_display(words: Sequence[str], breaks: frozenset[int]) -> str:
    """Single-spaced text with line-end hyphenation repaired and pdfium's soft-hyphen markers removed."""
    out: list[str] = []
    skip_space = False
    for i, w in enumerate(words):
        w = w.replace("\ufffe", "").replace("\u00ad", "").replace("\x02", "")
        if i in breaks and w.endswith("-"):
            out.append(("" if skip_space else (" " if out else "")) + w[:-1])
            skip_space = True
            continue
        out.append(("" if skip_space else (" " if out else "")) + w)
        skip_space = False
    return "".join(out)


@dataclass(frozen=True)
class Unit:
    """One answerable piece of a page: a sentence or a table row, with the verbatim words it came from."""
    page: int
    words: tuple[str, ...]                 # verbatim words (whitespace-split page text)
    breaks: frozenset[int] = frozenset()   # indexes of words whose trailing '-' is a line-break hyphen
    is_row: bool = False
    text: str = field(default="", compare=False)   # what the answer says (rows are rendered as "label: fig (col), ...")
    label: str = field(default="", compare=False)   # rows: the label words without the figures

    @property
    def unsafe(self) -> frozenset[int]:
        """Word indexes that must not appear inside a verbatim quote (markers, quotes, hyphen-joined pairs)."""
        bad = {i for i, w in enumerate(self.words) if any(c in w for c in _UNSAFE_CHARS)}
        for i in self.breaks:
            bad.update((i, i + 1))
        return frozenset(bad)

    def quote_window(self, wanted: set[str], max_words: int = 25) -> Optional[str]:
        """The best <= max_words verbatim stretch for the wanted tokens (None when every stretch is unsafe)."""
        n = len(self.words)
        bad = self.unsafe
        width = min(max_words, n)
        best: tuple[float, int] | None = None
        for s in range(0, n - width + 1):
            if any(i in bad for i in range(s, s + width)):
                continue
            hit = len(wanted & set(tokens(" ".join(self.words[s:s + width]))))
            if best is None or hit > best[0]:
                best = (hit, s)
        if best is not None:
            return " ".join(self.words[best[1]:best[1] + width])
        # all full-width windows touch unsafe words: fall back to the longest safe run
        runs: list[tuple[int, int]] = []
        start = None
        for i in range(n + 1):
            if i < n and i not in bad:
                start = i if start is None else start
            elif start is not None:
                runs.append((start, i))
                start = None
        runs = [r for r in runs if r[1] - r[0] >= 4]
        if not runs:
            return None
        a, b = max(runs, key=lambda r: (len(wanted & set(tokens(" ".join(self.words[r[0]:r[1]])))), r[1] - r[0]))
        return " ".join(self.words[a:min(b, a + max_words)])


def _render_row(words: Sequence[str], columns: Sequence[str]) -> tuple[str, str]:
    """('label: 6,991 (2025/26), 6,534 (2024/25)', 'label') for a table row; columns come from the last column-header line."""
    tail = 0
    for w in reversed(words):
        if not _is_figure(w):
            break
        tail += 1
    label, figs = " ".join(words[:-tail]), words[-tail:]
    cells = [f"{f} ({columns[i]})" if i < len(columns) else f for i, f in enumerate(figs)]
    return f"{label}: " + ", ".join(cells), label


def _split_sentences(words: list[str], breaks: set[int], page: int) -> list[Unit]:
    units: list[Unit] = []
    start = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        boundary = False
        if not last and _SENT_END.search(w) and w.lower() not in _ABBREV and len(w.rstrip(".!?")) > 1:
            nxt = words[i + 1]
            boundary = nxt[:1].isupper() or nxt[:1] in "\u201c\u2018(\u00a3$0123456789"
        if boundary or last:
            seg = words[start:i + 1]
            brk = frozenset(b - start for b in breaks if start <= b < i + 1)
            units.append(Unit(page, tuple(seg), brk, False, _clean_display(seg, brk)))
            start = i + 1
    return units


_CHROME_RE = re.compile(r"(?:^|\s)\d{1,3}$|^\d{1,3}\s|annual report|interim report|\bpage\s+\d+", re.I)


def _strip_running_chrome(lines: list[str]) -> list[str]:
    """Drop running headers/footers: short lines carrying a folio or 'Annual Report' within 3 lines of either page edge."""
    def chrome(ln: str) -> bool:
        return bool(ln) and len(ln.split()) <= 12 and not _SENT_END.search(ln) and bool(_CHROME_RE.search(ln))

    keep = [True] * len(lines)
    nonblank = [i for i, ln in enumerate(lines) if ln]
    for i in nonblank[:3] + nonblank[-3:]:
        if chrome(lines[i]):
            keep[i] = False
    return [ln for ln, k in zip(lines, keep) if k]


def page_units(page: int, text: str) -> list[Unit]:
    """Split page text into sentences and table-row lines. Headings and running headers/footers are dropped."""
    lines = _strip_running_chrome([ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")])
    units: list[Unit] = []
    words: list[str] = []
    breaks: set[int] = set()
    columns: list[str] = []

    def flush() -> None:
        nonlocal words, breaks
        if words:
            units.extend(_split_sentences(words, breaks, page))
        words, breaks = [], set()

    for ln in lines:
        if not ln:
            flush()
        elif _looks_like_column_header(ln):
            flush()
            columns = [w for w in ln.split() if _YEAR_PAIR.match(w)]
        elif _looks_like_row(ln):
            flush()
            ws = ln.split()
            rendered, label = _render_row(ws, columns)
            units.append(Unit(page, tuple(ws), frozenset(), True, rendered, label))
        elif _looks_like_heading(ln) and (not words or _SENT_END.search(words[-1])):
            flush()  # a heading after a finished sentence starts a new block and is not itself an answer
        else:
            ws = ln.split()
            words.extend(ws)
            if ln.endswith("-") and len(ws[-1]) > 2 and ws[-1][-2].isalpha():
                breaks.add(len(words) - 1)
    flush()
    # a long run of words that never ends a sentence is a contents list or a label column, not a statement
    return [u for u in units if len(u.text.split()) >= 3 and (u.is_row or len(u.words) < 40 or _SENT_END.search(u.words[-1]))]
