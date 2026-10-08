"""The synthetic annual report: structure, realism features, facts sidecar and PageIndex Flash compatibility."""
from __future__ import annotations

import re
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from scripts.make_sample_pdf import build_sample_pdf, facts_path, load_facts, min_pages, normalise, verify_facts

REPO_SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample_annual_report.pdf"
FOLIO_RE = re.compile(r"Northbridge Energy plc\s+Annual Report 2025/26\s+(\d+)")


def _open(path: Path) -> pdfium.PdfDocument:
    return pdfium.PdfDocument(str(path))


def _page_texts(path: Path) -> list[str]:
    doc = _open(path)
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range().replace("\r\n", "\n"))
            tp.close()
            page.close()
        return out
    finally:
        doc.close()


def _outline(path: Path) -> list[tuple[int, str, int]]:
    """(level, title, physical page) for every bookmark."""
    doc = _open(path)
    try:
        return [(b.level, b.get_title(), b.get_dest().get_index() + 1) for b in doc.get_toc()]
    finally:
        doc.close()


# ------------------------------------------------------------------------------------------------ build contract
def test_build_is_deterministic(tmp_path):
    a = build_sample_pdf(tmp_path / "a.pdf", pages=45, seed=3)
    b = build_sample_pdf(tmp_path / "b.pdf", pages=45, seed=3)
    c = build_sample_pdf(tmp_path / "c.pdf", pages=45, seed=4)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()
    assert facts_path(a).read_text(encoding="utf-8") == facts_path(b).read_text(encoding="utf-8")


def test_size_and_page_count(sample_pdf):
    assert sample_pdf.stat().st_size < 2 * 1024 * 1024
    doc = _open(sample_pdf)
    try:
        assert len(doc) == 60
    finally:
        doc.close()


def test_committed_sample_is_valid():
    """samples/ ships a ready-made copy for demos; it must stay consistent with its sidecar."""
    assert REPO_SAMPLE.is_file() and facts_path(REPO_SAMPLE).is_file()
    assert REPO_SAMPLE.stat().st_size < 2 * 1024 * 1024
    assert len(_page_texts(REPO_SAMPLE)) == 60
    assert verify_facts(REPO_SAMPLE) == []


@pytest.mark.parametrize("pages,offset", [(min_pages(), 0), (52, 1), (75, 4), (130, 2)])
def test_other_page_counts_and_offsets(tmp_path, pages, offset):
    pdf = build_sample_pdf(tmp_path / "s.pdf", pages=pages, printed_offset=offset)
    assert len(_page_texts(pdf)) == pages
    assert verify_facts(pdf) == []
    for f in load_facts(pdf):
        assert f["printed_page"] == (str(f["page"] - offset) if f["page"] > 2 else None)


def test_too_few_pages_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="pages must be"):
        build_sample_pdf(tmp_path / "s.pdf", pages=min_pages() - 1)


# ------------------------------------------------------------------------------------------------ outline
def test_outline_is_three_levels_deep_and_well_formed(sample_pdf):
    outline = _outline(sample_pdf)
    levels = {lv for lv, _, _ in outline}
    assert levels == {0, 1, 2}
    assert [t for lv, t, _ in outline if lv == 0] == ["Strategic report", "Governance", "Financial statements", "Other information"]
    pages = [p for _, _, p in outline]
    assert pages == sorted(pages) and 3 <= min(pages) and max(pages) <= 60
    for (prev_lv, _, _), (lv, title, _) in zip(outline, outline[1:]):
        assert lv <= prev_lv + 1, f"outline jumps a level at {title!r}"
    titles = [t for _, t, _ in outline]
    assert "Summary cash flow statement" in titles and "Note 29 Financial commitments and contingencies" in titles


def test_every_outline_entry_lands_on_a_page_showing_that_title(sample_pdf):
    texts = _page_texts(sample_pdf)
    for _, title, page in _outline(sample_pdf):
        assert normalise(title) in normalise(texts[page - 1]), f"{title!r} not printed on page {page}"


# ------------------------------------------------------------------------------------------------ folios and contents
def test_printed_folios_are_offset_and_front_matter_is_unnumbered(sample_pdf):
    texts = _page_texts(sample_pdf)
    assert FOLIO_RE.search(texts[0]) is None and FOLIO_RE.search(texts[1]) is None
    for physical in range(3, 61):
        m = FOLIO_RE.search(texts[physical - 1])
        assert m and int(m.group(1)) == physical - 2, f"page {physical}"


def test_contents_page_lists_printed_not_physical_folios(sample_pdf):
    contents = normalise(_page_texts(sample_pdf)[1])
    for _, title, page in _outline(sample_pdf):
        assert re.search(rf"{re.escape(title)} {page - 2}(?!\d)", contents), f"{title!r} -> {page - 2}"


# ------------------------------------------------------------------------------------------------ realism
def test_numbers_are_formatted_like_a_real_report(sample_pdf):
    text = normalise(_page_texts(sample_pdf)[16])        # physical 17: Summary cash flow statement
    assert "Cash generated from operations 6,991" in text
    assert "Capital expenditure (2,596)" in text
    assert "Proceeds from disposals 1,263" in text


def test_curly_quotes_dashes_and_hyphenated_line_ends_exist(sample_pdf):
    joined = "\n".join(_page_texts(sample_pdf))
    assert "’" in joined and "“" in joined and "–" in joined      # apostrophe, quote, en dash
    # pdfium turns a line-end hyphen into U+FFFE and glues the word; either form proves words were broken across lines
    assert joined.count("￾") + len(re.findall(r"\w-\n\w", joined)) >= 10


def test_landscape_and_rotated_pages(sample_pdf):
    doc = _open(sample_pdf)
    try:
        sizes = {i + 1: (round(doc[i].get_width()), round(doc[i].get_height()), doc[i].get_rotation()) for i in range(len(doc))}
    finally:
        doc.close()
    landscape = [p for p, (w, h, rot) in sizes.items() if (w, h) == (842, 595)]
    rotated = [p for p, (_, _, rot) in sizes.items() if rot == 90]
    assert len(landscape) == 2 and len(rotated) == 1           # true landscape MediaBox + a /Rotate 90 page, both 842x595 visible
    assert sizes[rotated[0]][:2] == (842, 595)
    five_year = next(p for _, t, p in _outline(sample_pdf) if t == "Five-year summary")
    assert five_year in landscape


def test_text_is_extractable_in_reading_order_on_two_column_pages(sample_pdf):
    text = normalise(_page_texts(sample_pdf)[3])                # physical 4: Highlights, two columns
    assert text.index("We now connect 8.4 million customers") < text.index("Headline numbers")


def test_cross_reference_uses_the_printed_page_of_the_target(sample_pdf):
    facts = {f["id"]: f for f in load_facts(sample_pdf)}
    xref = facts["note29_xref"]
    cited = int(re.search(r"note 29 on page (\d+)", xref["quote"]).group(1))
    target = next(p for _, t, p in _outline(sample_pdf) if t.startswith("Note 29"))
    assert cited == target - 2                                  # printed folio, not the physical page
    assert xref["related_pages"] == [target]
    assert "29. Financial commitments and contingencies" in _page_texts(sample_pdf)[target - 1]


# ------------------------------------------------------------------------------------------------ facts sidecar
def test_facts_sidecar_contract(sample_pdf, sample_facts):
    assert len(sample_facts) >= 12
    assert len({f["id"] for f in sample_facts}) == len(sample_facts)
    required = {"id", "kind", "question", "answer", "key", "page", "printed_page", "related_pages", "quote", "section_path"}
    for f in sample_facts:
        assert required <= set(f)
        assert f["question"].endswith("?") and f["answer"] and f["key"] and 1 <= f["page"] <= 60
        assert f["printed_page"] == str(f["page"] - 2)
        assert 1 <= len(f["section_path"]) <= 3
        assert len(f["quote"].split()) <= 30
    kinds = {f["kind"] for f in sample_facts}
    assert kinds == {"text", "table", "cross_reference"}
    assert sum(f["kind"] == "table" for f in sample_facts) >= 6    # numeric table facts


def test_every_fact_quote_is_verbatim_on_its_page(sample_pdf, sample_facts):
    assert verify_facts(sample_pdf, sample_facts) == []
    texts = [normalise(t) for t in _page_texts(sample_pdf)]
    for f in sample_facts:
        assert f["key"] in normalise(f["answer"]) or f["key"] in f["quote"]
        assert normalise(f["quote"]) in texts[f["page"] - 1]


def test_verify_facts_reports_a_wrong_quote(sample_pdf, sample_facts):
    broken = [{**sample_facts[0], "quote": "this sentence is not in the report"}]
    problems = verify_facts(sample_pdf, broken)
    assert len(problems) == 1 and sample_facts[0]["id"] in problems[0]


def test_each_fact_page_lies_inside_its_section_path(sample_pdf, sample_facts):
    outline = _outline(sample_pdf)
    for f in sample_facts:
        # walk the outline keeping the open ancestors; the fact's page must fall inside the entry for its section_path
        stack: dict[int, tuple[str, int]] = {}
        spans: dict[tuple[str, ...], tuple[int, int]] = {}
        for i, (lv, title, start) in enumerate(outline):
            for deeper in [d for d in stack if d >= lv]:
                del stack[deeper]
            stack[lv] = (title, start)
            end = next((p for l2, _, p in outline[i + 1:] if l2 <= lv), 61)
            spans[tuple(t for _, (t, _) in sorted(stack.items()))] = (start, end - 1)
        start, end = spans[tuple(f["section_path"])]
        assert start <= f["page"] <= end, f"{f['id']}: page {f['page']} outside {f['section_path']} ({start}-{end})"


# ------------------------------------------------------------------------------------------------ PageIndex compatibility
def test_pageindex_flash_builds_a_tree_from_the_bookmarks(sample_pdf, sample_facts):
    from pageindex.flash import page_index_flash

    result = page_index_flash(str(sample_pdf), summary=False, optimize=False)
    assert result["toc_source"] == "bookmarks"
    top = [n["title"] for n in result["structure"]]
    for title in ("Strategic report", "Governance", "Financial statements", "Other information"):
        assert title in top

    def chains(nodes, page, prefix=()):
        for n in nodes:
            here = (*prefix, n["title"])
            if n["start_index"] <= page <= n["end_index"]:
                yield here
                yield from chains(n.get("nodes") or [], page, here)

    for f in sample_facts:
        wanted = tuple(f["section_path"])
        assert any(c[:len(wanted)] == wanted for c in chains(result["structure"], f["page"])), \
            f"{f['id']}: no tree node chain {wanted} covers page {f['page']}"
