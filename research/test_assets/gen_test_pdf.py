"""Generate a 3-page hard-case test PDF + ground-truth JSON.

Page 1: two columns, hyphenated line ends, curly quotes, ligatures (U+FB01 etc), em dashes, GBP.
Page 2: financial table (right aligned numbers incl. (1,588)), RIGHT column drawn BEFORE left column
        (content stream order != reading order) + a footnote.
Page 3: plain page that we then rotate 90deg and give an offset CropBox with PyMuPDF (page geometry test).

Ground truth: for each named passage, the page (1-based), and the list of (x0, y0_top, x1, y1_bottom)
line rects in *unrotated top-left PDF-point coordinates* (as drawn, before any rotation / cropbox).
"""
import json
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONTS = Path("C:/Windows/Fonts")
pdfmetrics.registerFont(TTFont("Tm", str(FONTS / "times.ttf")))
pdfmetrics.registerFont(TTFont("TmB", str(FONTS / "timesbd.ttf")))
pdfmetrics.registerFont(TTFont("Ar", str(FONTS / "arial.ttf")))
pdfmetrics.registerFont(TTFont("ArB", str(FONTS / "arialbd.ttf")))

W, H = A4  # 595.27 x 841.89
TRUTH = {"passages": {}}

FS = 10.5
LEAD = 13.5


def rec(name, page, x0, ytop, x1, ybot):
    TRUTH["passages"].setdefault(name, {"page": page, "lines": []})["lines"].append(
        [round(x0, 2), round(ytop, 2), round(x1, 2), round(ybot, 2)])


def draw_lines(c, page, name, x, y_top, lines, font="Tm", size=FS, lead=LEAD):
    """Draw explicit lines starting at y_top (top-left coords). Records line rects under `name`
    (name may be None). Returns new y_top."""
    c.setFont(font, size)
    for ln in lines:
        base = H - (y_top + size)          # baseline in PDF coords
        c.drawString(x, base, ln)
        w = pdfmetrics.stringWidth(ln, font, size)
        if name:
            rec(name, page, x, y_top + 1.5, x + w, y_top + size + 2.5)
        y_top += lead
    return y_top


# --------------------------------------------------------------------------- page 1
LEFT_X, RIGHT_X, COL_W = 50, 310, 235


def page1(c):
    c.setFont("ArB", 18)
    c.drawString(50, H - 60, "Group financial review \u2013 our \u201cperformance\u201d")
    # left column: passage A has hyphenation: "infra-" / "structure", "net-" / "work"
    A = [
        "Capital investment in our networks reached \u00a34.1 billion",
        "in 2024/25, driven by the infra-",
        "structure programme in the UK and the",
        "\u201cClimate Leadership\u201d Act in New York. Our",
        "net-",
        "work reliability was 99.99% across all regions.",
    ]
    y = draw_lines(c, 1, "p1_A", LEFT_X, 90, A)
    y += 8
    # ligatures: ﬁ ﬂ ﬃ  (efficient, workflow, offi-cial). Use real ligature code points
    B = [
        "Our e\ufb03cient \ufb01nancial work\ufb02ow improved the",
        "o\ufb03cial net debt position; the \ufb01nal dividend",
        "was 31.36 pence per share (2023/24: 30.82 pence).",
    ]
    y = draw_lines(c, 1, "p1_B", LEFT_X, y, B)
    y += 8
    C = [
        "Underlying operating profit \u2014 adjusted for timing \u2014",
        "increased by 6% to \u00a36,689 million, whilst",
        "the Group\u2019s \u2018core\u2019 earnings per share rose.",
    ]
    y = draw_lines(c, 1, "p1_C", LEFT_X, y, C)

    # right column: a paragraph whose first words continue "same sentence" from left bottom
    D = [
        "Cash generated from continuing operations was",
        "\u00a36,991 million in 2024/25, down 4% from",
        "\u00a37,281 million, primarily due to the timing of",
        "working capital movements and lower receipts",
        "from our regulated businesses in the year.",
    ]
    y = draw_lines(c, 1, "p1_D", RIGHT_X, 90, D)
    y += 8
    E = [
        "Net debt at the end of the year was \u00a341,371",
        "million (2023/24: \u00a343,607 million). Interest",
        "paid of (1,588) million is shown net of capital",
        "interest and includes hedging.",
    ]
    draw_lines(c, 1, "p1_E", RIGHT_X, y, E)

    # A passage that CROSSES from the bottom of the left column to the top of the right column
    F_left = ["The Board has recommended a final dividend of",
              "31.36 pence per share, which, subject to"]
    F_right = ["shareholder approval at the AGM, will be paid",
               "on 12 September 2025 to holders on the register."]
    yl = draw_lines(c, 1, "p1_F", LEFT_X, 700, F_left)
    # right column continues at top of right column is at y=90.. we place it at y=500 on the right instead
    draw_lines(c, 1, "p1_F", RIGHT_X, 500, F_right)
    # filler so the right col has stuff above F_right
    draw_lines(c, 1, None, RIGHT_X, 420, ["(continued from previous column)"], font="Tm", size=9)
    c.showPage()


def page2(c):
    # Right column drawn first (stream order trap)
    c.setFont("ArB", 14)
    c.drawString(50, H - 55, "Summary cash flow statement")
    R = [
        "Net debt reconciliation shows that the net",
        "cash outflow (continuing) of \u00a3(6,885) million",
        "was partly offset by disposals of \u00a31,263 million.",
    ]
    draw_lines(c, 2, "p2_R", RIGHT_X, 90, R)
    L = [
        "The Group\u2019s cash generated from operations",
        "remained resilient despite macro-economic",
        "headwinds and higher interest rates through-",
        "out the reporting period.",
    ]
    draw_lines(c, 2, "p2_L", LEFT_X, 90, L)

    # Table: header + rows (Arial 8) with right aligned numbers
    top = 220
    cols = [50, 300, 380, 460, 540]   # label x, then right edges for numbers
    rows = [
        ("\u00a3m", "2024/25", "2023/24", "Change"),
        ("Cash generated from continuing operations", "6,991", "7,281", "(4%)"),
        ("Net interest paid", "(1,588)", "(1,479)", "(7%)"),
        ("Net tax paid", "(183)", "(342)", "46%"),
        ("Dividends paid", "(1,529)", "(1,718)", "11%"),
        ("Net cash outflow (continuing)", "(6,885)", "(61%)", "n/m"),
        ("Net debt at end of year", "(41,371)", "(43,607)", "5%"),
    ]
    c.setFont("Ar", 8.5)
    for r_i, row in enumerate(rows):
        font = "ArB" if r_i == 0 else "Ar"
        c.setFont(font, 8.5)
        y_top = top + r_i * 16
        base = H - (y_top + 8.5)
        c.drawString(cols[0], base, row[0])
        lw = pdfmetrics.stringWidth(row[0], font, 8.5)
        for k in range(1, 4):
            txt = row[k]
            tw = pdfmetrics.stringWidth(txt, font, 8.5)
            c.drawString(cols[k + 1] - tw, base, txt)
        rec(f"p2_row{r_i}", 2, cols[0], y_top + 1, cols[4], y_top + 11)
        # separators
        c.setLineWidth(0.3)
        c.line(50, H - (y_top + 13), 545, H - (y_top + 13))
    # Truth for a "row" = whole row line; for individual cell test: row 2 interest cell
    base2_top = top + 2 * 16
    txt = "(1,588)"
    tw = pdfmetrics.stringWidth(txt, "Ar", 8.5)
    rec("p2_cell_interest", 2, cols[2] - tw, base2_top + 1, cols[2], base2_top + 11)

    foot = [
        "1. Disposals of \u00a3(143) million were excluded from the cash flow, see note 29.",
        "2. Interest paid includes \u00a3(1,588) million of the FY25 charge.",
    ]
    draw_lines(c, 2, "p2_foot", 50, 500, foot, size=7.5, lead=9.5)
    c.showPage()


def page3(c):
    c.setFont("ArB", 14)
    c.drawString(50, H - 55, "Geometry test page (rotated + cropbox)")
    G = [
        "This page is rotated by ninety degrees and has an",
        "offset crop box, so naive coordinates will be wrong.",
        "The quick brown fox jumps over the lazy dog 12345.",
    ]
    draw_lines(c, 3, "p3_G", 120, 200, G)
    c.showPage()


def page4(c):
    """Stream order trap: right column drawn BEFORE left; the sentence flows left-bottom -> right-top.
    Full-width heading on top."""
    c.setFont("ArB", 16)
    c.drawString(50, H - 60, "Capital allocation framework")
    R = [
        "allocation framework remained appropriate for",
        "the delivery of our five-year plan, with headroom",
        "against our credit rating thresholds.",
    ]
    draw_lines(c, 4, "p4_X", RIGHT_X, 90, R)           # drawn first (stream order)
    Lc = [
        "Following a full review, the Board concluded",
        "that the Group’s capital",
    ]
    # make the left column long, so its bottom lines sit at y ~ 90+... ; place the flowing lines at the bottom
    draw_lines(c, 4, None, LEFT_X, 90, ["Introductory paragraph of the left column that is", "not part of the flowing sentence at all."])
    draw_lines(c, 4, "p4_X", LEFT_X, 300, Lc)
    # move right column's first lines visually to top: they are at y=90 (top of right column)
    c.showPage()


def page5(c):
    """Realistic landscape page: portrait MediaBox + /Rotate 90, content drawn so that it reads upright
    when displayed (this is how many annual reports ship landscape tables).  Truth is in VISIBLE space."""
    c.setPageRotation(90)
    c.saveState()
    c.translate(W, 0)
    c.rotate(90)           # now drawing on a landscape canvas of size (H wide, W tall), origin bottom-left
    VW, VH = H, W          # visible size
    def vline(txt, x, y_top, font="Tm", size=FS, name=None):
        base = VH - (y_top + size)
        c.setFont(font, size)
        c.drawString(x, base, txt)
        wdt = pdfmetrics.stringWidth(txt, font, size)
        if name:
            TRUTH["passages"].setdefault(name, {"page": 5, "visible": True, "lines": []})["lines"].append(
                [round(x, 2), round(y_top + 1.5, 2), round(x + wdt, 2), round(y_top + size + 2.5, 2)])
    c.setFont("ArB", 16)
    c.drawString(60, VH - 60, "Landscape page (rotated 90 via /Rotate)")
    L1 = ["Operating profit before exceptional items rose by 12% to",
          "£2,946 million, supported by strong performance in the",
          "UK Electricity Transmission segment and favourable",
          "foreign exchange movements."]
    for i, t in enumerate(L1):
        vline(t, 60, 100 + i * LEAD, name="p5_L")
    R1 = ["Capital expenditure was £8.3 billion (2023/24:",
          "£7.8 billion) as we continued to invest in",
          "energy network reinforcement."]
    for i, t in enumerate(R1):
        vline(t, 440, 100 + i * LEAD, name="p5_R")
    c.restoreState()
    c.showPage()


def main(out):
    c = canvas.Canvas(out, pagesize=A4)
    c.setTitle("Quote locator test")
    page1(c)
    page2(c)
    page3(c)
    page4(c)
    c.save()
    return out


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "test3.pdf"
    main(out)
    Path(out).with_suffix(".truth.json").write_text(json.dumps(TRUTH, indent=1))
    print("wrote", out)
