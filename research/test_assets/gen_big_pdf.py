"""Synthetic 300-page annual-report-like PDF for speed benchmarks (2 columns dense text + tables)."""
import random
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONTS = Path("C:/Windows/Fonts")
pdfmetrics.registerFont(TTFont("Tm", str(FONTS / "times.ttf")))
pdfmetrics.registerFont(TTFont("Ar", str(FONTS / "arial.ttf")))
pdfmetrics.registerFont(TTFont("ArB", str(FONTS / "arialbd.ttf")))

W, H = A4
random.seed(7)
VOCAB = ("capital investment network regulated revenue operating profit adjusted underlying group "
         "dividend share pence million billion cash flow debt interest tax disposal asset growth "
         "transmission distribution electricity gas customers reliability decarbonisation climate "
         "board governance remuneration committee director audit risk strategy sustainability "
         "emissions scope reduction target statutory result increase decrease year period "
         "compared principally driven timing movements receipts payments UK US New York "
         "infrastructure programme efficiency financial workflow official").split()


def make_line(max_w, size):
    words = []
    while True:
        w = random.choice(VOCAB)
        if random.random() < 0.08:
            w = f"{random.randint(1, 9)},{random.randint(100, 999)}"
        cand = " ".join(words + [w])
        if pdfmetrics.stringWidth(cand, "Tm", size) > max_w:
            break
        words.append(w)
    return " ".join(words)


def main(out, pages=300):
    c = canvas.Canvas(out, pagesize=A4)
    size, lead = 8.5, 10.2
    col_w = 240
    for p in range(1, pages + 1):
        c.setFont("ArB", 12)
        c.drawString(50, H - 50, f"Section {p // 20 + 1} - page {p}")
        # left col
        for col_x in (50, 310):
            c.setFont("Tm", size)
            y = H - 80
            n = 0
            while y > 60:
                c.drawString(col_x, y, make_line(col_w, size))
                y -= lead
                n += 1
                if n % 12 == 0:
                    y -= 6
        if p % 5 == 0:   # a table overlay region at the bottom
            c.setFont("Ar", 7)
            y = 200
            for r in range(8):
                c.drawString(50, y, f"Row {r} metric")
                for k, x in enumerate((350, 420, 490, 550)):
                    t = f"({random.randint(1, 9)},{random.randint(100, 999)})"
                    c.drawString(x - pdfmetrics.stringWidth(t, "Ar", 7), y, t)
                y -= 9
        c.showPage()
    c.save()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "big300.pdf")
    print("ok")
