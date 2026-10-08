import sys
import pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import quote_locator_prototype as ql
import pypdfium2 as pdfium
from PIL import Image, ImageDraw

cases = [
 ("test5.pdf", 1, "The Board has recommended a final dividend of 31.36 pence per share, which, subject to shareholder approval at the AGM, will be paid on 12 September 2025 to holders on the register."),
 ("test5.pdf", 1, "infrastructure programme in the UK and the \"Climate Leadership\" Act in New York. Our network reliability was 99.99%"),
 ("test5.pdf", 2, "Net interest paid (1,588) (1,479) (7%)"),
 ("test5.pdf", 4, "Following a full review, the Board concluded that the Group's capital allocation framework remained appropriate"),
 ("test5.pdf", 5, "supported by strong performance in the UK Electricity Transmission segment and favourable foreign exchange movements"),
 ("test5_geom.pdf", 3, "The quick brown fox jumps over the lazy dog 12345."),
 ("test5.pdf", 1, "Cash generated from continuing operations ... regulated businesses in the year."),
]
tiles = []
for path, pg, quote in cases:
    pdf = pdfium.PdfDocument(path)
    pw = ql.pdfium_page_words(pdf, pg - 1)
    res = ql.locate_in_page(pw, quote)
    page = pdf[pg - 1]
    scale = 1.0
    img = page.render(scale=scale).to_pil().convert("RGBA")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for r in res.rects:
        x0, y0 = r["x"] * img.width, r["y"] * img.height
        d.rectangle([x0, y0, x0 + r["w"] * img.width, y0 + r["h"] * img.height], fill=(255, 214, 0, 110), outline=(230, 60, 0, 255))
    img = Image.alpha_composite(img, ov).convert("RGB")
    # crop to bounding area of rects (+margin) for legibility
    if res.rects:
        xs0 = min(r["x"] for r in res.rects); ys0 = min(r["y"] for r in res.rects)
        xs1 = max(r["x"] + r["w"] for r in res.rects); ys1 = max(r["y"] + r["h"] for r in res.rects)
        m = 25
        box = (max(0, int(xs0 * img.width) - m), max(0, int(ys0 * img.height) - m), min(img.width, int(xs1 * img.width) + m), min(img.height, int(ys1 * img.height) + m))
        img = img.crop(box)
    print(path, pg, res.method, round(res.score, 2), res.order, len(res.rects), "rects;", res.matched_text[:70])
    tiles.append(img)
W = max(t.width for t in tiles); H = sum(t.height + 8 for t in tiles)
sheet = Image.new("RGB", (W, H), "white")
y = 0
for t in tiles:
    sheet.paste(t, (0, y)); y += t.height + 8
sheet.save("visual_check.png"); print(sheet.size)
