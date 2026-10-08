"""
Quick test for quote_locator_prototype.py  (run:  python quote_locator_test.py [test5.pdf] [test5_geom.pdf] [big300.pdf])

1. Backend agreement: PyMuPDF vs pypdfium2 vs pdfplumber word boxes on the same pages (incl. rotated+cropped page).
2. Locator accuracy on LLM-style quote variants (hyphenation, curly quotes, ligatures, numbers, dropped /
   replaced words, ellipsis, cross-column, stream-order trap, wrong page hint).
3. Geometry check of returned rects against ground truth line boxes written by gen_test_pdf.py.

The PDFs are produced by gen_test_pdf.py (reportlab) - see the research notes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
import quote_locator_prototype as ql  # noqa: E402

ASSETS = Path(__file__).parent / "test_assets"
PDF = sys.argv[1] if len(sys.argv) > 1 else str(ASSETS / "test5.pdf")
PDF_GEOM = sys.argv[2] if len(sys.argv) > 2 else str(ASSETS / "test5_geom.pdf")
BIG = sys.argv[3] if len(sys.argv) > 3 else str(ASSETS / "big300.pdf")   # python test_assets/gen_big_pdf.py test_assets/big300.pdf
BACKENDS = ["pymupdf", "pdfium", "pdfplumber"]

# clean (human-readable) text of each passage = the "expected highlight"
P = {
    "A": (1, "Capital investment in our networks reached £4.1 billion in 2024/25, driven by the infrastructure programme in the UK and the “Climate Leadership” Act in New York. Our network reliability was 99.99% across all regions."),
    "B": (1, "Our efficient financial workflow improved the official net debt position; the final dividend was 31.36 pence per share (2023/24: 30.82 pence)."),
    "C": (1, "Underlying operating profit — adjusted for timing — increased by 6% to £6,689 million, whilst the Group’s ‘core’ earnings per share rose."),
    "D": (1, "Cash generated from continuing operations was £6,991 million in 2024/25, down 4% from £7,281 million, primarily due to the timing of working capital movements and lower receipts from our regulated businesses in the year."),
    "F": (1, "The Board has recommended a final dividend of 31.36 pence per share, which, subject to shareholder approval at the AGM, will be paid on 12 September 2025 to holders on the register."),
    "ROW": (2, "Net interest paid (1,588) (1,479) (7%)"),
    "CELL": (2, "(1,588)"),
    "L5": (5, "Operating profit before exceptional items rose by 12% to £2,946 million, supported by strong performance in the UK Electricity Transmission segment and favourable foreign exchange movements."),
    "X": (4, "Following a full review, the Board concluded that the Group’s capital allocation framework remained appropriate for the delivery of our five-year plan, with headroom against our credit rating thresholds."),
}


def variants(page: int, clean: str) -> list[tuple[str, int, str, str | None]]:
    """(label, hinted_page, quote, expected_highlight_text) perturbations an LLM / extractor mismatch might produce.
    expected None == nothing on the page should match (paraphrase)."""
    w = clean.split()
    n = len(w)
    return [
        ("verbatim", page, clean, clean),
        ("whitespace-collapsed/newlines", page, clean.replace(" ", "\n  "), clean),
        ("straight quotes/apostrophes", page, clean.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'"), clean),
        ("UPPER case", page, clean.upper(), clean),
        ("em-dash -> hyphen, no spaces", page, clean.replace(" — ", "-"), clean),
        ("numbers: strip thousands commas", page, clean.replace(",", ""), clean),
        ("drop 2 middle words", page, " ".join(w[:n // 2] + w[n // 2 + 2:]), clean),
        ("replace 2 words", page, " ".join(w[:3] + ["banana", "orange"] + w[5:]), clean),
        ("truncate head 4 words", page, " ".join(w[4:]), " ".join(w[4:])),
        ("truncate tail 4 words", page, " ".join(w[:-4]), " ".join(w[:-4])),
        ("extra words before/after", page, "As stated in the report, " + clean + " This is notable.", clean),
        ("ellipsis in middle", page, " ".join(w[:4]) + " ... " + " ".join(w[-5:]), " ".join(w[:4] + w[-5:])),
        ("first half only", page, " ".join(w[:n // 2]), " ".join(w[:n // 2])),
        ("wrong page hint (+1/-1)", page + 1 if page < 4 else page - 1, clean, clean),
        ("paraphrase (unmatchable)", page, "The company said that money coming from its day to day activities fell slightly compared with the prior year", None),
    ]


def squash(t):  # noqa: D401
    return ql.squash(t)


def quality(result: ql.LocateResult, expected: str | None) -> float:
    """char-level ratio between highlighted text and the expected span (1.0 perfect).  For `expected is None`
    the best answer is method page/block with low score -> returns 1.0 if no precise match claimed."""
    if expected is None:
        return 1.0 if result.method in ("page", "block") else 0.0
    if result.method == "page":
        return 0.0
    return fuzz.ratio(squash(result.matched_text.replace(" ... ", " ")), squash(expected)) / 100


# ----------------------------------------------------------------------------------------------- backends


class Doc:
    def __init__(self, backend: str, path: str):
        self.backend, self.path = backend, path
        if backend == "pymupdf":
            import pymupdf
            self.d = pymupdf.open(path)
            self.page_count = self.d.page_count
        elif backend == "pdfium":
            import pypdfium2 as pdfium
            self.d = pdfium.PdfDocument(Path(path).read_bytes())
            self.page_count = len(self.d)
        else:
            import pdfplumber
            self.d = pdfplumber.open(path)
            self.page_count = len(self.d.pages)
        self._c: dict[int, ql.PageWords] = {}

    def words(self, p: int) -> ql.PageWords:
        if p not in self._c:
            if self.backend == "pymupdf":
                self._c[p] = ql.pymupdf_page_words(self.d[p - 1])
            elif self.backend == "pdfium":
                self._c[p] = ql.pdfium_page_words(self.d, p - 1)
            else:
                self._c[p] = ql.pdfplumber_page_words(self.d.pages[p - 1])
        return self._c[p]


def backend_agreement():
    print("\n== 1. backend agreement (visible-space word boxes, points) ==")
    for path in (PDF, PDF_GEOM):
        docs = {b: Doc(b, path) for b in BACKENDS}
        for pg in (1, 3, 5):
            ref = docs["pymupdf"].words(pg)
            print(f"{Path(path).name} page {pg}: visible size  " +
                  "  ".join(f"{b}={docs[b].words(pg).width:.1f}x{docs[b].words(pg).height:.1f}" for b in BACKENDS))
            ref_map = {(w.text, round(w.x0 / 20), round(w.y0 / 20)): w for w in ref.words}
            for b in ("pdfium", "pdfplumber"):
                other = docs[b].words(pg)
                diffs = []
                for w in other.words:
                    # nearest ref word with same squashed text
                    cands = [r for r in ref.words if ql.norm_token(r.text) == ql.norm_token(w.text) and ql.norm_token(w.text)]
                    if not cands:
                        continue
                    r = min(cands, key=lambda r: abs(r.x0 - w.x0) + abs(r.y0 - w.y0))
                    diffs.append((abs(r.x0 - w.x0), abs(r.x1 - w.x1), abs((r.y0 + r.y1) / 2 - (w.y0 + w.y1) / 2)))
                if diffs:
                    mx = [max(d[i] for d in diffs) for i in range(3)]
                    avg = [sum(d[i] for d in diffs) / len(diffs) for i in range(3)]
                    print(f"   {b:10s} vs pymupdf: {len(other.words)} words ({len(ref.words)} ref); "
                          f"mean |dx0|={avg[0]:.2f} |dx1|={avg[1]:.2f} |dyc|={avg[2]:.2f}  max={mx[0]:.1f}/{mx[1]:.1f}/{mx[2]:.1f}")
        for b, d in docs.items():
            if b == "pdfplumber":
                d.d.close()


def geometry_check(res: ql.LocateResult, truth_lines, pw: ql.PageWords) -> tuple[float, float]:
    """(line recall, rect precision) vs truth lines in visible points."""
    rects = [(r["x"] * pw.width, r["y"] * pw.height, (r["x"] + r["w"]) * pw.width, (r["y"] + r["h"]) * pw.height) for r in res.rects]
    rec = []
    for (tx0, ty0, tx1, ty1) in truth_lines:
        cov = 0.0
        for (x0, y0, x1, y1) in rects:
            vo = min(y1, ty1) - max(y0, ty0)
            if vo >= 0.5 * (ty1 - ty0):
                cov += max(0.0, min(x1, tx1) - max(x0, tx0))
        rec.append(min(1.0, cov / (tx1 - tx0)))
    prec = []
    for (x0, y0, x1, y1) in rects:
        prec.append(any(min(y1, ty1) - max(y0, ty0) >= 0.4 * (y1 - y0) and min(x1, tx1) - max(x0, tx0) > 0 for (tx0, ty0, tx1, ty1) in truth_lines))
    return sum(rec) / max(1, len(rec)), sum(prec) / max(1, len(prec))


def truth_lines(key, page, doc, truth, tkeys):
    """Ground-truth line boxes in visible points.  p5 passages are looked up with PyMuPDF search (landscape /Rotate page)."""
    if key == "L5":
        import pymupdf
        pg = pymupdf.open(PDF)[4]
        out = []
        for t in ["Operating profit before exceptional items rose by 12% to", "£2,946 million, supported by strong performance in the",
                  "UK Electricity Transmission segment and favourable", "foreign exchange movements."]:
            for r in pg.search_for(t):
                rr = r * pg.rotation_matrix
                out.append((rr.x0, rr.y0, rr.x1, rr.y1))
        return out
    return [tuple(l) for k in tkeys[key] for l in truth[k]["lines"]]


def run_suite():
    truth = json.loads(Path(PDF).with_suffix(".truth.json").read_text())["passages"]
    # p1_A..: map passage -> truth keys
    tkeys = {"A": ["p1_A"], "B": ["p1_B"], "C": ["p1_C"], "D": ["p1_D"], "F": ["p1_F"], "ROW": ["p2_row2"], "X": ["p4_X"], "L5": ["p5_L"]}
    for backend in BACKENDS:
        doc = Doc(backend, PDF)
        print(f"\n== 2/3. locator suite  backend={backend} ==")
        print(f"{'passage':7s} {'variant':32s} {'method':9s} {'score':>5s} {'qual':>5s} {'page':>4s} {'ord':7s} {'n':>2s}  geom(recall/prec)")
        tot = ok = 0
        t0 = time.perf_counter()
        for key, (page, clean) in P.items():
            for label, hint, quote, expected in variants(page, clean):
                if key == "CELL" and label not in ("verbatim", "straight quotes/apostrophes", "numbers: strip thousands commas", "wrong page hint (+1/-1)"):
                    continue   # one-token quote: perturbations are degenerate
                res = ql.locate(doc.words, doc.page_count, hint, quote, radius=1)
                q = quality(res, expected)
                geo = ""
                if key in tkeys and res.method in ("exact", "fuzzy") and label in ("verbatim", "straight quotes/apostrophes", "whitespace-collapsed/newlines"):
                    tl = truth_lines(key, res.page, doc, truth, tkeys)
                    pw = doc.words(res.page)
                    r_, p_ = geometry_check(res, tl, pw)
                    geo = f"{r_:.2f}/{p_:.2f}"
                good = q >= 0.8
                tot += 1
                ok += good
                print(f"{key:7s} {label:32s} {res.method:9s} {res.score:5.2f} {q:5.2f} {res.page:4d} {res.order:7s} {res.n_matches:2d}  {geo} {'' if good else '  <-- CHECK'}")
        print(f"summary[{backend}]: {ok}/{tot} variants OK in {time.perf_counter() - t0:.2f}s")


def geom_page_test():
    print("\n== geometry on rotated(90) + cropbox page (test5_geom.pdf page 3) ==")
    import pymupdf
    ref = pymupdf.open(PDF_GEOM)[2]
    q = "The quick brown fox jumps over the lazy dog 12345."
    for backend in BACKENDS:
        doc = Doc(backend, PDF_GEOM)
        res = ql.locate(doc.words, doc.page_count, 3, q)
        pw = doc.words(3)
        hit = ref.search_for("quick brown fox jumps over the lazy dog 12345.")[0] * ref.rotation_matrix
        # predicted bounding box (union of rects) in points
        xs = [(r["x"] * pw.width, r["y"] * pw.height, (r["x"] + r["w"]) * pw.width, (r["y"] + r["h"]) * pw.height) for r in res.rects]
        print(f"{backend:10s} method={res.method} score={res.score:.2f} visible={pw.width:.1f}x{pw.height:.1f} rects={len(res.rects)}"
              f"  first rect pts={tuple(round(v, 1) for v in xs[0]) if xs else None}   mupdf search_for(visible)={tuple(round(v,1) for v in hit)}")


def timing():
    print("\n== timing (one cold page, hinted page correct) ==")
    for backend in ("pymupdf", "pdfium"):
        path = BIG
        if not Path(path).exists():
            print("big300.pdf missing - skip")
            return
        doc = Doc(backend, path)
        words = doc.words(150)
        # take a 25-word passage from the middle of the page, perturbed
        mid = " ".join(w.text for w in words.words[300:325]).replace("-", " ")
        t = time.perf_counter()
        doc._c.clear()
        res = ql.locate(doc.words, doc.page_count, 150, mid)
        dt = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        res2 = ql.locate_in_page(words, "completely different text that is not on the page at all, banana orange")
        dt2 = (time.perf_counter() - t) * 1000
        print(f"{backend:8s} extract+locate (cold) {dt:6.1f} ms  method={res.method} score={res.score:.2f} | locate only (warm) miss-case {dt2:5.1f} ms method={res2.method}")


if __name__ == "__main__":
    backend_agreement()
    run_suite()
    geom_page_test()
    timing()
