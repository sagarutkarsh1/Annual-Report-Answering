"""pdfutil: inspection, clean page text, printed page labels (offline, tiny reportlab PDFs)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from reportlab.lib.pdfencrypt import StandardEncryption

from reportlens import pdfutil
from reportlens.pdfutil import PdfError, clean_text, detect_printed_labels, extract_page_texts, inspect_pdf
from tests import pdf_factory as pf


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> tuple[Path, pf.ReportPdf]:
    r = pf.build_report_pdf()
    path = tmp_path_factory.mktemp("pdfutil") / "report.pdf"
    path.write_bytes(r.data)
    return path, r


def write(tmp_path: Path, data: bytes, name: str = "doc.pdf") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------------------- inspect_pdf


def test_inspect_report(report):
    path, r = report
    info = inspect_pdf(path)
    assert info.page_count == r.page_count == 7
    assert info.text_pages == 7
    assert info.title == "ReportLens test annual report"
    assert len(info.sizes) == 7
    assert info.sizes[0] == pytest.approx((595.28, 841.89), abs=0.01)
    assert info.sizes[5] == pytest.approx((841.89, 595.28), abs=0.01)      # /Rotate 90 page: visible size is landscape


def test_inspect_reports_visible_size_after_rotate_and_crop(tmp_path):
    info = inspect_pdf(write(tmp_path, pf.build_geometry_pdf()))
    assert info.sizes[0] == pytest.approx((500, 530))                       # CropBox 530x500 under /Rotate 90 -> swapped
    assert info.sizes[1] == pytest.approx((595.28, 841.89), abs=0.01)
    assert info.sizes[2] == pytest.approx((841.89, 595.28), abs=0.01)
    assert info.sizes[3] == pytest.approx((530, 500))                       # CropBox only


def test_inspect_scanned_pdf_is_rejected(tmp_path):
    with pytest.raises(PdfError) as exc:
        inspect_pdf(write(tmp_path, pf.build_blank_pdf(4)))
    assert exc.value.code == "scanned_pdf"
    assert "OCR" in exc.value.message


def test_scanned_threshold_is_five_percent_of_pages(tmp_path):
    def doc(n_pages: int, n_text: int) -> Path:
        pages = [["A page that carries enough text to count as one."] if i < n_text else ["tiny"] for i in range(n_pages)]
        return write(tmp_path, pf.build_text_pdf(pages), f"d{n_pages}_{n_text}.pdf")

    assert inspect_pdf(doc(40, 2)).text_pages == 2            # exactly 5 %: accepted
    with pytest.raises(PdfError) as exc:
        inspect_pdf(doc(40, 1))                                # 2.5 %: scanned
    assert exc.value.code == "scanned_pdf"


def test_inspect_encrypted_pdf(tmp_path):
    with pytest.raises(PdfError) as exc:
        inspect_pdf(write(tmp_path, pf.build_encrypted_pdf("secret")))
    assert exc.value.code == "encrypted_pdf"
    assert "password" in exc.value.message.lower()


def test_owner_password_only_pdf_opens(tmp_path):
    data = pf.build_text_pdf([["Readable text on a page that is only owner-protected."]], encrypt=StandardEncryption("", ownerPassword="owner"))
    assert inspect_pdf(write(tmp_path, data)).page_count == 1


@pytest.mark.parametrize("data", [b"", b"hello world", b"%PDF-1.7\nnot really a pdf", b"\x00" * 2048])
def test_inspect_invalid_pdf(tmp_path, data):
    with pytest.raises(PdfError) as exc:
        inspect_pdf(write(tmp_path, data))
    assert exc.value.code == "invalid_pdf"


def test_inspect_truncated_pdf(tmp_path, report):
    _, r = report
    with pytest.raises(PdfError) as exc:
        inspect_pdf(write(tmp_path, r.data[:len(r.data) // 3]))
    assert exc.value.code == "invalid_pdf"


def test_inspect_missing_file(tmp_path):
    with pytest.raises(PdfError) as exc:
        inspect_pdf(tmp_path / "nope.pdf")
    assert exc.value.code == "invalid_pdf"


def test_pdf_file_is_not_locked_afterwards(tmp_path, report):
    _, r = report
    p = write(tmp_path, r.data)
    inspect_pdf(p)
    extract_page_texts(p)
    detect_printed_labels(p)
    p.unlink()                                                # Windows: PermissionError if any handle were left open
    assert not p.exists()


# --------------------------------------------------------------------------------------- extract_page_texts


def test_extract_page_texts_is_clean(report):
    path, r = report
    texts = extract_page_texts(path)
    assert len(texts) == 7
    p3 = texts[2]
    assert "\r" not in "".join(texts)
    assert pdfutil.SOFT_HYPHENS.isdisjoint("".join(texts))
    assert "infrastructure programme" in p3               # hyphenated line end joined
    assert "network reliability was 99.99%" in p3
    assert "efficient financial workflow" in p3           # ligature glyphs expanded
    assert "“Climate Leadership”" in p3                   # curly quotes kept
    assert "\n" in p3 and p3.count("\n") >= 10            # line breaks are kept
    for key in ("D", "E"):                                # digits are intact (PyPDF2 corrupts them on real reports)
        page, text = r.passages[key]
        for number in ("£6,991 million", "£7,281 million", "£41,371", "(1,588)"):
            if number in text:
                assert number in texts[page - 1]


def test_extract_table_row_stays_on_one_line(report):
    path, _ = report
    assert any("Net interest paid" in ln and "(1,588)" in ln and "(1,479)" in ln for ln in extract_page_texts(path)[3].splitlines())


def test_extract_blank_page_gives_empty_string(tmp_path):
    texts = extract_page_texts(write(tmp_path, pf.build_text_pdf([["Some text on the first page."], []])))
    assert texts[0] == "Some text on the first page." and texts[1] == ""


def test_extract_invalid_file_raises(tmp_path):
    with pytest.raises(PdfError):
        extract_page_texts(write(tmp_path, b"nope"))


@pytest.mark.parametrize("raw, expected", [
    ("a\r\nb\rc", "a\nb\nc"),
    ("infra" + chr(0xFFFE) + "structure", "infrastructure"),
    ("net" + chr(2) + "work", "network"),
    ("soft" + chr(0xAD) + "hyphen", "softhyphen"),
    ("a" + chr(0xA0) + "b" + chr(0x202F) + "c", "a b c"),
    ("zero" + chr(0x200B) + "width", "zerowidth"),
    ("e" + chr(0xFB03) + "cient " + chr(0xFB01) + "nal", "effi" + "cient final"),
    ("bell" + chr(7) + "tab\there", "belltab\there"),
    ("a  \nb \n\n\n\nc", "a\nb\n\nc"),
    ("  padded  ", "padded"),
    ("", ""),
])
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


# --------------------------------------------------------------------------------------- detect_printed_labels


def test_labels_for_report_with_unnumbered_cover_and_contents(report):
    path, _ = report
    expected = [None, None, "1", "2", "3", "4", "5"]          # body folio = physical page - 2; includes the /Rotate 90 page
    assert detect_printed_labels(path) == expected
    assert detect_printed_labels(path, extract_page_texts(path)) == expected


def test_labels_gap_filled_inside_the_numbered_span(tmp_path):
    folios = [None, "1", "2", None, "4", "5", None]          # a full-page photo has no folio; the back cover neither
    pages = [[f"Body text of physical page {i + 1}."] for i in range(7)]
    assert detect_printed_labels(write(tmp_path, pf.build_text_pdf(pages, folios))) == [None, "1", "2", "3", "4", "5", None]


def test_labels_roman_front_matter_then_arabic_body(tmp_path):
    folios = [None, "i", "ii", "iii", "iv", "1", "2", "3", "4", "5"]
    pages = [[f"Page {i + 1} of the test report."] for i in range(10)]
    assert detect_printed_labels(write(tmp_path, pf.build_text_pdf(pages, folios))) == folios


def test_labels_two_numbering_runs(tmp_path):
    folios = ["1", "2", "3", "4", "5", "1", "2", "3", "4"]       # a restart (appendix) needs its own offset
    pages = [[f"Page {i + 1}."] for i in range(9)]
    assert detect_printed_labels(write(tmp_path, pf.build_text_pdf(pages, folios))) == folios


def test_labels_ignore_stray_numbers_and_years(tmp_path):
    pages = [[f"Revenue 2024 {1000 + i}", "Net debt 41,371", f"Note {i + 7}"] for i in range(8)]
    folios = [None, None] + [str(i - 1) for i in range(3, 9)]
    got = detect_printed_labels(write(tmp_path, pf.build_text_pdf(pages, folios)))
    assert got == [None, None, "2", "3", "4", "5", "6", "7"]        # body text is not in the header/footer bands


@pytest.mark.parametrize("style", [
    "National Grid | Annual Report 2024/25 | {folio}", "Page {folio} of 308", "- {folio} -", "{folio}   Strategic report", "{folio}", "[{folio}]",
])
def test_labels_from_common_footer_styles(tmp_path, style):
    folios = [None, None, "1", "2", "3", "4"]
    pages = [[f"Body {i}."] for i in range(6)]
    data = pf.build_text_pdf(pages, folios, folio_style=style)
    assert detect_printed_labels(write(tmp_path, data)) == folios


def test_labels_none_without_folios(tmp_path):
    pages = [[f"Body text number {i}."] for i in range(6)]
    assert detect_printed_labels(write(tmp_path, pf.build_text_pdf(pages))) == [None] * 6


def test_labels_single_numbered_page_is_not_enough(tmp_path):
    pages = [["First."], ["Second."], ["Third."]]
    assert detect_printed_labels(write(tmp_path, pf.build_text_pdf(pages, [None, "17", None]))) == [None, None, None]


def test_labels_prefer_the_pdf_page_labels(tmp_path):
    pages = [[f"Page {i + 1}."] for i in range(5)]
    data = pf.build_text_pdf(pages, folios=["9"] * 5, page_labels=[(0, "ROMAN_LOWER", 1, ""), (2, "ARABIC", 1, "")])
    assert detect_printed_labels(write(tmp_path, data)) == ["i", "ii", "1", "2", "3"]


def test_labels_ignore_uninformative_pdf_page_labels(tmp_path):
    pages = [[f"Page {i + 1}."] for i in range(6)]
    folios = [None, None, "1", "2", "3", "4"]
    data = pf.build_text_pdf(pages, folios, page_labels=[(0, "ARABIC", 1, "")])           # plain 1..N says nothing
    assert detect_printed_labels(write(tmp_path, data)) == [None, None, "1", "2", "3", "4"]


def test_labels_skip_blank_pages_given_texts(tmp_path):
    p = write(tmp_path, pf.build_text_pdf([["a"], [], ["b"]], ["1", "2", "3"]))
    assert detect_printed_labels(p, ["a", "", "b"])[1] == "2"            # blank page still inside the numbered span


def test_labels_wrong_length_page_texts_are_ignored(report):
    path, _ = report
    assert detect_printed_labels(path, ["x"]) == [None, None, "1", "2", "3", "4", "5"]


def test_labels_invalid_file_raises(tmp_path):
    with pytest.raises(PdfError):
        detect_printed_labels(write(tmp_path, b"nope"))


# --------------------------------------------------------------------------------------- threads


def test_pdfium_calls_survive_concurrent_threads(report):
    path, _ = report
    errors: list[BaseException] = []
    results: list[list[str]] = []

    def work() -> None:
        try:
            for _ in range(3):
                inspect_pdf(path)
                results.append(extract_page_texts(path))
                detect_printed_labels(path)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(r == results[0] for r in results)
