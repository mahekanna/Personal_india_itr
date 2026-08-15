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


@dataclass
class Guidance:
    form: str
    form_reasons: List[str] = field(default_factory=list)
    form_supported: bool = True
    form_note: str = ""
    documents: Dict[str, List[DocumentNeed]] = field(default_factory=dict)
    deadlines: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def document_count(self) -> int:
        return sum(len(items) for items in self.documents.values())


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

    # ---- Everyone ---------------------------------------------------------
    _need("Always needed", guidance,
        DocumentNeed(
            "PAN, and an Aadhaar linked to it",
            "Your own records",
            "An unlinked PAN makes the return invalid, not merely late.",
        ),
        DocumentNeed(
            "Bank account number and IFSC for the refund",
            "Your passbook or net banking",
            "The account has to be pre-validated on the portal before a refund "
            "can be issued to it.",
        ),
        DocumentNeed(
            "Form 26AS",
            "Portal → e-File → Income Tax Returns → View Form 26AS (TRACES)",
            "The tax credit statement. Anything you claim that is not here "
            "will be disallowed at processing.",
        ),
        DocumentNeed(
            "Annual Information Statement (AIS) and TIS",
            "Portal → Services → AIS. Password is your PAN in lower case "
            "followed by your date of birth as DDMMYYYY",
            "Income the AIS shows and the return omits is the single "
            "commonest reason a notice arrives.",
        ),
    )

    # ---- Salary -----------------------------------------------------------
    if yes("salary"):
        _need("Salary", guidance,
            DocumentNeed(
                "Form 16, Part A and Part B"
                + (" — from every employer" if yes("multiple_employers") else ""),
                "Your employer, usually by mid-June",
            ),
        )
        if yes("multiple_employers"):
            guidance.warnings.append(
                "With two employers, each one almost certainly gave you the "
                "full standard deduction and the full basic exemption again. "
                "The combined figures rarely match either Form 16, and the "
                "shortfall is payable with interest."
            )
        if yes("foreign_equity"):
            _need("Salary", guidance, DocumentNeed(
                "Form 12BA",
                "Your employer, issued with Form 16",
                "The perquisite statement. It is what values an RSU vest or an "
                "ESPP discount, and what tells you whether the perquisite is "
                "already inside your Form 16 gross salary.",
            ))
        if yes("old_regime"):
            _need("Salary", guidance, DocumentNeed(
                "Rent receipts and your landlord's PAN",
                "Your landlord",
                "Only if you claim HRA, and the PAN is required once the rent "
                "passes ₹1,00,000 in the year.",
                essential=False,
            ))

    # ---- House property ---------------------------------------------------
    if yes("house_property"):
        _need("House property", guidance, DocumentNeed(
            "Home loan interest certificate",
            "Your lender, for the financial year",
            "It splits interest from principal — interest goes under section "
            "24(b), principal under 80C.",
            essential=False,
        ))
        if yes("house_let_out"):
            _need("House property", guidance,
                DocumentNeed("Rent agreement and a record of rent received",
                             "Your own records"),
                DocumentNeed("Municipal tax receipts",
                             "Your local authority",
                             "Deductible only in the year actually paid.",
                             essential=False),
            )

    # ---- Delivery equity and mutual funds ---------------------------------
    if yes("equity_delivery"):
        _need("Investments", guidance,
            DocumentNeed(
                "Capital gains statement, trade-wise",
                "Your broker's annual tax report",
                "Trade-wise rather than summary — the holding period on each "
                "lot decides the rate.",
            ),
            DocumentNeed(
                "Consolidated capital gains statement for mutual funds",
                "CAMS or KFintech, free by email",
                "Covers every fund house at once.",
                essential=False,
            ),
            DocumentNeed(
                "Highest quoted price on 31 January 2018",
                "The exchange, for anything bought before that date",
                "Section 55(2)(ac) grandfathers the gain up to that date. "
                "Without the figure the cost is understated and you pay tax on "
                "a gain that is not taxable.",
                essential=False,
            ),
        )

    # ---- Trading ----------------------------------------------------------
    if yes("fno") or yes("intraday"):
        _need("Trading", guidance,
            DocumentNeed(
                "Annual tax P&L, segment-wise",
                "Your broker — ICICI Direct, Zerodha Console and the rest all "
                "publish one after the year ends",
                "It must separate delivery, intraday and F&O. They are three "
                "different heads of income and merging them is unlawful in "
                "either direction.",
            ),
            DocumentNeed(
                "Annual charges and brokerage statement",
                "Your broker",
                "Brokerage, exchange and clearing charges, SEBI fees, STT or "
                "CTT, GST, stamp duty and depository charges are all "
                "deductible against business income. Not claiming them is the "
                "compensation for the concessional rate you have already lost.",
            ),
            DocumentNeed(
                "Bank statement for the trading account",
                "Your bank",
                "Needed to show that receipts and payments were banked — that "
                "is what keeps the section 44AB audit threshold at ₹10 crore "
                "rather than ₹1 crore.",
                essential=False,
            ),
        )
        guidance.warnings.append(
            "F&O and intraday make this a business return. That means ITR-3, "
            "and it means the old regime — if it turns out cheaper — has to be "
            "chosen on Form 10-IEA before the due date rather than on the "
            "return itself."
        )

    if yes("business"):
        _need("Business", guidance,
            DocumentNeed("Books of account, or receipts and expense records",
                         "Your own records"),
            DocumentNeed("Bank statements for the business account",
                         "Your bank"),
            DocumentNeed(
                "GST returns, if registered",
                "The GST portal",
                "Turnover has to agree with what was declared there.",
                essential=False,
            ),
        )

    # ---- Foreign ----------------------------------------------------------
    if yes("foreign_equity"):
        _need("Foreign holdings", guidance,
            DocumentNeed(
                "Vesting or release statements for every tranche",
                "Your stock plan administrator — E*TRADE, Fidelity, Schwab, "
                "Morgan Stanley",
                "Each vest needs the fair market value on its own date. "
                "Sixteen tranches are sixteen separate salary events at "
                "sixteen different exchange rates.",
            ),
            DocumentNeed(
                "ESPP purchase confirmations",
                "Your stock plan administrator",
                "The fair market value on the purchase date, the price you "
                "paid, and the price at the start of the offering. The "
                "lookback usually makes the real discount far more than the "
                "headline 15%.",
                essential=False,
            ),
            DocumentNeed(
                "Form 1099-B or realised gain-and-loss report",
                "Your broker",
                "Its cost basis is correct for US tax and wrong for Indian "
                "tax — section 49(2AA) fixes the cost at the value already "
                "taxed as a perquisite. Copying the US figure taxes the "
                "discount twice.",
                essential=False,
            ),
            DocumentNeed(
                "Year-end statement showing the position on 31 December and "
                "the highest value during the calendar year",
                "Your broker",
                "Schedule FA runs on the calendar year, not the financial "
                "year. For AY " + ay.ay + " that is calendar "
                + str(ay.fy_start.year) + ".",
            ),
            DocumentNeed(
                "The company's and the broker's registered address and ZIP code",
                "The company's investor relations page; the broker's statement",
                "Schedule FA asks for both and the portal will not accept the "
                "row without them.",
            ),
            DocumentNeed(
                "SBI TT buying rate for each relevant month end",
                "sbi.co.in, or your bank",
                "Rule 115 uses the rate on the last day of the month *before* "
                "the income arose — never the rate on the day itself.",
            ),
        )
        guidance.warnings.append(
            "Schedule FA has no threshold and no exemption. A resident and "
            "ordinarily resident who held any foreign asset at any point in "
            "the calendar year must report it, whatever it was worth and "
            "whether or not it paid anything. The penalty for omitting one is "
            "a flat ₹10 lakh under the Black Money Act, assessed on the "
            "non-disclosure rather than on any tax — so it applies in full "
            "even when the tax was paid correctly."
        )

    if yes("foreign_dividend"):
        _need("Foreign holdings", guidance,
            DocumentNeed(
                "Form 1099-DIV or the dividend activity report",
                "Your broker",
                "Declare the gross amount, before the 25% the US withholds. "
                "Netting it off understates income by a quarter and forfeits "
                "the credit.",
            ),
            DocumentNeed(
                "Form 67",
                "File it on the portal BEFORE you file the return",
                "The foreign tax credit is liable to be denied outright if "
                "Form 67 is not already on record. This is the single "
                "easiest way to lose money on this return.",
            ),
        )

    # ---- Other income and deductions --------------------------------------
    if yes("other_income"):
        _need("Other income", guidance,
            DocumentNeed("Interest certificates from every bank",
                         "Net banking, usually under 'Tax'"),
            DocumentNeed("Dividend statements",
                         "Your demat account or the registrar",
                         "TDS at 10% applies under section 194 once one payer "
                         "crosses ₹10,000 in the year.",
                         essential=False),
        )

    if yes("old_regime"):
        _need("Deductions", guidance,
            DocumentNeed("80C proofs — PF, PPF, ELSS, life insurance, tuition "
                         "fees, home loan principal", "Your own records",
                         essential=False),
            DocumentNeed("80D health insurance premium receipts",
                         "Your insurer", essential=False),
            DocumentNeed(
                "80G donation receipts",
                "The institution",
                "The receipt must carry the donee's PAN and its 80G "
                "registration number, or the deduction fails.",
                essential=False,
            ),
            DocumentNeed("NPS statement, for 80CCD(1B)",
                         "Your CRA — Protean or KFintech", essential=False),
        )

    _need("Taxes already paid", guidance, DocumentNeed(
        "Advance tax and self-assessment challans",
        "Portal → e-Pay Tax → Payment History",
        "The BSR code, challan serial number and date all go on the return, "
        "and the credit will not be matched without all three.",
        essential=False,
    ))

    _add_deadlines(guidance, answers, ay)
    return guidance


def _add_deadlines(guidance: Guidance, answers: Dict[str, bool], ay) -> None:
    trading = answers.get("fno") or answers.get("intraday")
    business = trading or answers.get("business")

    guidance.deadlines.append(
        f"Return due {ay.due_date_non_audit:%d %B %Y} — or "
        f"{ay.due_date_audit:%d %B %Y} if a tax audit under section 44AB "
        "applies."
    )
    if business:
        guidance.deadlines.append(
            f"Form 10-IEA, before {ay.due_date_non_audit:%d %B %Y}, if you "
            "want the old regime. With business income it cannot be chosen on "
            "the return, and section 115BAC(6) allows the opt-out only once."
        )
    if answers.get("foreign_dividend"):
        guidance.deadlines.append(
            "Form 67 must be on the portal before the return is filed, not "
            "with it."
        )
    guidance.deadlines.append(
        f"Losses only carry forward if the return is filed by "
        f"{ay.due_date_non_audit:%d %B %Y}. A belated return forfeits them."
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
