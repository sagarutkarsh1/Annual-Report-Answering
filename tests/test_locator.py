"""locator: quote -> rects (exact / fuzzy / fragments / block / page), numeric guard, neighbours, claim alignment, geometry."""
from __future__ import annotations

import random
import threading
import time

import pypdfium2 as pdfium
import pytest

from reportlens.locator import PdfiumDoc, align_claim, locate_quote, norm_token, numbers_in, squash
from reportlens.pdfutil import PDFIUM_LOCK, PdfError
from tests import pdf_factory as pf


@pytest.fixture(scope="module")
def report() -> pf.ReportPdf:
    return pf.build_report_pdf()


@pytest.fixture(scope="module")
def doc(report):
    d = PdfiumDoc(report.data)
    yield d
    d.close()


def passage(report, key: str) -> tuple[int, str]:
    return report.passages[key]


def assert_rects_ok(res) -> None:
    assert res.rects, "expected a highlight"
    for r in res.rects:
        assert 0 <= r["x"] <= 1 and 0 <= r["y"] <= 1 and r["w"] > 0 and r["h"] > 0
        assert r["x"] + r["w"] <= 1.0001 and r["y"] + r["h"] <= 1.0001


# --------------------------------------------------------------------------------------- normalisation


@pytest.mark.parametrize("raw, expected", [("(1,588)", "1588"), ("Group’s", "groups"), ("net-", "net"), ("ﬁnancial", "financial"), ("£6,991m", "6991m"), ("—", "")])
def test_norm_token(raw, expected):
    assert norm_token(raw) == expected


def test_squash_ignores_case_space_punctuation_and_ligatures():
    assert squash("Net  interest\npaid (1,588)") == squash("NET INTEREST PAID 1588") == "netinterestpaid1588"
    assert squash("e" + chr(0xFB03) + "cient") == "efficient"
    assert squash("infra-structure") == squash("infrastructure")


@pytest.mark.parametrize("text, expected", [
    ("£6,991 million", ["6991"]), ("(1,588) (1,479) (7%)", ["1588", "1479", "7"]), ("3.5% and 99.99%", ["3.5", "99.99"]),
    ("2024/25", ["2024", "25"]), ("see note 29.", ["29"]), ("no digits", []), ("operations¹", ["1"]),
])
def test_numbers_in(text, expected):
    assert numbers_in(text) == expected


# --------------------------------------------------------------------------------------- exact matches


@pytest.mark.parametrize("key", ["A", "B", "C", "D", "E", "F", "ROW", "CELL", "X", "L", "Z"])
def test_every_passage_is_found_exactly_where_it_is(doc, report, key):
    page, text = passage(report, key)
    res = locate_quote(doc, page, text)
    assert (res.page, res.hinted_page, res.method, res.score) == (page, page, "exact", 1.0)
    assert squash(res.matched_text) == squash(text)
    assert_rects_ok(res)
    assert (res.page_width, res.page_height) == pytest.approx(doc.page_sizes()[page - 1])


def test_line_level_rects(doc, report):
    page, text = passage(report, "A")
    assert len(locate_quote(doc, page, text).rects) == 6            # one rect per text line, hyphenated halves on their own lines
    assert len(locate_quote(doc, *passage(report, "D")).rects) == 5
    row = locate_quote(doc, *passage(report, "ROW"))
    assert len(row.rects) == 1                                       # a whole table row is one band
    assert row.rects[0]["w"] > 0.5


def test_matched_text_joins_hyphenated_words(doc, report):
    res = locate_quote(doc, *passage(report, "A"))
    assert "infrastructure programme" in res.matched_text and "network reliability" in res.matched_text


def test_passage_crossing_two_columns(doc, report):
    res = locate_quote(doc, *passage(report, "F"))
    xs = [r["x"] for r in res.rects]
    assert len(res.rects) == 4 and min(xs) < 0.2 and max(xs) > 0.45        # two lines left column, two right


def test_ambiguous_single_cell_reports_both_occurrences(doc, report):
    res = locate_quote(doc, *passage(report, "CELL"))
    assert res.n_matches == 2 and len(res.rects) == 1


@pytest.mark.parametrize("variant", [
    lambda t: t.replace(" ", "\n   "),
    lambda t: t.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'"),
    lambda t: t.upper(),
    lambda t: t.replace(" — ", "-"),
    lambda t: t.replace(",", ""),
    lambda t: " ".join(t.split()[3:]),
    lambda t: " ".join(t.split()[:-3]),
    lambda t: " ".join(t.split()[:len(t.split()) // 2]),
])
@pytest.mark.parametrize("key", ["A", "C", "D"])
def test_llm_style_variants_still_match_exactly(doc, report, key, variant):
    page, text = passage(report, key)
    res = locate_quote(doc, page, variant(text))
    assert res.method == "exact" and res.page == page and len(res.rects) >= 1


def test_hyphenation_ligatures_and_quote_styles(doc):
    assert locate_quote(doc, 3, "infra-structure programme in the UK").method == "exact"
    assert locate_quote(doc, 3, "e" + chr(0xFB03) + "cient " + chr(0xFB01) + "nancial work" + chr(0xFB02) + "ow").method == "exact"
    assert locate_quote(doc, 3, 'the Group\'s "core" earnings').method == "exact"


def test_stream_order_is_not_reading_order(doc, report):
    page, text = passage(report, "X")                 # right column is drawn first, the sentence flows left column -> right column
    res = locate_quote(doc, page, text)
    assert res.method == "exact" and res.order == "columns" and len(res.rects) == 5


# --------------------------------------------------------------------------------------- fuzzy / fragments / block / page


def test_fuzzy_match_with_dropped_and_replaced_words(doc, report):
    page, text = passage(report, "D")
    w = text.split()
    dropped = locate_quote(doc, page, " ".join(w[:len(w) // 2] + w[len(w) // 2 + 2:]))
    assert dropped.method == "fuzzy" and dropped.score >= 0.9
    replaced = locate_quote(doc, page, " ".join(w[:3] + ["banana", "orange"] + w[5:]))
    assert replaced.method == "fuzzy" and 0.78 <= replaced.score < 1.0
    assert squash(text).startswith(squash(replaced.matched_text)[:20])


def test_fuzzy_with_junk_before_and_after(doc, report):
    page, text = passage(report, "A")
    res = locate_quote(doc, page, "As stated in the report, " + text + " This is notable.")
    assert res.method == "fuzzy" and res.score >= 0.78
    assert squash(text) in squash(res.matched_text)                   # the whole passage is highlighted (a word or two of slack is fine)
    assert len(squash(res.matched_text)) < 1.1 * len(squash(text))


def test_fragments_with_ellipsis(doc, report):
    page, text = passage(report, "D")
    w = text.split()
    res = locate_quote(doc, page, " ".join(w[:4]) + " ... " + " ".join(w[-6:]))
    assert res.method == "fragments" and res.score >= 0.9
    assert len(res.rects) == 2                                    # each fragment on its own: no rect bridges the gap in between
    assert "…" not in res.matched_text and " ... " in res.matched_text
    bracketed = locate_quote(doc, page, " ".join(w[:4]) + " [...] " + " ".join(w[-6:]))
    assert bracketed.method == "fragments"


def test_block_is_an_approximate_area(doc, report):
    page, text = passage(report, "D")
    words = text.split()
    random.Random(1).shuffle(words)
    res = locate_quote(doc, page, " ".join(words))
    assert res.method == "block" and 0 < res.score <= 0.5 and res.rects


def test_unrelated_text_is_page_only(doc):
    res = locate_quote(doc, 3, "The company said that money coming from its day to day activities fell slightly compared with the prior year")
    assert (res.method, res.score, res.rects, res.matched_text, res.n_matches) == ("page", 0.0, [], "", 0)
    assert res.page == res.hinted_page == 3


@pytest.mark.parametrize("quote", ["", "   ", "a", "of", "\n\n"])
def test_unusable_quotes_are_page_only(doc, quote):
    res = locate_quote(doc, 3, quote)
    assert res.method == "page" and res.rects == []


# --------------------------------------------------------------------------------------- numeric guard


def test_numeric_guard_rejects_changed_numbers_in_a_table_row(doc):
    assert locate_quote(doc, 4, "Net interest paid (1,588) (1,479) (7%)").method == "exact"
    res = locate_quote(doc, 4, "Net interest paid (1,688) (1,479) (7%)")           # one digit different, 97 % similar text
    assert res.method == "page" and res.rects == []


def test_numeric_guard_single_cell(doc):
    assert locate_quote(doc, 4, "(1,588)").method == "exact"
    assert locate_quote(doc, 4, "(1,688)").method == "page"


def test_numeric_guard_rejects_a_number_that_only_matches_inside_a_longer_one(doc):
    # "1,588 million" is on the page; "588 million" squashes to a substring of it but is a different amount
    assert locate_quote(doc, 3, "588 million").method == "page"
    assert locate_quote(doc, 3, "1,588 million").method == "exact"


def test_numeric_guard_in_long_fuzzy_quotes(doc, report):
    page, text = passage(report, "D")
    res = locate_quote(doc, page, text.replace("£6,991", "£6,919"))
    assert res.method not in ("exact", "fuzzy", "fragments")
    res = locate_quote(doc, page, text.replace("down 4%", "down 5%"))
    assert res.method not in ("exact", "fuzzy", "fragments")
    # numbers the model left out are fine, the guard only checks the numbers it wrote
    assert locate_quote(doc, page, text.replace("£6,991 million ", "")).method == "fuzzy"


def test_numeric_guard_in_fragments_and_block(doc, report):
    page, text = passage(report, "D")
    w = text.replace("£6,991", "£6,919").split()
    res = locate_quote(doc, page, " ".join(w[:6]) + " ... " + " ".join(w[-6:]))
    assert res.method in ("fragments", "page", "block")
    assert res.method != "fragments" or "6,919" not in res.matched_text
    shuffled = text.replace("£6,991", "£6,919").split()
    random.Random(3).shuffle(shuffled)
    assert locate_quote(doc, page, " ".join(shuffled)).method == "page"          # an area that lacks the amount is not the place


# --------------------------------------------------------------------------------------- neighbours / whole document


def test_off_by_one_page_is_corrected(doc, report):
    page, text = passage(report, "A")
    for hint in (page - 1, page + 1):
        res = locate_quote(doc, hint, text)
        assert (res.page, res.hinted_page, res.method) == (page, hint, "exact")
        assert any("page corrected" in n for n in res.notes)


def test_neighbours_can_be_switched_off(doc):
    quote = "Following a full review"                       # short: no whole-document search either
    assert locate_quote(doc, 4, quote, neighbours=0).method == "page"
    res = locate_quote(doc, 4, quote, neighbours=1)
    assert (res.page, res.method) == (5, "exact")


def test_whole_document_search_rescues_a_far_wrong_page(doc, report):
    page, text = passage(report, "D")
    res = locate_quote(doc, 7, text, neighbours=1)                # printed-vs-physical confusion can be 2+ pages
    assert (res.page, res.hinted_page, res.method) == (page, 7, "exact")
    assert any("whole-document" in n for n in res.notes)


def test_short_quotes_are_not_searched_in_the_whole_document(doc):
    res = locate_quote(doc, 7, "Company Secretary", neighbours=1)       # page 7 has it
    assert res.page == 7
    res = locate_quote(doc, 5, "dividend of 31.36 pence", neighbours=1)   # only on page 3, 2 away: too short to trust elsewhere
    assert res.method == "page"


def test_neighbour_matches_never_downgrade_to_block(doc):
    res = locate_quote(doc, 4, "Following a full review the Board concluded that purple elephants", neighbours=1)
    assert res.page == 4 and res.method in ("page", "block")


def test_page_number_is_clamped(doc, report):
    page, text = passage(report, "Z")
    assert locate_quote(doc, 99, text).page == page
    assert locate_quote(doc, 0, passage(report, "A")[1], neighbours=3).page == 3


# --------------------------------------------------------------------------------------- geometry


def render_gray(data: bytes, page: int):
    """The page as PDFium draws it (1 px per point, grayscale): an independent check of where the rects are."""
    np = pytest.importorskip("numpy")                  # numpy and Pillow are transitive dependencies (ragas, pageindex)
    pytest.importorskip("PIL")
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(data)
        try:
            return np.array(pdf[page - 1].render(scale=1, grayscale=True).to_pil())
        finally:
            pdf.close()


def dark_bbox(img) -> tuple[float, float, float, float]:
    ys, xs = (img < 128).nonzero()
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


@pytest.mark.parametrize("page, size", [(1, (500, 530)), (2, (595.28, 841.89)), (3, (841.89, 595.28)), (4, (530, 500))])
def test_rects_follow_rotate_and_cropbox(page, size):
    data = pf.build_geometry_pdf()                  # one isolated line per page: /Rotate 90 + CropBox, 180, 270, CropBox only
    d = PdfiumDoc(data)
    try:
        res = locate_quote(d, page, "The quick brown fox jumps over the lazy dog 12345.")
        assert res.method == "exact" and len(res.rects) == 1
        assert (res.page_width, res.page_height) == pytest.approx(size, abs=0.01)
        img = render_gray(data, page)
        (x0, y0, x1, y1), (h, w) = dark_bbox(img), img.shape
        r = res.rects[0]
        got = (r["x"] * w, r["y"] * h, (r["x"] + r["w"]) * w, (r["y"] + r["h"]) * h)
        assert got == pytest.approx((x0, y0, x1, y1), abs=3.5)
    finally:
        d.close()


def test_landscape_rotated_page_has_upright_line_rects(doc, report):
    page, text = passage(report, "L")
    res = locate_quote(doc, page, text)
    assert (res.page_width, res.page_height) == pytest.approx((841.89, 595.28), abs=0.01)
    assert len(res.rects) == 4 and all(r["w"] > 3 * r["h"] * (res.page_height / res.page_width) for r in res.rects)
    img = render_gray(report.data, page)
    for r in res.rects:
        x0, y0 = int(r["x"] * img.shape[1]), int(r["y"] * img.shape[0])
        x1, y1 = int((r["x"] + r["w"]) * img.shape[1]), int((r["y"] + r["h"]) * img.shape[0])
        assert (img[y0:y1, x0:x1] < 128).sum() > 100                          # the rect sits on ink


# --------------------------------------------------------------------------------------- align_claim


def test_align_claim_finds_the_table_row(doc):
    res = align_claim(doc, 4, "Cash generated from continuing operations was £6,991 million")
    assert res is not None and res.page == 4 and res.method == "block"
    assert "Cash generated from continuing operations" in res.matched_text and "6,991" in res.matched_text
    assert len(res.rects) == 1 and res.score >= 0.6
    r = res.rects[0]
    assert 0.2 < r["y"] < 0.4 and r["w"] > 0.3


def test_align_claim_numbers_must_agree(doc):
    assert align_claim(doc, 4, "Cash generated from continuing operations was £6,919 million") is None
    assert align_claim(doc, 4, "Net interest paid was £1,688 million") is None
    assert align_claim(doc, 4, "Net interest paid was £1,588 million") is not None


def test_align_claim_narrative_sentence(doc):
    res = align_claim(doc, 3, "Net debt at year end was £41,371 million, down from £43,607 million.")
    assert res is not None and res.page == 3
    assert "41,371" in res.matched_text and len(res.rects) >= 2


def test_align_claim_without_numbers(doc):
    res = align_claim(doc, 5, "The Board concluded that the capital allocation framework remained appropriate")
    assert res is not None and res.page == 5 and "allocation framework" in res.matched_text


@pytest.mark.parametrize("claim", [
    "", "Revenue", "The weather was pleasant throughout the quarter", "Profit rose to 99,999 million", "Staff turnover fell sharply this year",
])
def test_align_claim_weak_support_is_none(doc, claim):
    assert align_claim(doc, 3, claim) is None


def test_align_claim_neighbours(doc):
    claim = "The Board recommended a final dividend of 31.36 pence per share, subject to shareholder approval"
    assert align_claim(doc, 4, claim) is None
    res = align_claim(doc, 4, claim, neighbours=1)
    assert res is not None and res.page == 3 and any("page corrected" in n for n in res.notes)


def test_align_claim_uses_geometry_when_the_table_is_drawn_column_by_column():
    d = PdfiumDoc(pf.build_column_major_table_pdf())
    try:
        res = align_claim(d, 1, "Operating profit was £4,512 million")
        assert res is not None and res.order == "lines"
        assert "Operating profit" in res.matched_text and "4,512" in res.matched_text
        assert len(res.rects) == 1
        assert align_claim(d, 1, "Operating profit was £4,521 million") is None
    finally:
        d.close()


# --------------------------------------------------------------------------------------- PdfiumDoc


def test_pdfiumdoc_basics(report, tmp_path):
    d = PdfiumDoc(report.data)
    assert d.page_count == 7
    pw = d.page_words(3)
    assert pw.page == 3 and pw.words and d.page_words(3) is pw             # cached
    assert [round(w) for w in d.page_sizes()[5]] == [842, 595]
    with pytest.raises(IndexError):
        d.page_words(0)
    with pytest.raises(IndexError):
        d.page_words(8)
    d.close()
    d.close()                                                              # idempotent
    with pytest.raises(RuntimeError):
        d.page_words(3)
    path = tmp_path / "r.pdf"
    path.write_bytes(report.data)
    with PdfiumDoc(path) as from_path:
        assert from_path.page_count == 7
    path.unlink()                                                          # opened from bytes: no file lock on Windows


@pytest.mark.parametrize("data", [b"", b"junk", b"%PDF-1.4 truncated"])
def test_pdfiumdoc_rejects_invalid_data(data):
    with pytest.raises(PdfError) as exc:
        PdfiumDoc(data)
    assert exc.value.code == "invalid_pdf"


def test_pdfiumdoc_rejects_encrypted_data():
    with pytest.raises(PdfError) as exc:
        PdfiumDoc(pf.build_encrypted_pdf())
    assert exc.value.code == "encrypted_pdf"


def test_page_word_cache_is_bounded_lru(report):
    d = PdfiumDoc(report.data, cache_pages=3)
    try:
        first = d.page_words(1)
        for p in (2, 3, 4):
            d.page_words(p)
        assert len(d._cache) == 3 and 1 not in d._cache                    # least recently used page evicted
        assert d.page_words(1) is not first                                # rebuilt on demand
        d.page_words(4)
        d.page_words(5)
        assert 4 in d._cache                                                # a touched page survives
    finally:
        d.close()


def test_squashed_pages(report):
    d = PdfiumDoc(report.data)
    try:
        assert not d.squashed_ready
        pages = d.squashed_pages()
        assert d.squashed_ready and len(pages) == 7 and d.squashed_pages() is pages
        assert squash(passage(report, "D")[1]) in pages[2]
    finally:
        d.close()


def test_empty_page_locates_to_page_only():
    d = PdfiumDoc(pf.build_text_pdf([["Some text."], []]))
    try:
        res = locate_quote(d, 2, "Some text that is not on this blank page at all", neighbours=0)
        assert res.method == "page" and d.page_words(2).words == []
        assert align_claim(d, 2, "Some text that is not here") is None
    finally:
        d.close()


# --------------------------------------------------------------------------------------- threads and speed


def test_concurrent_locating_on_two_documents(report):
    d1, d2 = PdfiumDoc(report.data), PdfiumDoc(report.data, cache_pages=2)
    errors: list[BaseException] = []
    keys = ["A", "D", "X", "ROW", "L", "Z"]

    def work(d: PdfiumDoc, seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(25):
                key = rng.choice(keys)
                page, text = passage(report, key)
                res = locate_quote(d, page, text)
                assert res.method == "exact" and res.page == page
                assert align_claim(d, 4, "Cash generated from continuing operations was £6,991 million") is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(d1 if i % 2 else d2, i)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    d1.close()
    d2.close()
    assert not errors, errors


@pytest.fixture(scope="module")
def big():
    return pf.build_big_pdf(300)


def test_300_page_report_locates_quickly_once_pages_are_cached(big):
    data, lines = big
    d = PdfiumDoc(data)
    try:
        targets = [(p, " ".join(lines[p - 1][3:5])) for p in range(10, 300, 15)][:20]
        for p, _ in targets:                                # cold extraction is paid once per page
            d.page_words(p)
        t0 = time.perf_counter()
        for p, quote in targets:
            res = locate_quote(d, p, quote)
            assert (res.method, res.page) == ("exact", p)
        assert time.perf_counter() - t0 < 0.5
    finally:
        d.close()


def test_300_page_report_wrong_page_needs_a_warmed_whole_document_search(big):
    data, lines = big
    d = PdfiumDoc(data)
    try:
        quote = " ".join(lines[199][10:13])
        assert not d.squashed_ready
        cold = locate_quote(d, 120, quote, neighbours=1)    # 80 pages off; squashing 300 pages inside a request is skipped
        assert cold.method in ("page", "block")
        d.squashed_pages()                                   # what the service does once, in the background
        warm = locate_quote(d, 120, quote, neighbours=1)
        assert (warm.page, warm.method) == (200, "exact")
    finally:
        d.close()
