"""Which ITR, and which documents — asked before anything is uploaded.

The wizard used to open on "upload your documents", which is the wrong first
question. Nobody knows which documents until they know which form, and nobody
knows which form until someone asks what their year actually looked like.

So this asks about a dozen yes-or-no things, and answers two questions from
them: **which return** and **what to go and fetch**. It runs entirely off the
answers — no document has been read at this point and none needs to be.

The form prediction reuses ``itr.selector`` rather than reimplementing it, by
building a skeleton return that carries just enough shape for the selector to
decide. Two implementations of the same rules would drift, and the one the user
sees first would be the one nobody tested.

The checklist is the more useful half. Most of what makes an Indian return go
wrong is a document nobody knew to collect — the Form 12BA that values an RSU
perquisite, the 31 January 2018 price that grandfathers a pre-2018 holding, the
broker's registered address that Schedule FA will not submit without, and Form
67, which has to be on the portal *before* the return rather than with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from .money import D
from .schemas import (
    CapitalGainItem,
    HouseProperty,
    SalaryIncome,
    TaxReturn,
    TradingSegment,
)


# --------------------------------------------------------------------------
# The questions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    help: str = ""
    group: str = "Income"


QUESTIONS: List[Question] = [
    Question(
        "resident",
        "Were you a resident of India for the whole year?",
        "Broadly, 182 days or more in India during the financial year. If you "
        "moved abroad or arrived mid-year, say no — it changes the form and "
        "what has to be reported.",
        "About you",
    ),
    Question(
        "salary", "Did you receive a salary or a pension?",
        "Including any month of it, and including a job you left mid-year.",
    ),
    Question(
        "multiple_employers", "From more than one employer?",
        "A mid-year job change means two Form 16s, and the second employer "
        "usually gave the exemptions again — which is the commonest reason a "
        "salaried return ends up owing tax.",
    ),
    Question(
        "house_property", "Do you own a house or flat?",
        "Whether you live in it, let it out, or it sits empty.",
    ),
    Question(
        "house_let_out", "Is any of it let out?",
        "Rent received is taxable even where the loan interest exceeds it.",
    ),
    Question(
        "multiple_houses", "Do you own more than one?",
        "",
    ),
    Question(
        "equity_delivery",
        "Did you sell any shares or mutual fund units you had been holding?",
        "Delivery-based sales. This is capital gains, and it is the only part "
        "of a trading account that is.",
        "Investments and trading",
    ),
    Question(
        "intraday",
        "Did you buy and sell equity within the same day?",
        "Intraday equity is a *speculative business* under section 43(5). Its "
        "loss can only be set against speculative income and it lapses after "
        "four years, so it has to be kept separate from everything else.",
        "Investments and trading",
    ),
    Question(
        "fno",
        "Did you trade futures or options — equity, index, currency or "
        "commodity?",
        "F&O is business income, not capital gains. The provisos to section "
        "43(5) take derivatives on a recognised exchange out of the definition "
        "of speculation, so it is ordinary business income at slab rates — and "
        "it means ITR-3.",
        "Investments and trading",
    ),
    Question(
        "foreign_equity",
        "Do you hold shares in a foreign company — RSUs, ESPP or bought "
        "directly?",
        "Even one share, and even if it produced no income. Schedule FA has no "
        "threshold, and omitting a holding carries a flat ₹10 lakh penalty "
        "under the Black Money Act regardless of the tax at stake.",
        "Foreign",
    ),
    Question(
        "foreign_dividend",
        "Did any foreign holding pay a dividend?",
        "Reinvested counts. It is income on the payment date whether or not a "
        "rupee reached your account.",
        "Foreign",
    ),
    Question(
        "business",
        "Did you run a business or a profession, other than trading?",
        "Consulting, freelancing, a shop, professional fees.",
    ),
    Question(
        "other_income",
        "Any interest, Indian dividends, or income from anything else?",
        "Savings and fixed deposit interest, dividends from Indian companies, "
        "gifts, family pension. The AIS has all of it, and so does the "
        "department.",
    ),
    Question(
        "winnings",
        "Any lottery, betting or game-show winnings?",
        "Taxed at a flat 30% with no deduction and no rebate.",
    ),
    Question(
        "crypto",
        "Did you buy or sell crypto, or any other virtual digital asset?",
        "Section 115BBH taxes these at a flat 30% with no deduction except the "
        "cost, and — unusually — a loss cannot be set off against anything at "
        "all, not even another crypto gain.",
        "Investments and trading",
    ),
    Question(
        "huf",
        "Are you filing for a Hindu Undivided Family rather than yourself?",
        "",
        "About you",
    ),
    Question(
        "director_or_unlisted",
        "Are you a director of a company, or do you hold unlisted shares?",
        "Either one rules out the simpler forms on its own.",
        "About you",
    ),
    Question(
        "carried_losses",
        "Do you have losses carried forward from an earlier year?",
        "From an earlier return that was filed on time. They cannot be carried "
        "in the simplest form.",
        "About you",
    ),
    Question(
        "old_regime",
        "Do you want to claim deductions like 80C, 80D or HRA?",
        "These need the old regime. The system computes both and tells you "
        "which is cheaper, so answer yes if you might.",
        "About you",
    ),
]

_GROUP_ORDER = ("About you", "Income", "Investments and trading", "Foreign")


def grouped_questions() -> List[tuple]:
    """Questions in display order, grouped by section."""
    return [
        (group, [q for q in QUESTIONS if q.group == group])
        for group in _GROUP_ORDER
    ]


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


@dataclass
class DocumentNeed:
    name: str
    where: str
    why: str = ""
    essential: bool = True
    # Which download to pick when the source offers a choice. This is where
    # people lose the most time: TRACES offers HTML, text and PDF; the AIS
    # offers PDF and JSON; a broker offers PDF and Excel. Only one of each is
    # the right answer, and nothing on those screens says which.
    file_format: str = ""

    @property
    def key(self) -> str:
        """A stable identifier, so a tick survives re-answering the questions."""
        import hashlib

        return hashlib.sha1(self.name.encode()).hexdigest()[:12]


@dataclass
class Trip:
    """One place you have to go, and everything to collect while you are there.

    Documents were grouped by head of income, which is how the *return* is
    organised and not how the collecting is. You do not visit "salary"; you log
    into one portal, email one employer, open one broker account. Grouping by
    destination turns a list of twenty-eight things into five short errands, in
    an order where nothing blocks anything after it.
    """

    number: int
    title: str
    where: str
    note: str = ""
    documents: List[DocumentNeed] = field(default_factory=list)


# Situations this system does not compute correctly. Each one is asked about
# rather than inferred, because the failure would otherwise be silent — a
# confident, plausible, wrong return — and that is the worst way for a tax tool
# to be wrong.
UNSUPPORTED = {
    "crypto": (
        "Crypto and other virtual digital assets are not implemented at all. "
        "Section 115BBH taxes them at a flat 30%, allows no deduction beyond "
        "the cost of acquisition, and does not permit a loss to be set off "
        "against anything — not even another crypto gain. This system would "
        "put the gain into ordinary capital gains at 12.5% or slab rates and "
        "quietly allow set-offs the section forbids. Use something else, or a "
        "chartered accountant."
    ),
    "huf": (
        "Only individual returns are produced. A HUF return is a different "
        "assessee with its own PAN, and the generated JSON declares the status "
        "as individual."
    ),
    "business": (
        "A business or profession with real books — stock, debtors, "
        "depreciation, a balance sheet — is beyond what this computes. It "
        "handles trading, where the broker's statement is the only record "
        "there is. Presumptive schemes under 44AD and 44ADA are recognised but "
        "their JSON is not generated."
    ),
}

NON_RESIDENT_WARNING = (
    "You have said you were not resident for the whole year. Residence, and "
    "the RNOR status in between, change which income is taxable at all, "
    "whether the basic exemption can shelter capital gains, and which treaty "
    "applies. None of that is properly implemented here. The computation will "
    "produce a number and it should not be relied on — this system is built "
    "for a resident and ordinarily resident individual."
)


@dataclass
class Guidance:
    form: str
    form_reasons: List[str] = field(default_factory=list)
    form_supported: bool = True
    form_note: str = ""
    # The same documents twice over: as ordered errands, which is how they get
    # collected, and flat by group, which is how the upload page shows them.
    trips: List[Trip] = field(default_factory=list)
    documents: Dict[str, List[DocumentNeed]] = field(default_factory=dict)
    deadlines: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # Things this system cannot do for the situation described. Shown first
    # and shown loudly, before any figure is entered.
    blockers: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.blockers

    @property
    def document_count(self) -> int:
        return sum(len(trip.documents) for trip in self.trips)

    @property
    def all_documents(self) -> List[DocumentNeed]:
        return [item for trip in self.trips for item in trip.documents]

    def gathered(self, collected) -> int:
        keys = set(collected or ())
        return sum(1 for item in self.all_documents if item.key in keys)

    def outstanding(self, collected) -> List[DocumentNeed]:
        """What is still to fetch, essentials first — the actual next action."""
        keys = set(collected or ())
        left = [item for item in self.all_documents if item.key not in keys]
        return sorted(left, key=lambda item: not item.essential)


def _need(group: str, guidance: Guidance, *needs: DocumentNeed) -> None:
    guidance.documents.setdefault(group, []).extend(needs)


def build_guidance(answers: Dict[str, bool], assessment_year: str) -> Guidance:
    """Turn the answers into a form and a shopping list."""
    from .itr.selector import select_form
    from .tax.rules import get_ay

    ay = get_ay(assessment_year)
    yes = lambda key: bool(answers.get(key))     # noqa: E731

    decision = select_form(_skeleton(answers, assessment_year))
    guidance = Guidance(
        form=decision.form,
        form_reasons=decision.reasons,
        form_supported=decision.supported,
        form_note=decision.note,
    )

    guidance.trips = _build_trips(answers, ay)
    # The flat grouping is kept because the upload page and the tests read it,
    # but the trips are what a person actually follows.
    for trip in guidance.trips:
        guidance.documents[f"{trip.number}. {trip.title}"] = trip.documents


    _add_warnings(guidance, answers)

    for key, message in UNSUPPORTED.items():
        if yes(key):
            guidance.blockers.append(message)
    if not yes("resident"):
        guidance.blockers.append(NON_RESIDENT_WARNING)
    if not guidance.form_supported and guidance.form_note:
        guidance.blockers.append(f"{guidance.form}: {guidance.form_note}")

    _add_deadlines(guidance, answers, ay)
    return guidance


def _add_warnings(guidance: Guidance, answers: Dict[str, bool]) -> None:
    """The handful of things that cost real money and nobody expects."""
    yes = lambda key: bool(answers.get(key))     # noqa: E731

    if yes("multiple_employers"):
        guidance.warnings.append(
            "With two employers, each one almost certainly gave you the full "
            "standard deduction and the full basic exemption again. The "
            "combined figures rarely match either Form 16, and the shortfall "
            "is payable with interest."
        )

    if yes("fno") or yes("intraday"):
        guidance.warnings.append(
            "F&O and intraday make this a business return. That means ITR-3, "
            "and it means the old regime — if it turns out cheaper — has to be "
            "chosen on Form 10-IEA before the due date rather than on the "
            "return itself."
        )

    if yes("foreign_equity"):
        guidance.warnings.append(
            "Schedule FA has no threshold and no exemption. A resident and "
            "ordinarily resident who held any foreign asset at any point in "
            "the calendar year must report it, whatever it was worth and "
            "whether or not it paid anything. The penalty for omitting one is "
            "a flat ₹10 lakh under the Black Money Act, assessed on the "
            "non-disclosure rather than on any tax — so it applies in full "
            "even when the tax was paid correctly."
        )


def _build_trips(answers: Dict[str, bool], ay) -> List[Trip]:
    """The whole collection, as errands in the order they should be run.

    Ordering rule: nothing in a later trip is needed to complete an earlier
    one. The portal comes first because Form 26AS and the AIS between them tell
    you what the department already believes about your year, which is the only
    way to know whether anything further down is missing.
    """
    yes = lambda key: bool(answers.get(key))     # noqa: E731
    trading = yes("fno") or yes("intraday")
    trips: List[Trip] = []
    fy_label = f"{ay.fy_start.year}" if ay else ""

    # -- 1 ------------------------------------------------------------------
    portal = Trip(
        1, "The income tax portal",
        "incometax.gov.in — log in with your PAN",
        f"One login. Select assessment year {ay.ay} everywhere it asks — that "
        f"is the year after the money was earned. If a statement looks emptier "
        f"than you expect, you have picked the previous year.",
    )
    portal.documents += [
        DocumentNeed(
            "Form 26AS",
            "e-File → Income Tax Returns → View Form 26AS → continue to "
            "TRACES → View Tax Credit",
            "Every TDS and TCS entry, and every challan. Anything you claim "
            "that is not here is disallowed when the return is processed.",
            file_format="PDF — use 'Export as PDF'. Text and the zip also work "
                        "now, but the PDF is the format most exercised.",
        ),
        DocumentNeed(
            "Annual Information Statement (AIS)",
            "Services → AIS → " + ay.ay
            + ". Password is your PAN in lower case then date of birth as "
              "DDMMYYYY",
            "What the department already knows: salary, interest, dividends, "
            "securities sales. Income it shows and the return omits is the "
            "commonest reason a notice arrives.",
            file_format="JSON — the portal offers PDF and JSON, and the JSON "
                        "is structured data that parses cleanly. Take the PDF "
                        "as well if you want something readable.",
        ),
        DocumentNeed(
            "Advance tax and self-assessment challans",
            "e-Pay Tax → Payment History",
            "You need the BSR code, challan serial number and date. The credit "
            "is not matched without all three.",
            essential=False,
            file_format="PDF, or simply note the three numbers",
        ),
    ]
    trips.append(portal)

    # -- 2 ------------------------------------------------------------------
    if yes("salary"):
        employer = Trip(
            len(trips) + 1, "Your employer",
            "Payroll or the HR portal",
            "Ask for both together. Form 16 usually arrives by mid-June; "
            "Form 12BA has to be asked for by name more often than not."
            + (" You need a set from every employer you had in the year."
               if yes("multiple_employers") else ""),
        )
        employer.documents.append(DocumentNeed(
            "Form 16, Parts A and B",
            "Your employer" + (", from each one" if yes("multiple_employers")
                               else ""),
            "Part A is the TDS summary; Part B is the salary breakdown. You "
            "need both.",
            file_format="PDF. If it is password protected the password is "
                        "usually your PAN then date of birth.",
        ))
        if yes("foreign_equity"):
            employer.documents.append(DocumentNeed(
                "Form 12BA",
                "Your employer, issued alongside Form 16",
                "The perquisite statement. It values your RSU vests and ESPP "
                "discount, and tells you whether they are already inside the "
                "Form 16 gross salary — which decides whether they get added "
                "again or not at all.",
                file_format="PDF",
            ))
        if yes("old_regime"):
            employer.documents.append(DocumentNeed(
                "Rent receipts and your landlord's PAN",
                "Your landlord",
                "Only if you claim HRA. The PAN is required once rent passes "
                "₹1,00,000 in the year.",
                essential=False, file_format="Scan or photo",
            ))
        trips.append(employer)

    # -- 3 ------------------------------------------------------------------
    if trading or yes("equity_delivery"):
        broker = Trip(
            len(trips) + 1, "Your Indian broker",
            "Your broker's reports or console section",
            "Everything here is one annual download per report. Take the "
            "spreadsheet, never the PDF — the PDF of a trade list parses "
            "badly and there is no reason to use it.",
        )
        if trading:
            broker.documents += [
                DocumentNeed(
                    "Annual tax P&L, segment-wise",
                    "Your broker's annual tax report for the financial year",
                    "It must keep delivery, intraday and F&O apart. They are "
                    "three different heads of income and merging them is "
                    "unlawful in either direction.",
                    file_format="XLSX or CSV — not PDF",
                ),
                DocumentNeed(
                    "Annual charges and brokerage statement",
                    "Your broker",
                    "Brokerage, exchange and clearing charges, SEBI fees, STT "
                    "or CTT, GST, stamp duty and depository charges are all "
                    "deductible against business income. That is the "
                    "compensation for the concessional capital-gains rate you "
                    "have already lost by trading derivatives.",
                    file_format="XLSX or CSV if offered, otherwise PDF",
                ),
            ]
        if yes("equity_delivery"):
            broker.documents += [
                DocumentNeed(
                    "Capital gains statement, trade-wise",
                    "Your broker's annual tax report",
                    "Trade-wise, not summary — the holding period on each lot "
                    "decides the rate.",
                    file_format="XLSX or CSV",
                ),
                DocumentNeed(
                    "Consolidated capital gains statement for mutual funds",
                    "CAMS or KFintech, free by email",
                    "Covers every fund house at once, so you do not have to "
                    "visit each.",
                    essential=False, file_format="PDF or XLSX",
                ),
            ]
        trips.append(broker)

    # -- 4 ------------------------------------------------------------------
    if yes("foreign_equity") or yes("foreign_dividend"):
        foreign = Trip(
            len(trips) + 1, "Your foreign broker or stock plan",
            "E*TRADE, Fidelity, Schwab, Morgan Stanley StockPlan Connect",
            "The most-missed trip. Note that Schedule FA runs on the "
            f"**calendar** year — 1 January to 31 December {fy_label} — not "
            "the financial year, so the year-end statement is a different "
            "period from everything else here.",
        )
        if yes("foreign_equity"):
            foreign.documents += [
                DocumentNeed(
                    "Vesting or release report, every tranche",
                    "Your stock plan administrator",
                    "Each vest needs the fair market value on its own date. "
                    "Sixteen tranches are sixteen salary events at sixteen "
                    "exchange rates.",
                    file_format="CSV or XLSX if offered — the PDF release "
                                "confirmations work but are one file per vest",
                ),
                DocumentNeed(
                    "ESPP purchase confirmations",
                    "Your stock plan administrator",
                    "Fair market value on the purchase date, the price you "
                    "paid, and the price at the start of the offering.",
                    essential=False, file_format="CSV, XLSX or PDF",
                ),
                DocumentNeed(
                    "Form 1099-B or realised gain-and-loss report",
                    "Your broker",
                    "Its cost basis is right for US tax and wrong for Indian "
                    "tax — section 49(2AA) uses the value already taxed as a "
                    "perquisite. The system corrects it; do not copy it.",
                    essential=False, file_format="CSV or XLSX",
                ),
                DocumentNeed(
                    f"Year-end statement: position on 31 December {fy_label} "
                    "and the year's highest value",
                    "Your broker",
                    "For Schedule FA, which runs on the calendar year.",
                    file_format="PDF — you will read two figures off it",
                ),
                DocumentNeed(
                    "The company's and the broker's registered address and "
                    "ZIP code",
                    "The company's investor relations page; the broker's "
                    "statement header",
                    "Schedule FA asks for both and the portal will not accept "
                    "the row without them.",
                    file_format="Copy the text — nothing to download",
                ),
            ]
        if yes("foreign_dividend"):
            foreign.documents += [
                DocumentNeed(
                    "Form 1099-DIV or dividend activity report",
                    "Your broker",
                    "Declare the gross amount, before the 25% the US withholds. "
                    "Netting it off understates income by a quarter and "
                    "forfeits the credit.",
                    file_format="CSV or XLSX",
                ),
                DocumentNeed(
                    "Form 67",
                    "File it on the portal BEFORE you file the return — "
                    "e-File → Income Tax Forms → File Income Tax Forms",
                    "Not a download: something you have to file. The foreign "
                    "tax credit is liable to be denied outright if Form 67 is "
                    "not already on record, which makes this the easiest way "
                    "to lose money on the whole return.",
                    file_format="Filed online — nothing to download",
                ),
            ]
        trips.append(foreign)

    # -- 5 ------------------------------------------------------------------
    lookups = Trip(
        len(trips) + 1, "Things to look up",
        "Nothing to download — figures to type in",
        "Leave these until the system tells you which ones it actually needs. "
        "It names the exact months and scrips on the Foreign and Income pages, "
        "so looking them up now is wasted effort.",
    )
    if yes("foreign_equity") or yes("foreign_dividend"):
        lookups.documents.append(DocumentNeed(
            "SBI TT buying rate, last day of each relevant month",
            "sbi.co.in, or ask your bank",
            "Rule 115 uses the rate on the last day of the month *before* the "
            "income arose, never the day itself. The system lists exactly "
            "which months it needs.",
            file_format="A number per month",
        ))
    if yes("equity_delivery"):
        lookups.documents.append(DocumentNeed(
            "Highest quoted price on 31 January 2018",
            "The exchange, for anything bought before that date",
            "Section 55(2)(ac) grandfathers the gain up to that date. Without "
            "it you pay tax on a gain that is not taxable.",
            essential=False, file_format="A price per scrip",
        ))
    if yes("old_regime"):
        lookups.documents.append(DocumentNeed(
            "Deduction proofs — 80C, 80D, 80G, NPS",
            "Your own records",
            "An 80G receipt must carry the donee's PAN and 80G registration "
            "number or the deduction fails.",
            essential=False, file_format="Figures, with receipts kept on file",
        ))
    if lookups.documents:
        trips.append(lookups)

    return trips


def _add_deadlines(guidance: Guidance, answers: Dict[str, bool], ay) -> None:
    trading = answers.get("fno") or answers.get("intraday")
    business = trading or answers.get("business")

    from .tax.rules import due_date_for

    due = due_date_for(ay, has_business=bool(business))
    guidance.deadlines.append(
        f"Return due {due:%d %B %Y}"
        + (
            " — section 139(1) gives a business return without an audit until "
            f"{ay.due_date_business_non_audit:%d %B %Y}, a month later than "
            f"the {ay.due_date_non_audit:%d %B} date everyone quotes"
            if business else ""
        )
        + f". With a tax audit under section 44AB it is "
          f"{ay.due_date_audit:%d %B %Y}."
    )
    if business:
        guidance.deadlines.append(
            f"Form 10-IEA, on or before {due:%d %B %Y}, if you want the old "
            "regime. With business income it cannot be chosen on the return, "
            "and section 115BAC(6) allows the opt-out only once. Miss this "
            "date and the new regime applies for the year whatever the "
            "comparison says."
        )
    if answers.get("foreign_dividend"):
        guidance.deadlines.append(
            "Form 67 must be on the portal before the return is filed, not "
            "with it."
        )
    guidance.deadlines.append(
        f"Business and capital losses only carry forward if the return is "
        f"filed by {due:%d %B %Y}. A belated return forfeits them — a house "
        "property loss is the one exception and survives either way."
    )
    guidance.deadlines.append(
        "E-verify within 30 days of filing. An unverified return is treated as "
        "never having been filed at all."
    )


# --------------------------------------------------------------------------


def _skeleton(answers: Dict[str, bool], assessment_year: str) -> TaxReturn:
    """A return carrying just enough shape for the selector to decide.

    The amounts are deliberately nominal — nothing here is a figure, only a
    presence. What matters to ``select_form`` is which heads exist.
    """
    yes = lambda key: bool(answers.get(key))     # noqa: E731
    tr = TaxReturn(assessment_year=assessment_year)

    if not yes("resident"):
        tr.taxpayer.residential_status = "NRI"
    if yes("salary"):
        tr.salaries = [SalaryIncome(employer_name="Employer", salary_17_1=D(1))]
        if yes("multiple_employers"):
            tr.salaries.append(
                SalaryIncome(employer_name="Employer 2", salary_17_1=D(1))
            )
    if yes("house_property"):
        tr.house_properties = [HouseProperty(
            property_type="LOP" if yes("house_let_out") else "SOP"
        )]
        if yes("multiple_houses"):
            tr.house_properties.append(HouseProperty(property_type="SOP"))
    if yes("equity_delivery"):
        tr.capital_gains = [CapitalGainItem(category="ltcg_112a")]
    for key, segment in (("fno", "equity_fo"), ("intraday", "equity_intraday")):
        if yes(key):
            tr.trading_segments.append(TradingSegment(segment=segment))
    if yes("foreign_equity"):
        tr.taxpayer.has_foreign_assets = True
    if yes("business"):
        tr.has_business_income = True
    if yes("winnings"):
        tr.other_sources.winnings_115bb = D(1)
    if yes("director_or_unlisted"):
        tr.taxpayer.is_company_director = True
    if yes("carried_losses"):
        from .schemas import BroughtForwardLoss

        tr.brought_forward_losses = [BroughtForwardLoss(assessment_year="")]
    return tr
