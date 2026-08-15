"""Foreign dividends.

Since the Finance Act 2020 removed the dividend distribution tax, a dividend is
ordinary income taxed at slab rates — there is no 12.5% concession and no
₹1.25 lakh shelter. A US dividend is taxed in full in India, on the **gross**
amount before the 25% the US withholds under Article 10 of the India-US treaty.
The withheld tax comes back as a credit under section 90, not as a deduction.

Two things people get wrong:

* Netting off the withholding and declaring only what reached the account. That
  understates income by a quarter and forfeits the credit.
* Treating a reinvested dividend as not-yet-income. It is income on the payment
  date whether it arrives as cash or as more shares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional

from ..money import D
from ..schemas import DividendReceipt, ForeignTaxPayment, TaxReturn
from .forex import SALARY_AND_OTHER_SOURCES, ForexTable, RateUnavailable

# Article 10 of the India-US treaty caps withholding on dividends paid to an
# individual resident of India at 25%. Anything above that is not creditable —
# it has to be reclaimed from the IRS instead.
US_TREATY_DIVIDEND_CAP = D("0.25")


@dataclass
class DividendLine:
    receipt: DividendReceipt
    gross_inr: Decimal
    foreign_tax_inr: Decimal
    rate_used: Optional[Decimal]
    creditable_inr: Decimal
    excess_withholding_inr: Decimal = D(0)
    error: str = ""


@dataclass
class DividendResult:
    lines: List[DividendLine] = field(default_factory=list)
    total_gross_inr: Decimal = D(0)
    total_foreign_tax_inr: Decimal = D(0)
    total_creditable_inr: Decimal = D(0)
    reinvested_count: int = 0
    reinvested_value_inr: Decimal = D(0)
    warnings: List[str] = field(default_factory=list)


def compute_dividends(tr: TaxReturn, forex: ForexTable) -> DividendResult:
    result = DividendResult()

    for receipt in tr.dividends:
        if receipt.gross_amount_fx <= 0:
            continue
        if not receipt.pay_date:
            result.warnings.append(
                f"A dividend from {receipt.symbol or 'a holding'} has no payment "
                "date, so Rule 115 cannot pick an exchange rate for it."
            )
            continue

        try:
            rate = (
                receipt.forex_rate_override
                if receipt.forex_rate_override
                else forex.rate_for(
                    receipt.pay_date, receipt.currency, SALARY_AND_OTHER_SOURCES
                ).value
            )
        except RateUnavailable as exc:
            result.lines.append(DividendLine(
                receipt=receipt, gross_inr=D(0), foreign_tax_inr=D(0),
                rate_used=None, creditable_inr=D(0), error=str(exc),
            ))
            result.warnings.append(str(exc))
            continue

        gross_inr = receipt.gross_amount_fx * rate
        foreign_tax_inr = receipt.foreign_tax_withheld_fx * rate

        # Withholding above the treaty rate is not creditable in India.
        treaty_ceiling = receipt.gross_amount_fx * US_TREATY_DIVIDEND_CAP
        excess_fx = max(D(0), receipt.foreign_tax_withheld_fx - treaty_ceiling)
        creditable_inr = (receipt.foreign_tax_withheld_fx - excess_fx) * rate

        result.lines.append(DividendLine(
            receipt=receipt, gross_inr=gross_inr,
            foreign_tax_inr=foreign_tax_inr, rate_used=rate,
            creditable_inr=creditable_inr,
            excess_withholding_inr=excess_fx * rate,
        ))
        result.total_gross_inr += gross_inr
        result.total_foreign_tax_inr += foreign_tax_inr
        result.total_creditable_inr += creditable_inr

        if receipt.is_reinvested:
            result.reinvested_count += 1
            result.reinvested_value_inr += gross_inr

    _add_warnings(result)
    return result


def _add_warnings(result: DividendResult) -> None:
    if result.reinvested_count:
        result.warnings.append(
            f"₹{result.reinvested_value_inr:,.0f} of dividends across "
            f"{result.reinvested_count} payment(s) were reinvested rather than "
            "paid out. That is still taxable income this year — the reinvestment "
            "buys a new lot, it does not defer the tax."
        )

    excess = sum((line.excess_withholding_inr for line in result.lines), D(0))
    if excess > 0:
        result.warnings.append(
            f"₹{excess:,.0f} was withheld abroad above the 25% the India-US "
            "treaty permits, and is not creditable here. It usually means a "
            "Form W-8BEN is missing or has lapsed with your broker — file one "
            "and reclaim the excess from the IRS."
        )

    if result.total_gross_inr > 0 and result.total_foreign_tax_inr == 0:
        result.warnings.append(
            "Foreign dividends are recorded with no tax withheld. That is "
            "unusual for a US payer — check the 1099-DIV, because unclaimed "
            "withholding is a credit you are entitled to."
        )


def to_foreign_tax_payments(result: DividendResult) -> List[ForeignTaxPayment]:
    """Roll the dividend lines up into Form 67 / Schedule TR entries."""
    by_country: dict = {}
    for line in result.lines:
        if line.creditable_inr <= 0 and line.gross_inr <= 0:
            continue
        key = line.receipt.country_code
        entry = by_country.setdefault(key, ForeignTaxPayment(
            country_code=key,
            income_head="other_sources",
            nature_of_income="Dividend",
            currency=line.receipt.currency,
            treaty_article="10",
            relief_section="90",
        ))
        entry.income_fx += line.receipt.gross_amount_fx
        entry.income_inr += line.gross_inr
        entry.tax_paid_fx += line.receipt.foreign_tax_withheld_fx
        entry.tax_paid_inr += line.creditable_inr
    return list(by_country.values())
