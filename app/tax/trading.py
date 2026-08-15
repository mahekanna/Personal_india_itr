"""Trading income: what is business, what is speculative, and how much turnover.

Three segments of the same demat account are three different heads of income,
and getting the split wrong is the single most expensive mistake a trader makes
on an Indian return.

**Delivery equity** is capital gains — sections 111A and 112A, with securities
transaction tax paid and the concessional rates that go with it. Nothing in this
module touches it.

**Intraday equity** is a *speculative* business under section 43(5): a contract
settled otherwise than by actual delivery. Its loss is ring-fenced by section
73 — it can meet speculative income and nothing else, and it carries forward
only four years, not eight.

**Derivatives are not speculative**, however much they feel like it. Clause (d)
of the proviso to section 43(5) takes an eligible transaction in derivatives
carried out on a recognised stock exchange out of the definition, and clause (e)
does the same for commodity derivatives on a recognised association chargeable
to commodity transaction tax — with a further proviso covering agricultural
commodity derivatives, which bear no CTT. So equity, index, currency and
commodity F&O are all ordinary non-speculative business income, taxed at slab
rates, with the loss available against every head except salary.

That last point is the one worth money: section 71(2A) bars a business loss from
being set off against salary, but not against capital gains or other sources. A
year of F&O losses can be set against the gain on a US share sale. It cannot be
set against the salary the RSU vested into.

**Turnover** is its own trap. The ICAI Guidance Note on Tax Audit was revised in
2023, and the eighth edition dropped the full sale consideration of options from
the computation. Turnover is now the absolute value of the profit or loss on
each trade, summed — for futures and options alike — with the premium received
on sale added only where it has not already been taken into the net profit.
Most published guidance still shows the old method, which inflates the figure
enormously and pushes people into an audit they do not need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from ..money import D, non_negative

# Section 44AB(a). The ordinary threshold is ₹1 crore, but the proviso raises it
# to ₹10 crore where neither cash receipts nor cash payments exceed 5% of the
# total — which is always true of a broking account, since every rupee moves by
# bank transfer.
AUDIT_LIMIT_DIGITAL = D("100000000")      # ₹10 crore
AUDIT_LIMIT_CASH = D("10000000")          # ₹1 crore
CASH_FRACTION_LIMIT = D("0.05")

# Which segment is speculative and which is not.
SPECULATIVE_SEGMENTS = ("equity_intraday",)
NON_SPECULATIVE_SEGMENTS = (
    "equity_fo", "currency_fo", "commodity_fo", "other_business"
)

SEGMENT_LABELS = {
    "equity_intraday": "Intraday equity (speculative — s.43(5))",
    "equity_fo": "Equity and index F&O (s.43(5)(d))",
    "currency_fo": "Currency F&O (s.43(5)(d))",
    "commodity_fo": "Commodity F&O (s.43(5)(e))",
    "other_business": "Other business or profession",
}


@dataclass
class SegmentResult:
    """One trading segment, with its turnover and its own audit exposure."""

    segment: str
    label: str
    is_speculative: bool
    gross_profit: Decimal = D(0)          # net of nothing; the raw P&L
    turnover: Decimal = D(0)
    expenses: Decimal = D(0)
    net_income: Decimal = D(0)            # gross_profit - expenses
    notes: List[str] = field(default_factory=list)


@dataclass
class TradingResult:
    segments: List[SegmentResult] = field(default_factory=list)
    speculative_income: Decimal = D(0)
    non_speculative_income: Decimal = D(0)
    total_turnover: Decimal = D(0)
    audit_required: bool = False
    audit_reason: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def has_anything(self) -> bool:
        return bool(self.segments)

    def by_segment(self, segment: str) -> Optional[SegmentResult]:
        for result in self.segments:
            if result.segment == segment:
                return result
        return None


# --------------------------------------------------------------------------
# Turnover
# --------------------------------------------------------------------------


def turnover_from_trades(
    profits: List[Decimal], option_sell_premium: Decimal = D(0)
) -> Decimal:
    """ICAI Guidance Note on Tax Audit, Revised 2023, para 5.10.

    The absolute value of every trade's result, favourable and unfavourable
    alike, added together. A ₹10,000 profit and a ₹10,000 loss make ₹20,000 of
    turnover, not nil.

    ``option_sell_premium`` is added only where the premium received on writing
    an option has *not* already been taken into the trade-by-trade net profit.
    A broker's tax P&L nets it, so the usual answer is to leave this at zero —
    adding it twice is how a modest book turns into a spurious audit case.
    """
    total = sum((abs(D(profit)) for profit in profits), D(0))
    return total + non_negative(option_sell_premium)


def audit_required(
    turnover: Decimal,
    *,
    cash_receipts_fraction: Decimal = D(0),
    cash_payments_fraction: Decimal = D(0),
) -> tuple[bool, str]:
    """Section 44AB(a) and its proviso.

    Returns whether an audit is required and why. The threshold is ₹10 crore
    unless more than 5% of receipts or payments moved in cash, in which case it
    drops to ₹1 crore.
    """
    mostly_banked = (
        cash_receipts_fraction <= CASH_FRACTION_LIMIT
        and cash_payments_fraction <= CASH_FRACTION_LIMIT
    )
    limit = AUDIT_LIMIT_DIGITAL if mostly_banked else AUDIT_LIMIT_CASH
    if turnover <= limit:
        return False, ""
    return True, (
        f"Turnover of ₹{turnover:,.0f} exceeds the ₹{limit:,.0f} threshold in "
        "section 44AB(a)"
        + ("" if mostly_banked else ", which applies because more than 5% of "
           "receipts or payments were in cash")
        + ". The accounts must be audited and the return filed by 31 October."
    )


# --------------------------------------------------------------------------
# The whole trading book
# --------------------------------------------------------------------------


def compute_trading(tr) -> TradingResult:
    """Split the trading book into its statutory buckets.

    Reads ``tr.trading_segments``. Each segment carries its own profit,
    turnover and expenses, because they are separate businesses for the purpose
    of section 73 even though they sit in one demat account.
    """
    result = TradingResult()

    for entry in tr.trading_segments:
        if not (entry.gross_profit or entry.turnover or entry.expenses):
            continue
        speculative = entry.segment in SPECULATIVE_SEGMENTS
        expenses = entry.total_expenses
        net = entry.gross_profit - expenses

        segment = SegmentResult(
            segment=entry.segment,
            label=SEGMENT_LABELS.get(entry.segment, entry.segment),
            is_speculative=speculative,
            gross_profit=entry.gross_profit,
            turnover=entry.turnover,
            expenses=expenses,
            net_income=net,
        )
        if entry.turnover <= 0 and entry.gross_profit:
            segment.notes.append(
                "No turnover figure is on file for this segment, so it counts "
                "as nil towards the section 44AB test. Turnover is not the "
                "value of your trades — it is the absolute profit and loss on "
                "each one, added up."
            )
        result.segments.append(segment)

        if speculative:
            result.speculative_income += net
        else:
            result.non_speculative_income += net
        result.total_turnover += entry.turnover

    if not result.segments:
        return result

    required, reason = audit_required(
        result.total_turnover,
        cash_receipts_fraction=tr.business.cash_receipts_fraction,
        cash_payments_fraction=tr.business.cash_payments_fraction,
    )
    result.audit_required = required
    result.audit_reason = reason

    _add_warnings(tr, result)
    return result


def _add_warnings(tr, result: TradingResult) -> None:
    speculative = [s for s in result.segments if s.is_speculative]
    non_speculative = [s for s in result.segments if not s.is_speculative]

    if non_speculative:
        result.warnings.append(
            "Futures and options are **not** speculative income. Clause (d) of "
            "the proviso to section 43(5) takes derivatives traded on a "
            "recognised stock exchange out of that definition, and clause (e) "
            "does the same for commodity derivatives. They are ordinary "
            "business income at slab rates, which means ITR-3 — not ITR-2, and "
            "certainly not capital gains."
        )

    if speculative and non_speculative:
        result.warnings.append(
            "Intraday equity is kept apart from the F&O segments deliberately. "
            "Section 73 ring-fences a speculative loss: it can be set off only "
            "against speculative income, and it carries forward four years "
            "rather than eight. Merging the two — which most broker summaries "
            "invite you to do — either wastes the loss or claims it unlawfully."
        )

    if result.audit_required:
        result.warnings.append(result.audit_reason)
    elif result.total_turnover > AUDIT_LIMIT_CASH:
        result.warnings.append(
            f"Turnover of ₹{result.total_turnover:,.0f} is above ₹1 crore but "
            "below the ₹10 crore threshold that applies when receipts and "
            "payments are banked rather than in cash — so no audit is required "
            "under section 44AB(a). Keep the bank evidence: the ₹1 crore figure "
            "is the one most commentary still quotes."
        )

    loss_segments = [s for s in non_speculative if s.net_income < 0]
    if loss_segments and tr.salaries:
        result.warnings.append(
            "A business loss may be set off against capital gains and other "
            "sources, but section 71(2A) bars it against salary. With salary "
            "income on this return, the loss will be used against your other "
            "heads first and the balance carried forward eight years under "
            "section 72 — which needs the return filed by the due date."
        )

    if any(s.turnover > 0 and s.expenses <= 0 for s in result.segments):
        result.warnings.append(
            "One or more segments show no expenses. When trading is business "
            "income, brokerage, exchange and clearing charges, SEBI fees, GST, "
            "stamp duty, depository charges and even the securities transaction "
            "tax are all deductible against it — as are the demonstrable costs "
            "of carrying it on. That is the compensation for losing the "
            "concessional capital-gains rate; not claiming them is money left "
            "on the table."
        )
