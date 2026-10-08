"""PDF inspection, clean page text and printed page labels (pypdfium2 only; PyMuPDF is AGPL and deliberately not used).

PDFium is not thread-safe - not even across documents - so every call into it goes through the single process-wide
`PDFIUM_LOCK`.  Documents are always opened from bytes: opening by path would keep a Windows file lock that breaks
deleting a session folder.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pypdfium2 as pdfium
from pypdfium2 import raw as pdfium_c

log = logging.getLogger("reportlens.pdfutil")

# One lock for the whole process (re-entrant so helpers can nest).  locator.py imports it too.
PDFIUM_LOCK = threading.RLock()

MIN_TEXT_CHARS = 20          # a page "has text" from this many non-blank characters
SCANNED_TEXT_FRACTION = 0.05  # fewer text pages than this share of the document => treated as scanned
_FALLBACK_SIZE = (595.0, 842.0)

# PDFium turns a line-end hyphen into U+FFFE (text range) / U+0002 (bounded text) and drops the line break, i.e. it has
# already joined the word for us; a soft hyphen (U+00AD) is invisible.  All three mean "join the two halves".
SOFT_HYPHENS = frozenset("\ufffe\x02\u00ad")


class PdfError(Exception):
    """A PDF we cannot (or will not) process.  `code` is one of invalid_pdf | encrypted_pdf | scanned_pdf."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    sizes: list[tuple[float, float]]     # visible size in points per page, after /Rotate and CropBox
    text_pages: int                      # pages carrying at least MIN_TEXT_CHARS characters of text
    title: Optional[str]


# --------------------------------------------------------------------------------------- opening / geometry


def read_pdf_bytes(path: Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise PdfError("invalid_pdf", f"The file could not be read: {exc.strerror or exc}") from exc


def open_document(data: bytes) -> pdfium.PdfDocument:
    """Open a PDF held in memory; maps every failure to PdfError.  The caller closes it (under PDFIUM_LOCK)."""
    if not data:
        raise PdfError("invalid_pdf", "The file is empty.")
    with PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
                raise PdfError("encrypted_pdf", "The PDF is password protected; remove the password and upload it again.") from exc
            raise PdfError("invalid_pdf", "The file is not a readable PDF.") from exc
        except Exception as exc:  # noqa: BLE001 - pdfium wrappers can surface odd errors on hostile input
            log.warning("unexpected error opening PDF: %r", exc)
            raise PdfError("invalid_pdf", "The file is not a readable PDF.") from exc
        try:
            if len(pdf) == 0:
                raise PdfError("invalid_pdf", "The PDF has no pages.")
        except BaseException:
            pdf.close()
            raise
        return pdf


@dataclass(frozen=True)
class PageGeometry:
    """Maps PDF user space (origin bottom-left, unrotated) to the visible page (origin top-left, after /Rotate + CropBox)."""
    rotation: int
    left: float
    bottom: float
    right: float
    top: float
    width: float       # visible size in points
    height: float

    def to_visible(self, px: float, py: float) -> tuple[float, float]:
        r = self.rotation
        if r == 0:
            return px - self.left, self.top - py
        if r == 90:
            return py - self.bottom, px - self.left
        if r == 180:
            return self.right - px, py - self.bottom
        return self.top - py, self.right - px          # 270

    def rect_to_visible(self, l: float, b: float, r: float, t: float) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) with x0<=x1, y0<=y1 in visible space for a PDF-space rect given as (left, bottom, right, top)."""
        (xa, ya), (xb, yb) = self.to_visible(l, t), self.to_visible(r, b)
        return min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)


def page_geometry(page: pdfium.PdfPage) -> PageGeometry:
    """Caller holds PDFIUM_LOCK.  The crop box is clipped to the media box, as PDFium does when rendering."""
    rot = page.get_rotation() % 360
    if rot not in (0, 90, 180, 270):
        rot = 0
    cl, cb, cr, ct = page.get_cropbox()
    ml, mb, mr, mt = page.get_mediabox()
    cl, cb, cr, ct = max(cl, ml), max(cb, mb), min(cr, mr), min(ct, mt)
    if cr <= cl or ct <= cb:                      # degenerate crop box: fall back to the media box
        cl, cb, cr, ct = ml, mb, mr, mt
    w, h = (cr - cl, ct - cb) if rot in (0, 180) else (ct - cb, cr - cl)
    if w <= 0 or h <= 0:
        w, h = _FALLBACK_SIZE
        cl, cb, cr, ct = 0.0, 0.0, w, h
        rot = 0
    return PageGeometry(rot, cl, cb, cr, ct, w, h)


def page_chars(tp) -> str:
    """Text of a PDFium text page, index-aligned with the char list (so `get_charbox(i)` belongs to `text[i]`)."""
    n = tp.count_chars()
    text = tp.get_text_range()
    if len(text) == n:
        return text
    # PDFium excluded or inserted characters in the range text: rebuild it from the char list itself.
    get_unicode = pdfium_c.FPDFText_GetUnicode
    return "".join(chr(cp) if 0 < (cp := get_unicode(tp, i)) < 0x110000 else " " for i in range(n))


# --------------------------------------------------------------------------------------- clean text

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
_SPACES = {"\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u202f": " ", "\u3000": " ", "\u2028": "\n", "\u2029": "\n"}
_DROPPED = {ord(c): None for c in "\ufffe\x02\u00ad\u200b\u200c\u200d\u2060\ufeff\uffff"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


def clean_text(raw: str) -> str:
    """Normalise PDFium text for the agent / RAGAS / highlighter: LF newlines, hyphenated words joined, ligatures and
    exotic spaces expanded, control characters gone.  Line breaks are kept (tables and bullets depend on them)."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_DROPPED)
    for src, dst in {**_LIGATURES, **_SPACES}.items():
        if src in text:
            text = text.replace(src, dst)
    text = _CONTROL_RE.sub("", text)
    text = _TRAILING_WS_RE.sub("\n", text)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


# --------------------------------------------------------------------------------------- inspection


def _count_visible_chars(raw: str) -> int:
    return sum(1 for ch in raw if not ch.isspace())


def inspect_pdf(path: Path) -> PdfInfo:
    """Page count, visible page sizes, how many pages carry text, document title.  Raises PdfError for unreadable,
    password-protected and scanned (image-only) files."""
    pdf = open_document(read_pdf_bytes(path))
    sizes: list[tuple[float, float]] = []
    text_pages = 0
    try:
        with PDFIUM_LOCK:
            n = len(pdf)
            title = _title(pdf)
        for i in range(n):
            with PDFIUM_LOCK:
                size, has_text = _inspect_page(pdf, i)
            sizes.append(size)
            text_pages += has_text
    finally:
        with PDFIUM_LOCK:
            pdf.close()
    if text_pages < SCANNED_TEXT_FRACTION * n:
        raise PdfError("scanned_pdf", "The PDF has no text layer (it looks scanned). Run OCR on it and upload it again.")
    return PdfInfo(page_count=n, sizes=sizes, text_pages=text_pages, title=title)


def _title(pdf: pdfium.PdfDocument) -> Optional[str]:
    try:
        title = (pdf.get_metadata_dict().get("Title") or "").strip()
    except Exception:  # noqa: BLE001 - metadata is optional
        return None
    return title or None


def _inspect_page(pdf: pdfium.PdfDocument, index: int) -> tuple[tuple[float, float], bool]:
    try:
        page = pdf[index]
    except pdfium.PdfiumError:
        log.warning("page %d could not be loaded", index + 1)
        return _FALLBACK_SIZE, False
    try:
        geo = page_geometry(page)
        tp = page.get_textpage()
        try:
            has_text = tp.count_chars() >= MIN_TEXT_CHARS and _count_visible_chars(tp.get_text_range()) >= MIN_TEXT_CHARS
        finally:
            tp.close()
        return (geo.width, geo.height), has_text
    finally:
        page.close()


def extract_page_texts(path: Path) -> list[str]:
    """Clean text of every page (index 0 == page 1), the same text the agent reads and RAGAS judges."""
    pdf = open_document(read_pdf_bytes(path))
    texts: list[str] = []
    try:
        with PDFIUM_LOCK:
            n = len(pdf)
        for i in range(n):
            with PDFIUM_LOCK:
                texts.append(_page_text(pdf, i))
    finally:
        with PDFIUM_LOCK:
            pdf.close()
    return texts


def _page_text(pdf: pdfium.PdfDocument, index: int) -> str:
    try:
        page = pdf[index]
    except pdfium.PdfiumError:
        log.warning("page %d could not be loaded", index + 1)
        return ""
    try:
        tp = page.get_textpage()
        try:
            return clean_text(page_chars(tp))
        finally:
            tp.close()
    finally:
        page.close()


# --------------------------------------------------------------------------------------- printed page labels

_BAND_FOOTER = 0.88        # visible y (fraction of height) below which a text run counts as footer
_BAND_HEADER = 0.10        # ... above which it counts as header
_ROMAN_RE = re.compile(r"^(?:x{0,3})(?:ix|iv|v?i{0,3})$", re.I)
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}
_SEPARATORS_RE = re.compile(r"[\s|/•·–—‒―]+")
_EDGE_PUNCT = "()[]{}.,:;-–—−*•·"
_MAX_RUNS = 4              # numbering sequences recognised in one document (front matter, body, appendices, ...)


def _roman_to_int(token: str) -> int:
    total = 0
    values = [_ROMAN_VALUES[c] for c in token.lower()]
    for i, v in enumerate(values):
        total += -v if i + 1 < len(values) and v < values[i + 1] else v
    return total


def _int_to_roman(value: int) -> str:
    out = ""
    for sym, n in (("x", 10), ("ix", 9), ("v", 5), ("iv", 4), ("i", 1)):
        while value >= n:
            out += sym
            value -= n
    return out


def _candidates(text: str) -> Iterator[tuple[str, int, bool]]:
    """(kind, value, upper) folio candidates in a header/footer text run: standalone 1-4 digit numbers and roman numerals."""
    for tok in _SEPARATORS_RE.split(text):
        tok = tok.strip(_EDGE_PUNCT)
        if not tok:
            continue
        if tok.isascii() and tok.isdigit():
            if len(tok) <= 4 and not tok.startswith("0"):
                yield "arabic", int(tok), False
        elif tok.isalpha() and tok.isascii() and _ROMAN_RE.match(tok) and (len(tok) >= 2 or tok.lower() in _ROMAN_VALUES):
            yield "roman", _roman_to_int(tok), tok.isupper()


def _band_candidates(pdf: pdfium.PdfDocument, index: int) -> list[tuple[str, int, bool]]:
    """Folio candidates found in the header and footer bands of one page (visible space, so /Rotate pages work)."""
    try:
        page = pdf[index]
    except pdfium.PdfiumError:
        return []
    try:
        geo = page_geometry(page)
        tp = page.get_textpage()
        try:
            found: list[tuple[str, int, bool]] = []
            for k in range(tp.count_rects()):
                rect = tp.get_rect(k)
                _, y0, _, y1 = geo.rect_to_visible(*rect)
                yc = (y0 + y1) / 2 / geo.height
                if yc >= _BAND_FOOTER or yc <= _BAND_HEADER:
                    found.extend(_candidates(clean_text(tp.get_text_bounded(*rect))))
            return found
        finally:
            tp.close()
    finally:
        page.close()


def _pdf_page_labels(pdf: pdfium.PdfDocument, n: int) -> Optional[list[Optional[str]]]:
    """The document's own /PageLabels, or None when absent or uninformative (e.g. plain 1..N, or one label repeated)."""
    labels = [pdf.get_page_label(i).strip() for i in range(n)]
    if not any(labels) or all(lab == str(i + 1) for i, lab in enumerate(labels)):
        return None
    if len(set(labels)) < 0.5 * n:
        return None
    return [lab or None for lab in labels]


def detect_printed_labels(path: Path, page_texts: Optional[list[str]] = None) -> list[Optional[str]]:
    """Printed folio of every page ("87", "xii"), None where unknown.  len == page count.

    Order: the PDF's /PageLabels when meaningful, else voting on numbers/roman numerals found in each page's footer and
    header.  Voting uses offset = printed - physical: the dominant offset labels the pages that carry it plus the pages
    between them (a full-page photo inside the body has no folio); pages outside the numbered span (cover, contents,
    back cover) stay None.  `page_texts` (from extract_page_texts) only lets us skip blank pages."""
    pdf = open_document(read_pdf_bytes(path))
    try:
        with PDFIUM_LOCK:
            n = len(pdf)
            native = _pdf_page_labels(pdf, n)
        if native is not None:
            return native
        blank = [not t.strip() for t in page_texts] if page_texts is not None and len(page_texts) == n else [False] * n
        candidates: dict[int, list[tuple[str, int, bool]]] = {}
        for i in range(n):
            if blank[i]:
                continue
            with PDFIUM_LOCK:
                cands = _band_candidates(pdf, i)
            if cands:
                candidates[i] = cands
    finally:
        with PDFIUM_LOCK:
            pdf.close()
    return _labels_from_votes(candidates, n, sum(not b for b in blank))


def _labels_from_votes(candidates: dict[int, list[tuple[str, int, bool]]], n: int, text_pages: int) -> list[Optional[str]]:
    """Turn per-page folio candidates into labels: accept the best-supported (kind, offset) pairs one after the other
    (pages explained by an accepted offset no longer vote), then label each run's own pages and the gaps between them."""
    min_support = max(2, -(-text_pages * 3 // 100))          # 3 % of the text pages, at least 2
    unexplained = dict(candidates)
    runs: list[tuple[str, int, bool, list[int]]] = []        # (kind, offset, upper-case numerals, pages that claim it)

    def hits(page: int, kind: str, offset: int) -> list[tuple[str, int, bool]]:
        return [c for c in unexplained[page] if c[0] == kind and c[1] - (page + 1) == offset]

    for _ in range(_MAX_RUNS):
        votes: Counter[tuple[str, int]] = Counter()
        for page, cands in unexplained.items():
            for kind, value in {(k, v) for k, v, _ in cands}:
                votes[(kind, value - (page + 1))] += 1
        if not votes:
            break
        (kind, offset), support = votes.most_common(1)[0]
        if support < (min_support if not runs else max(3, min_support)):
            break
        claimed = [page for page in unexplained if hits(page, kind, offset)]
        upper = 2 * sum(c[2] for page in claimed for c in hits(page, kind, offset)) > len(claimed)
        runs.append((kind, offset, upper, claimed))
        for page in claimed:
            del unexplained[page]
    labels: list[Optional[str]] = [None] * n
    owned = {page for *_, claimed in runs for page in claimed}
    for kind, offset, upper, claimed in runs:
        for page in range(min(claimed), max(claimed) + 1):
            if labels[page] is None and (page in claimed or page not in owned):     # a gap page never steals another run's page
                labels[page] = _format_label(kind, page + 1 + offset, upper)
    return labels


def _format_label(kind: str, value: int, upper: bool) -> Optional[str]:
    if value < 1:
        return None
    if kind == "arabic":
        return str(value)
    if value > 39:                                           # beyond what the roman pattern can produce
        return None
    roman = _int_to_roman(value)
    return roman.upper() if upper else roman
