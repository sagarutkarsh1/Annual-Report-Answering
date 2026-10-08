"""Append a realistic landscape page (portrait MediaBox + /Rotate 90, text drawn rotated so it reads upright)
and a rotated+cropped geometry variant of page 3.  Writes test5.pdf and test5_geom.pdf and updates truth."""
import json, pymupdf
H = 841.8897705078125; W = 595.2755737304688
doc = pymupdf.open("test4.pdf")
page = doc.new_page(width=W, height=H)
fn = "tiro"   # built-in Times Roman
TRUTH = json.load(open("test4.truth.json"))
def vline(txt, vx, vy_base, name):
    page.insert_text((vy_base, H - vx), txt, fontname=fn, fontsize=10.5, rotate=90)
vlines = [
 ("Landscape page (rotated 90 via /Rotate)", 60, 60, None),
 ("Operating profit before exceptional items rose by 12% to", 60, 100, "p5_L"),
 ("\u00a32,946 million, supported by strong performance in the", 60, 113.5, "p5_L"),
 ("UK Electricity Transmission segment and favourable", 60, 127, "p5_L"),
 ("foreign exchange movements.", 60, 140.5, "p5_L"),
 ("Capital expenditure was \u00a38.3 billion (2023/24:", 440, 100, "p5_R"),
 ("\u00a37.8 billion) as we continued to invest in", 440, 113.5, "p5_R"),
 ("energy network reinforcement.", 440, 127, "p5_R"),
]
for t, vx, vy, nm in vlines:
    vline(t, vx, vy, nm)
page.set_rotation(90)
print("page5 rect", page.rect, "rotation", page.rotation)
doc.save("test5.pdf")
d2 = pymupdf.open("test5.pdf")
pg = d2[4]
pix = pg.get_pixmap(dpi=72); pix.save("page5.png")
print(repr(pg.get_text("text")))
p = d2[2]; p.set_cropbox(pymupdf.Rect(30,100,500,600)); p.set_rotation(90); d2.save("test5_geom.pdf")
json.dump(TRUTH, open("test5.truth.json","w"), indent=1)
