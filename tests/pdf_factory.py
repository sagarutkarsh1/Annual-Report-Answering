"""Small PDF generators for the pdfutil / locator / citations tests (reportlab only, bundled Vera font => same output everywhere).

`build_report_pdf()` is a 7-page mini annual report with the layouts the locator must survive: unnumbered cover + contents and
body folios = physical - 2, two columns, hyphenated line ends, curly quotes, ligature glyphs, a table with right-aligned
numbers, a page whose content stream draws the right column first, and a /Rotate 90 landscape page.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import reportlab
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.pdfencrypt import StandardEncryption

W, H = A4                      # 595.27 x 841.89
FS, LEAD = 10.5, 13.5
LEFT_X, RIGHT_X = 50, 310
FOLIO_OFFSET = 2               # printed folio = physical page - 2

_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
_registered = False


def _fonts() -> None:
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont("Vera", str(_FONT_DIR / "Vera.ttf")))
        pdfmetrics.registerFont(TTFont("VeraBd", str(_FONT_DIR / "VeraBd.ttf")))
        _registered = True


def _canvas(buf: io.BytesIO, **kw) -> canvas.Canvas:
    _fonts()
    return canvas.Canvas(buf, pagesize=A4, invariant=1, **kw)


def lines_at(c: canvas.Canvas, x: float, y_top: float, lines: Sequence[str], *, size: float = FS, lead: float = LEAD,
             font: str = "Vera") -> float:
    """Draw lines downwards from `y_top` (measured from the top of the page); returns the next free y_top."""
    c.setFont(font, size)
    for ln in lines:
        c.drawString(x, H - (y_top + size), ln)
        y_top += lead
    return y_top


def footer(c: canvas.Canvas, folio: Optional[str], *, width: float = W) -> None:
    """Running footer: report name on the left, folio (if any) on the right, 36 pt above the bottom edge."""
    c.setFont("Vera", 8)
    c.drawString(50, 36, "Annual Report 2024/25")
    if folio:
        c.drawRightString(width - 50, 36, folio)


def header(c: canvas.Canvas, text: str) -> None:
    c.setFont("Vera", 8)
    c.drawString(50, H - 30, text)


# --------------------------------------------------------------------------------------- the mini report

# key -> (physical page, clean text as it reads on the page).  Hyphenation, ligatures and quotes are drawn below.
PASSAGES: dict[str, tuple[int, str]] = {
    "A": (3, "Capital investment in our networks reached £4.1 billion in 2024/25, driven by the infrastructure programme in "
             "the UK and the “Climate Leadership” Act in New York. Our network reliability was 99.99% across all regions."),
    "B": (3, "Our efficient financial workflow improved the net debt position; the final dividend was 31.36 pence per share "
             "(2023/24: 30.82 pence)."),
    "C": (3, "Underlying operating profit — adjusted for timing — increased by 6% to £6,689 million, whilst the Group’s "
             "‘core’ earnings per share rose."),
    "D": (3, "Cash generated from continuing operations was £6,991 million in 2024/25, down 4% from £7,281 million, primarily "
             "due to the timing of working capital movements and lower receipts from our regulated businesses in the year."),
    "E": (3, "Net debt at the end of the year was £41,371 million (2023/24: £43,607 million). Interest paid of (1,588) million "
             "is shown net of capital interest and includes hedging."),
    "F": (3, "The Board has recommended a final dividend of 31.36 pence per share, which, subject to shareholder approval at the "
             "AGM, will be paid on 12 September 2025 to holders on the register."),
    "ROW": (4, "Net interest paid (1,588) (1,479) (7%)"),
    "CELL": (4, "(1,588)"),
    "X": (5, "Following a full review, the Board concluded that the Group’s capital allocation framework remained appropriate "
             "for the delivery of our five-year plan, with headroom against our credit rating thresholds."),
    "L": (6, "Operating profit before exceptional items rose by 12% to £2,946 million, supported by strong performance in the UK "
             "Electricity Transmission segment and favourable foreign exchange movements."),
    "Z": (7, "This report has been approved by the Board of Directors and is signed on its behalf by the Company Secretary."),
}


@dataclass(frozen=True)
class ReportPdf:
    data: bytes
    page_count: int
    passages: dict[str, tuple[int, str]]


def _page_cover(c: canvas.Canvas) -> None:
    c.setFont("VeraBd", 30)
    c.drawString(50, H - 300, "Annual Report 2024/25")
    c.setFont("Vera", 14)
    c.drawString(50, H - 330, "Powering a cleaner future")
    c.showPage()


def _page_contents(c: canvas.Canvas) -> None:
    c.setFont("VeraBd", 18)
    c.drawString(50, H - 90, "Contents")
    lines_at(c, 50, 130, ["Strategic report", "Financial review", "Financial statements", "Notes to the accounts"], size=12, lead=20)
    c.showPage()


def _page_narrative(c: canvas.Canvas) -> None:
    header(c, "Strategic report")
    c.setFont("VeraBd", 16)
    c.drawString(LEFT_X, H - 70, "Group financial review – our “performance”")
    y = lines_at(c, LEFT_X, 90, ["Capital investment in our networks reached £4.1 billion", "in 2024/25, driven by the infra-",
                                 "structure programme in the UK and the", "“Climate Leadership” Act in New York. Our", "net-",
                                 "work reliability was 99.99% across all regions."]) + 8
    y = lines_at(c, LEFT_X, y, ["Our efficient ﬁnancial workﬂow improved the", "net debt position; the ﬁnal dividend",
                                "was 31.36 pence per share (2023/24: 30.82 pence)."]) + 8
    lines_at(c, LEFT_X, y, ["Underlying operating profit — adjusted for timing —", "increased by 6% to £6,689 million, whilst",
                            "the Group’s ‘core’ earnings per share rose."])
    y = lines_at(c, RIGHT_X, 90, ["Cash generated from continuing operations was", "£6,991 million in 2024/25, down 4% from",
                                  "£7,281 million, primarily due to the timing of", "working capital movements and lower receipts",
                                  "from our regulated businesses in the year."]) + 8
    lines_at(c, RIGHT_X, y, ["Net debt at the end of the year was £41,371", "million (2023/24: £43,607 million). Interest",
                             "paid of (1,588) million is shown net of capital", "interest and includes hedging."])
    # a passage that crosses from the bottom of the left column to the right column (drawn consecutively)
    lines_at(c, LEFT_X, 650, ["The Board has recommended a final dividend of", "31.36 pence per share, which, subject to"])
    lines_at(c, RIGHT_X, 450, ["shareholder approval at the AGM, will be paid", "on 12 September 2025 to holders on the register."])
    footer(c, str(3 - FOLIO_OFFSET))
    c.showPage()


def _page_table(c: canvas.Canvas) -> None:
    header(c, "Financial review")
    c.setFont("VeraBd", 14)
    c.drawString(50, H - 55, "Summary cash flow statement")
    # right column is drawn BEFORE the left one: stream order != reading order
    lines_at(c, RIGHT_X, 90, ["Net debt reconciliation shows that the net", "cash outflow (continuing) of £(6,885) million",
                              "was partly offset by disposals of £1,263 million."])
    lines_at(c, LEFT_X, 90, ["The Group’s cash generated from operations", "remained resilient despite macro-economic",
                             "headwinds and higher interest rates through-", "out the reporting period."])
    edges = [50, 300, 380, 460, 540]
    rows = [("£m", "2024/25", "2023/24", "Change"),
            ("Cash generated from continuing operations", "6,991", "7,281", "(4%)"),
            ("Net interest paid", "(1,588)", "(1,479)", "(7%)"),
            ("Net tax paid", "(183)", "(342)", "46%"),
            ("Dividends paid", "(1,529)", "(1,718)", "11%"),
            ("Net cash outflow (continuing)", "(6,885)", "(61%)", "n/m"),
            ("Net debt at end of year", "(41,371)", "(43,607)", "5%")]
    for i, row in enumerate(rows):
        font = "VeraBd" if i == 0 else "Vera"
        c.setFont(font, 8.5)
        base = H - (220 + i * 16 + 8.5)
        c.drawString(edges[0], base, row[0])
        for k in range(1, 4):
            c.drawString(edges[k + 1] - pdfmetrics.stringWidth(row[k], font, 8.5), base, row[k])
    lines_at(c, 50, 500, ["1. Disposals of £(143) million were excluded from the cash flow, see note 29.",
                          "2. Interest paid includes £(1,588) million of the FY25 charge."], size=7.5, lead=9.5)
    footer(c, str(4 - FOLIO_OFFSET))
    c.showPage()


def _page_trap(c: canvas.Canvas) -> None:
    header(c, "Financial review")
    c.setFont("VeraBd", 16)
    c.drawString(50, H - 60, "Capital allocation framework")
    lines_at(c, RIGHT_X, 90, ["allocation framework remained appropriate for", "the delivery of our five-year plan, with headroom",
                              "against our credit rating thresholds."])        # drawn first, read last
    lines_at(c, LEFT_X, 90, ["Introductory paragraph of the left", "column that is not part of the", "flowing sentence at all."])
    lines_at(c, LEFT_X, 300, ["Following a full review, the Board concluded", "that the Group’s capital"])
    footer(c, str(5 - FOLIO_OFFSET))
    c.showPage()


def _page_landscape(c: canvas.Canvas) -> None:
    """Portrait MediaBox + /Rotate 90, content drawn so that it reads upright once displayed (how landscape tables ship)."""
    c.setPageSize((H, W))           # reportlab swaps the MediaBox of a rotated page back to portrait
    c.setPageRotation(90)
    c.saveState()
    c.translate(W, 0)
    c.rotate(90)                    # canvas is now H wide, W tall
    vw, vh = H, W
    c.setFont("VeraBd", 16)
    c.drawString(60, vh - 60, "Landscape page (rotated 90 via /Rotate)")
    c.setFont("Vera", FS)
    for i, ln in enumerate(["Operating profit before exceptional items rose by 12% to", "£2,946 million, supported by strong performance in the",
                            "UK Electricity Transmission segment and favourable", "foreign exchange movements."]):
        c.drawString(60, vh - (100 + FS + i * LEAD), ln)
    for i, ln in enumerate(["Capital expenditure was £8.3 billion (2023/24:", "£7.8 billion) as we continued to invest in",
                            "energy network reinforcement."]):
        c.drawString(440, vh - (100 + FS + i * LEAD), ln)
    c.setFont("Vera", 8)
    c.drawString(50, 36, "Annual Report 2024/25")
    c.drawRightString(vw - 50, 36, str(6 - FOLIO_OFFSET))
    c.restoreState()
    c.showPage()
    c.setPageSize(A4)
    c.setPageRotation(0)


def _page_closing(c: canvas.Canvas) -> None:
    header(c, "Governance")
    lines_at(c, LEFT_X, 120, ["This report has been approved by the Board of Directors and is signed", "on its behalf by the Company Secretary."])
    footer(c, str(7 - FOLIO_OFFSET))
    c.showPage()


def build_report_pdf() -> ReportPdf:
    buf = io.BytesIO()
    c = _canvas(buf)
    c.setTitle("ReportLens test annual report")
    for page in (_page_cover, _page_contents, _page_narrative, _page_table, _page_trap, _page_landscape, _page_closing):
        page(c)
    c.save()
    return ReportPdf(buf.getvalue(), 7, PASSAGES)


# --------------------------------------------------------------------------------------- other generators


def build_text_pdf(pages: Sequence[Sequence[str]], folios: Optional[Sequence[Optional[str]]] = None, *, page_labels: Optional[list] = None,
                   encrypt: Optional[StandardEncryption] = None, folio_style: Optional[str] = None) -> bytes:
    """One text block per page (lines top-down at x=50); optional footer folios (`folio_style` = one footer string with a
    {folio} placeholder instead of the default "report name ... folio" pair) and /PageLabels
    (`page_labels` = [(page_index, style, start, prefix), ...] as accepted by Canvas.addPageLabel; style: "ARABIC", "ROMAN_LOWER", "ROMAN_UPPER", "LETTERS_LOWER", "LETTERS_UPPER")."""
    buf = io.BytesIO()
    c = _canvas(buf, encrypt=encrypt)
    for i, lines in enumerate(pages):
        lines_at(c, 50, 100, lines)
        if folios is not None and folio_style and folios[i]:
            c.setFont("Vera", 8)
            c.drawString(50, 36, folio_style.format(folio=folios[i]))
        elif folios is not None:
            footer(c, folios[i])
        c.showPage()
    for label in page_labels or []:
        c.addPageLabel(*label)
    c.save()
    return buf.getvalue()


def build_blank_pdf(page_count: int = 3) -> bytes:
    """Pages with vector graphics only: no text layer, like a scan."""
    buf = io.BytesIO()
    c = _canvas(buf)
    for _ in range(page_count):
        c.rect(100, 100, 300, 400, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


def build_encrypted_pdf(user_password: str = "secret") -> bytes:
    return build_text_pdf([["This document needs a password to open."]], encrypt=StandardEncryption(user_password, ownerPassword="owner"))


def build_geometry_pdf() -> bytes:
    """Four pages with ONE isolated line each, drawn at the same raw spot, then rotated / cropped differently:
    /Rotate 90 + CropBox, /Rotate 180, /Rotate 270, CropBox only.  The visible position is checked by rendering."""
    buf = io.BytesIO()
    c = _canvas(buf)
    for rotation, crop in ((90, (30, 100, 560, 600)), (180, None), (270, None), (0, (30, 100, 560, 600))):
        c.setPageSize((H, W) if rotation in (90, 270) else A4)       # keeps the raw MediaBox portrait in every case
        c.setPageRotation(rotation)
        c.setCropBox(crop)
        c.setFont("VeraBd", 14)
        c.drawString(120, 400, "The quick brown fox jumps over the lazy dog 12345.")
        c.showPage()
    c.save()
    return buf.getvalue()


COLUMN_MAJOR_ROWS = [("Revenue", "17,813", "16,102"), ("Cost of sales", "(9,204)", "(8,771)"), ("Gross profit", "8,609", "7,331"),
                     ("Distribution costs", "(1,120)", "(1,044)"), ("Administrative expenses", "(2,310)", "(2,198)"),
                     ("Operating profit", "4,512", "4,001"), ("Finance income", "310", "287"), ("Finance costs", "(1,276)", "(1,190)"),
                     ("Profit before tax", "3,546", "3,098"), ("Tax charge", "(812)", "(704)"), ("Profit for the year", "2,734", "2,394"),
                     ("Dividends paid", "(1,529)", "(1,718)"), ("Net debt", "(41,371)", "(43,607)"), ("Capital expenditure", "(3,912)", "(3,640)")]


def build_column_major_table_pdf() -> bytes:
    """A table whose cells are drawn column by column (all labels, then all current-year values, then all prior-year values):
    stream order is not row order, only the geometry puts a label next to its numbers."""
    buf = io.BytesIO()
    c = _canvas(buf)
    c.setFont("VeraBd", 12)
    c.drawString(50, H - 60, "Group income statement")
    c.setFont("Vera", 9)
    for col, (x, right_aligned) in enumerate(((50, False), (380, True), (460, True))):
        for i, row in enumerate(COLUMN_MAJOR_ROWS):
            base = H - (100 + i * 18)
            if right_aligned:
                c.drawRightString(x, base, row[col])
            else:
                c.drawString(x, base, row[col])
    c.showPage()
    c.save()
    return buf.getvalue()


_VOCAB = ("revenue operating profit margin dividend capital network regulated investment efficiency customers energy transmission "
          "distribution climate risk governance board committee audit remuneration shareholders growth transition emissions "
          "reliability safety people culture strategy performance outlook liquidity borrowings pension provisions impairment "
          "segment adjusted underlying statutory reported").split()


def build_big_pdf(page_count: int = 300, seed: int = 7) -> tuple[bytes, list[list[str]]]:
    """A long dense report: every page has 36 lines of seeded random prose with page-specific figures.  Returns (pdf, lines per page)."""
    rng = random.Random(seed)
    buf = io.BytesIO()
    c = _canvas(buf)
    all_lines: list[list[str]] = []
    for p in range(page_count):
        lines = []
        for k in range(36):
            words = [rng.choice(_VOCAB) for _ in range(9)]
            words[rng.randrange(9)] = f"£{rng.randrange(100, 9999)},{rng.randrange(100, 999)}m"
            words[0] = words[0].capitalize()
            lines.append(" ".join(words) + f" ({p + 1}.{k + 1})")
        lines_at(c, 40, 60, lines, size=9, lead=19)
        footer(c, str(p + 1 - FOLIO_OFFSET) if p >= FOLIO_OFFSET else None)
        all_lines.append(lines)
        c.showPage()
    c.save()
    return buf.getvalue(), all_lines
