"""Generate the synthetic "Northbridge Energy plc Annual Report 2025/26" used by tests, demos and the mock server.

    python scripts/make_sample_pdf.py [--out samples/sample_annual_report.pdf] [--pages 60] [--offset 2] [--seed 0]

What makes it a useful stand-in for a real report (every item is something a real annual report does to PDF tooling):
  * a 3-level PDF outline (bookmarks) so PageIndex Flash builds a tree from it;
  * a cover and a contents page with no folio, then printed folios that differ from the physical page by `printed_offset`;
  * two-column narrative pages with hyphenated line ends, curly quotes and en dashes;
  * financial tables with real-report number formatting (6,991 / (2,596) / 1,263), a landscape page (true landscape
    MediaBox) and a rotated page (portrait MediaBox + /Rotate 90);
  * notes with cross-references such as "see note 29 on page 54" (printed folio, not physical page);
  * section divider pages and group lead pages, so every outline level has a page of its own.

Every statement a test might ask about is a registered fact. The sidecar `<name>.facts.json` is a list of
{id, kind (text|table|cross_reference), question, answer, key (a string every correct answer contains), page (physical),
printed_page, related_pages (other pages that answer it too), quote (verbatim page text), section_path (outline breadcrumb)}.
Output is deterministic for a given (pages, printed_offset, seed): reportlab runs in `invariant` mode.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import reportlab
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

log = logging.getLogger("reportlens.sample_pdf")

COMPANY = "Northbridge Energy plc"
REPORT_TITLE = "Annual Report 2025/26"

# --------------------------------------------------------------------------------------------- fonts / colours
# Bitstream Vera ships inside reportlab, so the sample builds identically on every machine (no system fonts needed).
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
for _name, _file in (("NB", "Vera.ttf"), ("NB-Bold", "VeraBd.ttf"), ("NB-Italic", "VeraIt.ttf")):
    try:
        pdfmetrics.getFont(_name)
    except KeyError:
        pdfmetrics.registerFont(TTFont(_name, str(_FONT_DIR / _file)))

TEAL = HexColor("#0B6E75")
NAVY = HexColor("#10243E")
AMBER = HexColor("#E8A33D")
GREY = HexColor("#5B6670")
LIGHT = HexColor("#EAF2F3")
RULE = HexColor("#B9C4C8")
INK = HexColor("#1B1F24")

PAGE_W, PAGE_H = A4
MARGIN_X = 50.0
COL_GAP = 22.0
TOP_Y = 78.0          # first body line (below the running header)
BOTTOM_PAD = 66.0     # footer zone height


@dataclass(frozen=True)
class Style:
    font: str = "NB"
    size: float = 8.6
    leading: float = 12.2
    color: Color = INK
    after: float = 5.0


S_BODY = Style()
S_SMALL = Style(size=7.4, leading=10.0, color=GREY, after=3.0)
S_H1 = Style("NB-Bold", 22, 27, NAVY, 8)
S_H2 = Style("NB-Bold", 14, 18, TEAL, 6)
S_H3 = Style("NB-Bold", 10.2, 14, NAVY, 3)
S_KICKER = Style("NB-Bold", 8, 11, TEAL, 2)


# --------------------------------------------------------------------------------------------- text helpers
def fmt(n: float, *, dp: int = 0) -> str:
    """Report-style number: 6,991 and (2,596) for negatives."""
    s = f"{abs(n):,.{dp}f}"
    return f"({s})" if n < 0 else s


def width(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


_PREFIXES = ("infra", "inter", "under", "over", "trans", "counter", "multi", "super", "electro", "hydro")
_SUFFIXES = ("ments", "ment", "tions", "tion", "sions", "sion", "ance", "ances", "ence", "ences", "ness", "ship",
             "ities", "ity", "able", "ible", "ical", "ally", "ising", "ised", "ising", "ings", "ing", "ular", "ative")


def _hyphen_points(word: str) -> list[int]:
    """Morpheme-boundary break positions (index where the right part starts), latest first.
    Rough but plausible ("invest-ment", "infra-structure"); only pure-letter words of >= 8 letters qualify."""
    if len(word) < 8 or not word.isalpha():
        return []
    low = word.lower()
    pts = set()
    for p in _PREFIXES:
        if low.startswith(p) and len(low) - len(p) >= 3:
            pts.add(len(p))
    for s in _SUFFIXES:
        if low.endswith(s) and len(low) - len(s) >= 3:
            pts.add(len(low) - len(s))
    return sorted(pts, reverse=True)


def wrap(text: str, font: str, size: float, max_w: float, *, protect: frozenset[str] = frozenset()) -> list[str]:
    """Greedy line breaking with morpheme hyphenation (words in `protect` are never split)."""
    lines: list[str] = []
    cur = ""
    words = text.split(" ")
    i = 0
    while i < len(words):
        w = words[i]
        trial = f"{cur} {w}" if cur else w
        if width(trial, font, size) <= max_w:
            cur = trial
            i += 1
            continue
        if w.lower() not in protect:
            for cut in _hyphen_points(w):
                head = f"{cur} {w[:cut]}-" if cur else f"{w[:cut]}-"
                if width(head, font, size) <= max_w:
                    lines.append(head)
                    cur = ""
                    words[i] = w[cut:]
                    break
            else:
                cut = None
            if cut is not None:
                continue
        if cur:
            lines.append(cur)
            cur = ""
        else:  # a single over-long token: emit as is rather than loop forever
            lines.append(w)
            i += 1
    if cur:
        lines.append(cur)
    return lines


def _protect_set(*quotes: str) -> frozenset[str]:
    return frozenset(w.lower() for q in quotes for w in q.split(" "))


# --------------------------------------------------------------------------------------------- the numbers
@dataclass(frozen=True)
class Fin:
    """One consistent set of figures (GBP m unless noted); every page that quotes a figure derives it from here."""
    rev: int = 14812
    rev_py: int = 13791
    uop: int = 3127
    uop_py: int = 2864
    exceptional: int = -295
    exceptional_py: int = -212
    fin_costs: int = -1042
    fin_costs_py: int = -998
    tax: int = -358
    tax_py: int = -331
    # cash flow
    cgo: int = 6991
    cgo_py: int = 6534
    int_paid: int = -1142
    int_paid_py: int = -1068
    tax_paid: int = -426
    tax_paid_py: int = -397
    capex: int = -2596
    capex_py: int = -2288
    disposals: int = 1263
    disposals_py: int = 412
    divs_paid: int = -1534
    divs_paid_py: int = -1471
    # balance sheet
    ppe: int = 41236
    ppe_py: int = 39870
    intang: int = 2418
    intang_py: int = 2371
    invest: int = 1092
    invest_py: int = 1030
    inventories: int = 612
    inventories_py: int = 588
    receivables: int = 3284
    receivables_py: int = 3102
    cash: int = 2947
    cash_py: int = 2310
    borrow_cur: int = -2288
    borrow_cur_py: int = -2614
    payables: int = -4611
    payables_py: int = -4380
    borrow_nc: int = -22125
    borrow_nc_py: int = -22414
    deferred_tax: int = -4214
    deferred_tax_py: int = -4008
    pensions: int = -1106
    pensions_py: int = -1298
    provisions: int = -1287
    provisions_py: int = -1203

    # derived ----------------------------------------------------------------------------------------
    @property
    def op(self) -> int:
        return self.uop + self.exceptional

    @property
    def op_py(self) -> int:
        return self.uop_py + self.exceptional_py

    @property
    def pbt(self) -> int:
        return self.op + self.fin_costs

    @property
    def pbt_py(self) -> int:
        return self.op_py + self.fin_costs_py

    @property
    def profit(self) -> int:
        return self.pbt + self.tax

    @property
    def profit_py(self) -> int:
        return self.pbt_py + self.tax_py

    @property
    def op_cash(self) -> int:
        return self.cgo + self.int_paid + self.tax_paid

    @property
    def op_cash_py(self) -> int:
        return self.cgo_py + self.int_paid_py + self.tax_paid_py

    @property
    def net_cash(self) -> int:
        return self.op_cash + self.capex + self.disposals + self.divs_paid

    @property
    def net_cash_py(self) -> int:
        return self.op_cash_py + self.capex_py + self.disposals_py + self.divs_paid_py

    @property
    def net_debt(self) -> int:
        return -(self.borrow_cur + self.borrow_nc) - self.cash

    @property
    def net_debt_py(self) -> int:
        return -(self.borrow_cur_py + self.borrow_nc_py) - self.cash_py

    @property
    def nca(self) -> int:
        return self.ppe + self.intang + self.invest

    @property
    def nca_py(self) -> int:
        return self.ppe_py + self.intang_py + self.invest_py

    @property
    def ca(self) -> int:
        return self.inventories + self.receivables + self.cash

    @property
    def ca_py(self) -> int:
        return self.inventories_py + self.receivables_py + self.cash_py

    @property
    def cl(self) -> int:
        return self.borrow_cur + self.payables

    @property
    def cl_py(self) -> int:
        return self.borrow_cur_py + self.payables_py

    @property
    def ncl(self) -> int:
        return self.borrow_nc + self.deferred_tax + self.pensions + self.provisions

    @property
    def ncl_py(self) -> int:
        return self.borrow_nc_py + self.deferred_tax_py + self.pensions_py + self.provisions_py

    @property
    def equity(self) -> int:
        return self.nca + self.ca + self.cl + self.ncl

    @property
    def equity_py(self) -> int:
        return self.nca_py + self.ca_py + self.cl_py + self.ncl_py


FIN = Fin()
DIV_INTERIM, DIV_FINAL, DIV_TOTAL, DIV_TOTAL_PY = 11.53, 24.62, 36.15, 33.48
SHARES_M = 3712
assert round(DIV_INTERIM + DIV_FINAL, 2) == DIV_TOTAL
assert FIN.net_debt == 21466 and FIN.net_debt_py == 22718, (FIN.net_debt, FIN.net_debt_py)
assert FIN.profit == 1432 and FIN.op_cash == 5423 and FIN.net_cash == 2556


# --------------------------------------------------------------------------------------------- plan
@dataclass
class Leaf:
    title: str
    path: tuple[str, ...]            # ancestors' titles (L1, [L2]) - the leaf's own title is NOT included
    pages: int
    min_pages: int = 1
    flex: bool = False               # may grow/shrink when `pages` != 60
    mode: str = "2col"               # "2col" | "1col"
    page_kind: str = "portrait"      # portrait | landscape (true MediaBox) | rotated (/Rotate 90)
    slug: str = ""
    start: int = 0                   # physical page, assigned by plan()

    @property
    def full_path(self) -> list[str]:
        return [*self.path, self.title]


def _leaf(title, path, pages, slug, *, flex=False, min_pages=1, mode="2col", page_kind="portrait") -> Leaf:
    return Leaf(title, tuple(path), pages, min_pages, flex, mode, page_kind, slug)


SR, GOV, FS, OTH = "Strategic report", "Governance", "Financial statements", "Other information"
FR, BM, RK, NT = "Financial review", "Our business model", "Principal risks and uncertainties", "Notes to the financial statements"


def base_plan() -> list[Leaf]:
    """Section dividers and group lead pages exist so that every outline level lands on a page of its own
    (PageIndex Flash drops a parent bookmark that shares its page with its first child)."""
    return [
        _leaf(SR, [], 1, "div_sr", mode="1col"),
        _leaf("Highlights", [SR], 1, "highlights"),
        _leaf("Chairman’s statement", [SR], 1, "chair", flex=True),
        _leaf("Chief executive’s review", [SR], 3, "ceo", flex=True),
        _leaf(BM, [SR], 1, "bm"),
        _leaf("Generation", [SR, BM], 1, "generation"),
        _leaf("Networks", [SR, BM], 1, "networks"),
        _leaf("Customers and services", [SR, BM], 1, "customers"),
        _leaf("Key performance indicators", [SR], 2, "kpi", flex=True),
        _leaf(FR, [SR], 2, "fr", flex=True, mode="1col"),
        _leaf("Summary cash flow statement", [SR, FR], 1, "cashflow", mode="1col"),
        _leaf("Net debt and funding", [SR, FR], 2, "netdebt", flex=True),
        _leaf("Dividend and capital allocation", [SR, FR], 1, "dividend"),
        _leaf(RK, [SR], 1, "pru"),
        _leaf("Principal risks", [SR, RK], 2, "risks", flex=True, min_pages=2, mode="1col"),
        _leaf("Viability statement", [SR, RK], 1, "viability"),
        _leaf("Sustainability", [SR], 2, "sustain", flex=True),
        _leaf(GOV, [], 1, "div_gov", mode="1col"),
        _leaf("Board of directors", [GOV], 2, "board", flex=True),
        _leaf("Corporate governance report", [GOV], 2, "govreport", flex=True),
        _leaf("Audit committee report", [GOV], 2, "audit", flex=True),
        _leaf("Directors’ remuneration report", [GOV], 2, "remuneration", flex=True, mode="1col"),
        _leaf("Directors’ report", [GOV], 1, "dirreport"),
        _leaf(FS, [], 1, "div_fs", mode="1col"),
        _leaf("Independent auditor’s report", [FS], 3, "auditor", flex=True, mode="1col"),
        _leaf("Consolidated income statement", [FS], 1, "income", mode="1col"),
        _leaf("Consolidated balance sheet", [FS], 1, "balance", mode="1col"),
        _leaf("Consolidated statement of changes in equity", [FS], 1, "equity", mode="1col"),
        _leaf("Consolidated cash flow statement", [FS], 1, "cfstatement", mode="1col"),
        _leaf(NT, [FS], 1, "n1", mode="1col"),
        _leaf("Note 2 Segmental analysis", [FS, NT], 1, "n2", mode="1col", page_kind="rotated"),
        _leaf("Notes 3 to 5 Operating costs, finance costs and tax", [FS, NT], 1, "n3", mode="1col"),
        _leaf("Notes 6 to 12 Non-current assets and working capital", [FS, NT], 2, "n6", flex=True, mode="1col"),
        _leaf("Notes 13 to 20 Borrowings and financial instruments", [FS, NT], 2, "n13", flex=True, mode="1col"),
        _leaf("Notes 21 to 26 Provisions and pensions", [FS, NT], 2, "n21", flex=True, mode="1col"),
        _leaf("Notes 27 and 28 Share capital and reserves", [FS, NT], 1, "n27", mode="1col"),
        _leaf("Note 29 Financial commitments and contingencies", [FS, NT], 1, "n29", mode="1col"),
        _leaf("Notes 30 and 31 Related parties and post balance sheet events", [FS, NT], 1, "n30", mode="1col"),
        _leaf(OTH, [], 1, "div_oth", mode="1col"),
        _leaf("Five-year summary", [OTH], 1, "fiveyear", mode="1col", page_kind="landscape"),
        _leaf("Glossary", [OTH], 1, "glossary"),
        _leaf("Shareholder information", [OTH], 1, "shareholder"),
    ]


def min_pages() -> int:
    """Smallest report the plan can be squeezed into (cover + contents + every leaf at its minimum)."""
    return 2 + sum(leaf.min_pages if leaf.flex else leaf.pages for leaf in base_plan())


def plan(pages: int) -> list[Leaf]:
    """Leaves with page counts adjusted to the requested total; `start` is the physical first page."""
    leaves = base_plan()
    delta = pages - (2 + sum(leaf.pages for leaf in leaves))
    flex = [leaf for leaf in leaves if leaf.flex]
    step = 1 if delta > 0 else -1
    guard = 0
    while delta != 0:
        moved = False
        for leaf in (flex if step > 0 else list(reversed(flex))):
            if delta == 0:
                break
            if step < 0 and leaf.pages <= leaf.min_pages:
                continue
            leaf.pages += step
            delta -= step
            moved = True
        guard += 1
        if not moved or guard > 10_000:
            raise ValueError(f"cannot lay out {pages} pages (minimum {min_pages()})")
    page = 3
    for leaf in leaves:
        leaf.start = page
        page += leaf.pages
    return leaves


# --------------------------------------------------------------------------------------------- filler text
# Sentences deliberately avoid the registered facts' topics (revenue, profit, dividend, net debt ...).
# Placeholders: {pct} 61-96, {n} 3-19, {m} 120-940, {k} 11-98 (thousand), {d} 2-40 (days/hours).
_POOL: dict[str, list[str]] = {
    "strategy": [
        "Our strategy rests on three priorities: a reliable network, a cleaner generation mix and fair outcomes for customers.",
        "We have continued to simplify how we work, moving routine planning decisions closer to the regional teams that know their networks best.",
        "Investment decisions are tested against a simple question: does this make the system safer, cleaner or more affordable for the people we serve?",
        "During the year we refreshed our long-term plan with the Board, stress-testing it against slower demand growth and faster electrification.",
        "We published our updated “Clean Network 2030” roadmap, which sets out the milestones we will report against each year.",
        "Partnerships with local authorities and community groups helped us to shape connection plans for {n} new housing developments.",
        "Our operating model separates long-term asset planning from day-to-day delivery, so that each team has clear accountability.",
        "We reviewed our portfolio during the year and concluded that our current mix of networks, flexible generation and customer services remains right.",
        "Delivery of the plan depends on skilled people, so we continued to invest in training centres in the North East, Yorkshire and the Midlands.",
        "Local energy plans developed with {n} councils now guide where we reinforce the network first.",
        "We measure progress against a balanced scorecard covering safety, reliability, customer service, financial discipline and carbon.",
        "Management reviews the strategy with the Board twice a year and tests it against alternative views of the future energy system.",
    ],
    "generation": [
        "Our wind and solar portfolio generated {m} GWh during the year, supported by high availability across all {n} sites.",
        "The refurbishment of the Harrowgate pumped-storage station was completed {d} days ahead of schedule and within budget.",
        "Offshore wind output was affected by an unusually calm autumn, partly offset by strong spring and winter wind speeds.",
        "We commissioned two further battery storage sites, adding {m} MW of fast-response capacity to the system.",
        "Maintenance outages were concentrated in the summer months to protect availability during the winter peak.",
        "Capacity market agreements now cover the majority of our flexible capacity for the next four delivery years.",
        "The Ravensworth solar farm reached full output in November and now supplies enough electricity for around {k},000 homes.",
        "Availability of our gas-fired peaking plant was {pct}% during periods of system stress, which is above the contracted level.",
        "We completed the planned inspection of all {n} hydro turbines and replaced the runners on two units.",
        "A new control room in Newcastle now coordinates dispatch across our wind, solar and storage assets from a single platform.",
        "Biodiversity surveys at each generation site informed the design of habitat management plans that will run for the life of the asset.",
        "Load factors on our onshore wind fleet were broadly in line with the long-term average for the year.",
    ],
    "networks": [
        "We replaced {m} kilometres of ageing underground cable and upgraded {n} primary substations during the year.",
        "Average power cut duration fell to {d} minutes per customer, our best result since reporting began.",
        "Storm Ailsa was the most severe event of the year; {k},000 customers were reconnected within the first {d} hours.",
        "Smart-grid sensors now monitor {m} distribution transformers, allowing faults to be located remotely.",
        "Our connections team processed {m} applications for heat pumps and electric vehicle chargers each month on average.",
        "Vegetation management moved to a risk-based cycle, concentrating resources on circuits with the highest fault history.",
        "Flood defences at {n} substations were raised during the year in line with our adaptation plan.",
        "A new framework contract with our alliance partners should shorten the time taken to energise major connections.",
        "Overhead line inspections now use drones and thermal imaging, which has reduced the need for crews to work at height.",
        "We reinforced the transmission corridor between the Tyne valley and the Humber to release capacity for new wind generation.",
        "Network losses were broadly flat, and we continued to replace the least efficient transformers first.",
        "Real-time data from smart meters helped our control room to restore supplies to many customers more quickly after faults.",
    ],
    "customers": [
        "Our contact centres answered {k},000 calls, and {pct}% of customers told us their issue was resolved at first contact.",
        "The Priority Services Register now supports {k},000 customers who benefit from additional help during an outage.",
        "We launched a new app that lets customers report and track faults, and {pct}% of reports now arrive digitally.",
        "Our community energy programme helped {m} households to reduce bills through insulation and efficiency advice.",
        "Complaints fell by {n}% compared with last year, driven by faster responses to connection enquiries.",
        "We partnered with {n} charities to extend support for customers in vulnerable circumstances.",
        "Customers can now book connection appointments online, and the median wait for a quotation fell by {d} days.",
        "Our independent customer panel met four times and reviewed our performance on both service and bills.",
        "Winter outreach visits reached {k},000 households, with advice on keeping warm and what to do in a power cut.",
        "Translation and accessibility services were extended, and information is now available in {n} languages.",
        "Proactive text messages about planned work reduced calls to our contact centres on the day of an outage.",
        "Satisfaction among business customers improved, supported by named account managers for our largest connections.",
    ],
    "people": [
        "We welcomed {m} new apprentices and graduates, and {pct}% of our workforce now has a development plan agreed with their manager.",
        "Engagement among colleagues remained high, with a survey response rate of {pct}% across every business unit.",
        "Our gender pay gap narrowed again, and women now hold {n}0% of senior leadership roles.",
        "Colleagues completed an average of {d} hours of training during the year, with a focus on digital and engineering skills.",
        "A new returners programme attracted {n} experienced engineers back into the industry.",
        "We continued to pay the real Living Wage to every employee and to the contractors we engage directly.",
        "Our colleague networks for ethnicity, disability, LGBT+ and carers each have an executive sponsor.",
        "Flexible working is available from day one, and {pct}% of office-based colleagues use some form of hybrid pattern.",
        "Voluntary turnover was lower than the sector average, helped by clearer career paths for field engineers.",
        "We opened a second training academy in Leeds, which will train around {m} technicians a year.",
        "Wellbeing support includes confidential counselling, financial education and a menopause support network.",
        "Succession planning covers every executive role and the critical engineering positions beneath them.",
    ],
    "safety": [
        "Safety remains our first priority – every meeting of the Board and each of its committees begins with a safety item.",
        "We introduced wearable proximity sensors for crews working near live equipment, now used on {n} sites.",
        "Contractor safety forums were held in each region, bringing together {m} supervisors to share lessons from near misses.",
        "Mental health first-aiders are now available at every major depot and office.",
        "A new standard for working at height was rolled out to all field teams following a review of near-miss reports.",
        "Public safety campaigns near substations and overhead lines reached more than {k}0,000 people through schools and social media.",
        "Every serious incident is reviewed by a director, and the lessons are shared across the Group within {d} days.",
        "Critical-risk verifications were completed on {m} jobs during the year to confirm that controls work in practice.",
    ],
    "regulation": [
        "We engaged constructively with the regulator on the next price control, submitting our business plan on time.",
        "The regulator’s annual review confirmed that we met or exceeded {n} of our {n}0 output commitments.",
        "Changes to connection rules will help to release capacity for new demand more quickly and fairly.",
        "We responded to {n} consultations during the year, including proposals on network charging and market reform.",
        "Incentive mechanisms for customer service and reliability continue to reward performance above the regulatory baseline.",
        "The Government’s clean power plan increases the importance of timely network investment, and we are working with the system operator on a joint approach.",
        "Reforms to the planning system should shorten consenting times for new overhead lines and substations.",
        "We support the regulator’s proposals to strengthen the licence conditions on governance and financial resilience.",
        "Our regulatory reporting was externally assured, and no material issues were identified.",
        "Price control reopeners allow us to adjust funding for projects whose scope or cost is uncertain at the start of the period.",
    ],
    "climate": [
        "Our science-based targets were validated during the year and cover emissions from our operations and our supply chain.",
        "Sulphur hexafluoride leakage fell by {n}% following the replacement of the oldest switchgear.",
        "Around {pct}% of our vehicle fleet is now electric, and we expect to complete the transition by 2030.",
        "We planted {k},000 trees across {n} sites and committed to a measurable net gain in biodiversity on new projects.",
        "Climate-related financial disclosures are set out in line with the recommendations of the Task Force and form part of this report.",
        "Our transition plan identifies the investments, partnerships and policy changes needed to reach net zero by 2045.",
        "Physical climate risk assessments were completed for {m} critical assets, with adaptation plans prioritised by consequence.",
        "Scope 3 emissions are the largest part of our footprint, so we are working with major suppliers on lower-carbon steel and cable.",
        "Heat from our largest substations now supplies a district heating scheme in Gateshead.",
        "Internal carbon pricing is applied to every investment case above £{n} million.",
    ],
    "supply": [
        "We spent £{m} million with local suppliers, and {pct}% of our spend is with small and medium-sized businesses.",
        "Supply chain due diligence covers every supplier with annual spend above £{k},000, including labour standards checks.",
        "Lead times for large transformers remain long, so we placed framework orders early to protect our delivery plan.",
        "Our supplier code of conduct was refreshed to include requirements on carbon reporting and responsible sourcing.",
        "Prompt payment performance remained strong – {pct}% of undisputed invoices were paid within 30 days.",
        "Dual sourcing has been introduced for the most critical categories of switchgear and protection equipment.",
        "We held supplier days in Newcastle and Manchester to explain our pipeline of work over the next five years.",
        "Modern slavery risk assessments were updated and {n} high-risk suppliers received on-site audits.",
    ],
    "governance": [
        "The Board met {n} times during the year; attendance by directors averaged {pct}%.",
        "The Nomination Committee reviewed succession plans for the Board and for the senior leadership team.",
        "An externally facilitated Board evaluation concluded that the Board and its committees continue to operate effectively.",
        "Directors received regular briefings on regulation, cyber security, climate and market developments.",
        "The Board approved a refreshed delegation of authority to support faster, better-informed decisions.",
        "We comply with the UK Corporate Governance Code, and any areas of departure are explained in this report.",
        "The Board visited the Tyneside control centre and two major substation projects to see operations and meet colleagues.",
        "Workforce engagement is led by a designated non-executive director, who met employee representatives on {n} occasions.",
        "Conflicts of interest are declared at the start of each meeting and recorded in the minutes.",
        "The Board reviewed the Group’s purpose, values and culture, drawing on survey results, whistleblowing reports and site visits.",
        "Committee terms of reference were refreshed and are available on the Company’s website.",
    ],
    "risk": [
        "Each principal risk has an executive owner, a defined appetite and a set of controls that are tested independently.",
        "Emerging risks are discussed quarterly by the Executive Committee and escalated to the Board where appropriate.",
        "Scenario analysis considers severe but plausible events, including prolonged supply interruptions and market stress.",
        "Internal audit completed {n}0 reviews during the year and tracked {m} actions to closure.",
        "The risk register is updated throughout the year, and heat maps are reviewed by the Audit Committee.",
        "Reverse stress tests identify the combination of events that would threaten the Group’s liquidity or licence to operate.",
        "Business continuity plans were tested through {n} exercises, including a simulated loss of a primary control room.",
        "Third-line assurance is provided by internal audit, supplemented by specialist reviews of safety and cyber controls.",
    ],
    "innovation": [
        "Our innovation fund backed {n} pilots, including a trial of dynamic network pricing with {m} participating households.",
        "Digital twins of {n}0 substations are now used to plan outages and test upgrade options before work starts.",
        "A partnership with a university research centre is helping us to forecast local demand at street level.",
        "Machine-learning models help prioritise inspections, reducing the number of unnecessary site visits.",
        "We trialled hydrogen-ready switchgear at one site, which could simplify future conversions.",
        "Open data portals now publish half-hourly network capacity maps for developers and local authorities.",
    ],
    "shareholder": [
        "We held meetings with investors during the year, covering strategy, regulation, sustainability and governance.",
        "Shareholders can receive communications electronically and manage their holdings online through the registrar’s portal.",
        "The Company’s ordinary shares are admitted to the premium listing segment of the London Stock Exchange.",
        "Unclaimed dividends can be reclaimed from the registrar at any time, and a reunification service is available for lost holdings.",
        "Shareholders who prefer paper communications can opt in by contacting the registrar.",
        "The Company has an active programme to help shareholders to avoid investment scams.",
    ],
    "audit": [
        "The Committee reviewed the significant accounting judgements and the clarity of the disclosures presented in this report.",
        "The Committee considered the independence and objectivity of the external auditor, including non-audit services.",
        "Management’s assessment of the going concern assumption and the long-term viability statement was challenged by the Committee.",
        "The Committee met privately with the external auditor and the head of internal audit without management present.",
        "Our audit approach was tailored to the Group’s regulated structure and to the areas of greatest estimation uncertainty.",
        "We communicated the scope and findings of the audit to the Audit Committee throughout the year.",
        "Materiality was set with reference to a proportion of underlying profit, and misstatements above a lower threshold were reported to the Committee.",
        "Our audit of the carrying value of network assets focused on the capitalisation of labour and overhead costs.",
    ],
}

_NOTES_TOPICS = {
    "accounting": [
        "The consolidated financial statements have been prepared in accordance with UK-adopted international accounting standards.",
        "The financial statements are presented in pounds sterling, rounded to the nearest million except where otherwise stated.",
        "The directors have a reasonable expectation that the Group has adequate resources to continue in operational existence for at least twelve months.",
        "Revenue is recognised when control of the goods or services transfers to the customer, at the amount the Group expects to be entitled to.",
        "Judgements and estimates are reviewed on an ongoing basis; changes are recognised in the period in which the estimate is revised.",
        "Property, plant and equipment is stated at cost less accumulated depreciation and any recognised impairment losses.",
        "Where an asset is acquired under a lease, a right-of-use asset and a corresponding lease liability are recognised at commencement.",
        "Depreciation is charged on a straight-line basis over the estimated useful lives of the assets, which range from 5 to 80 years.",
        "Financial assets are classified at amortised cost, fair value through other comprehensive income or fair value through profit or loss.",
        "Borrowings are initially recognised at fair value, net of transaction costs, and subsequently carried at amortised cost.",
        "Deferred tax is provided using the balance sheet liability method on temporary differences between carrying amounts and tax bases.",
        "Provisions are recognised when the Group has a present obligation as a result of a past event and a reliable estimate can be made.",
        "Foreign currency transactions are translated at the rate ruling on the date of the transaction; monetary balances are retranslated at the year end.",
        "Inventories are stated at the lower of cost and net realisable value, with cost determined on a weighted average basis.",
        "Retirement benefit obligations are measured using the projected unit credit method and discounted at the yield on high-quality corporate bonds.",
        "Impairment reviews compare the carrying value of an asset or cash-generating unit with its recoverable amount.",
        "Contributions received from customers towards the cost of connections are deferred and released over the life of the related assets.",
        "Government grants are recognised when there is reasonable assurance that the conditions will be met and the grant will be received.",
        "Share-based payments are measured at fair value at the date of grant and expensed over the vesting period.",
        "Derivative financial instruments are held to manage interest rate, inflation and currency exposures and are not used for speculation.",
    ],
}

_NOTE_TITLES = {
    3: "Operating costs", 4: "Net finance costs", 5: "Taxation", 6: "Property, plant and equipment", 7: "Intangible assets",
    8: "Investments", 9: "Inventories", 10: "Trade and other receivables", 11: "Cash and cash equivalents",
    12: "Trade and other payables", 13: "Borrowings", 14: "Net debt reconciliation", 15: "Derivative financial instruments",
    16: "Fair value measurement", 17: "Financial risk management", 18: "Leases", 19: "Hedging activities",
    20: "Interest rate sensitivity", 21: "Provisions", 22: "Decommissioning obligations", 23: "Deferred tax",
    24: "Retirement benefit obligations", 25: "Pension scheme assets", 26: "Share-based payments",
    27: "Called-up share capital", 28: "Other reserves", 30: "Related party transactions",
    31: "Events after the reporting period",
}

_MINI_TABLE_ROWS = [
    "Land and buildings", "Plant and machinery", "Network assets", "Assets under construction", "Software",
    "Customer contracts", "Other", "Trade receivables", "Accrued income", "Prepayments", "Deposits",
    "Within one year", "One to five years", "After five years", "Fixed rate", "Floating rate", "Index-linked",
]


class Filler:
    """Seeded sentence source; every sentence is drawn once per topic cycle so pages do not repeat themselves."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self._queues: dict[str, list[str]] = {}
        self._used: set[str] = set()

    def start_leaf(self) -> None:
        self._used: set[str] = set()

    def _sentence(self, topic: str, pool: dict[str, list[str]]) -> str:
        q = self._queues.setdefault(topic, [])
        for _ in range(len(pool[topic]) + 1):   # skip templates already used on this leaf while others remain
            if not q:
                q.extend(pool[topic])
                self.rng.shuffle(q)
            t = q.pop()
            if t not in self._used:
                break
        self._used.add(t)
        r = self.rng
        return t.format(pct=r.randint(61, 96), n=r.randint(3, 19), m=r.randint(120, 940), k=r.randint(11, 98), d=r.randint(2, 40))

    def paragraph(self, topic: str, sentences: int = 4, pool: Optional[dict[str, list[str]]] = None) -> str:
        pool = pool or _POOL
        return " ".join(self._sentence(topic, pool) for _ in range(sentences))

    def mini_rows(self, n: int) -> list[tuple[str, int, int]]:
        names = self.rng.sample(_MINI_TABLE_ROWS, n)
        return [(nm, self.rng.randint(80, 4200), self.rng.randint(80, 4200)) for nm in names]


# --------------------------------------------------------------------------------------------- facts
@dataclass
class FactSpec:
    id: str
    kind: str          # text | table | cross_reference
    question: str
    answer: str
    quote: str


# Per fact: `key` = the string any correct answer must contain; `related` = leaf slugs of other pages that answer it too.
_FACT_KEYS = {
    "customers_connected": "8.4 million", "ltifr": "0.12", "revenue": "14,812", "tax_rate": "20.0%", "uop": "3,127",
    "cgo": "6,991", "capex": "2,596", "disposals": "1,263", "net_debt": "21,466", "note29_xref": "note 29", "dividend": "36.15",
    "cyber_risk": "cyber attack", "viability": "31 March 2031", "emissions_target": "62%", "ceo": "Raj Patel",
    "audit_fees": "£4.3 million", "ceo_pay": "3,214", "audit_opinion": "true and fair view", "profit_year": "1,432",
    "total_equity": "15,958", "commitments": "1,946", "five_year_revenue": "12,964",
}
_FACT_RELATED = {"note29_xref": ("n29",)}


# --------------------------------------------------------------------------------------------- layout engine
class LayoutOverflow(RuntimeError):
    pass


class Layout:
    """Flows content through columns and pages of ONE leaf; the leaf owns exactly `leaf.pages` pages."""

    def __init__(self, rep: "Report", leaf: Leaf):
        self.rep, self.leaf = rep, leaf
        self.page_idx = -1
        self.col = 0
        self.y = 0.0
        self.col_top = TOP_Y
        self.cols: list[tuple[float, float]] = []
        self._open_page()

    # ---- geometry
    @property
    def vw(self) -> float:
        return self.rep.vw

    @property
    def vh(self) -> float:
        return self.rep.vh

    @property
    def bottom(self) -> float:
        return self.vh - BOTTOM_PAD

    def _columns(self) -> list[tuple[float, float]]:
        full = self.vw - 2 * MARGIN_X
        if self.leaf.mode == "1col":
            return [(MARGIN_X, full)]
        cw = (full - COL_GAP) / 2
        return [(MARGIN_X, cw), (MARGIN_X + cw + COL_GAP, cw)]

    @property
    def x(self) -> float:
        return self.cols[self.col][0]

    @property
    def w(self) -> float:
        return self.cols[self.col][1]

    @property
    def full_width(self) -> float:
        return self.vw - 2 * MARGIN_X

    # ---- pages
    def _open_page(self) -> None:
        self.page_idx += 1
        self.rep.begin_page(self.leaf, first=self.page_idx == 0)
        self.cols = self._columns()
        self.col = 0
        self.col_top = TOP_Y
        self.y = TOP_Y

    def start_columns_here(self) -> None:
        """After a full-width title block: columns begin at the current y of this page."""
        self.col_top = self.y
        self.col = 0

    def new_column(self) -> bool:
        if self.col + 1 < len(self.cols):
            self.col += 1
            self.y = self.col_top
            return True
        if self.page_idx + 1 >= self.leaf.pages:
            return False
        self.rep.end_page()
        self._open_page()
        return True

    def space(self) -> float:
        return self.bottom - self.y

    def ensure(self, height: float) -> bool:
        """Make `height` available in the current column, advancing columns/pages when needed."""
        while self.space() < height:
            if not self.new_column():
                return False
        return True

    def finish(self) -> None:
        """Leaf content is done: any unused pages of the leaf budget are an error (the filler should have used them)."""
        if self.page_idx + 1 != self.leaf.pages:
            raise LayoutOverflow(f"leaf {self.leaf.title!r}: used {self.page_idx + 1} of {self.leaf.pages} pages")
        self.rep.end_page()

    # ---- primitives
    def text_line(self, x: float, y_top: float, text: str, st: Style, *, right: bool = False) -> None:
        c = self.rep.c
        c.setFont(st.font, st.size)
        c.setFillColor(st.color)
        base = self.vh - (y_top + st.size)
        (c.drawRightString if right else c.drawString)(x, base, text)

    def title_block(self) -> None:
        """Kicker (parent section), leaf title and a rule, spanning the page width; columns start below."""
        leaf = self.leaf
        if len(leaf.path) > 1:
            self.text_line(MARGIN_X, self.y, leaf.path[-1].upper(), S_KICKER)
            self.y += S_KICKER.leading + 2
        for ln in wrap(leaf.title, S_H1.font, S_H1.size, self.full_width):
            self.text_line(MARGIN_X, self.y, ln, S_H1)
            self.y += S_H1.leading
        c = self.rep.c
        c.setStrokeColor(AMBER)
        c.setLineWidth(1.6)
        c.line(MARGIN_X, self.vh - (self.y + 2), MARGIN_X + 46, self.vh - (self.y + 2))
        self.y += 14
        self.start_columns_here()

    def heading(self, text: str, st: Style = S_H2) -> bool:
        lines = wrap(text, st.font, st.size, self.w)
        need = len(lines) * st.leading + st.after + S_BODY.leading * 2  # keep with the next lines
        if not self.ensure(need):
            return False
        for ln in lines:
            self.text_line(self.x, self.y, ln, st)
            self.y += st.leading
        self.y += st.after
        return True

    def para(self, text: str, st: Style = S_BODY, *, fact: Optional[FactSpec] = None, keep: bool = False) -> bool:
        """Place a paragraph. Fact paragraphs are kept in one column and never hyphenate the quoted words."""
        protect = _protect_set(fact.quote) if fact else frozenset()
        lines = wrap(text, st.font, st.size, self.w, protect=protect)
        keep = keep or fact is not None
        if keep:
            if not self.ensure(len(lines) * st.leading):
                return False
            first_page = self.rep.phys
            for ln in lines:
                self.text_line(self.x, self.y, ln, st)
                self.y += st.leading
            if fact:
                self.rep.register_fact(fact, self.leaf, first_page)
        else:
            i, n = 0, len(lines)
            while i < n:
                fit = int(self.space() // st.leading)
                rem = n - i
                take = rem if rem <= fit else (fit - 1 if rem - fit == 1 else fit)  # never strand a single line
                if take < min(2, rem):  # orphan control
                    if not self.new_column():
                        return False
                    continue
                for ln in lines[i:i + take]:
                    self.text_line(self.x, self.y, ln, st)
                    self.y += st.leading
                i += take
                if i < n and not self.new_column():
                    return False
        self.y += st.after
        return True

    def table(self, rows: list[list[str]], widths: list[float], *, aligns: Optional[str] = None, header: int = 1,
              bold_rows: frozenset[int] = frozenset(), size: float = 8.2, facts: Optional[dict[int, FactSpec]] = None,
              wrap_cells: bool = False) -> bool:
        """Draw a table (cells are drawn one by one, as in real reports). `aligns` is e.g. 'lrr'.
        Rows never split; a table continues on the next column/page. Returns False when it does not fit."""
        aligns = aligns or "l" + "r" * (len(widths) - 1)
        lead = size + 5.0
        x0 = self.x
        c = self.rep.c
        for ri, row in enumerate(rows):
            font = "NB-Bold" if (ri < header or ri in bold_rows) else "NB"
            cells = [wrap(cell, font, size, widths[ci] - 8) if wrap_cells and aligns[ci] == "l" else [cell]
                     for ci, cell in enumerate(row)]
            h = max(len(cl) for cl in cells) * (size + 2.2) + (lead - size - 2.2)
            if not self.ensure(h):
                return False
            top = self.y
            if ri < header:
                c.setFillColor(LIGHT)
                c.rect(x0, self.vh - (top + h), sum(widths), h, stroke=0, fill=1)
            xx = x0
            for ci, lines in enumerate(cells):
                for li, ln in enumerate(lines):
                    st = Style(font, size, size + 2.2, NAVY if ri < header else INK)
                    ty = top + 3 + li * (size + 2.2)
                    if aligns[ci] == "l":
                        self.text_line(xx + 4, ty, ln, st)
                    else:
                        self.text_line(xx + widths[ci] - 4, ty, ln, st, right=True)
                xx += widths[ci]
            c.setStrokeColor(RULE)
            c.setLineWidth(0.35)
            c.line(x0, self.vh - (top + h), x0 + sum(widths), self.vh - (top + h))
            if facts and ri in facts:
                self.rep.register_fact(facts[ri], self.leaf, self.rep.phys)
            self.y += h
        self.y += S_BODY.after + 3
        return True

    def toc_row(self, depth: int, title: str, folio: str) -> bool:
        font, size = ("NB-Bold", 10.0) if depth == 0 else ("NB", 9.2)
        if not self.ensure(18):
            return False
        st = Style(font, size, 16, NAVY if depth == 0 else INK)
        self.text_line(self.x + depth * 16, self.y, title, st)
        self.text_line(self.x + self.w, self.y, folio, st, right=True)
        self.y += 18
        return True

    def must(self, ok: bool, what: str) -> None:
        if not ok:
            raise LayoutOverflow(f"leaf {self.leaf.title!r}: {what} does not fit in {self.leaf.pages} page(s)")

    def fill(self, topic: str, *, sentences: tuple[int, int] = (3, 5), pool: Optional[dict] = None,
             topics: Optional[list[str]] = None, max_paragraphs: Optional[int] = None) -> None:
        """Pour filler paragraphs until the leaf's page budget is used up.
        A paragraph that does not fit is retried shorter; three misses in a row end the leaf."""
        flt = self.rep.filler
        order = topics or [topic]
        misses, i = 0, 0
        while misses < 3 and (max_paragraphs is None or i < max_paragraphs):
            tp = order[i % len(order)]
            i += 1
            n_sent = flt.rng.randint(*sentences) if misses == 0 else 1
            if not self.para(flt.paragraph(tp, n_sent, pool), keep=True):
                misses += 1
                continue
            misses = 0


# --------------------------------------------------------------------------------------------- report builder
class Report:
    def __init__(self, path: Path, pages: int, printed_offset: int, seed: int):
        self.path = path
        self.pages = pages
        self.offset = printed_offset
        self.leaves = plan(pages)
        self.filler = Filler(seed)
        self.facts: list[dict] = []
        self.phys = 0
        self.vw, self.vh = PAGE_W, PAGE_H
        self._page_open = False
        self._rot = False
        self._outline_done: set[tuple[str, ...]] = set()
        self.c = canvas.Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
        self.c.setTitle(f"{COMPANY} {REPORT_TITLE}")
        self.c.setAuthor(COMPANY)
        self.c.setSubject("Synthetic annual report used for software testing - not a real company")
        self.c.setCreator("ReportLens sample generator")
        self.leaf_by_slug = {lf.slug: lf for lf in self.leaves}

    # ---- helpers used by content
    def printed(self, physical: int) -> int:
        return physical - self.offset

    def note_page(self, slug: str) -> int:
        return self.printed(self.leaf_by_slug[slug].start)

    def register_fact(self, spec: FactSpec, leaf: Leaf, page: int) -> None:
        self.facts.append({
            "id": spec.id, "kind": spec.kind, "question": spec.question, "answer": spec.answer, "key": _FACT_KEYS[spec.id],
            "page": page, "printed_page": str(self.printed(page)) if page > 2 else None,
            "related_pages": [self.leaf_by_slug[s].start for s in _FACT_RELATED.get(spec.id, ())],
            "quote": spec.quote, "section_path": leaf.full_path,
        })

    # ---- page lifecycle
    def begin_page(self, leaf: Optional[Leaf], *, first: bool = True, kind: str = "portrait") -> None:
        c = self.c
        self.phys += 1
        page_kind = leaf.page_kind if leaf else kind
        self._rot = page_kind == "rotated"
        # reportlab swaps the MediaBox when /Rotate is 90, so a landscape pagesize + /Rotate 90 yields the portrait
        # MediaBox that real "rotated table" pages have; we then draw in a landscape virtual canvas (see below).
        c.setPageSize(landscape(A4) if page_kind in ("landscape", "rotated") else A4)
        self.vw, self.vh = landscape(A4) if page_kind in ("landscape", "rotated") else A4
        c.saveState()
        c.setPageRotation(90 if self._rot else 0)  # reportlab keeps the last value for later pages
        if self._rot:
            c.translate(PAGE_W, 0)
            c.rotate(90)
        self._page_open = True
        if leaf is None:
            return
        if first:
            self._outline(leaf)
        self._chrome(leaf)

    def end_page(self) -> None:
        if not self._page_open:
            return
        self.c.restoreState()
        self.c.showPage()
        self._page_open = False

    def _outline(self, leaf: Leaf) -> None:
        key = f"p{self.phys}"
        self.c.bookmarkPage(key)
        chain = [*leaf.path, leaf.title]
        for depth in range(len(chain)):
            ident = tuple(chain[: depth + 1])
            if ident not in self._outline_done:
                self._outline_done.add(ident)
                self.c.addOutlineEntry(chain[depth], key, level=depth, closed=False)

    def _chrome(self, leaf: Leaf) -> None:
        """Running header and footer with the printed folio (physical page - offset)."""
        c = self.c
        hdr = Style("NB", 7.4, 10, GREY)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.4)
        c.line(MARGIN_X, self.vh - 44, self.vw - MARGIN_X, self.vh - 44)
        c.line(MARGIN_X, 48, self.vw - MARGIN_X, 48)
        c.setFont(hdr.font, hdr.size)
        c.setFillColor(GREY)
        c.drawString(MARGIN_X, self.vh - 38, leaf.path[0] if leaf.path else leaf.title)
        c.drawRightString(self.vw - MARGIN_X, self.vh - 38, REPORT_TITLE)
        c.drawString(MARGIN_X, 30, f"{COMPANY}   {REPORT_TITLE}")
        c.setFont("NB-Bold", 8.6)
        c.setFillColor(NAVY)
        c.drawRightString(self.vw - MARGIN_X, 30, str(self.printed(self.phys)))

    # ---- special pages
    def cover(self) -> None:
        self.begin_page(None)
        c, vw, vh = self.c, self.vw, self.vh
        c.setFillColor(NAVY)
        c.rect(0, 0, vw, vh, stroke=0, fill=1)
        c.setFillColor(TEAL)
        c.rect(0, 0, vw, 250, stroke=0, fill=1)
        c.setFillColor(AMBER)
        c.rect(MARGIN_X, vh - 190, 54, 5, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("NB-Bold", 34)
        c.drawString(MARGIN_X, vh - 240, "Northbridge")
        c.drawString(MARGIN_X, vh - 280, "Energy plc")
        c.setFont("NB", 17)
        c.drawString(MARGIN_X, vh - 330, "Annual Report 2025/26")
        c.setFont("NB-Italic", 11)
        c.drawString(MARGIN_X, vh - 360, "Powering a fairer, cleaner energy system")
        c.setFont("NB", 8)
        c.drawString(MARGIN_X, 60, "Registered in England and Wales No. 04812357. Registered office: 1 Quayside Walk, Newcastle upon Tyne NE1 3QX.")
        c.drawString(MARGIN_X, 48, "This is a synthetic report generated for software testing. Northbridge Energy plc does not exist.")
        self.end_page()

    def contents(self) -> None:
        self.begin_page(None)
        c = self.c
        c.setFillColor(NAVY)
        c.setFont("NB-Bold", 22)
        c.drawString(MARGIN_X, self.vh - 80, "Contents")
        c.setFillColor(AMBER)
        c.rect(MARGIN_X, self.vh - 92, 46, 2.2, stroke=0, fill=1)
        y = 124.0
        seen: set[tuple[str, ...]] = set()
        for leaf in self.leaves:
            chain = [*leaf.path, leaf.title]
            for depth, title in enumerate(chain):
                ident = tuple(chain[: depth + 1])
                if ident in seen:
                    continue
                seen.add(ident)
                if depth == 0:
                    y += 8
                font, size, color = (("NB-Bold", 10.5, TEAL), ("NB-Bold", 8.8, NAVY), ("NB", 8.2, INK))[depth]
                x = MARGIN_X + depth * 14
                folio = str(self.printed(leaf.start))
                c.setFont(font, size)
                c.setFillColor(color)
                c.drawString(x, self.vh - y, title)
                c.drawRightString(self.vw - MARGIN_X, self.vh - y, folio)
                c.setStrokeColor(RULE)  # dotted leader drawn as a line, so the extracted text stays clean
                c.setDash(1, 2.5)
                c.setLineWidth(0.5)
                c.line(x + width(title, font, size) + 6, self.vh - y + 2,
                       self.vw - MARGIN_X - width(folio, font, size) - 6, self.vh - y + 2)
                c.setDash()
                y += (14.5, 13.0, 11.8)[depth]
        self.end_page()

    # ---- driver
    def build(self) -> None:
        self.cover()
        self.contents()
        for leaf in self.leaves:
            self.filler.start_leaf()
            lay = Layout(self, leaf)
            lay.title_block()
            getattr(_Content, "divider" if leaf.slug.startswith("div_") else leaf.slug)(self, lay)
            lay.finish()
        self.c.save()
        if self.phys != self.pages:
            raise RuntimeError(f"built {self.phys} pages, expected {self.pages}")


# --------------------------------------------------------------------------------------------- page content
def _f(id_: str, kind: str, question: str, answer: str, quote: str) -> FactSpec:
    return FactSpec(id_, kind, question, answer, quote)


class _Content:
    """One function per leaf slug: places the registered facts first (they must fit), then pours filler."""

    @staticmethod
    def highlights(r: Report, L: Layout) -> None:
        quote = "We now connect 8.4 million customers across our regional networks, an increase of 96,000 on the previous year."
        L.must(L.para("Our year at a glance. " + quote + " Reliability, safety and affordability improved together, and we delivered "
                      "the largest programme of network investment in our history.",
                      fact=_f("customers_connected", "text", "How many customers does Northbridge connect?",
                              "8.4 million customers, an increase of 96,000 on the previous year.", quote)), "highlights fact")
        L.heading("Headline numbers", S_H3)
        rows = [["Measure", "2025/26", "2024/25"],
                ["Revenue (£m)", fmt(FIN.rev), fmt(FIN.rev_py)],
                ["Underlying operating profit (£m)", fmt(FIN.uop), fmt(FIN.uop_py)],
                ["Total dividend per share (pence)", f"{DIV_TOTAL:.2f}", f"{DIV_TOTAL_PY:.2f}"],
                ["Customers connected (million)", "8.4", "8.3"]]
        L.must(L.table(rows, [128, 54, 54]), "highlights table")
        L.fill("strategy", topics=["strategy", "networks", "customers"])

    @staticmethod
    def chair(r: Report, L: Layout) -> None:
        L.para("Dear shareholder,")
        L.para("It is my privilege to present the Annual Report for the year ended 31 March 2026, my third as Chair of Northbridge Energy plc. "
               "The Board is proud of what colleagues have achieved in a year that tested our networks, our supply chains and our people.")
        L.fill("strategy", topics=["strategy", "governance", "regulation", "people"])

    @staticmethod
    def ceo(r: Report, L: Layout) -> None:
        L.para("This was a year of delivery. We invested more than ever before in our networks, connected record volumes of new demand and "
               "kept customers’ energy bills as low as we responsibly could.")
        L.heading("Operational performance")
        L.fill("networks", topics=["networks", "generation", "customers", "innovation", "people", "supply"])

    @staticmethod
    def generation(r: Report, L: Layout) -> None:
        L.fill("generation", topics=["generation", "climate", "innovation"])

    @staticmethod
    def networks(r: Report, L: Layout) -> None:
        L.fill("networks", topics=["networks", "innovation", "supply"])

    @staticmethod
    def customers(r: Report, L: Layout) -> None:
        L.fill("customers", topics=["customers", "regulation"])

    @staticmethod
    def kpi(r: Report, L: Layout) -> None:
        quote = "Our lost-time injury frequency rate improved to 0.12 per 100,000 hours worked, from 0.17 last year."
        L.must(L.para("Safety. " + quote + " There were no fatalities among employees or contractors.",
                      fact=_f("ltifr", "text", "What was the lost-time injury frequency rate in 2025/26?",
                              "0.12 per 100,000 hours worked (0.17 in the previous year).", quote)), "kpi fact")
        L.heading("Key performance indicators", S_H3)
        rows = [["Indicator", "2025/26", "2024/25"],
                ["Lost-time injury frequency rate", "0.12", "0.17"],
                ["Average power cut duration (minutes)", "38", "44"],
                ["Customer satisfaction score (out of 10)", "8.7", "8.5"],
                ["Colleague engagement (%)", "81", "79"],
                ["Scope 1 and 2 emissions (ktCO2e)", "1,184", "1,267"]]
        L.must(L.table(rows, [128, 54, 54]), "kpi table")
        L.fill("safety", topics=["safety", "customers", "climate", "people"])

    @staticmethod
    def fr(r: Report, L: Layout) -> None:
        q1 = f"Group revenue increased by 7.4% to £{fmt(FIN.rev)} million (2024/25: £{fmt(FIN.rev_py)} million), reflecting higher regulated allowances and strong demand for new connections."
        L.must(L.para("Financial review. " + q1, fact=_f("revenue", "text", "What was Northbridge’s group revenue in 2025/26?",
                      f"£{fmt(FIN.rev)} million, up 7.4% on £{fmt(FIN.rev_py)} million.", q1.split(", reflecting")[0])), "revenue fact")
        q2 = "The effective tax rate for the year was 20.0%, compared with 20.0% in the prior year."
        L.must(L.para(q2, fact=_f("tax_rate", "text", "What was the Group’s effective tax rate for 2025/26?",
                      "20.0%, the same as the prior year.", "The effective tax rate for the year was 20.0%")), "tax rate fact")
        L.para("The table below summarises the Group’s results on both a statutory and an underlying basis. Underlying results exclude exceptional "
               "items, which are described in note 3 on page %d." % r.note_page("n3"))
        rows = [["£m", "2025/26", "2024/25", "Change"],
                ["Revenue", fmt(FIN.rev), fmt(FIN.rev_py), "7.4%"],
                ["Underlying operating profit", fmt(FIN.uop), fmt(FIN.uop_py), "9.2%"],
                ["Exceptional items", fmt(FIN.exceptional), fmt(FIN.exceptional_py), "n/m"],
                ["Operating profit", fmt(FIN.op), fmt(FIN.op_py), "6.8%"],
                ["Net finance costs", fmt(FIN.fin_costs), fmt(FIN.fin_costs_py), "(4.4%)"],
                ["Profit before tax", fmt(FIN.pbt), fmt(FIN.pbt_py), "8.2%"]]
        facts = {2: _f("uop", "table", "What was underlying operating profit in 2025/26?",
                       f"£{fmt(FIN.uop)} million, up 9.2% from £{fmt(FIN.uop_py)} million.",
                       f"Underlying operating profit {fmt(FIN.uop)} {fmt(FIN.uop_py)} 9.2%")}
        L.must(L.table(rows, [250, 80, 80, 80], facts=facts), "results table")
        L.fill("regulation", topics=["regulation", "strategy", "supply"])

    @staticmethod
    def cashflow(r: Report, L: Layout) -> None:
        L.para("The Group generated strong cash flow during the year. The table summarises the main components; the full statement is on page %d."
               % r.printed(r.leaf_by_slug["cfstatement"].start))
        rows = [["£m", "2025/26", "2024/25"],
                ["Cash generated from operations", fmt(FIN.cgo), fmt(FIN.cgo_py)],
                ["Net interest paid", fmt(FIN.int_paid), fmt(FIN.int_paid_py)],
                ["Tax paid", fmt(FIN.tax_paid), fmt(FIN.tax_paid_py)],
                ["Net cash inflow from operating activities", fmt(FIN.op_cash), fmt(FIN.op_cash_py)],
                ["Capital expenditure", fmt(FIN.capex), fmt(FIN.capex_py)],
                ["Proceeds from disposals", fmt(FIN.disposals), fmt(FIN.disposals_py)],
                ["Dividends paid", fmt(FIN.divs_paid), fmt(FIN.divs_paid_py)],
                ["Net cash movement before financing", fmt(FIN.net_cash), fmt(FIN.net_cash_py)]]
        facts = {
            1: _f("cgo", "table", "What was cash generated from operations in 2025/26?", f"£{fmt(FIN.cgo)} million.",
                  f"Cash generated from operations {fmt(FIN.cgo)} {fmt(FIN.cgo_py)}"),
            5: _f("capex", "table", "What was capital expenditure in the summary cash flow statement for 2025/26?",
                  f"£{fmt(-FIN.capex)} million outflow, shown as {fmt(FIN.capex)}.", f"Capital expenditure {fmt(FIN.capex)} {fmt(FIN.capex_py)}"),
            6: _f("disposals", "table", "How much did Northbridge receive from disposals in 2025/26?", f"£{fmt(FIN.disposals)} million.",
                  f"Proceeds from disposals {fmt(FIN.disposals)} {fmt(FIN.disposals_py)}"),
        }
        L.must(L.table(rows, [300, 90, 90], facts=facts, bold_rows=frozenset({4, 8})), "cash flow table")
        L.para("Operating cash flow covered capital expenditure and dividends in full. The increase in disposal proceeds reflects the sale of a "
               "minority stake in an offshore transmission asset.")
        L.fill("supply", topics=["supply", "regulation"])

    @staticmethod
    def netdebt(r: Report, L: Layout) -> None:
        reduction = FIN.net_debt_py - FIN.net_debt
        q = f"Net debt at 31 March 2026 was £{fmt(FIN.net_debt)} million, a reduction of £{fmt(reduction)} million from £{fmt(FIN.net_debt_py)} million a year earlier."
        L.must(L.para("Net debt and funding. " + q, fact=_f("net_debt", "text", "What was net debt at 31 March 2026?",
                      f"£{fmt(FIN.net_debt)} million, down £{fmt(reduction)} million from £{fmt(FIN.net_debt_py)} million.", q)), "net debt fact")
        q2 = f"Details of our financial commitments and contingent liabilities are set out in note 29 on page {r.note_page('n29')}."
        L.must(L.para(q2, fact=_f("note29_xref", "cross_reference", "Where are the Group’s financial commitments and contingent liabilities described?",
                      f"In note 29, on printed page {r.note_page('n29')} (physical page {r.leaf_by_slug['n29'].start}).", q2.rstrip("."))), "xref fact")
        L.para(f"The reduction reflects the net cash inflow of £{fmt(FIN.net_cash)} million before financing, partly offset by £{fmt(FIN.net_cash - reduction)} million "
               "of non-cash movements, mainly the indexation of inflation-linked debt.")
        L.fill("supply", topics=["supply", "risk", "regulation"])

    @staticmethod
    def dividend(r: Report, L: Layout) -> None:
        q = f"The Board recommends a final dividend of {DIV_FINAL:.2f} pence per share, which together with the interim dividend of {DIV_INTERIM:.2f} pence makes a total dividend for the year of {DIV_TOTAL:.2f} pence per share."
        L.must(L.para("Dividend. " + q, fact=_f("dividend", "text", "What is the total dividend per share for 2025/26?",
                      f"{DIV_TOTAL:.2f} pence per share ({DIV_INTERIM:.2f} pence interim plus {DIV_FINAL:.2f} pence final).",
                      f"makes a total dividend for the year of {DIV_TOTAL:.2f} pence per share")), "dividend fact")
        L.para(f"The total dividend is {((DIV_TOTAL / DIV_TOTAL_PY) - 1) * 100:.1f}% higher than last year’s {DIV_TOTAL_PY:.2f} pence. Subject to approval at the Annual "
               "General Meeting, the final dividend will be paid on 28 August 2026 to shareholders on the register on 31 July 2026.")
        L.fill("strategy", topics=["shareholder", "strategy"])

    @staticmethod
    def pru(r: Report, L: Layout) -> None:
        L.para("The Board is responsible for determining the nature and extent of the principal risks the Group is willing to take.")
        L.fill("risk", topics=["risk", "governance"])

    @staticmethod
    def bm(r: Report, L: Layout) -> None:
        L.para("We own and operate electricity generation, transmission and distribution assets and serve customers across the North of England "
               "and the Midlands. The three parts of our business model reinforce one another.")
        L.fill("strategy", topics=["strategy", "generation", "networks", "customers"])

    @staticmethod
    def divider(r: Report, L: Layout) -> None:
        intro = {SR: "Our strategy, how we performed during the year and the principal risks we manage.",
                 GOV: "How the Board and its committees oversee the business, and how we reward our leaders.",
                 FS: "The audited financial statements, the notes to the accounts and the auditor’s report.",
                 OTH: "A five-year summary, a glossary of terms and information for shareholders."}[L.leaf.title]
        L.para(intro)
        L.y += 10
        seen: set[tuple[str, ...]] = set()
        for lf in r.leaves:
            chain = lf.full_path[1:] if lf.path[:1] == (L.leaf.title,) else []
            for depth, title in enumerate(chain):
                ident = tuple(chain[: depth + 1])
                if ident not in seen:
                    seen.add(ident)
                    L.must(L.toc_row(depth, title, str(r.printed(lf.start))), "divider entry")

    @staticmethod
    def risks(r: Report, L: Layout) -> None:
        q = "The risk of a significant cyber attack on our operational technology was assessed as having increased during the year."
        L.must(L.para("Cyber security. " + q, fact=_f("cyber_risk", "text", "Which principal risk was assessed as having increased during the year?",
                      "The risk of a significant cyber attack on operational technology.", q.rstrip("."))), "cyber fact")
        rows = [["Principal risk", "Change", "Rating", "Key mitigations"],
                ["Network resilience and extreme weather", "Stable", "High", "Targeted reinforcement, storm response plans and mutual aid agreements with neighbouring operators."],
                ["Cyber security of operational technology", "Increased", "High", "Segmented control networks, continuous monitoring and annual red-team exercises."],
                ["Regulatory and political change", "Stable", "Medium", "Early engagement on price control design and scenario planning."],
                ["Supply chain and delivery", "Decreased", "Medium", "Framework agreements, dual sourcing and early ordering of long-lead equipment."],
                ["Safety and wellbeing", "Stable", "Medium", "Critical-risk standards, contractor assurance and mental health support."],
                ["Climate change and transition", "Stable", "Medium", "Adaptation plans, science-based targets and flexible capacity."],
                ["People and skills", "Increased", "Medium", "Apprenticeships, retention incentives and returner programmes."]]
        L.must(L.table(rows, [150, 52, 46, 247], aligns="llll", wrap_cells=True, size=8), "risk table")
        L.fill("risk", topics=["risk", "regulation", "supply", "climate"])

    @staticmethod
    def viability(r: Report, L: Layout) -> None:
        q = "The directors assessed the viability of the Group over a period of five years to 31 March 2031."
        L.must(L.para("Viability statement. " + q + " This period aligns with our business plan and the regulatory price control cycle.",
                      fact=_f("viability", "text", "Over what period did the directors assess the Group’s viability?",
                              "Five years, to 31 March 2031.", q.rstrip("."))), "viability fact")
        L.fill("risk", topics=["risk", "audit"])

    @staticmethod
    def sustain(r: Report, L: Layout) -> None:
        q = "We are committed to reducing our Scope 1 and 2 emissions by 62% by 2030 against a 2019/20 baseline, our “Clean Network 2030” commitment."
        L.must(L.para(q, fact=_f("emissions_target", "text", "By how much does Northbridge plan to reduce its Scope 1 and 2 emissions by 2030?",
                      "A 62% reduction in Scope 1 and 2 emissions by 2030 against a 2019/20 baseline (the “Clean Network 2030” commitment).",
                      "reducing our Scope 1 and 2 emissions by 62% by 2030 against a 2019/20 baseline")), "emissions fact")
        L.fill("climate", topics=["climate", "supply", "people"])

    @staticmethod
    def board(r: Report, L: Layout) -> None:
        q = "Raj Patel, Chief Executive Officer, joined the Board in 2019 and was appointed Chief Executive in 2022."
        L.must(L.para(q, fact=_f("ceo", "text", "Who is Northbridge’s Chief Executive Officer?",
                      "Raj Patel, who joined the Board in 2019 and became Chief Executive in 2022.", q.rstrip("."))), "board fact")
        L.para("Dame Helen Marsh, Chair, joined the Board in 2023. She was previously Deputy Chair of a FTSE 100 infrastructure group.")
        L.para("Karen Osei, Chief Financial Officer, joined the Board in 2021 and chairs the Group’s disclosure committee.")
        L.fill("governance", topics=["governance", "people"])

    @staticmethod
    def govreport(r: Report, L: Layout) -> None:
        L.fill("governance", topics=["governance", "regulation", "risk", "shareholder"])

    @staticmethod
    def audit(r: Report, L: Layout) -> None:
        q = "Total fees paid to the external auditor were £4.3 million, of which audit-related fees were £3.1 million."
        L.must(L.para(q, fact=_f("audit_fees", "text", "What were the total fees paid to the external auditor?",
                      "£4.3 million, of which £3.1 million was for audit-related services.", q.rstrip("."))), "audit fee fact")
        L.fill("audit", topics=["audit", "risk"])

    @staticmethod
    def remuneration(r: Report, L: Layout) -> None:
        L.para("The table below sets out the single total figure of remuneration for each executive director for the year ended 31 March 2026.")
        rows = [["£000", "2025/26", "2024/25"],
                ["Raj Patel, Chief Executive Officer", "3,214", "2,968"],
                ["Karen Osei, Chief Financial Officer", "1,872", "1,745"]]
        facts = {1: _f("ceo_pay", "table", "What was the Chief Executive’s single total figure of remuneration for 2025/26?",
                       "£3,214,000 (£3,214 thousand).", "Raj Patel, Chief Executive Officer 3,214 2,968")}
        L.must(L.table(rows, [300, 90, 90], facts=facts), "pay table")
        L.fill("governance", topics=["governance", "people"])

    @staticmethod
    def dirreport(r: Report, L: Layout) -> None:
        L.fill("governance", topics=["governance", "shareholder"])

    @staticmethod
    def auditor(r: Report, L: Layout) -> None:
        q = "In our opinion, the Group financial statements give a true and fair view of the state of the Group’s affairs as at 31 March 2026."
        L.must(L.para("Opinion. " + q, fact=_f("audit_opinion", "text", "What is the external auditor’s opinion on the Group financial statements?",
                      "They give a true and fair view of the Group’s affairs as at 31 March 2026.", q.rstrip("."))), "opinion fact")
        L.fill("audit", topics=["audit", "risk"])

    @staticmethod
    def income(r: Report, L: Layout) -> None:
        rows = [["£m", "2025/26", "2024/25"],
                ["Revenue", fmt(FIN.rev), fmt(FIN.rev_py)],
                ["Underlying operating profit", fmt(FIN.uop), fmt(FIN.uop_py)],
                ["Exceptional items", fmt(FIN.exceptional), fmt(FIN.exceptional_py)],
                ["Operating profit", fmt(FIN.op), fmt(FIN.op_py)],
                ["Net finance costs", fmt(FIN.fin_costs), fmt(FIN.fin_costs_py)],
                ["Profit before tax", fmt(FIN.pbt), fmt(FIN.pbt_py)],
                ["Tax", fmt(FIN.tax), fmt(FIN.tax_py)],
                ["Profit for the year", fmt(FIN.profit), fmt(FIN.profit_py)],
                ["Earnings per share (pence)", f"{FIN.profit / SHARES_M * 100:.1f}", f"{FIN.profit_py / SHARES_M * 100:.1f}"]]
        facts = {8: _f("profit_year", "table", "What was the Group’s profit for the year 2025/26?", f"£{fmt(FIN.profit)} million.",
                       f"Profit for the year {fmt(FIN.profit)} {fmt(FIN.profit_py)}")}
        L.para("For the year ended 31 March 2026. The accompanying notes form part of these financial statements.", S_SMALL)
        L.must(L.table(rows, [300, 90, 90], facts=facts, bold_rows=frozenset({4, 6, 8})), "income statement")

    @staticmethod
    def balance(r: Report, L: Layout) -> None:
        F = FIN
        rows = [["£m", "2026", "2025"],
                ["Property, plant and equipment", fmt(F.ppe), fmt(F.ppe_py)],
                ["Intangible assets", fmt(F.intang), fmt(F.intang_py)],
                ["Investments", fmt(F.invest), fmt(F.invest_py)],
                ["Non-current assets", fmt(F.nca), fmt(F.nca_py)],
                ["Inventories", fmt(F.inventories), fmt(F.inventories_py)],
                ["Trade and other receivables", fmt(F.receivables), fmt(F.receivables_py)],
                ["Cash and short-term investments", fmt(F.cash), fmt(F.cash_py)],
                ["Current assets", fmt(F.ca), fmt(F.ca_py)],
                ["Borrowings due within one year", fmt(F.borrow_cur), fmt(F.borrow_cur_py)],
                ["Trade and other payables", fmt(F.payables), fmt(F.payables_py)],
                ["Current liabilities", fmt(F.cl), fmt(F.cl_py)],
                ["Borrowings due after one year", fmt(F.borrow_nc), fmt(F.borrow_nc_py)],
                ["Deferred tax liabilities", fmt(F.deferred_tax), fmt(F.deferred_tax_py)],
                ["Retirement benefit obligations", fmt(F.pensions), fmt(F.pensions_py)],
                ["Provisions", fmt(F.provisions), fmt(F.provisions_py)],
                ["Non-current liabilities", fmt(F.ncl), fmt(F.ncl_py)],
                ["Total equity", fmt(F.equity), fmt(F.equity_py)]]
        facts = {17: _f("total_equity", "table", "What was total equity at 31 March 2026?", f"£{fmt(F.equity)} million.",
                        f"Total equity {fmt(F.equity)} {fmt(F.equity_py)}")}
        L.para("At 31 March 2026. The financial statements were approved by the Board on 20 May 2026.", S_SMALL)
        L.must(L.table(rows, [300, 90, 90], facts=facts, bold_rows=frozenset({4, 8, 11, 16, 17})), "balance sheet")

    @staticmethod
    def equity(r: Report, L: Layout) -> None:
        F = FIN
        rows = [["£m", "Share capital", "Share premium", "Other reserves", "Retained earnings", "Total"],
                ["At 1 April 2025", "412", "1,946", "1,208", fmt(F.equity_py - 412 - 1946 - 1208), fmt(F.equity_py)],
                ["Profit for the year", "-", "-", "-", fmt(F.profit), fmt(F.profit)],
                ["Dividends paid", "-", "-", "-", fmt(F.divs_paid), fmt(F.divs_paid)],
                ["Other movements", "-", "-", "37", fmt(F.equity - F.equity_py - F.profit - F.divs_paid - 37), fmt(F.equity - F.equity_py - F.profit - F.divs_paid)],
                ["At 31 March 2026", "412", "1,946", "1,245", fmt(F.equity - 412 - 1946 - 1245), fmt(F.equity)]]
        L.para("For the year ended 31 March 2026.", S_SMALL)
        L.must(L.table(rows, [150, 66, 66, 66, 70, 62], bold_rows=frozenset({5})), "equity statement")

    @staticmethod
    def cfstatement(r: Report, L: Layout) -> None:
        F = FIN
        rows = [["£m", "2025/26", "2024/25"],
                ["Cash generated from operations", fmt(F.cgo), fmt(F.cgo_py)],
                ["Interest paid", fmt(F.int_paid), fmt(F.int_paid_py)],
                ["Tax paid", fmt(F.tax_paid), fmt(F.tax_paid_py)],
                ["Net cash from operating activities", fmt(F.op_cash), fmt(F.op_cash_py)],
                ["Purchase of property, plant and equipment", fmt(F.capex), fmt(F.capex_py)],
                ["Proceeds from disposals", fmt(F.disposals), fmt(F.disposals_py)],
                ["Net cash used in investing activities", fmt(F.capex + F.disposals), fmt(F.capex_py + F.disposals_py)],
                ["Dividends paid to shareholders", fmt(F.divs_paid), fmt(F.divs_paid_py)],
                ["Net cash movement before financing", fmt(F.net_cash), fmt(F.net_cash_py)]]
        L.para("For the year ended 31 March 2026.", S_SMALL)
        L.must(L.table(rows, [300, 90, 90], bold_rows=frozenset({4, 7, 9})), "cash flow statement")

    # ---- notes ----------------------------------------------------------------------------------------
    @staticmethod
    def _note_block(r: Report, L: Layout, num: int, title: str) -> bool:
        flt = r.filler
        if not L.heading(f"{num}. {title}", S_H3):
            return False
        ok = L.para(flt.paragraph("accounting", 2, _NOTES_TOPICS), keep=True)
        rows = [["£m", "2026", "2025"]] + [[n, fmt(a), fmt(b)] for n, a, b in flt.mini_rows(3)]
        return ok and L.table(rows, [300, 90, 90], size=7.8)

    @staticmethod
    def _notes(r: Report, L: Layout, nums: list[int], intro: Optional[str] = None) -> None:
        if intro:
            L.para(intro)
        for num in nums:
            if not _Content._note_block(r, L, num, _NOTE_TITLES[num]):
                break
        # pad any unused budget with accounting boilerplate
        L.fill("accounting", pool=_NOTES_TOPICS)

    @staticmethod
    def n1(r: Report, L: Layout) -> None:
        L.para("The accompanying notes form part of these consolidated financial statements.")
        L.heading("1. Accounting policies", S_H3)
        L.fill("accounting", pool=_NOTES_TOPICS)

    @staticmethod
    def n2(r: Report, L: Layout) -> None:
        L.para("The Group’s operating segments reflect the way the business is managed and reported to the Board.")
        rows = [["£m", "Generation", "Networks", "Customers", "Corporate", "Group"],
                ["Revenue", "3,962", "8,214", "2,318", "318", fmt(FIN.rev)],
                ["Underlying operating profit", "712", "2,094", "281", "40", fmt(FIN.uop)],
                ["Capital expenditure", "(584)", "(1,732)", "(206)", "(74)", fmt(FIN.capex)],
                ["Net assets", "9,214", "21,306", "1,812", fmt(FIN.equity - 9214 - 21306 - 1812), fmt(FIN.equity)]]
        L.must(L.table(rows, [180, 110, 110, 110, 110, 100]), "segment table")
        L.fill("accounting", pool=_NOTES_TOPICS)

    @staticmethod
    def n3(r: Report, L: Layout) -> None:
        _Content._notes(r, L, [3, 4, 5], "Exceptional items of £295 million relate mainly to restructuring and the impairment of a legacy generation asset.")

    @staticmethod
    def n6(r: Report, L: Layout) -> None:
        _Content._notes(r, L, [6, 7, 8, 9, 10, 11, 12])

    @staticmethod
    def n13(r: Report, L: Layout) -> None:
        _Content._notes(r, L, [13, 14, 15, 16, 17, 18, 19, 20],
                        f"Net debt of £{fmt(FIN.net_debt)} million is reconciled in note 14; the maturity profile of borrowings is shown in note 13.")

    @staticmethod
    def n21(r: Report, L: Layout) -> None:
        _Content._notes(r, L, [21, 22, 23, 24, 25, 26])

    @staticmethod
    def n27(r: Report, L: Layout) -> None:
        _Content._notes(r, L, [27, 28], "See note 13 on page %d for the Group’s borrowings and capital management policy." % r.note_page("n13"))

    @staticmethod
    def n29(r: Report, L: Layout) -> None:
        L.heading("29. Financial commitments and contingencies", S_H3)
        q = "Capital expenditure contracted for but not provided for at 31 March 2026 was £1,946 million (2025: £1,708 million)."
        L.must(L.para(q, fact=_f("commitments", "text", "How much capital expenditure was contracted for but not provided for at 31 March 2026?",
                      "£1,946 million (2025: £1,708 million).", q.rstrip("."))), "commitments fact")
        L.para("The Group has given guarantees in the normal course of business. No material loss is expected to arise from these contingent liabilities. "
               "Further information on borrowings is given in note 13 on page %d." % r.note_page("n13"))
        L.fill("accounting", pool=_NOTES_TOPICS)

    @staticmethod
    def n30(r: Report, L: Layout) -> None:
        _Content._notes(r, L, [30, 31])

    @staticmethod
    def fiveyear(r: Report, L: Layout) -> None:
        L.para("Five-year summary of the Group’s results, as reported. Figures for earlier years have not been restated.")
        rows = [["£m unless stated", "2025/26", "2024/25", "2023/24", "2022/23", "2021/22"],
                ["Revenue", fmt(FIN.rev), fmt(FIN.rev_py), "12,964", "12,437", "11,102"],
                ["Underlying operating profit", fmt(FIN.uop), fmt(FIN.uop_py), "2,611", "2,452", "2,203"],
                ["Profit for the year", fmt(FIN.profit), fmt(FIN.profit_py), "1,206", "1,148", "982"],
                ["Net debt", fmt(FIN.net_debt), fmt(FIN.net_debt_py), "23,402", "23,877", "24,215"],
                ["Dividend per share (pence)", f"{DIV_TOTAL:.2f}", f"{DIV_TOTAL_PY:.2f}", "31.02", "28.74", "26.40"]]
        facts = {1: _f("five_year_revenue", "table", "What was group revenue in 2023/24 according to the five-year summary?",
                       "£12,964 million.", f"Revenue {fmt(FIN.rev)} {fmt(FIN.rev_py)} 12,964 12,437 11,102")}
        L.must(L.table(rows, [230, 100, 100, 100, 100, 100], facts=facts), "five-year table")
        L.fill("shareholder", topics=["shareholder"], max_paragraphs=3)

    @staticmethod
    def glossary(r: Report, L: Layout) -> None:
        terms = [("Alternative performance measure (APM)", "A financial measure not defined by accounting standards, such as underlying operating profit."),
                 ("Capital expenditure", "Cash spent on acquiring or upgrading property, plant and equipment."),
                 ("Distribution network", "The lower-voltage wires and substations that carry electricity to homes and businesses."),
                 ("Exceptional items", "Items that are material or unusual in nature and are shown separately to aid understanding of performance."),
                 ("Net debt", "Borrowings less cash and short-term investments."),
                 ("Regulated asset value", "The value of network assets on which the regulator allows the Group to earn a return."),
                 ("Scope 1 and 2 emissions", "Direct emissions from our operations and indirect emissions from the electricity we purchase."),
                 ("Transmission network", "The high-voltage system that moves electricity over long distances between generators and distribution networks.")]
        for t, d in terms:
            L.para(f"{t}. {d}", keep=True)
        L.fill("shareholder", topics=["shareholder"], max_paragraphs=2)

    @staticmethod
    def shareholder(r: Report, L: Layout) -> None:
        L.para("Annual General Meeting: 24 July 2026 at 11.00am, at the Company’s registered office.")
        L.para("Financial calendar: half-year results 12 November 2026; preliminary results for 2026/27 on 19 May 2027.")
        L.para("Registrar: Quayside Registrars Limited, PO Box 4410, Leeds LS1 9XX. Shareholder helpline +44 (0)800 555 0142.")
        L.fill("shareholder", topics=["shareholder"], max_paragraphs=3)


# --------------------------------------------------------------------------------------------- public API
def build_sample_pdf(path: str | Path, pages: int = 60, printed_offset: int = 2, seed: int = 0, *,
                     write_facts: bool = True) -> Path:
    """Build the sample report at `path` (and `<stem>.facts.json` beside it). Deterministic for equal arguments."""
    if pages < min_pages():
        raise ValueError(f"pages must be >= {min_pages()} (one page per section plus cover and contents)")
    if printed_offset < 0:
        raise ValueError("printed_offset must be >= 0")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rep = Report(path, pages, printed_offset, seed)
    rep.build()
    if write_facts:
        facts = sorted(rep.facts, key=lambda f: (f["page"], f["id"]))
        path.with_suffix(".facts.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("wrote %s (%d pages, %d facts)", path, pages, len(rep.facts))
    return path


def facts_path(pdf: str | Path) -> Path:
    return Path(pdf).with_suffix(".facts.json")


def load_facts(pdf: str | Path) -> list[dict]:
    return json.loads(facts_path(pdf).read_text(encoding="utf-8"))


_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Whitespace-collapsed page text (pdfium emits \\r\\n and U+FFFE for line-end hyphens)."""
    return _WS.sub(" ", text.replace("￾", "").replace("\x02", "")).strip()


def verify_facts(pdf: str | Path, facts: Optional[list[dict]] = None) -> list[str]:
    """Check every fact's quote really is on its page (pdfium text). Returns a list of problems (empty = all good)."""
    import pypdfium2 as pdfium

    facts = facts if facts is not None else load_facts(pdf)
    problems: list[str] = []
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for f in facts:
            page = doc[f["page"] - 1]
            tp = page.get_textpage()
            text = normalise(tp.get_text_range())
            tp.close()
            page.close()
            if normalise(f["quote"]) not in text:
                problems.append(f"{f['id']}: quote not found on page {f['page']}: {f['quote']!r}")
    finally:
        doc.close()
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "samples" / "sample_annual_report.pdf"))
    ap.add_argument("--pages", type=int, default=60)
    ap.add_argument("--offset", type=int, default=2, help="physical page minus printed folio")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = build_sample_pdf(args.out, args.pages, args.offset, args.seed)
    problems = verify_facts(out)
    for p in problems:
        log.error(p)
    log.info("%s: %.0f KB, %d fact problem(s)", out, out.stat().st_size / 1024, len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
