"""Foreign tax credit under section 90, computed the way Rule 128 requires.

The credit is **not** simply the tax paid abroad. Rule 128(2) allows the lower
of the foreign tax and the Indian tax attributable to that same income, worked
out separately for each source in each country. Where India taxes the income at
a lower effective rate than the US withheld, the difference is lost — it does
not become a refund and it does not carry forward.

Rule 128(9) requires **Form 67 on the portal before the return is filed**. The
tribunals have repeatedly held the requirement to be procedural rather than
fatal — Brinda RamaKrishna and the benches that followed it — but CPC still
denies the credit first and makes you litigate. Filing Form 67 first is a
five-minute job that avoids all of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from ..money import D, non_negative
from ..schemas import ForeignTaxPayment, TaxReturn


@dataclass
class CreditLine:
    country_code: str
    nature_of_income: str
    income_inr: Decimal
    foreign_tax_inr: Decimal
    indian_tax_on_income: Decimal
    credit_allowed: Decimal
    forfeited: Decimal
    head: str = "other_sources"


@dataclass
class FTCResult:
    lines: List[CreditLine] = field(default_factory=list)
    total_credit: Decimal = D(0)
    total_forfeited: Decimal = D(0)
    doubly_taxed_income: Decimal = D(0)
    warnings: List[str] = field(default_factory=list)


def compute_ftc(
    tr: TaxReturn,
    payments: List[ForeignTaxPayment],
    *,
    total_income: Decimal,
    tax_before_credit: Decimal,
    special_rate_for: Optional[dict] = None,
) -> FTCResult:
    """Work out the section 90 relief.

    ``tax_before_credit`` is the Indian liability including surcharge and cess
    but before any foreign credit, which is the figure Rule 128 measures the
    attributable tax against.
    """
    result = FTCResult()
    if not payments or total_income <= 0 or tax_before_credit <= 0:
        _warn_about_form67(tr, result, bool(payments))
        return result

    # The average rate of Indian tax — the standard measure of "tax
    # attributable to" a slice of income where that slice is taxed at slab
    # rates along with everything else.
    average_rate = tax_before_credit / total_income

    for payment in payments:
        if payment.income_inr <= 0:
            continue
        rate = average_rate
        if special_rate_for and payment.income_head == "capital_gains":
            # Gains taxed at their own rate are measured at that rate, not at
            # the average, because that is the tax actually attributable.
            rate = special_rate_for.get("capital_gains", average_rate)

        indian_tax = payment.income_inr * rate
        credit = min(payment.tax_paid_inr, indian_tax)
        forfeited = non_negative(payment.tax_paid_inr - credit)

        result.lines.append(CreditLine(
            country_code=payment.country_code,
            nature_of_income=payment.nature_of_income,
            income_inr=payment.income_inr,
            foreign_tax_inr=payment.tax_paid_inr,
            indian_tax_on_income=indian_tax,
            credit_allowed=credit,
            forfeited=forfeited,
            head=payment.income_head,
        ))
        result.total_credit += credit
        result.total_forfeited += forfeited
        result.doubly_taxed_income += payment.income_inr

    if result.total_forfeited > 0:
        result.warnings.append(
            f"₹{result.total_forfeited:,.0f} of foreign tax cannot be credited, "
            "because India taxes that income at a lower rate than was withheld "
            "abroad. Rule 128 caps the credit at the Indian tax on the same "
            "income; the excess is not refundable and does not carry forward."
        )

    _warn_about_form67(tr, result, True)
    return result


def _warn_about_form67(tr: TaxReturn, result: FTCResult, has_credit: bool) -> None:
    if not has_credit:
        return
    if not tr.foreign_settings.form67_filed:
        result.warnings.append(
            "Form 67 has not been marked as filed. Rule 128(9) wants it on the "
            "portal before the return, and CPC routinely denies the credit "
            "when it is missing. File it first — e-File → Income Tax Forms → "
            "File Income Tax Forms → Form 67 — then come back and tick the box."
        )
