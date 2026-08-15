"""Employee Stock Purchase Plans.

An ESPP takes payroll deductions over an offering period and buys shares at a
discount on the purchase date. Two consequences, and the second is where money
quietly goes missing.

**The discount is salary.** Section 17(2)(vi) taxes the gap between the fair
market value on the date of allotment and what you actually paid, at slab rates,
with TDS under section 192 in the month of purchase.

**The cost basis on sale is the fair market value, not the price you paid.**
Section 49(2AA) fixes the cost at the amount already brought to tax. The broker's
own statement, and a US 1099-B, both show the discounted purchase price as the
basis — because that is correct for US tax and wrong for Indian tax. Copying it
across taxes the discount twice: once as salary in the year of purchase, and
again as capital gain on sale.

The size of the error is not small. A plan buying at 85% of the lower of the
offering-start and purchase-date prices — the standard US lookback — routinely
produces an effective discount of thirty per cent or more when the share price
has risen over the offering period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional

from ..money import D, non_negative
from ..schemas import ESPPPurchase, TaxReturn
from .forex import SALARY_AND_OTHER_SOURCES, ForexTable, Rate, RateUnavailable
from .rsu import Lot


@dataclass
class ESPPResult:
    purchase: ESPPPurchase
    perquisite_inr: Decimal
    cost_basis_inr: Decimal
    amount_paid_inr: Decimal
    rate: Optional[Rate]
    error: str = ""

    @property
    def effective_discount(self) -> Decimal:
        return self.purchase.discount_fraction


@dataclass
class ESPPComputation:
    results: List[ESPPResult] = field(default_factory=list)
    lots: List[Lot] = field(default_factory=list)
    total_perquisite_inr: Decimal = D(0)
    total_paid_inr: Decimal = D(0)
    warnings: List[str] = field(default_factory=list)
    missing_rate_months: List[str] = field(default_factory=list)


def compute_espp(tr: TaxReturn, forex: ForexTable) -> ESPPComputation:
    out = ESPPComputation()

    for purchase in tr.espp_purchases:
        if not purchase.purchase_date or purchase.shares_purchased <= 0:
            continue

        try:
            if purchase.forex_rate_override:
                rate = Rate(purchase.currency, "override",
                            purchase.forex_rate_override, provisional=False,
                            note="Entered by you")
            else:
                # The perquisite is salary, so Rule 115 attaches to the month
                # the income fell due — the month of allotment.
                rate = forex.rate_for(
                    purchase.purchase_date, purchase.currency,
                    SALARY_AND_OTHER_SOURCES,
                )
        except RateUnavailable as exc:
            out.results.append(ESPPResult(
                purchase=purchase, perquisite_inr=D(0), cost_basis_inr=D(0),
                amount_paid_inr=D(0), rate=None, error=str(exc),
            ))
            out.missing_rate_months.append(exc.month)
            continue

        perquisite = purchase.total_discount_fx * rate.value
        # Section 49(2AA). Deliberately the fair market value, not
        # ``amount_paid_fx`` — see the module docstring.
        cost_basis = purchase.total_cost_basis_fx * rate.value

        out.results.append(ESPPResult(
            purchase=purchase,
            perquisite_inr=perquisite,
            cost_basis_inr=cost_basis,
            amount_paid_inr=purchase.amount_paid_fx * rate.value,
            rate=rate,
        ))
        out.total_perquisite_inr += perquisite
        out.total_paid_inr += purchase.amount_paid_fx * rate.value

        out.lots.append(Lot(
            symbol=purchase.symbol,
            acquired=purchase.purchase_date,
            shares=purchase.shares_purchased,
            cost_inr=cost_basis,
            cost_fx=purchase.total_cost_basis_fx,
            currency=purchase.currency,
            origin="espp",
            country_code=purchase.country_code,
            source_document=purchase.source_document,
        ))

    _add_warnings(tr, out)
    return out


def perquisite_to_add_to_salary(result: ESPPComputation) -> Decimal:
    """Only the purchases the employer did not already put through payroll."""
    return sum(
        (item.perquisite_inr for item in result.results
         if not item.purchase.included_in_form16),
        D(0),
    )


def _add_warnings(tr: TaxReturn, out: ESPPComputation) -> None:
    if not out.results:
        return

    if out.total_perquisite_inr > 0:
        out.warnings.append(
            f"₹{out.total_perquisite_inr:,.0f} of ESPP discount is salary under "
            "section 17(2)(vi), taxed at slab rates in the year of purchase. "
            "Your own contributions are not deductible against it — they came "
            "out of salary that was already taxed."
        )

    # The basis trap, stated in rupees so it is not abstract.
    basis_gap = sum(
        (item.cost_basis_inr - item.amount_paid_inr for item in out.results),
        D(0),
    )
    if basis_gap > 0:
        out.warnings.append(
            f"When you sell these shares the cost of acquisition is "
            f"₹{sum((i.cost_basis_inr for i in out.results), D(0)):,.0f} — the "
            "fair market value already taxed as a perquisite, per section "
            f"49(2AA) — not the ₹{sum((i.amount_paid_inr for i in out.results), D(0)):,.0f} "
            "you actually paid. Your broker's statement and any US 1099-B will "
            f"show the lower figure, which would overstate the gain by "
            f"₹{basis_gap:,.0f} and tax the discount twice. The capital-gains "
            "schedule here uses the correct basis."
        )

    lookback = [
        item for item in out.results
        if item.purchase.offering_price_fx > 0
        and item.purchase.offering_price_fx < item.purchase.fmv_per_share_fx
    ]
    if lookback:
        best = max(lookback, key=lambda i: i.effective_discount)
        out.warnings.append(
            f"The lookback is working in your favour: on {best.purchase.symbol} "
            f"the price rose from {best.purchase.currency} "
            f"{best.purchase.offering_price_fx} at the start of the offering to "
            f"{best.purchase.fmv_per_share_fx} on the purchase date, making the "
            f"real discount {best.effective_discount * 100:.1f}% rather than the "
            "headline rate. The whole of that is perquisite."
        )

    unpriced = [item for item in out.results if item.purchase.fmv_per_share_fx <= 0]
    if unpriced:
        out.warnings.append(
            f"{len(unpriced)} ESPP purchase(s) have no fair market value on "
            "file, so no perquisite can be computed and the cost basis would be "
            "nil. The figure is on the purchase confirmation from your broker."
        )

    underwater = [
        item for item in out.results
        if item.purchase.fmv_per_share_fx > 0
        and item.purchase.price_paid_per_share_fx > item.purchase.fmv_per_share_fx
    ]
    if underwater:
        out.warnings.append(
            "One or more purchases were made above the fair market value on the "
            "day. There is no perquisite in that case — a negative discount is "
            "not a deduction — but the cost basis is still the fair market "
            "value, which is below what you paid."
        )

    # Rule 3(8) strictly wants a merchant-banker valuation for shares not listed
    # on a recognised Indian exchange. Saying so is honest; refusing to compute
    # would be useless.
    out.warnings.append(
        "Fair market value for a share not listed on a recognised Indian stock "
        "exchange is, strictly, a Category I merchant banker's valuation under "
        "Rule 3(8) — not the closing price on the NYSE or NASDAQ. In practice "
        "employers use the market price and report it in Form 16. Use whatever "
        "figure your Form 16 uses, so the return and the TDS agree."
    )
