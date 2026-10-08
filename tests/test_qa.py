"""qa: the agent run as a UI event stream.

Part 1 feeds synthetic SDK events (both protocol lanes) through QAEngine with a fake client: streaming, steps, contexts,
errors, cancellation.  Part 2 runs the REAL PageIndex SDK against devtools.mock_openai on the indexed sample report."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterator, Optional

import httpx
import openai
import pytest
from pageindex.errors import PageIndexAPIError

from reportlens import pageindex_compat as compat
from reportlens import qa
from reportlens.citations import CitationContext, strip_markers
from reportlens.locator import PdfiumDoc
from reportlens.models import ContextPage, DocumentInfo
from reportlens.qa import ANSWER_INSTRUCTIONS, QAEngine, QAError, build_instructions, page_numbering_note
from tests import pdf_factory as pf

# ====================================================================================================== synthetic world
PAGES = [
    ["Annual Report 2025/26", "Northbridge Energy plc"],
    ["Contents", "Strategic report 3", "Financial review 5"],
    ["Strategic report", "Group revenue increased by 7.4% to 14,812 million", "in the year ended 31 March 2026."],
    ["Capital expenditure", "We plan capital expenditure of 4,900 million over the", "next five years to deliver our transition plan."],
    ["Financial review", "Cash generated from operations was 6,991 million, mainly", "from customer receipts."],
    ["Outlook", "Management expects underlying growth to continue next year."],
]
TREE = [
    {"title": "Strategic report", "node_id": "0001", "start_index": 3, "end_index": 4},
    {"title": "Financial review", "node_id": "0002", "start_index": 5, "end_index": 6},
]
LABELS = [None, None, "1", "2", "3", "4"]
REVENUE = "Group revenue increased by 7.4% to 14,812 million"
CAPEX = "We plan capital expenditure of 4,900 million over the"
CASH = "Cash generated from operations was 6,991 million, mainly"
DOC = DocumentInfo(id="doc00001", filename="Report.pdf", doc_name="Report.pdf", size_bytes=1, pi_doc_id="pi-test", status="ready")


@pytest.fixture(scope="module")
def pdf() -> Iterator[PdfiumDoc]:
    doc = PdfiumDoc(pf.build_text_pdf(PAGES, folios=LABELS))
    yield doc
    doc.close()


@pytest.fixture
def ctx(pdf) -> CitationContext:
    return CitationContext(doc_display_name="Report.pdf", pdf=pdf, tree=TREE, printed_labels=LABELS)


def cite(page: int, quote: str, doc: str = "Report.pdf") -> str:
    return f'<cite doc="{doc}" page="{page}" quote="{quote}"/>'


# ------------------------------------------------------------------------------------------------ raw SDK event builders
class Ids:
    n = 0

    @classmethod
    def next(cls, prefix: str) -> str:
        cls.n += 1
        return f"{prefix}_{cls.n}"


def reasoning_item(idx: int) -> list[dict]:
    item = {"type": "reasoning", "id": Ids.next("rs"), "summary": []}
    return [{"type": "response.output_item.added", "output_index": idx, "item": item},
            {"type": "response.output_item.done", "output_index": idx, "item": item}]


def call_events(name: str, args: dict, idx: int, call_id: Optional[str] = None) -> tuple[list[dict], str]:
    call_id = call_id or Ids.next("call")
    raw = json.dumps(args)
    item = {"type": "function_call", "id": Ids.next("fc"), "call_id": call_id, "name": name, "arguments": ""}
    evs = [{"type": "response.output_item.added", "output_index": idx, "item": item},
           {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": idx, "delta": raw[:5]},
           {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": idx, "delta": raw[5:]},
           {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": idx, "name": name, "arguments": raw},
           {"type": "response.output_item.done", "output_index": idx, "item": {**item, "arguments": raw, "status": "completed"}}]
    return evs, call_id


def chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def message_events(text: str, idx: int, size: int = 6) -> list[dict]:
    item = {"type": "message", "id": Ids.next("msg"), "role": "assistant", "content": []}
    evs = [{"type": "response.output_item.added", "output_index": idx, "item": item}]
    evs += [{"type": "response.output_text.delta", "item_id": item["id"], "output_index": idx, "content_index": 0, "delta": d}
            for d in chunks(text, size)]
    evs.append({"type": "response.output_item.done", "output_index": idx, "item": item})
    return evs


def page_payload(*page_numbers: int) -> dict:
    return {"success": True, "doc_name": "Report.pdf", "content": [{"page": p, "text": "\n".join(PAGES[p - 1])} for p in page_numbers]}


def tool_output(call_id: str, payload: dict) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": [{"type": "input_text", "text": json.dumps(payload)}]}


USAGE = {"input_tokens": 12_000, "input_tokens_details": {"cached_tokens": 4_000, "cache_write_tokens": 0},
         "output_tokens": 800, "output_tokens_details": {"reasoning_tokens": 300}, "total_tokens": 12_800}


def terminal(items: list[dict], *, kind: str = "response.completed", usage: Optional[dict] = USAGE, output: Optional[list] = None,
             error: Optional[dict] = None, incomplete: Optional[dict] = None, model: str = "gpt-5.6-sol") -> dict:
    status = {"response.completed": "completed", "response.incomplete": "incomplete", "response.failed": "failed"}[kind]
    return {"type": kind, "response": {"status": status, "model": model, "items": items, "output": output or [], "usage": usage,
                                       "error": error, "incomplete_details": incomplete}}


def scenario(answer: str, *, reads: tuple[tuple[int, ...], ...] = ((3,),), size: int = 6, structure: bool = True) -> list[dict]:
    """The run of a well-behaved agent: [outline], then one get_page_content per `reads` entry, then the answer.  Each model
    turn starts with a reasoning item, like a real reasoning model."""
    events: list[dict] = []
    items: list[dict] = []
    if structure:
        evs, cid = call_events("get_document_structure", {"doc_name": "Report.pdf"}, 1)
        events += reasoning_item(0) + evs
        items.append(tool_output(cid, {"success": True, "structure": TREE}))
    for pages in reads:
        spec = ",".join(str(p) for p in pages)
        evs, cid = call_events("get_page_content", {"doc_name": "Report.pdf", "pages": spec}, 1)
        events += reasoning_item(0) + evs
        items.append(tool_output(cid, page_payload(*pages)))
    events += reasoning_item(0) + message_events(answer, 1, size)
    events.append(terminal(items))
    return events


# ------------------------------------------------------------------------------------------------ fake SDK client
class FakeResponsesStream:
    """What `chat(protocol="responses", stream=True)` returns: an iterator of raw events with close()."""

    def __init__(self, events: list, gate: Optional[threading.Event] = None, fail: Optional[BaseException] = None):
        self._events, self._gate, self._fail = events, gate, fail
        self.closed = threading.Event()
        self.yielded = 0
        self._it = self._generate()

    def _generate(self) -> Iterator[Any]:
        try:
            for ev in self._events:
                if self._gate is not None and self.yielded >= 1:
                    self._gate.wait(10)               # the SDK waiting on the network
                self.yielded += 1
                yield ev
            if self._fail is not None:
                raise self._fail
        finally:
            self.closed.set()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self) -> None:
        self.closed.set()
        self._it.close()


class FakeChatStream:
    """What `chat(stream=True)` returns: `.events` (typed dicts) plus close()."""

    def __init__(self, events: list, fail: Optional[BaseException] = None):
        self._events, self._fail = events, fail
        self.closed = threading.Event()
        self.events_gen = self._generate()

    def _generate(self) -> Iterator[Any]:
        yield from self._events
        if self._fail is not None:
            raise self._fail

    @property
    def events(self):
        return self.events_gen

    def close(self) -> None:
        self.closed.set()


class FakeClient:
    def __init__(self, stream):
        self.stream = stream
        self.calls: list[tuple[list, dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.stream


def engine(settings, stream, **overrides) -> tuple[QAEngine, FakeClient]:
    client = FakeClient(stream)
    return QAEngine(settings.with_(**overrides), client_factory=lambda s, sid: client), client


def run(eng: QAEngine, ctx: CitationContext, question: str = "What was revenue?", history: Optional[list] = None, **kw) -> list[dict]:
    return list(eng.ask(session_id="s" * 32, doc=DOC, question=question, history=history or [], ctx=ctx, **kw))


def of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]


def streamed_text(events: list[dict]) -> str:
    return "".join(e["text"] for e in of(events, "token"))


# ====================================================================================================== page numbering note
def test_page_numbering_note_for_a_consistent_offset():
    labels = [None, None] + [str(n) for n in range(1, 40)]
    note = page_numbering_note(labels)
    assert note and "minus 2" in note and "PDF page 89" in note and "87" in note


def test_page_numbering_note_ignores_roman_front_matter_and_gaps():
    labels = ["i", "ii", "iii"] + [None, None] + [str(n) for n in range(1, 20)] + [None] * 5
    note = page_numbering_note(labels)
    assert note and "minus 5" in note


@pytest.mark.parametrize("labels", [
    [None] * 30,                                           # no folios at all
    ["1", "2"],                                            # too few to trust
    [str(n) for n in range(1, 11)] + [str(n + 7) for n in range(11, 21)],   # two different offsets, neither dominant
    ["i", "ii", "iii", "iv", "v", "vi"],                   # Roman only
])
def test_page_numbering_note_is_omitted_when_unknown_or_inconsistent(labels):
    assert page_numbering_note(labels) is None


def test_page_numbering_note_when_printed_equals_physical_or_exceeds_it():
    assert "equals its PDF page" in page_numbering_note([str(n) for n in range(1, 30)])
    note = page_numbering_note([str(n + 3) for n in range(1, 30)])
    assert "plus 3" in note and "PDF page 84" in note


def test_instructions_carry_the_citation_rules_and_the_note(ctx):
    text = build_instructions(ctx)
    for must in ("quote=", "25 words", "no double quotation marks", "PDF page number", "Never cite a page you did not read",
                 "immediately after the claim", "does not contain the answer", "markdown table", "unit, currency and period",
                 "primary financial statements", "physical risk", "capital expenditure", "Outlook and guidance", "cash flow statement"):
        assert must in text, must
    assert "PAGE NUMBERS" in text and "minus 2" in text
    unnumbered = CitationContext("R.pdf", ctx.pdf, TREE, [None, None, "1", "2"])           # too few folios to establish an offset
    assert "PAGE NUMBERS" not in build_instructions(unnumbered)
    assert build_instructions(unnumbered) == ANSWER_INSTRUCTIONS.rstrip()


@pytest.mark.parametrize("spec,page_count,expected", [
    ("88-90", 300, [88, 89, 90]),
    ("3,7,10", 300, [3, 7, 10]),
    ("1-3,7,9-12", 300, [1, 2, 3, 7, 9, 10, 11, 12]),
    ("5, 7 - 9", 300, [5, 7, 8, 9]),
    ("9-7", 300, [7, 8, 9]),
    ("298-305", 300, [298, 299, 300]),             # out-of-range pages are clipped, as the tool reports them instead of failing
    ("0-2", 300, [1, 2]),
    ("500", 300, []),
    ("12", 300, [12]),
    (12, 300, [12]),
    ("abc,,", 300, []),
    (None, 300, []),
    ("1-99999999", 60, list(range(1, 61))),
])
def test_page_specs_expand_like_the_tool_reads_them(spec, page_count, expected):
    assert qa._expand_pages(spec, page_count) == expected


@pytest.mark.parametrize("pages,label,text", [
    ([88, 89, 90], "Read pages 88-90", "88-90"),
    ([5, 7, 8, 9], "Read pages 5,7-9", "5,7-9"),
    ([12], "Read page 12", "12"),
    ([1, 3, 5], "Read pages 1,3,5", "1,3,5"),
    ([], "Read page content", ""),
])
def test_step_labels_compress_page_ranges(pages, label, text):
    assert qa._read_label(pages) == label and qa._format_pages(pages) == text
    assert qa._tool_label("get_page_content", pages) == label


def test_other_tools_get_plain_labels():
    assert qa._tool_label("get_document_structure", []) == "Looked at the document outline"
    assert qa._tool_label("get_document", []) == qa._tool_label("browse_documents", []) == "Checked the document"
    assert qa._tool_label("remove_document", []) == "Used remove_document"
    assert qa._read_label([5], failed=True) == "Could not read page 5"


# ====================================================================================================== responses lane
def test_responses_lane_happy_path(settings, ctx):
    answer = f"Revenue grew 7.4% to 14,812 million. {cite(3, REVENUE)}\n- Capex is 4,900 million. {cite(4, CAPEX)}"
    eng, client = engine(settings, FakeResponsesStream(scenario(answer, reads=((3, 4),))))
    events = run(eng, ctx)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "step" and kinds[-1] == "final"
    assert events[0]["step"].kind == "thinking" and events[0]["step"].status == "running"
    steps = [e["step"] for e in of(events, "step")]
    assert [s.label for s in steps] == ["Thinking", "Looked at the document outline", "Thinking", "Read pages 3-4", "Thinking"]
    assert steps[3].pages == [3, 4] and steps[3].tool == "get_page_content"
    assert {e["step_id"] for e in of(events, "step_done")} == {"s1", "s2", "s3", "s4", "s5"}

    final = events[-1]
    assert final["status"] == "answered"
    assert [c.page for c in final["contexts"]] == [3, 4] and all(isinstance(c, ContextPage) for c in final["contexts"])
    assert "Group revenue" in final["contexts"][0].text
    built = final["answer"]
    assert [c.cited_page for c in built.citations] == [3, 4]
    assert all(c.quote_source == "model" and c.rects for c in built.citations)
    assert built.stats["n_unread_pages"] == 0 and built.stats["n_quote_verified"] == 2
    assert built.text == "Revenue grew 7.4% to 14,812 million. [[c1]]\n- Capex is 4,900 million. [[c2]]"

    # tokens: no tag fragment, markers whole, and together they ARE the final text
    text = streamed_text(events)
    assert "<cite" not in text and "quote=" not in text and "/>" not in text
    assert text.strip() == built.text
    assert [e["text"] for e in of(events, "token") if e["text"].startswith("[")] == ["[[c1]]", "[[c2]]"]
    # citation events arrive with their marker, before the final answer, with the model's quote and no rects
    cits = of(events, "citation")
    assert [c["citation"].id for c in cits] == ["c1", "c2"]
    assert cits[0]["citation"].quote == REVENUE and cits[0]["citation"].rects == [] and cits[0]["citation"].section_path == ["Strategic report"]
    assert cits[0]["citation"].printed_page == "1" and cits[0]["citation"].claim.startswith("Revenue grew")
    assert kinds.index("citation") < kinds.index("final")
    for n, cit in enumerate(cits, 1):                                  # the citation is known before the chip that points at it
        marker = next(i for i, e in enumerate(events) if e["type"] == "token" and e["text"] == f"[[c{n}]]")
        assert events.index(cit) < marker

    assert final["usage"].model == "gpt-5.6-sol" and final["usage"].input_tokens == 12_000 and final["usage"].cached_tokens == 4_000
    assert final["usage"].reasoning_tokens == 300
    assert final["usage"].cost_usd == pytest.approx((8_000 * 4.0 + 4_000 * 0.4 + 800 * 20.0) / 1e6)
    assert final["elapsed_ms"] >= 0 and [s.id for s in final["steps"]] == ["s1", "s2", "s3", "s4", "s5"]
    assert all(s.status == "done" for s in final["steps"])


def test_call_arguments_to_the_sdk(settings, ctx):
    labels = [None, None] + [str(n) for n in range(1, 30)]
    wide = CitationContext("R.pdf", ctx.pdf, TREE, labels)
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1 [[c1]]"},
               {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]
    eng, client = engine(settings, FakeResponsesStream(scenario("Fine.")), history_turns=1, chat_max_turns=7,
                         chat_reasoning_effort="high", chat_model="gpt-5.6-terra")
    run(eng, wide, "Follow-up?", history)
    (messages, kwargs), = client.calls
    assert messages == [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
                        {"role": "user", "content": "Follow-up?"}]                 # last pair only, then the question
    assert kwargs["doc_id"] == "pi-test" and kwargs["stream"] is True and kwargs["citations"] is True
    assert kwargs["protocol"] == "responses" and kwargs["reasoning_effort"] == "high" and kwargs["max_turns"] == 7
    assert kwargs["instructions"].startswith(ANSWER_INSTRUCTIONS) and "PAGE NUMBERS" in kwargs["instructions"]


def test_history_is_cleaned_before_it_reaches_the_agent(settings, ctx):
    history = [{"role": "assistant", "content": "orphan answer"}, {"role": "user", "content": "q1"},
               {"role": "assistant", "content": "Revenue was 5 [[c1]]\n[[c2]] more"}, {"role": "system", "content": "x"},
               {"role": "user", "content": "   "}, {"role": "assistant", "content": "z" * 5000}]
    eng, client = engine(settings, FakeResponsesStream(scenario("Fine.")), history_turns=6)
    run(eng, ctx, "Next?", history)
    messages = client.calls[0][0]
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant", "user"]
    assert messages[1]["content"] == "Revenue was 5\nmore" and len(messages[2]["content"]) == 3000


def test_non_reasoning_models_get_no_reasoning_effort(settings, ctx):
    eng, client = engine(settings, FakeResponsesStream(scenario("Fine.")), chat_model="gpt-4.1", chat_reasoning_effort="medium")
    run(eng, ctx)
    assert client.calls[0][1]["reasoning_effort"] is None


@pytest.mark.parametrize("size", [1, 2, 3, 5, 11, 1000])
def test_markers_stay_whole_and_text_is_independent_of_how_deltas_split(settings, ctx, size):
    answer = (f"Revenue grew 7.4% to 14,812 million. {cite(3, REVENUE)}\n"
              f"| Item | Value |\n|---|---|\n| Capex | 4,900 {cite(4, CAPEX)} |\n"
              f"Cash was 6,991 million. {cite(5, CASH)}")
    eng, _ = engine(settings, FakeResponsesStream(scenario(answer, reads=((3,), (4, 5)), size=size)))
    events = run(eng, ctx)
    tokens = [e["text"] for e in of(events, "token")]
    text = "".join(tokens)
    assert "<" not in text and "quote=" not in text
    assert re.findall(r"\[\[c\d+\]\]", text) == ["[[c1]]", "[[c2]]", "[[c3]]"]
    assert all(re.fullmatch(r"\[\[c\d+\]\]", t) for t in tokens if "[" in t)                 # a marker is never split or merged
    assert text.strip() == events[-1]["answer"].text
    assert [c["citation"].claim for c in of(events, "citation")][1] == "Capex 4,900"         # the table row is the claim


def test_parallel_tool_calls_share_one_thinking_gap_and_all_pages_are_read(settings, ctx):
    a, ca = call_events("get_page_content", {"doc_name": "Report.pdf", "pages": "3"}, 1)
    b, cb = call_events("get_page_content", {"doc_name": "Report.pdf", "pages": "5-6"}, 2)
    events = (reasoning_item(0) + a + b + reasoning_item(0) +
              message_events(f"Revenue 14,812 million. {cite(3, REVENUE)} Cash 6,991 million. {cite(5, CASH)}", 1) +
              [terminal([tool_output(ca, page_payload(3)), tool_output(cb, page_payload(5, 6))])])
    eng, _ = engine(settings, FakeResponsesStream(events))
    out = run(eng, ctx)
    labels = [e["step"].label for e in of(out, "step")]
    assert labels == ["Thinking", "Read page 3", "Read pages 5-6", "Thinking"]          # no thinking step between parallel calls
    first_done = next(i for i, e in enumerate(out) if e["type"] == "step_done" and e["step_id"] != "s1")
    assert [e["step"].id for e in out[:first_done] if e["type"] == "step"] == ["s1", "s2", "s3"]   # both calls started before either ended
    final = out[-1]
    assert [c.page for c in final["contexts"]] == [3, 5, 6] and final["status"] == "answered"


def test_pages_read_twice_appear_once_in_read_order(settings, ctx):
    answer = f"Revenue 14,812 million. {cite(3, REVENUE)}"
    eng, _ = engine(settings, FakeResponsesStream(scenario(answer, reads=((4, 3), (3, 5)))))
    assert [c.page for c in run(eng, ctx)[-1]["contexts"]] == [4, 3, 5]


def test_tool_error_output_marks_the_step_and_reads_nothing(settings, ctx):
    evs, cid = call_events("get_page_content", {"doc_name": "Report.pdf", "pages": "5"}, 1)
    err = {"error": "page out of range", "errorCode": "invalid_pages", "next_steps": {}}
    events = (reasoning_item(0) + evs + reasoning_item(0) + message_events("I could not read that page.", 1) +
              [terminal([tool_output(cid, err)])])
    eng, _ = engine(settings, FakeResponsesStream(events))
    final = run(eng, ctx)[-1]
    assert final["contexts"] == [] and final["status"] == "no_sources"
    assert [s.label for s in final["steps"]][1] == "Could not read page 5"


def test_empty_answer_is_an_error(settings, ctx):
    evs, cid = call_events("get_page_content", {"pages": "3"}, 1)
    events = reasoning_item(0) + evs + [terminal([tool_output(cid, page_payload(3))])]
    eng, _ = engine(settings, FakeResponsesStream(events))
    with pytest.raises(QAError) as err:
        run(eng, ctx)
    assert err.value.code == "agent_failed" and err.value.message == "The model did not return an answer. Try again."


def test_answer_made_only_of_cites_counts_as_empty(settings, ctx):
    eng, _ = engine(settings, FakeResponsesStream(scenario(cite(3, REVENUE))))
    with pytest.raises(QAError) as err:
        run(eng, ctx)
    assert err.value.code == "agent_failed"


def test_answer_without_citations_is_no_sources(settings, ctx):
    eng, _ = engine(settings, FakeResponsesStream(scenario("The report does not state this.", reads=((3,),))))
    final = run(eng, ctx)[-1]
    assert final["status"] == "no_sources" and final["answer"].citations == [] and final["contexts"][0].page == 3


def test_answer_with_citations_but_no_page_read_is_no_sources(settings, ctx):
    eng, _ = engine(settings, FakeResponsesStream(scenario(f"Revenue was 14,812 million. {cite(3, REVENUE)}", reads=())))
    final = run(eng, ctx)[-1]
    assert final["status"] == "no_sources" and final["contexts"] == []
    assert final["answer"].stats["n_unread_pages"] == 1


def test_cite_of_an_unread_page_is_counted_but_kept(settings, ctx):
    answer = f"Revenue 14,812 million. {cite(3, REVENUE)} Cash 6,991 million. {cite(5, CASH)}"
    eng, _ = engine(settings, FakeResponsesStream(scenario(answer, reads=((3,),))))
    final = run(eng, ctx)[-1]
    assert final["status"] == "answered" and final["answer"].stats["n_unread_pages"] == 1 and len(final["answer"].citations) == 2


def test_cite_to_a_page_beyond_the_document_is_dropped(settings, ctx):
    answer = f"Revenue 14,812 million. {cite(3, REVENUE)} Nonsense. {cite(99, 'nothing here at all really')}"
    eng, _ = engine(settings, FakeResponsesStream(scenario(answer)))
    events = run(eng, ctx)
    assert [c["citation"].id for c in of(events, "citation")] == ["c1"]
    assert "[[c2]]" not in streamed_text(events) and events[-1]["answer"].stats["n_dropped"] == 1


def test_narration_before_a_tool_call_never_reaches_the_user(settings, ctx):
    evs, cid = call_events("get_page_content", {"pages": "3"}, 2)
    answer = f"Revenue grew to 14,812 million. {cite(3, REVENUE)}"
    events = (reasoning_item(0) + message_events("Let me look at the report first.", 1) + evs + reasoning_item(0) +
              message_events(answer, 1) + [terminal([tool_output(cid, page_payload(3))])])
    eng, _ = engine(settings, FakeResponsesStream(events))
    out = run(eng, ctx)
    assert "Let me look" not in streamed_text(out) and "Let me look" not in out[-1]["answer"].text
    assert streamed_text(out).strip() == out[-1]["answer"].text


def test_long_narration_may_leak_into_the_stream_but_not_into_the_answer(settings, ctx):
    evs, cid = call_events("get_page_content", {"pages": "3"}, 2)
    narration = "I will now read the strategic report carefully and thoroughly. " * 6
    events = (reasoning_item(0) + message_events(narration, 1, 40) + evs + reasoning_item(0) +
              message_events(f"Revenue 14,812 million. {cite(3, REVENUE)}", 1) + [terminal([tool_output(cid, page_payload(3))])])
    eng, _ = engine(settings, FakeResponsesStream(events))
    out = run(eng, ctx)
    assert "carefully" not in out[-1]["answer"].text and out[-1]["answer"].text == "Revenue 14,812 million. [[c1]]"
    assert [c["citation"].id for c in of(out, "citation")] == ["c1"]                   # numbering restarts for the real answer


def test_text_only_in_the_final_envelope_is_still_delivered(settings, ctx):
    answer = f"Revenue 14,812 million. {cite(3, REVENUE)}"
    evs, cid = call_events("get_page_content", {"pages": "3"}, 0)
    output = [{"type": "function_call", "name": "get_page_content", "call_id": cid},
              {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}]
    events = evs + [terminal([tool_output(cid, page_payload(3))], output=output)]
    eng, _ = engine(settings, FakeResponsesStream(events))
    out = run(eng, ctx)
    assert streamed_text(out).strip() == "Revenue 14,812 million. [[c1]]" and out[-1]["status"] == "answered"


def test_failed_terminal_event_maps_to_the_error_code(settings, ctx):
    err = {"code": "invalid_api_key", "message": "bad key"}
    eng, _ = engine(settings, FakeResponsesStream([terminal([], kind="response.failed", error=err)]))
    with pytest.raises(QAError) as exc:
        run(eng, ctx)
    assert exc.value.code == "openai_auth"


def test_incomplete_without_text_is_an_error_and_with_text_is_delivered(settings, ctx):
    eng, _ = engine(settings, FakeResponsesStream([terminal([], kind="response.incomplete", incomplete={"reason": "max_output_tokens"})]))
    with pytest.raises(QAError) as exc:
        run(eng, ctx)
    assert exc.value.code == "agent_failed" and "max_output_tokens" in exc.value.message
    events = scenario(f"Partial answer. {cite(3, REVENUE)}")
    events[-1] = terminal(events[-1]["response"]["items"], kind="response.incomplete", incomplete={"reason": "max_output_tokens"})
    eng, _ = engine(settings, FakeResponsesStream(events))
    assert run(eng, ctx)[-1]["type"] == "final"


def test_stream_that_ends_without_a_terminal_event_still_answers_if_text_arrived(settings, ctx):
    events = scenario(f"Revenue 14,812 million. {cite(3, REVENUE)}")[:-1]
    eng, _ = engine(settings, FakeResponsesStream(events))
    final = run(eng, ctx)[-1]
    assert final["status"] == "no_sources" and final["usage"].input_tokens == 0 and final["usage"].cost_usd is None


def test_unknown_model_has_tokens_but_no_cost(settings, ctx):
    events = scenario("Fine.")
    events[-1]["response"]["model"] = "some-future-model"
    eng, _ = engine(settings, FakeResponsesStream(events))
    usage = run(eng, ctx)[-1]["usage"]
    assert usage.model == "some-future-model" and usage.input_tokens == 12_000 and usage.cost_usd is None


def test_cache_writes_are_priced_and_the_summed_prompt_does_not_trigger_the_surcharge(settings, ctx):
    big = {"input_tokens": 300_000, "input_tokens_details": {"cached_tokens": 100_000, "cache_write_tokens": 20_000},
           "output_tokens": 1_000, "output_tokens_details": {"reasoning_tokens": 0}}
    events = scenario("Fine.", reads=((3,), (4,)))
    events[-1]["response"]["usage"] = big
    eng, _ = engine(settings, FakeResponsesStream(events))
    cost = run(eng, ctx)[-1]["usage"].cost_usd
    assert cost == pytest.approx((180_000 * 4.0 + 100_000 * 0.4 + 20_000 * 5.0 + 1_000 * 20.0) / 1e6)     # 4 requests: no surcharge


# ------------------------------------------------------------------------------------------------ step timing (deterministic clock)
class ManualClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def timed(clock: ManualClock, parts: list[tuple[float, Any]]) -> Iterator[Any]:
    """Events, each preceded by a clock advance; consumed in the same thread, so the advance lands before the event is seen."""
    for dt, ev in parts:
        clock.t += dt
        yield ev


def drive(ctx, parts, clock) -> tuple[list[dict], "qa._Run"]:
    run_ = qa._Run(ctx, clock)
    out = list(run_.start())
    yield_from(run_.consume_responses(timed(clock, parts)), out)
    out += list(run_.finish())
    return out, run_


def yield_from(gen, sink: list) -> Any:
    try:
        while True:
            sink.append(next(gen))
    except StopIteration as stop:
        return stop.value


def test_thinking_steps_cover_the_gap_before_each_output_and_tool_steps_their_own_span(ctx):
    clock = ManualClock()
    a, ca = call_events("get_document_structure", {}, 1, "c1")
    b, cb = call_events("get_page_content", {"pages": "3"}, 1, "c2")
    parts = [(2.0, reasoning_item(0)[0]),                       # turn 1 reasons ...
             (3.0, a[0]), (0.1, a[3]), (0.1, a[4]),             # ... 5 s in, it calls the outline tool (call spans 0.2 s)
             (6.0, reasoning_item(0)[0]),                       # tool ran; turn 2 reasons for 6 s ...
             (0.5, b[0]), (0.1, b[3]), (0.1, b[4]),
             (4.0, reasoning_item(0)[0]),                       # turn 3
             (1.0, message_events("Done.", 1, 100)[0]), (0.0, message_events("Done.", 1, 100)[1]),
             (0.2, terminal([]))]
    out, run_ = drive(ctx, parts, clock)
    by_id = {s.id: s for s in run_.steps}
    labels = [(s.label, s.elapsed_ms) for s in run_.steps]
    assert labels == [("Thinking", 5000), ("Looked at the document outline", 200), ("Thinking", 6500),
                      ("Read page 3", 200), ("Thinking", 5000)]
    assert all(s.status == "done" for s in by_id.values())
    assert [e["type"] for e in out].count("step") == 5 == [e["type"] for e in out].count("step_done")


def test_back_to_back_tool_calls_without_a_reasoning_item_split_on_a_pause(ctx):
    clock = ManualClock()
    a, _ = call_events("get_document_structure", {}, 0, "c1")
    b, _ = call_events("get_page_content", {"pages": "3"}, 1, "c2")
    c, _ = call_events("get_page_content", {"pages": "4"}, 2, "c3")
    parts = [(1.0, a[0]), (0.0, a[4]),
             (2.0, b[0]), (0.0, b[4]),                          # 2 s after the previous call: a new model turn
             (0.01, c[0]), (0.0, c[4])]                         # 10 ms after: a parallel call of the same turn
    _, run_ = drive(ctx, parts, clock)
    assert [(s.label, s.kind) for s in run_.steps] == [
        ("Thinking", "thinking"), ("Looked at the document outline", "tool"), ("Thinking", "thinking"),
        ("Read page 3", "tool"), ("Read page 4", "tool")]
    assert run_.steps[2].elapsed_ms == 2000


def test_answer_without_tools_closes_the_first_thinking_step_at_the_first_token(ctx):
    clock = ManualClock()
    msg = message_events("No tools needed.", 0, 100)
    _, run_ = drive(ctx, [(2.5, msg[0]), (0.0, msg[1]), (0.0, terminal([]))], clock)
    assert [(s.kind, s.elapsed_ms) for s in run_.steps] == [("thinking", 2500)]


# ====================================================================================================== chat lane
def chat_events(answer: str, *, size: int = 6) -> list[dict]:
    def out(payload: dict) -> dict:
        return {"type": "text", "text": json.dumps(payload)}
    events: list[dict] = [
        {"type": "tool_call", "call_id": "c1", "name": "get_document_structure", "arguments": {"doc_name": "Report.pdf"}},
        {"type": "tool_result", "call_id": "c1", "name": "get_document_structure", "output": out({"success": True, "structure": TREE})},
        {"type": "tool_call", "call_id": "c2", "name": "get_page_content", "arguments": {"doc_name": "Report.pdf", "pages": "3-6"}},
        {"type": "tool_result", "call_id": "c2", "name": "get_page_content", "output": out(page_payload(3, 4))}]   # only 3-4 came back
    events += [{"type": "answer", "delta": d} for d in chunks(answer, size)]
    return events


def test_chat_lane_happy_path(settings, ctx):
    answer = f"Revenue grew 7.4% to 14,812 million. {cite(3, REVENUE)}\n- Capex is 4,900 million. {cite(4, CAPEX)}"
    eng, client = engine(settings, FakeChatStream(chat_events(answer)), chat_protocol="chat", chat_model="gpt-4.1")
    events = run(eng, ctx)
    (messages, kwargs), = client.calls
    assert "protocol" not in kwargs and kwargs["stream"] is True and kwargs["citations"] is True and kwargs["reasoning_effort"] is None
    steps = [e["step"] for e in of(events, "step")]
    assert [s.label for s in steps] == ["Thinking", "Looked at the document outline", "Thinking", "Read pages 3-6", "Thinking"]
    done = {e["step_id"]: e for e in of(events, "step_done")}
    assert done["s4"]["label"] == "Read pages 3-4" and done["s4"]["pages"] == [3, 4]      # refined to what was really returned
    assert done["s2"]["elapsed_ms"] >= 0 and set(done) == {"s1", "s2", "s3", "s4", "s5"}
    final = events[-1]
    assert final["status"] == "answered" and [c.page for c in final["contexts"]] == [3, 4]
    assert final["answer"].text == "Revenue grew 7.4% to 14,812 million. [[c1]]\n- Capex is 4,900 million. [[c2]]"
    assert streamed_text(events).strip() == final["answer"].text and "<cite" not in streamed_text(events)
    assert final["usage"].model == "gpt-4.1" and final["usage"].input_tokens == 0 and final["usage"].cost_usd is None   # the lane reports none
    assert client.stream.closed.is_set()


def test_chat_lane_discards_narration_thinking_and_closes_the_stream_on_error(settings, ctx):
    events = [{"type": "thinking", "delta": "hmm"}, {"type": "answer", "delta": "Checking. "}] + chat_events(f"Fine. {cite(3, REVENUE)}")
    eng, client = engine(settings, FakeChatStream(events), chat_protocol="chat")
    out = run(eng, ctx)
    assert "Checking" not in streamed_text(out) and out[-1]["answer"].text == "Fine. [[c1]]"
    failing = FakeChatStream(chat_events("x")[:2], fail=PageIndexAPIError("The model backend failed: boom"))
    eng, _ = engine(settings, failing, chat_protocol="chat")
    with pytest.raises(QAError) as exc:
        run(eng, ctx)
    assert exc.value.code == "agent_failed" and "boom" in exc.value.message and failing.closed.is_set()


def test_chat_lane_parallel_calls_and_a_failed_tool(settings, ctx):
    def out(payload: dict) -> dict:
        return {"type": "text", "text": json.dumps(payload)}
    events = [
        {"type": "tool_call", "call_id": "a", "name": "get_page_content", "arguments": {"pages": "3"}},
        {"type": "tool_call", "call_id": "b", "name": "get_page_content", "arguments": {"pages": "5"}},
        {"type": "tool_result", "call_id": "a", "name": "get_page_content", "output": out(page_payload(3))},
        {"type": "tool_result", "call_id": "b", "name": "get_page_content", "output": out({"error": "boom", "errorCode": "x"})},
        *[{"type": "answer", "delta": d} for d in chunks(f"Revenue 14,812 million. {cite(3, REVENUE)}", 4)]]
    eng, _ = engine(settings, FakeChatStream(events), chat_protocol="chat")
    out_events = run(eng, ctx)
    assert [e["step"].label for e in of(out_events, "step")] == ["Thinking", "Read page 3", "Read page 5", "Thinking"]
    done = {e["step_id"]: e["label"] for e in of(out_events, "step_done")}
    assert done == {"s1": "Thinking", "s2": "Read page 3", "s3": "Could not read page 5", "s4": "Thinking"}
    assert [c.page for c in out_events[-1]["contexts"]] == [3] and out_events[-1]["status"] == "answered"


# ====================================================================================================== errors
def status_error(cls, status: int, message: str, code: Optional[str] = None):
    request = httpx.Request("POST", "http://mock/v1/responses")
    response = httpx.Response(status, request=request, json={"error": {"message": message, "code": code}})
    return cls(message, response=response, body={"message": message, "code": code})


def wrapped(cause: BaseException, text: str = "The model backend failed") -> PageIndexAPIError:
    """The SDK's own wrapping: PageIndexAPIError(...) raised `from` the OpenAI error."""
    try:
        raise PageIndexAPIError(f"{text}: {cause}", status_code=getattr(cause, "status_code", None)) from cause
    except PageIndexAPIError as exc:
        return exc


ERROR_CASES = [
    (status_error(openai.AuthenticationError, 401, "Incorrect API key provided", "invalid_api_key"), "openai_auth", "rejected the API key"),
    (status_error(openai.PermissionDeniedError, 403, "Country, region, or territory not supported"), "openai_auth", "rejected the API key"),
    (status_error(openai.RateLimitError, 429, "Rate limit reached for gpt-5.6-sol", "rate_limit_exceeded"), "openai_rate_limit", "rate limit"),
    (status_error(openai.RateLimitError, 429, "You exceeded your current quota", "insufficient_quota"), "openai_rate_limit", "out of credit"),
    (status_error(openai.RateLimitError, 429, "Spend limit", "project_spend_limit_exceeded"), "openai_rate_limit", "out of credit"),
    (status_error(openai.NotFoundError, 404, "The model `gpt-5.6-sol` does not exist", "model_not_found"), "openai_model", "PI_CHAT_MODEL"),
    (status_error(openai.BadRequestError, 400, "The requested model 'gpt-9' does not exist or you do not have access", None), "openai_model", "gpt-5.6-sol"),
    (status_error(openai.BadRequestError, 400, "Function tools with reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions"),
     "openai_model", "PI_CHAT_PROTOCOL=responses"),
    (status_error(openai.BadRequestError, 400, "Unsupported value: 'temperature'"), "agent_failed", "temperature"),
    (status_error(openai.InternalServerError, 500, "The server had an error"), "agent_failed", "HTTP 500"),
    (openai.APIConnectionError(request=httpx.Request("POST", "http://mock/v1/responses")), "agent_failed", "Could not reach OpenAI"),
    (openai.APITimeoutError(request=httpx.Request("POST", "http://mock/v1/responses")), "agent_failed", "Could not reach OpenAI"),
    (PageIndexAPIError("The agent did not finish within max_turns (12). Raise max_turns, or narrow the question."), "agent_max_turns",
     "PI_CHAT_MAX_TURNS"),
    (PageIndexAPIError("Documents not found or access denied: pi-x"), "agent_failed", "not found"),
    (PageIndexAPIError("nope", status_code=401), "openai_auth", "API key"),
    (RuntimeError("secret internals: C:\\Users\\x\\file.py line 3"), "agent_failed", "stopped unexpectedly"),
]


@pytest.mark.parametrize("exc,code,fragment", ERROR_CASES, ids=[f"{c[1]}-{i}" for i, c in enumerate(ERROR_CASES)])
@pytest.mark.parametrize("lane", ["raw", "wrapped"])
def test_errors_from_the_stream_map_to_qa_errors(settings, ctx, exc, code, fragment, lane):
    raised = wrapped(exc) if lane == "wrapped" and isinstance(exc, openai.OpenAIError) else exc
    eng, _ = engine(settings, FakeResponsesStream([], fail=raised))
    with pytest.raises(QAError) as err:
        run(eng, ctx)
    assert err.value.code == code
    assert fragment.lower() in err.value.message.lower()
    assert "Traceback" not in err.value.message and "C:\\" not in err.value.message and len(err.value.message) < 300


def test_max_turns_exceeded_from_the_agents_sdk_is_recognised_through_its_wrapper(settings, ctx):
    from agents.exceptions import MaxTurnsExceeded
    try:
        try:
            raise MaxTurnsExceeded("Max turns (12) exceeded")
        except MaxTurnsExceeded as inner:
            raise PageIndexAPIError("The agent did not finish within max_turns (12).") from inner
    except PageIndexAPIError as exc:
        eng, _ = engine(settings, FakeResponsesStream([], fail=exc))
    with pytest.raises(QAError) as err:
        run(eng, ctx)
    assert err.value.code == "agent_max_turns" and "PI_CHAT_MAX_TURNS" in err.value.message


def test_a_failure_to_start_the_agent_is_mapped_too(settings, ctx):
    def factory(s, sid):
        raise PageIndexAPIError("The OpenAI backend is not configured: no key")
    eng = QAEngine(settings, client_factory=factory)
    with pytest.raises(QAError) as err:
        run(eng, ctx)
    assert err.value.code == "agent_failed" and "not configured" in err.value.message


def test_document_without_a_pageindex_id_is_refused(settings, ctx):
    eng, client = engine(settings, FakeResponsesStream([]))
    with pytest.raises(QAError) as err:
        list(eng.ask(session_id="s" * 32, doc=DOC.model_copy(update={"pi_doc_id": None}), question="q", history=[], ctx=ctx))
    assert err.value.code == "agent_failed" and client.calls == []


def test_errors_are_logged_with_a_traceback_but_never_shown(settings, ctx, caplog):
    eng, _ = engine(settings, FakeResponsesStream([], fail=RuntimeError("kaboom")))
    with caplog.at_level("ERROR", logger="reportlens.qa"), pytest.raises(QAError):
        run(eng, ctx)
    assert any(r.exc_info and "kaboom" in str(r.exc_info[1]) for r in caplog.records)


@pytest.mark.parametrize("exc, code", [
    (status_error(openai.AuthenticationError, 401, "Incorrect API key provided: sk-proj-abc***xyz", "invalid_api_key"), "openai_auth"),
    (status_error(openai.RateLimitError, 429, "Rate limit reached", "rate_limit_exceeded"), "openai_rate_limit"),
])
def test_known_openai_failures_are_one_warning_without_a_traceback(settings, ctx, caplog, exc, code):
    eng, _ = engine(settings, FakeResponsesStream([], fail=exc))
    with caplog.at_level("DEBUG", logger="reportlens.qa"), pytest.raises(QAError) as err:
        run(eng, ctx)
    assert err.value.code == code
    mine = [r for r in caplog.records if r.name == "reportlens.qa"]
    assert [r.levelname for r in mine] == ["WARNING"] and not any(r.exc_info for r in mine)
    assert code in mine[0].getMessage() and "sk-" not in mine[0].getMessage()


# ====================================================================================================== cancellation
def test_cancel_while_the_sdk_is_waiting_raises_promptly_and_closes_the_stream(settings, ctx):
    gate = threading.Event()
    stream = FakeResponsesStream(scenario("x"), gate=gate)
    eng, _ = engine(settings, stream)
    cancel = threading.Event()
    gen = eng.ask(session_id="s" * 32, doc=DOC, question="q", history=[], ctx=ctx, cancel=cancel)
    assert next(gen)["type"] == "step"
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(QAError) as err:
        for _ in gen:
            pass
    assert err.value.code == "cancelled" and time.monotonic() - started < 2.0       # the SDK is still blocked: ask() did not wait for it
    assert not stream.closed.is_set()
    gate.set()                                                                      # the SDK yields its next event ...
    assert stream.closed.wait(5)                                                    # ... and the run is torn down, not continued
    assert stream.yielded <= 2


def test_cancel_between_events_stops_consuming(settings, ctx):
    stream = FakeResponsesStream(scenario("A fairly long answer. " * 20, size=3))
    eng, _ = engine(settings, stream)
    cancel = threading.Event()
    with pytest.raises(QAError) as err:
        for ev in eng.ask(session_id="s" * 32, doc=DOC, question="q", history=[], ctx=ctx, cancel=cancel):
            if ev["type"] == "token":
                cancel.set()
    assert err.value.code == "cancelled" and stream.closed.wait(5)


def test_cancel_set_before_the_call_never_starts_the_agent(settings, ctx):
    eng, client = engine(settings, FakeResponsesStream(scenario("x")))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(QAError) as err:
        run(eng, ctx, cancel=cancel)
    assert err.value.code == "cancelled" and client.calls == []


def test_closing_the_generator_stops_the_run(settings, ctx):
    stream = FakeResponsesStream(scenario("A fairly long answer. " * 20, size=3))
    eng, _ = engine(settings, stream)
    gen = eng.ask(session_id="s" * 32, doc=DOC, question="q", history=[], ctx=ctx)
    for ev in gen:
        if ev["type"] == "token":
            break
    gen.close()
    assert stream.closed.wait(5)


def test_no_helper_thread_survives_a_finished_run(settings, ctx):
    eng, _ = engine(settings, FakeResponsesStream(scenario("Fine.")))
    run(eng, ctx)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(t.name == "qa-agent-stream" and t.is_alive() for t in threading.enumerate()):
        time.sleep(0.05)
    assert not any(t.name == "qa-agent-stream" and t.is_alive() for t in threading.enumerate())


# ====================================================================================================== rewrite_question
class RewriteServer:
    """A one-endpoint OpenAI stand-in: chat completions with a fixed reply (or a delay / an HTTP failure)."""

    def __init__(self, reply: str = "What was Northbridge's revenue in 2024/25?", status: int = 200, delay: float = 0.0):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append({"path": self.path, "json": body})
                time.sleep(delay)
                payload = ({"id": "x", "object": "chat.completion", "created": 1, "model": body.get("model"),
                            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": reply}}]}
                           if status == 200 else {"error": {"message": "boom", "code": "server_error"}})
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)


HISTORY = [{"role": "user", "content": "What was Northbridge's revenue in 2025/26?"},
           {"role": "assistant", "content": "Revenue was 14,812 million. [[c1]]"}]


@pytest.fixture
def rewrite_server():
    servers: list[RewriteServer] = []

    def make(**kw) -> RewriteServer:
        servers.append(RewriteServer(**kw))
        return servers[-1]
    yield make
    for s in servers:
        s.stop()


def test_rewrite_without_history_makes_no_call(settings, rewrite_server):
    server = rewrite_server()
    eng = QAEngine(settings.with_(openai_base_url=server.base_url))
    assert eng.rewrite_question([], "What was revenue?") == "What was revenue?"
    assert server.requests == []


def test_rewrite_turns_a_follow_up_into_a_standalone_question(settings, rewrite_server):
    server = rewrite_server(reply='"What was Northbridge\'s revenue in 2024/25?"\n')
    eng = QAEngine(settings.with_(openai_base_url=server.base_url, question_rewrite_model="gpt-4.1-mini"))
    assert eng.rewrite_question(HISTORY, "And in 2024/25?") == "What was Northbridge's revenue in 2024/25?"
    (req,) = server.requests
    assert req["path"].endswith("/chat/completions") and req["json"]["model"] == "gpt-4.1-mini"
    prompt = json.dumps(req["json"]["messages"])
    assert "And in 2024/25?" in prompt and "14,812 million" in prompt and "[[c1]]" not in prompt
    assert "standalone" in req["json"]["messages"][0]["content"] and "reasoning_effort" not in req["json"]


def test_rewrite_accepts_the_question_history_argument_order(settings, rewrite_server):
    server = rewrite_server(reply="Standalone question here?")
    eng = QAEngine(settings.with_(openai_base_url=server.base_url))
    assert eng.rewrite_question("And in 2024/25?", HISTORY) == "Standalone question here?"


@pytest.mark.parametrize("make_server", [
    lambda mk: mk(status=500),
    lambda mk: mk(reply=""),
    lambda mk: mk(reply="x" * 3000),
])
def test_rewrite_falls_back_to_the_question_on_any_failure(settings, rewrite_server, make_server):
    server = make_server(rewrite_server)
    eng = QAEngine(settings.with_(openai_base_url=server.base_url))
    assert eng.rewrite_question(HISTORY, "And in 2024/25?") == "And in 2024/25?"


def test_rewrite_falls_back_when_the_server_is_unreachable_or_there_is_no_key(settings):
    eng = QAEngine(settings.with_(openai_base_url="http://127.0.0.1:9/v1"))
    started = time.monotonic()
    assert eng.rewrite_question(HISTORY, "And in 2024/25?") == "And in 2024/25?"
    assert time.monotonic() - started < 10
    eng = QAEngine(settings.with_(openai_api_key=None, openai_base_url="http://127.0.0.1:9/v1"))
    assert eng.rewrite_question(HISTORY, "And in 2024/25?") == "And in 2024/25?"


def test_rewrite_times_out_instead_of_hanging(settings, rewrite_server, monkeypatch):
    monkeypatch.setattr(qa, "_REWRITE_TIMEOUT_S", 0.4)
    server = rewrite_server(delay=3.0)
    eng = QAEngine(settings.with_(openai_base_url=server.base_url))
    started = time.monotonic()
    assert eng.rewrite_question(HISTORY, "And in 2024/25?") == "And in 2024/25?"
    assert time.monotonic() - started < 2.5


def test_rewrite_never_retries(settings, rewrite_server):
    server = rewrite_server(status=500)
    QAEngine(settings.with_(openai_base_url=server.base_url)).rewrite_question(HISTORY, "q?")
    assert len(server.requests) == 1


# ====================================================================================================== real SDK + mock server
@pytest.fixture(scope="module")
def world(tmp_path_factory, sample_pdf):
    """The sample report indexed ONCE by the real PageIndex SDK against a throw-away mock OpenAI server (the store holds no URL)."""
    from devtools.mock_openai import start_mock_server
    from reportlens.config import load_settings
    from reportlens.pdfutil import detect_printed_labels
    from scripts.make_sample_pdf import load_facts

    server = start_mock_server()
    base = load_settings(environ={"OPENAI_API_KEY": "sk-test-not-real", "OPENAI_BASE_URL": server.base_url})
    settings = base.with_(data_dir=tmp_path_factory.mktemp("qa-world") / "data")
    sid = "a" * 32
    indexed = compat.make_client(settings, sid, for_indexing=True).submit_document(str(sample_pdf))
    pi_doc_id = indexed["doc_id"]
    tree = compat.make_client(settings, sid).get_tree(pi_doc_id, include_text=False)["result"]
    pdf = PdfiumDoc(sample_pdf)
    doc = DocumentInfo(id="docsample", filename=sample_pdf.name, doc_name=sample_pdf.name, size_bytes=1, pi_doc_id=pi_doc_id,
                       status="ready", page_count=pdf.page_count)
    ctx = CitationContext(sample_pdf.name, pdf, tree, detect_printed_labels(sample_pdf))
    server.stop()
    facts = [f for f in load_facts(sample_pdf) if f["kind"] in ("text", "table")]
    yield {"settings": settings.with_(openai_base_url=None), "sid": sid, "doc": doc, "ctx": ctx, "facts": facts}
    pdf.close()
    compat.restore_openai_env()


@pytest.fixture
def live(world):
    """The indexed world plus a fresh mock server per test: no request log, scripted failure or delay leaks between tests."""
    from devtools.mock_openai import start_mock_server
    server = start_mock_server()
    yield {**world, "server": server, "settings": world["settings"].with_(openai_base_url=server.base_url)}
    server.stop()


def ask_real(w, question: str, *, protocol: str = "responses", history: Optional[list] = None, cancel=None,
             model: Optional[str] = None) -> Iterator[dict]:
    settings = w["settings"].with_(chat_protocol=protocol, **({"chat_model": model} if model else {}))
    return QAEngine(settings).ask(session_id=w["sid"], doc=w["doc"], question=question, history=history or [], ctx=w["ctx"], cancel=cancel)


def agent_requests(w) -> list[dict]:
    return w["server"].requests_of("agent")


@pytest.mark.slow
@pytest.mark.parametrize("protocol", ["responses", "chat"])
@pytest.mark.parametrize("fact_id", ["customers_connected", "ltifr", "tax_rate", "capex", "net_debt"])
def test_real_sdk_answers_a_known_fact(live, protocol, fact_id):
    fact = next(f for f in live["facts"] if f["id"] == fact_id)
    events = list(ask_real(live, fact["question"], protocol=protocol))
    final = events[-1]
    assert final["type"] == "final" and final["status"] == "answered"

    steps = [e["step"] for e in of(events, "step")]
    tools = [s.tool for s in steps if s.kind == "tool"]
    assert tools[0] == "get_document_structure" and "get_page_content" in tools
    assert steps[0].kind == "thinking" and tools.index("get_document_structure") < tools.index("get_page_content")
    read_labels = [s.label for s in steps if s.tool == "get_page_content"]
    assert all(re.fullmatch(r"Read pages? [\d,-]+", label) for label in read_labels)

    assert fact["page"] in {c.page for c in final["contexts"]}
    built = final["answer"]
    located = [c for c in built.citations if c.page == fact["page"] and c.quote_source == "model" and c.rects]
    assert located, [c.model_dump(exclude={"rects"}) for c in built.citations]
    assert fact["key"] in built.text or any(fact["key"] in (c.quote or "") for c in built.citations)
    assert all(c.printed_page for c in built.citations) and located[0].section_path
    assert built.stats["n_unread_pages"] == 0 and built.stats["n_dropped"] == 0

    text = streamed_text(events)
    assert "<cite" not in text and "quote=" not in text
    assert text.strip() == built.text
    assert re.findall(r"\[\[c\d+\]\]", text) == [f"[[c{i}]]" for i in range(1, len(built.citations) + 1)]
    assert [e["citation"].id for e in of(events, "citation")] == [c.id for c in built.citations]
    assert len(of(events, "step_done")) == len(steps)

    if protocol == "responses":
        usage = final["usage"]
        assert usage.input_tokens > 0 and usage.output_tokens > 0 and usage.model == "gpt-5.6-sol"
        assert usage.cost_usd is not None and 0 < usage.cost_usd < 1
    assert final["elapsed_ms"] > 0


@pytest.mark.slow
@pytest.mark.parametrize("protocol", ["responses", "chat"])
def test_real_sdk_sends_our_instructions_history_and_document_block(live, protocol):
    fact = live["facts"][0]
    history = [{"role": "user", "content": "Tell me about customers."}, {"role": "assistant", "content": "We connect many customers. [[c1]]"}]
    events = list(ask_real(live, fact["question"], protocol=protocol, history=history))
    assert events[-1]["type"] == "final"
    first = agent_requests(live)[0]["json"]
    body = json.dumps(first)
    assert "Never cite a page you did not read" in body and "quote=" in body and "GROUNDING" in body        # ours + the SDK's cite prompt
    assert "Tell me about customers." in body and "We connect many customers." in body and "[[c1]]" not in body
    assert body.index("The user has specified document") < body.index("Tell me about customers.") < body.index(fact["question"])
    assert "PAGE NUMBERS" in body and "minus 2" in body                                                     # the sample's folios = page - 2
    if protocol == "responses":
        assert first["reasoning"]["effort"] == "medium" and first["stream"] is True


@pytest.mark.slow
def test_real_sdk_follow_up_uses_the_same_document_and_still_cites(live):
    fact = live["facts"][0]
    first = list(ask_real(live, fact["question"]))
    answer = strip_markers(first[-1]["answer"].text)
    second = list(ask_real(live, "And how many customers were connected the year before?",
                           history=[{"role": "user", "content": fact["question"]}, {"role": "assistant", "content": answer}]))
    assert second[-1]["type"] == "final" and second[-1]["contexts"]


@pytest.mark.slow
def test_real_sdk_question_the_report_cannot_answer_has_no_sources(live):
    events = list(ask_real(live, "Zzyzx quokka blorptastic nebulae?"))
    final = events[-1]
    assert final["status"] == "no_sources" and final["answer"].citations == [] and "[[c" not in streamed_text(events)


def wait_for_helper_threads_to_end(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name == "qa-agent-stream" and t.is_alive() for t in threading.enumerate()):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.slow
@pytest.mark.parametrize("protocol", ["responses", "chat"])
def test_cancel_really_stops_the_agent_run(live, protocol):
    """A full run is three model requests (outline, pages, answer).  The cancel lands at the SDK's next event, which on both
    lanes arrives while turn 2 is still in flight: that request is hung up on and no third request is ever made."""
    server = live["server"]
    server.delay_ms = 120                                       # a model turn: ~1.2 s to the first byte, then an event every 120 ms
    cancel = threading.Event()
    gen = ask_real(live, live["facts"][0]["question"], protocol=protocol, cancel=cancel)
    cancelled_at = 0.0
    with pytest.raises(QAError) as err:
        for ev in gen:
            if ev["type"] == "step" and ev["step"].kind == "tool":
                cancelled_at = time.monotonic()
                cancel.set()                                    # the outline call is out; the page read and the answer are not
    assert err.value.code == "cancelled" and time.monotonic() - cancelled_at < 2.0          # ask() itself returns at once
    time.sleep(4.5)                                             # long enough for the SDK's next event to arrive and tear the run down
    settled = len(agent_requests(live))
    assert settled == 2
    assert server.aborted_streams >= 1                          # the in-flight request was hung up on, not read to the end
    time.sleep(3.0)
    assert len(agent_requests(live)) == settled                 # nothing new is started afterwards
    assert wait_for_helper_threads_to_end()


@pytest.mark.slow
def test_closing_the_generator_cancels_the_real_run(live):
    live["server"].delay_ms = 120
    gen = ask_real(live, live["facts"][0]["question"])
    for ev in gen:
        if ev["type"] == "step" and ev["step"].kind == "tool":
            break
    gen.close()
    time.sleep(4.0)
    settled = len(agent_requests(live))
    assert settled <= 2 and live["server"].aborted_streams >= 1
    time.sleep(3.0)
    assert len(agent_requests(live)) == settled
    assert wait_for_helper_threads_to_end()


# (protocol, model, request path): the chat lane of a gpt-5.6 model is bridged to /responses by LiteLLM; gpt-4.1 uses /chat/completions
LANES = [("responses", "gpt-5.6-sol", "/responses"), ("chat", "gpt-5.6-sol", "/responses"), ("chat", "gpt-4.1", "/chat/completions")]
REAL_ERRORS = [
    ("auth", "openai_auth", "rejected the API key"),
    ("rate_limit", "openai_rate_limit", "rate limit"),
    ("model", "openai_model", "PI_CHAT_MODEL"),
    ("server", "agent_failed", "HTTP 500"),
]


@pytest.mark.slow
@pytest.mark.parametrize("protocol,model,path", LANES)
@pytest.mark.parametrize("kind,code,fragment", REAL_ERRORS)
def test_real_sdk_errors_become_qa_errors(live, protocol, model, path, kind, code, fragment):
    live["server"].fail_next(kind=kind, path=path)
    with pytest.raises(QAError) as err:
        list(ask_real(live, live["facts"][0]["question"], protocol=protocol, model=model))
    assert err.value.code == code, err.value.message
    assert fragment.lower() in err.value.message.lower()
    assert "Traceback" not in err.value.message and len(err.value.message) < 300
    assert len(agent_requests(live)) == 1                       # a failed call is not retried into a second request


@pytest.mark.slow
def test_real_max_turns_error(live):
    eng = QAEngine(live["settings"].with_(chat_max_turns=1))
    with pytest.raises(QAError) as err:
        list(eng.ask(session_id=live["sid"], doc=live["doc"], question=live["facts"][0]["question"], history=[], ctx=live["ctx"]))
    assert err.value.code == "agent_max_turns" and "PI_CHAT_MAX_TURNS" in err.value.message


@pytest.mark.slow
def test_real_rewrite_question_against_the_mock_server(live):
    eng = QAEngine(live["settings"])
    assert eng.rewrite_question([], "What was revenue?") == "What was revenue?"
    out = eng.rewrite_question(HISTORY, "And in 2024/25?")
    requests = [r for r in live["server"].requests if r["path"].endswith("/chat/completions")]
    assert len(requests) == 1 and requests[0]["json"]["model"] == "gpt-4.1-mini" and out
    live["server"].fail_next(kind="server")
    assert eng.rewrite_question(HISTORY, "And in 2024/25?") == "And in 2024/25?"
