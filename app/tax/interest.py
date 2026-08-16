"""Interest under sections 234A, 234B and 234C, and the section 234F fee.

Interest runs at 1% for every month *or part of a month*, so a single day's
delay costs a full month. All three sections compute on "assessed tax", which
is the tax and surcharge and cess less TDS, TCS and relief — but not less
advance tax.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple

from ..money import D, non_negative, rupees
from .advance_tax import DeferrableItem, section_234c
from .rules import AssessmentYear, due_date_for


@dataclass
class InterestResult:
    section_234a: Decimal = D(0)
    section_234b: Decimal = D(0)
    section_234c: Decimal = D(0)
    section_234f: Decimal = D(0)
    notes: List[str] = field(default_factory=list)

    @property
    def total_interest(self) -> Decimal:
        return self.section_234a + self.section_234b + self.section_234c

    @property
    def total(self) -> Decimal:
        return self.total_interest + self.section_234f


def _months_between(start: date, end: date) -> int:
    """Whole months, counting any part of a month as a full one."""
    if end <= start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day > start.day:
        months += 1
    return max(months, 1)


def _round_down_to_hundred(amount: Decimal) -> Decimal:
    """Rule 119A: the amount interest is computed on is rounded to ₹100."""
    if amount <= 0:
        return D(0)
    return (amount // D("100")) * D("100")


def compute_interest_and_fee(
    ay: AssessmentYear,
    *,
    total_tax_liability: Decimal,   # tax + surcharge + cess, after relief
    tds_tcs: Decimal,
    advance_tax_instalments: List[Tuple[date, Decimal]],
    self_assessment_paid: Decimal,
    total_income: Decimal,
    filing_date: Optional[date],
    is_audit_case: bool = False,
    has_business: bool = False,
    has_only_pension_or_no_business: bool = False,
    is_senior_citizen: bool = False,
    deferrable: Optional[List[DeferrableItem]] = None,
) -> InterestResult:
    result = InterestResult()
    due_date = due_date_for(
        ay, has_business=has_business, audit=is_audit_case
    )
    filing = filing_date or date.today()

    assessed_tax = non_negative(total_tax_liability - tds_tcs)
    advance_paid_total = sum((amount for _, amount in advance_tax_instalments), D(0))

    # ---- Section 234F: fee for a belated return ---------------------------
    if filing > due_date:
        result.section_234f = (
            ay.late_fee_small
            if total_income <= ay.late_fee_income_threshold
            else ay.late_fee_large
        )
        result.notes.append(
            f"Return filed on {filing:%d %b %Y}, after the due date of "
            f"{due_date:%d %b %Y} — section 234F fee applies."
        )
        if total_income <= D("250000"):
            # No fee where the return was not required to be filed at all.
            result.section_234f = D(0)

    # A senior citizen with no business income is exempt from advance tax
    # altogether — section 207(2).
    advance_tax_exempt = is_senior_citizen and has_only_pension_or_no_business
    below_floor = assessed_tax < ay.advance_tax_floor

    # ---- Section 234A: delay in filing ------------------------------------
    if filing > due_date:
        unpaid = non_negative(
            total_tax_liability - tds_tcs - advance_paid_total - self_assessment_paid
        )
        months = _months_between(due_date, filing)
        result.section_234a = rupees(
            _round_down_to_hundred(unpaid) * ay.interest_rate_per_month * months
        )
        if result.section_234a:
            result.notes.append(
                f"Section 234A: {months} month(s) at 1% on the unpaid tax."
            )

    if advance_tax_exempt or below_floor:
        if advance_tax_exempt:
            result.notes.append(
                "Advance tax is not payable — section 207(2) exempts a resident "
                "senior citizen with no business income."
            )
        return result

    # ---- Section 234B: shortfall in advance tax ---------------------------
    if advance_paid_total < assessed_tax * D("0.90"):
        shortfall = non_negative(assessed_tax - advance_paid_total)
        # Interest runs from 1 April of the assessment year to the date of
        # payment (approximated by the filing date).
        start = date(ay.fy_end.year, 4, 1)
        months = _months_between(start, filing)
        result.section_234b = rupees(
            _round_down_to_hundred(shortfall) * ay.interest_rate_per_month * months
        )
        result.notes.append(
            f"Section 234B: advance tax of ₹{advance_paid_total:,.0f} is less "
            f"than 90% of the assessed tax of ₹{assessed_tax:,.0f}; "
            f"{months} month(s) of interest charged."
        )

    # ---- Section 234C: deferment of instalments ---------------------------
    # The first proviso to section 234C(1) excuses the earlier instalments
    # where the shortfall is down to capital gains, dividends, winnings or
    # first-year business income. Without it, a December share sale would be
    # charged interest for failing to pay tax on it in June.
    result.section_234c, notes = section_234c(
        ay, assessed_tax, advance_tax_instalments, deferrable or []
    )
    result.notes.extend(notes)
    return result
