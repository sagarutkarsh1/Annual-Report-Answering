"""citations: tag parsing, streaming filter, claim extraction, tree breadcrumbs, per-cite resolution, build_answer."""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from pathlib import Path

import pytest

from reportlens import citations as ci
from reportlens.citations import (MARKER, CitationContext, CiteStreamFilter, RawCite, build_answer, claim_for, parse_cites, resolve_cite,
                                  strip_markers, tree_path_for_page)
from reportlens.locator import PdfiumDoc
from reportlens.pdfutil import detect_printed_labels
from tests import pdf_factory as pf

# --------------------------------------------------------------------------------------- fixtures


TREE = [
    {"title": "Cover and contents", "node_id": "0001", "start_index": 1, "end_index": 2, "summary": "..."},
    {"title": "Strategic report", "node_id": "0002", "start_index": 3, "end_index": 5, "nodes": [
        {"title": "Strategic report (intro)", "node_id": "0003", "start_index": 3, "end_index": 3},
        {"title": "Financial review", "node_id": "0004", "start_index": 3, "end_index": 5, "nodes": [
            {"title": "Income and cash flow", "node_id": "0005", "start_index": 4, "end_index": 4}]}]},
    {"title": "Governance", "node_id": "0006", "start_index": 6, "end_index": 7},
]
LABELS = [None, None, "1", "2", "3", "4", "5"]


@pytest.fixture(scope="module")
def report() -> pf.ReportPdf:
    return pf.build_report_pdf()


@pytest.fixture(scope="module")
def pdf(report):
    d = PdfiumDoc(report.data)
    yield d
    d.close()


@pytest.fixture
def ctx(pdf) -> CitationContext:
    return CitationContext(doc_display_name="Report.pdf", pdf=pdf, tree=TREE, printed_labels=LABELS)


def cite(page="3", quote=None, doc="Report.pdf") -> str:
    q = f' quote="{quote}"' if quote is not None else ""
    return f'<cite doc="{doc}" page="{page}"{q}/>'


# --------------------------------------------------------------------------------------- parse_cites


def test_parse_basic_and_offsets():
    raw = 'Revenue rose <cite doc="Report.pdf" page="12" quote="Revenue rose by 5%"/> a lot.'
    [c] = parse_cites(raw)
    assert (c.doc, c.page, c.quote) == ("Report.pdf", 12, "Revenue rose by 5%")
    assert raw[c.start:c.end] == '<cite doc="Report.pdf" page="12" quote="Revenue rose by 5%"/>'


def test_parse_attributes_in_any_order_and_quote_styles():
    [a, b, c] = parse_cites("""<cite quote="x y z q" page="4" doc="d.pdf"/> <cite page='5' quote='it is' doc='d.pdf' /> <cite   doc = "d.pdf"
        page = "6"  />""")
    assert (a.page, a.doc, a.quote) == (4, "d.pdf", "x y z q")
    assert (b.page, b.quote) == (5, "it is")
    assert (c.page, c.quote) == (6, None)


@pytest.mark.parametrize("value, page", [("12", 12), ("12-13", 12), ("p.12", 12), ("p. 12", 12), ("pp. 12–13", 12), ("page 7", 7), (" 9 ", 9), ("3,4", 3)])
def test_parse_page_forms(value, page):
    [c] = parse_cites(f'<cite doc="d" page="{value}"/>')
    assert c.page == page


@pytest.mark.parametrize("tag", [
    '<cite doc="d"/>', '<cite doc="d" page=""/>', '<cite doc="d" page="abc"/>', '<cite doc="d" page="0"/>', '<cite/>', '<cite doc="d" page="3"',
    '<cite doc="d" page="3" quote="never closed', '<citex page="3"/>', '<cit page="3"/>', "< cite page=3 />",
])
def test_parse_skips_unusable_tags(tag):
    assert parse_cites(f"before {tag} after") == []


def test_parse_quote_with_apostrophes_double_quotes_and_angle_brackets():
    raw = ('<cite doc="d" page="3" quote="The Group\'s "core" earnings rose > 5% & <b>more</b>"/> next '
           '<cite doc="d" page="4" quote="He said "hello" to us"/>')
    a, b = parse_cites(raw)
    assert a.quote == 'The Group\'s "core" earnings rose > 5% & <b>more</b>'
    assert b.quote == 'He said "hello" to us'
    [c] = parse_cites("<cite doc='d' page='3' quote='the Group's profit'/>")
    assert c.quote == "the Group's profit"


def test_parse_curly_quote_delimiters_whitespace_and_entities():
    [a, b] = parse_cites("<cite doc=“d” page=“3” quote=“net debt”/><cite doc='d' page='3' quote='line one\n   line  two &amp; &quot;q&quot;'/>")
    assert (a.page, a.quote) == (3, "net debt")
    assert b.quote == 'line one line two & "q"'


def test_parse_tag_variants():
    cs = parse_cites('<CITE DOC="d" PAGE="3"/> <cite doc="d" page="4"> <cite doc="d" page=5/> <cite doc="d" page=6 quote=abc></cite> </cite>')
    assert [c.page for c in cs] == [3, 4, 5, 6]
    assert cs[3].quote == "abc"


def test_parse_accepts_what_the_pageindex_sdk_emits():
    """The tags of the SDK's citation prompt (agent_tools.LOCAL_CITATION_PROMPTS["cite"]), incl. the block attribute and file names with spaces."""
    cs = parse_cites('Up <cite doc="National Grid_Annual_Report.pdf" page="89"/> and <cite doc="a b.pdf" page="12" block="b7"/> and '
                     '<cite doc="a b.pdf" page="3">text</cite>.')
    assert [(c.doc, c.page, c.quote) for c in cs] == [("National Grid_Annual_Report.pdf", 89, None), ("a b.pdf", 12, None), ("a b.pdf", 3, None)]


def test_parse_doc_is_optional_and_never_raises():
    [c] = parse_cites('<cite page="8"/>')
    assert c.doc is None and c.page == 8
    for junk in ("", "<", "<cite", "<<<<cite page=3/>>>", "\x00<cite page='1'/>\x00", "<cite " * 50, '<cite quote="' * 20):
        parse_cites(junk)


def test_parse_ignores_comparison_operators_in_prose():
    raw = "if a < b and c > d then <cite doc='x' page='2'/> is fine"
    tag = "<cite doc='x' page='2'/>"
    assert parse_cites(raw) == [RawCite("x", 2, None, raw.index(tag), raw.index(tag) + len(tag))]


# --------------------------------------------------------------------------------------- CiteStreamFilter


def render(events) -> str:
    return "".join(ev[1] if ev[0] == "text" else MARKER.format(n=ev[2]) for ev in events)


def run(raw: str, chunks: list[str] | None = None, page_count=None) -> tuple[str, list[tuple], CiteStreamFilter]:
    f = CiteStreamFilter(page_count)
    events: list[tuple] = []
    for chunk in chunks if chunks is not None else [raw]:
        events += f.feed(chunk)
    events += f.flush()
    return render(events), [ev for ev in events if ev[0] == "cite"], f


PAGES = 20            # pages 99 and 0 are out of range, 12-13 is fine

SAMPLES = [
    # (raw, expected display text)
    ('Cash was £6,991 million <cite doc="R" page="3" quote="Cash was £6,991 million"/>. Net debt fell.<cite doc="R" page="4"/> Done.',
     "Cash was £6,991 million [[c1]]. Net debt fell.[[c2]] Done."),
    ("""The Group's profit <cite page='3' doc='R' quote='the Group's "core" earnings > 5% and <b>bold</b>'/> and <cite page="12-13" doc="x" /> more < text > here.""",
     "The Group's profit [[c1]] and [[c2]] more < text > here."),
    ('Two in a row<cite doc="R" page="3"/><cite doc="R" page="4"/> then text.', "Two in a row[[c1]][[c2]] then text."),
    ('Bad page <cite doc="R" page="99" quote="x"/> here and <cite doc="R"/> there <cite doc="R" page="5"/>.', "Bad page here and there [[c1]]."),
    ("A stray </cite> close and <cite doc='R' page='3'>tagged</cite> text.", "A stray close and [[c1]]tagged text."),
    ('Unterminated at the end <cite doc="R" page="3" quote="abc', 'Unterminated at the end <cite doc="R" page="3" quote="abc'),
    ("Ends with a lone <", "Ends with a lone <"),
    ("No cites at all, just prose with a < b > c.", "No cites at all, just prose with a < b > c."),
    ("Line one.\n<cite page='3' doc='R'/>\n\n| a | b |\n|---|---|\n| 1 | 2 <cite page='4' doc='R'/> |", "Line one.\n[[c1]]\n\n| a | b |\n|---|---|\n| 1 | 2 [[c2]] |"),
    ("<cite page='3'/>starts the answer", "[[c1]]starts the answer"),
    ('<CITE doc="R" page="3"/> upper and <Cite doc="R" page="4" /> mixed', "[[c1]] upper and [[c2]] mixed"),
]


@pytest.mark.parametrize("raw, expected", SAMPLES)
def test_filter_converts_whole_input(raw, expected):
    text, _, _ = run(raw, page_count=PAGES)
    assert text == expected


@pytest.mark.parametrize("raw, expected", SAMPLES)
def test_filter_output_does_not_depend_on_chunking(raw, expected):
    for cut in range(1, len(raw)):                                # split at EVERY character boundary
        assert run(raw, [raw[:cut], raw[cut:]], page_count=PAGES)[0] == expected, cut
    rng = random.Random(len(raw))
    for _ in range(200):                                         # random chunkings, including single characters
        chunks, i = [], 0
        while i < len(raw):
            step = rng.choice([1, 1, 2, 3, 5, 8, 13, 40])
            chunks.append(raw[i:i + step])
            i += step
        text, cites, _ = run(raw, chunks, page_count=PAGES)
        assert text == expected
        assert cites == run(raw, page_count=PAGES)[1]                 # same RawCites, same offsets, same numbers


@pytest.mark.parametrize("raw, expected", SAMPLES)
def test_filter_never_emits_half_a_tag_or_marker(raw, expected):
    """Everything handed out so far must be a prefix of the final text: a half-emitted tag could not satisfy that."""
    rng = random.Random(7)
    for _ in range(100):
        f = CiteStreamFilter(PAGES)
        shown, i = "", 0
        while i < len(raw):
            step = rng.choice([1, 2, 3, 7])
            shown += render(f.feed(raw[i:i + step]))
            assert expected.startswith(shown), (shown, expected)
            assert "<cite" not in shown.lower() or "<cite" in expected.lower()
            assert shown.count("[[") == shown.count("]]")
            i += step
        shown += render(f.flush())
        assert shown == expected


def test_filter_holds_a_partial_tag_until_it_is_complete():
    f = CiteStreamFilter()
    assert f.feed("Revenue <") == [("text", "Revenue ")]
    assert f.feed("ci") == []
    assert f.feed('te doc="R" pa') == []
    assert f.feed('ge="3" quote="a b') == []
    [(kind, rc, n)] = f.feed('c"/> rest')[:1]
    assert (kind, rc.page, rc.quote, n) == ("cite", 3, "a bc", 1)
    assert f.flush() == []


def test_filter_flush_releases_an_unterminated_tag_as_text():
    f = CiteStreamFilter()
    assert f.feed('see <cite doc="R" page="3" quote="oops') == [("text", "see ")]
    assert f.flush() == [("text", '<cite doc="R" page="3" quote="oops')]
    assert f.flush() == []
    g = CiteStreamFilter()
    g.feed("1 <")
    assert g.flush() == [("text", "<")]


def test_filter_gives_up_on_a_huge_unterminated_tag():
    f = CiteStreamFilter()
    out = f.feed('<cite doc="R" page="3" quote="' + "x" * (ci._MAX_TAG_CHARS + 10))
    assert out and out[0][0] == "text" and out[0][1].startswith("<cite")       # not held forever: it was prose


def test_filter_numbering_offsets_and_dropped_count():
    raw = 'a <cite page="9"/> b <cite page="2" doc="R"/> c <cite doc="R"/> d <cite page="3"/>'
    text, cites, f = run(raw, page_count=5)
    assert text == "a b [[c1]] c d [[c2]]"
    assert [(c[2], c[1].page) for c in cites] == [(1, 2), (2, 3)]
    assert f.dropped == 2
    for _, rc, _ in cites:
        assert raw[rc.start:rc.end].startswith("<cite") and raw[rc.start:rc.end].endswith("/>")
    assert run(raw)[2].dropped == 1                                              # without a page count only the pageless tag goes


def test_filter_collapses_whitespace_left_by_removed_tags():
    assert run('word <cite page="99"/> next', page_count=PAGES)[0] == "word next"
    assert run('word<cite page="99"/> next', page_count=PAGES)[0] == "word next"
    assert run('word <cite page="99"/>next', page_count=PAGES)[0] == "word next"
    assert run('end.\n <cite page="99"/> \nnext', page_count=PAGES)[0] == "end.\n \nnext"
    assert run('keeps  two spaces', page_count=PAGES)[0] == "keeps  two spaces"


_PIECES = [
    "plain text ", "£6,991 million ", "a < b ", "x > y ", "e.g. ", "\n", "\n\n- bullet ", "| a | b |\n", " ", "  ", "<", "<c", "</", "<cite",
    '<cite doc="R" page="3"/>', "<cite page='4' doc='R' quote='it's \"fine\" > ok'/>", '<cite doc="R" page="3" quote="He said "hi" to us"/>',
    '<cite doc="R" page="99"/>', '<cite doc="R"/>', "</cite>", '<cite doc="R" page="3" quote="unterminated',
]
_WELL_FORMED = [_PIECES[i] for i in (0, 1, 2, 3, 4, 5, 6, 7, 8, 14, 16)] + ["<cite page='4' doc='R' quote='plain \"inner\" > text'/>"]
_TAG_RE = re.compile(r"""<cite\s+(?:[^<>"']|"[^"]*"|'[^']*')*?/>""")


def _random_answer(rng: random.Random, pieces: list[str]) -> str:
    return "".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))


def test_filter_random_answers_are_chunking_independent():
    rng = random.Random(2024)
    for _ in range(250):
        raw = _random_answer(rng, _PIECES)
        whole, whole_cites, whole_f = run(raw, page_count=PAGES)
        for _ in range(4):
            chunks, i = [], 0
            while i < len(raw):
                step = rng.choice([1, 1, 2, 3, 5, 9, 31])
                chunks.append(raw[i:i + step])
                i += step
            text, cites, f = run(raw, chunks, page_count=PAGES)
            assert (text, cites, f.dropped) == (whole, whole_cites, whole_f.dropped), (raw, chunks)


def test_filter_matches_a_plain_regex_reference_on_well_formed_answers():
    rng = random.Random(99)
    for _ in range(250):
        raw = _random_answer(rng, _WELL_FORMED)
        counter = iter(range(1, 1000))
        expected = _TAG_RE.sub(lambda m: MARKER.format(n=next(counter)), raw)
        assert run(raw, page_count=PAGES)[0] == expected, raw
        assert [c.start for c in parse_cites(raw)] == [m.start() for m in _TAG_RE.finditer(raw)]



# --------------------------------------------------------------------------------------- strip_markers


@pytest.mark.parametrize("text, expected", [
    ("Revenue [[c1]]. Next [[c2]][[c3]] words", "Revenue. Next words"),
    ("[[c1]] starts here", "starts here"),
    ("- bullet [[c12]]\n- [[c2]][[c3]] other", "- bullet\n- other"),
    ("no markers [c1] or [[x]]", "no markers [c1] or [[x]]"),
    ("", ""),
])
def test_strip_markers(text, expected):
    assert strip_markers(text) == expected


# --------------------------------------------------------------------------------------- claim_for


def claim(raw: str, nth: int = 0) -> str:
    starts = [m.start() for m in ci.CITE_RE.finditer(raw)]
    return claim_for(raw, starts[nth])


@pytest.mark.parametrize("raw, nth, expected", [
    ('Revenue was £6,991 million. Margin fell 2%.<cite doc="a" page="1"/>', 0, "Margin fell 2%."),
    ('Revenue was £6,991 million <cite doc="a" page="1"/>.', 0, "Revenue was £6,991 million"),
    ('Revenue was £6,991 million. <cite doc="a" page="1"/>', 0, "Revenue was £6,991 million."),
    ('Revenue was £6.99 billion in 2024. Profit was £1.2 billion.<cite page="1"/>', 0, "Profit was £1.2 billion."),
    ('Spend on e.g. Network upgrades rose. No. 5 plan met Mr. Smith’s goals.<cite page="1"/>', 0, "No. 5 plan met Mr. Smith’s goals."),
    ('Spend on e.g. Network upgrades rose.<cite page="1"/>', 0, "Spend on e.g. Network upgrades rose."),
    ('- **Revenue** rose 5% to £1.2 billion. It was a record.<cite page="1"/>\n- Next bullet', 0, "Revenue rose 5% to £1.2 billion. It was a record."),
    ('1. Dividends were 31.36p<cite page="1"/>', 0, "Dividends were 31.36p"),
    ('* Net debt `fell` to [the note](http://x.y/z) value<cite page="1"/>', 0, "Net debt fell to the note value"),
    ('## Summary\nRevenue grew 5%.<cite page="1"/>', 0, "Revenue grew 5%."),
    ('First sentence. The key figure was £5m.\n<cite page="1"/>', 0, "The key figure was £5m."),
    ('First.\n\n\n<cite page="1"/>', 0, "First."),
    ('Claim one.<cite page="1"/><cite page="2"/>', 1, "Claim one."),
    ('Claim [[c1]] again <cite page="2"/>', 0, "Claim again"),
    ('<cite page="1"/>Opens the answer', 0, ""),
    ('> Quoted **bold** text<cite page="1"/>', 0, "Quoted bold text"),
    ('Earlier <cite page="1"/> and then the later claim<cite page="2"/>', 1, "Earlier and then the later claim"),
])
def test_claim_for(raw, nth, expected):
    assert claim(raw, nth) == expected


def test_claim_for_table_row_includes_cells_after_the_tag():
    raw = '| Item | Value |\n|---|---|\n| Cash generated | 6,991 <cite page="4"/> | **5%** |\n| Other | 1 |'
    assert claim(raw) == "Cash generated 6,991 5%"
    own_line = '| Cash generated | 6,991 |\n<cite page="4"/>'
    assert claim(own_line) == "Cash generated 6,991"


def test_claim_for_long_text_is_capped_at_its_tail():
    raw = "word " * 300 + '<cite page="1"/>'
    c = claim(raw)
    assert len(c) <= 500 and c.endswith("word") and not c.startswith(" ")


def test_claim_for_offset_out_of_range_and_empty():
    assert claim_for("", 0) == "" and claim_for("abc.", 50) == "abc."


# --------------------------------------------------------------------------------------- tree_path_for_page


def test_tree_path_deepest_node_and_breadcrumb():
    assert tree_path_for_page(TREE, 4) == (["Strategic report", "Financial review", "Income and cash flow"], "0005", [4, 4])
    assert tree_path_for_page(TREE, 5) == (["Strategic report", "Financial review"], "0004", [3, 5])
    assert tree_path_for_page(TREE, 7) == (["Governance"], "0006", [6, 7])
    assert tree_path_for_page(TREE, 1) == (["Cover and contents"], "0001", [1, 2])


def test_tree_path_intro_node_is_not_repeated_in_the_breadcrumb():
    path, node_id, rng = tree_path_for_page(TREE, 3)
    assert (path, node_id, rng) == (["Strategic report"], "0003", [3, 3])


def test_tree_path_outside_the_tree():
    assert tree_path_for_page(TREE, 0) == ([], None, None)
    assert tree_path_for_page(TREE, 99) == ([], None, None)
    assert tree_path_for_page([], 3) == ([], None, None)
    assert tree_path_for_page(None, 3) == ([], None, None)


PAGEINDEX_TREE = [   # the shape of the Fed report tree in research/03: neighbours share their boundary pages
    {"title": "Monetary Policy", "node_id": "0004", "start_index": 9, "end_index": 21, "nodes": [
        {"title": "March Summary", "node_id": "0005", "start_index": 9, "end_index": 15},
        {"title": "June Summary", "node_id": "0010", "start_index": 15, "end_index": 21}]},
    {"title": "Financial Stability", "node_id": "0014", "start_index": 21, "end_index": 31, "nodes": [
        {"title": "Financial Stability (intro)", "node_id": "0015", "start_index": 21, "end_index": 22},
        {"title": "Monitoring", "node_id": "0016", "start_index": 22, "end_index": 28},
        {"title": "Policy", "node_id": "0022", "start_index": 28, "end_index": 31}]},
]


@pytest.mark.parametrize("page, path, node_id", [
    (9, ["Monetary Policy", "March Summary"], "0005"),
    (14, ["Monetary Policy", "March Summary"], "0005"),
    (15, ["Monetary Policy", "June Summary"], "0010"),            # shared boundary page: the section that starts here
    (21, ["Financial Stability"], "0015"),                        # starts here (not "Monetary Policy" which ends here); "(intro)" does not repeat the title
    (22, ["Financial Stability", "Monitoring"], "0016"),          # boundary of intro 21-22 and Monitoring 22-28: the one that opens here
    (23, ["Financial Stability", "Monitoring"], "0016"),
    (28, ["Financial Stability", "Policy"], "0022"),              # boundary of 22-28 and 28-31: the one that opens here
    (31, ["Financial Stability", "Policy"], "0022"),
])
def test_tree_path_shared_boundary_pages(page, path, node_id):
    assert tree_path_for_page(PAGEINDEX_TREE, page)[:2] == (path, node_id)


def test_tree_path_prefers_the_section_that_opens_on_the_page_over_a_deeper_one_that_ends_there():
    tree = [
        {"title": "Strategy", "node_id": "1", "start_index": 1, "end_index": 10, "nodes": [
            {"title": "Markets", "node_id": "2", "start_index": 1, "end_index": 10, "nodes": [
                {"title": "Europe", "node_id": "3", "start_index": 1, "end_index": 10}]}]},
        {"title": "Governance", "node_id": "4", "start_index": 10, "end_index": 20, "nodes": [
            {"title": "Board", "node_id": "5", "start_index": 10, "end_index": 14},
            {"title": "Committees", "node_id": "6", "start_index": 14, "end_index": 20}]},
    ]
    assert tree_path_for_page(tree, 9) == (["Strategy", "Markets", "Europe"], "3", [1, 10])
    assert tree_path_for_page(tree, 10) == (["Governance", "Board"], "5", [10, 14])      # not the deeper "Europe" that ends here
    assert tree_path_for_page(tree, 14) == (["Governance", "Committees"], "6", [14, 20])
    assert tree_path_for_page(tree, 12) == (["Governance", "Board"], "5", [10, 14])
    assert tree_path_for_page(tree, 20) == (["Governance", "Committees"], "6", [14, 20])


def test_tree_path_tolerates_malformed_nodes():
    messy = [
        "not a node", None, {"title": "No range"}, {"title": "Backwards", "start_index": 9, "end_index": 3},
        {"title": "  Spaced  ", "node_id": 7, "start_index": "3", "end_index": "5", "nodes": [
            {"title": None, "node_id": "", "start_index": 4, "end_index": 4, "nodes": "oops"},
            {"start_index": 4, "end_index": 5, "title": "Child"}, 5]},
        {"title": "Later", "node_id": "0009", "start_index": 6, "end_index": None},
    ]
    assert tree_path_for_page(messy, 3) == (["Spaced"], "7", [3, 5])
    path, node_id, rng = tree_path_for_page(messy, 4)
    assert (path, node_id, rng) == (["Spaced"], None, [4, 4])           # narrowest depth-1 node has no title and an empty id
    assert tree_path_for_page(messy, 5) == (["Spaced", "Child"], None, [4, 5])
    assert tree_path_for_page(messy, 6) == ([], None, None)


# --------------------------------------------------------------------------------------- resolve_cite


def test_resolve_verified_quote(ctx, report):
    page, text = report.passages["D"]
    c = resolve_cite(RawCite("Report.pdf", page, text, 0, 0), "Cash generated was high", ctx, 4)
    assert (c.id, c.index, c.doc_name, c.page, c.cited_page) == ("c4", 4, "Report.pdf", 3, 3)
    assert (c.quote_source, c.match_method, c.match_score) == ("model", "exact", 1.0)
    assert c.printed_page == "1" and c.section_path == ["Strategic report"] and c.node_id == "0003" and c.node_range == [3, 3]
    assert len(c.rects) == 5 and c.quote.startswith("Cash generated from continuing operations")
    assert (c.page_width, c.page_height) == pytest.approx((595.28, 841.89), abs=0.01)
    assert c.claim == "Cash generated was high"


def test_resolve_corrects_an_off_by_one_page(ctx, report):
    page, text = report.passages["A"]
    c = resolve_cite(RawCite("Report.pdf", page + 1, text, 0, 0), None, ctx, 1)
    assert (c.page, c.cited_page, c.quote_source, c.printed_page) == (3, 4, "model", "1")
    assert c.claim is None


def test_resolve_falls_back_to_claim_alignment(ctx):
    raw = RawCite("Report.pdf", 4, "an invented quote that the report does not contain anywhere at all", 0, 0)
    c = resolve_cite(raw, "Cash generated from continuing operations was £6,991 million", ctx, 1)
    assert (c.quote_source, c.match_method, c.page) == ("aligned", "block", 4)
    assert "6,991" in c.quote and len(c.rects) == 1 and c.section_path == ["Strategic report", "Financial review", "Income and cash flow"]


def test_resolve_ignores_too_short_quotes(ctx):
    c = resolve_cite(RawCite("Report.pdf", 4, "ok", 0, 0), "Cash generated from continuing operations was £6,991 million", ctx, 1)
    assert c.quote_source == "aligned"


def test_resolve_page_only(ctx):
    c = resolve_cite(RawCite("Report.pdf", 6, None, 0, 0), "Something the page does not say at all", ctx, 2)
    assert (c.quote, c.quote_source, c.match_method, c.match_score, c.rects) == (None, "none", "page", 0.0, [])
    assert (c.page, c.printed_page, c.section_path, c.node_id) == (6, "4", ["Governance"], "0006")
    assert (c.page_width, c.page_height) == pytest.approx((841.89, 595.28), abs=0.01)         # the landscape page


def test_resolve_block_area_keeps_its_rects_but_is_not_a_verified_quote(ctx, report):
    page, text = report.passages["D"]
    words = text.split()
    random.Random(2).shuffle(words)
    c = resolve_cite(RawCite("Report.pdf", page, " ".join(words), 0, 0), None, ctx, 1)
    assert (c.match_method, c.quote_source) == ("block", "none") and c.rects and 0 < c.match_score <= 0.5


def test_resolve_handles_missing_labels_and_out_of_range_pages(pdf):
    short = CitationContext("R.pdf", pdf, [], [None])
    c = resolve_cite(RawCite(None, 99, None, 0, 0), None, short, 1)
    assert (c.page, c.cited_page, c.printed_page, c.section_path, c.node_id, c.node_range) == (7, 99, None, [], None, None)
    assert resolve_cite(RawCite(None, 3, None, 0, 0), None, short, 1).printed_page is None


# --------------------------------------------------------------------------------------- build_answer


ANSWER = (
    'Cash generated from continuing operations was £6,991 million '
    '<cite doc="Report.pdf" page="3" quote="Cash generated from continuing operations was £6,991 million"/>.\n'
    '- Net interest paid was £1,588 million.<cite doc="Report.pdf" page="3"/>\n'
    '| Item | Value |\n|---|---|\n| Cash generated from continuing operations | 6,991 <cite doc="Report.pdf" page="4"/> |\n'
    'A bad page <cite doc="Report.pdf" page="99" quote="nothing"/> is dropped.\n'
    'The Group\'s "core" capital <cite page="5" doc="a" quote="Following a full review, the Board concluded that the Group\'s capital allocation framework"/> end.'
)


def test_build_answer_end_to_end(ctx):
    b = build_answer(ANSWER, ctx)
    assert b.text == (
        "Cash generated from continuing operations was £6,991 million [[c1]].\n- Net interest paid was £1,588 million.[[c2]]\n"
        "| Item | Value |\n|---|---|\n| Cash generated from continuing operations | 6,991 [[c3]] |\n"
        "A bad page is dropped.\nThe Group's \"core\" capital [[c4]] end.")
    assert [c.id for c in b.citations] == ["c1", "c2", "c3", "c4"] and [c.index for c in b.citations] == [1, 2, 3, 4]
    assert [(c.page, c.quote_source) for c in b.citations] == [(3, "model"), (3, "aligned"), (4, "aligned"), (5, "model")]
    assert b.citations[1].claim == "Net interest paid was £1,588 million."
    assert b.citations[2].claim == "Cash generated from continuing operations 6,991"
    assert b.stats == {"n_cites": 4, "n_quote_verified": 2, "n_aligned": 2, "n_page_only": 0, "n_unread_pages": 0, "n_dropped": 1}
    assert [(s.page, s.printed_page, s.refs, s.citation_ids) for s in b.sources] == [(3, "1", 2, ["c1", "c2"]), (4, "2", 1, ["c3"]), (5, "3", 1, ["c4"])]
    assert b.sources[1].section_path == ["Strategic report", "Financial review", "Income and cash flow"]
    for m in range(1, 5):
        assert MARKER.format(n=m) in b.text


def test_build_answer_numbering_stays_contiguous_when_cites_are_dropped(ctx):
    raw = f'a {cite(0)} b {cite(3)} c {cite(100)} d {cite(4)} e <cite doc="x"/> f {cite("x")}'
    b = build_answer(raw, ctx)
    assert b.text == "a b [[c1]] c d [[c2]] e f"
    assert [c.id for c in b.citations] == ["c1", "c2"] and b.stats["n_dropped"] == 4 and b.stats["n_cites"] == 2


def test_build_answer_flags_cites_to_pages_the_agent_never_read(pdf):
    ctx = CitationContext("R.pdf", pdf, TREE, LABELS, read_pages={3})
    b = build_answer(f"x {cite(3)} y {cite(4)} z {cite(4)}", ctx)
    assert b.stats["n_unread_pages"] == 2 and b.stats["n_page_only"] == 3
    assert build_answer(f"x {cite(4)}", CitationContext("R.pdf", pdf, TREE, LABELS)).stats["n_unread_pages"] == 0


def test_build_answer_sources_group_by_located_page_and_sort(ctx, report):
    page, text = report.passages["A"]
    raw = f'{cite(6)} one. {cite(page + 1, text)} two. {cite(5)} three. {cite(3)}'
    b = build_answer(raw, ctx)
    assert [(s.page, s.refs) for s in b.sources] == [(3, 2), (5, 1), (6, 1)]
    assert b.sources[0].citation_ids == ["c2", "c4"]


def test_build_answer_literal_markers_from_the_model_are_removed(ctx):
    b = build_answer(f"Text [[c7]] more {cite(3)}", ctx)
    assert b.text == "Text more [[c1]]"


MALFORMED = [None, "", "   ", "<", "<cite", "<cite doc=", '<cite doc="a" page="3" quote="unterminated', "<<<<>>>>", "[[c1]]", "\x00\x01\x02",
             "<cite " * 200, '<cite page="3"/>' * 50, "é" * 10_000 + '<cite page="3"/>', "</cite>" * 30]


@pytest.mark.parametrize("raw", MALFORMED, ids=[f"malformed{i}" for i in range(len(MALFORMED))])
def test_build_answer_never_raises_on_malformed_output(ctx, raw):
    b = build_answer(raw, ctx)
    assert isinstance(b.text, str) and len(b.citations) == b.stats["n_cites"]
    assert [c.id for c in b.citations] == [f"c{i}" for i in range(1, len(b.citations) + 1)]


def test_build_answer_no_cites_is_just_text(ctx):
    b = build_answer("The report does not say.", ctx)
    assert (b.text, b.citations, b.sources) == ("The report does not say.", [], [])
    assert b.stats == {"n_cites": 0, "n_quote_verified": 0, "n_aligned": 0, "n_page_only": 0, "n_unread_pages": 0, "n_dropped": 0}


def test_build_answer_survives_a_failing_locator(ctx, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("pdfium exploded")

    monkeypatch.setattr(ci, "locate_quote", boom)
    with caplog.at_level(logging.ERROR, logger="reportlens.citations"):
        b = build_answer(f'Claim {cite(4, "Net interest paid (1,588)")} done', ctx)
    assert b.text == "Claim [[c1]] done"
    c = b.citations[0]
    assert (c.page, c.match_method, c.quote_source, c.rects, c.section_path[-1]) == (4, "page", "none", [], "Income and cash flow")
    assert "could not resolve citation 1" in caplog.text


def test_build_answer_text_equals_the_streamed_text(ctx):
    streamed, _, _ = run(ANSWER, [ANSWER[i:i + 5] for i in range(0, len(ANSWER), 5)], page_count=PAGES)
    assert build_answer(ANSWER, ctx).text == streamed.strip()


def test_build_answer_from_several_threads(ctx):
    expected = build_answer(ANSWER, ctx)
    results, errors = [], []

    def work() -> None:
        try:
            for _ in range(5):
                results.append(build_answer(ANSWER, ctx))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(r.text == expected.text and r.citations == expected.citations and r.stats == expected.stats for r in results)


def test_build_answer_is_fast_for_20_cites_on_a_300_page_report():
    data, lines = pf.build_big_pdf(300)
    d = PdfiumDoc(data)
    try:
        pages = list(range(8, 300, 14))[:20]
        raw = "".join(f"Statement {i}. {cite(p, ' '.join(lines[p - 1][4:6]))} " for i, p in enumerate(pages))
        ctx = CitationContext("Big.pdf", d, [{"title": "All", "node_id": "0001", "start_index": 1, "end_index": 300, "nodes": [
            {"title": f"Chapter {k}", "node_id": f"{k + 2:04d}", "start_index": k * 10 + 1, "end_index": k * 10 + 11} for k in range(30)]}],
            [str(p) for p in range(1, 301)])
        for p in pages:
            d.page_words(p)                                         # cold page extraction is paid once per page
        t0 = time.perf_counter()
        b = build_answer(raw, ctx)
        elapsed = time.perf_counter() - t0
        assert b.stats["n_cites"] == 20 and b.stats["n_quote_verified"] == 20 and b.stats["n_page_only"] == 0
        assert elapsed < 1.0, elapsed
        assert [c.page for c in b.citations] == pages
    finally:
        d.close()


# --------------------------------------------------------------------------------------- the generated sample report (optional)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"


@pytest.mark.skipif(not (SAMPLE_DIR / "sample_annual_report.pdf").exists() or not (SAMPLE_DIR / "sample_annual_report.facts.json").exists(),
                    reason="samples/ not generated")
def test_sample_report_facts_resolve_to_their_page_and_folio():
    pdf_path = SAMPLE_DIR / "sample_annual_report.pdf"
    facts = [f for f in json.loads((SAMPLE_DIR / "sample_annual_report.facts.json").read_text(encoding="utf-8")) if f.get("quote")]
    d = PdfiumDoc(pdf_path)
    try:
        ctx = CitationContext("sample_annual_report.pdf", d, [], detect_printed_labels(pdf_path))
        for fact in facts:
            quote = fact["quote"].replace('"', "'")
            built = build_answer(f'{fact["answer"]} <cite doc="sample_annual_report.pdf" page="{fact["page"]}" quote="{quote}"/>', ctx)
            [c] = built.citations
            assert (c.page, c.printed_page, c.quote_source, c.match_method) == (fact["page"], fact["printed_page"], "model", "exact"), fact["id"]
            assert c.rects
    finally:
        d.close()
