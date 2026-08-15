"""RSUs, ESPP and dividend reinvestment.

Three separate taxable moments, and people routinely collapse them into one:

1. **Vesting** is salary. Section 17(2)(vi) taxes the fair market value on the
   vesting date as a perquisite, at slab rates, with TDS under section 192.
2. **Dividends** are other-sources income in the year declared — reinvested or
   not. Reinvestment does not defer anything.
3. **Sale** is capital gains, with the cost basis fixed by section 49(2AA) at
   the value already taxed at vesting, or at the reinvested amount for a lot
   bought by a dividend.

The trap in the third one is the holding period. Sections 111A and 112A need
securities transaction tax, which is never paid on a NYSE or NASDAQ trade, so a
US share is not "listed" for this purpose however obviously listed it looks. It
turns long term at **24 months**, not 12. Selling at 18 months means slab rates,
not 12.5% — on a large tranche that difference runs to lakhs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..money import D, non_negative
from ..schemas import CapitalGainItem, DividendReceipt, RSUVest, TaxReturn
from .forex import CAPITAL_GAINS, SALARY_AND_OTHER_SOURCES, ForexTable, Rate, RateUnavailable

LONG_TERM_MONTHS_FOREIGN = 24


@dataclass
class VestResult:
    vest: RSUVest
    perquisite_inr: Decimal
    rate: Optional[Rate]
    shares_retained: Decimal
    sale_to_cover_gain_inr: Decimal = D(0)
    error: str = ""


@dataclass
class Lot:
    """Shares acquired on one date, at one cost."""

    symbol: str
    acquired: Optional[date]
    shares: Decimal
    cost_inr: Decimal
    cost_fx: Decimal
    currency: str
    origin: str                       # rsu_vest | espp | dividend_reinvest | purchase
    country_code: str = "2"
    source_document: str = ""

    @property
    def cost_per_share_inr(self) -> Decimal:
        return self.cost_inr / self.shares if self.shares else D(0)

    @property
    def cost_per_share_fx(self) -> Decimal:
        return self.cost_fx / self.shares if self.shares else D(0)


@dataclass
class ForeignEquityResult:
    vests: List[VestResult] = field(default_factory=list)
    lots: List[Lot] = field(default_factory=list)
    total_perquisite_inr: Decimal = D(0)
    warnings: List[str] = field(default_factory=list)
    provisional_rate_months: List[str] = field(default_factory=list)
    missing_rate_months: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Vesting
# --------------------------------------------------------------------------


def compute_vests(tr: TaxReturn, forex: ForexTable) -> ForeignEquityResult:
    result = ForeignEquityResult()

    for vest in tr.rsu_vests:
        if not vest.vest_date or vest.shares_vested <= 0:
            continue

        try:
            if vest.forex_rate_override:
                rate = Rate(vest.currency, "override", vest.forex_rate_override,
                            provisional=False, note="Entered by you")
            else:
                rate = forex.rate_for(
                    vest.vest_date, vest.currency, SALARY_AND_OTHER_SOURCES
                )
        except RateUnavailable as exc:
            result.vests.append(
                VestResult(vest=vest, perquisite_inr=D(0), rate=None,
                           shares_retained=vest.shares_retained, error=str(exc))
            )
            result.missing_rate_months.append(exc.month)
            continue

        perquisite = vest.gross_value_fx * rate.value
        outcome = VestResult(
            vest=vest, perquisite_inr=perquisite, rate=rate,
            shares_retained=vest.shares_retained,
        )

        # Sell-to-cover happens on the vesting date at close to the vesting
        # price, so the gain is small — but it is a transfer, and it belongs in
        # the capital-gains schedule rather than being quietly ignored.
        if vest.shares_sold_to_cover > 0 and vest.sale_price_per_share_fx > 0:
            proceeds_fx = vest.shares_sold_to_cover * vest.sale_price_per_share_fx
            basis_fx = vest.shares_sold_to_cover * vest.fmv_per_share_fx
            outcome.sale_to_cover_gain_inr = (proceeds_fx - basis_fx) * rate.value

        result.vests.append(outcome)
        result.total_perquisite_inr += perquisite

        if vest.shares_vested > 0:
            # The lot covers every share that vested, including any the broker
            # sold to cover withholding. That sale is fed back in as an ordinary
            # disposal, so it consumes from this lot rather than needing a
            # parallel code path.
            result.lots.append(Lot(
                symbol=vest.symbol,
                acquired=vest.vest_date,
                shares=vest.shares_vested,
                # Section 49(2AA): cost is the amount already taxed as a
                # perquisite, so the same rupee is never taxed twice.
                cost_inr=vest.shares_vested * vest.fmv_per_share_fx * rate.value,
                cost_fx=vest.shares_vested * vest.fmv_per_share_fx,
                currency=vest.currency,
                origin="rsu_vest",
                country_code=vest.country_code,
                source_document=vest.source_document,
            ))

    result.provisional_rate_months = forex.provisional_months(
        [v.vest_date for v in tr.rsu_vests]
    )
    _warn_about_form16(tr, result)
    return result


def _warn_about_form16(tr: TaxReturn, result: ForeignEquityResult) -> None:
    """The perquisite is usually already inside the Form 16 gross salary."""
    if not result.total_perquisite_inr:
        return
    already_included = [v for v in tr.rsu_vests if v.included_in_form16]
    if already_included and len(already_included) == len(tr.rsu_vests):
        result.warnings.append(
            f"₹{result.total_perquisite_inr:,.0f} of RSU perquisite is marked as "
            "already included in your Form 16, so it has not been added to "
            "salary again. Check that the Form 16 perquisite figure under "
            "section 17(2) really does cover every vest — if a tranche is "
            "missing, untick it below and it will be added."
        )
    elif already_included:
        result.warnings.append(
            "Some vests are marked as included in Form 16 and some are not. "
            "Only the ones not included have been added to salary. Reconcile "
            "the total against the section 17(2) perquisite in your Form 16."
        )


def perquisite_to_add_to_salary(result: ForeignEquityResult) -> Decimal:
    """Only the vests the employer did not already put through payroll."""
    return sum(
        (outcome.perquisite_inr for outcome in result.vests
         if not outcome.vest.included_in_form16),
        D(0),
    )


# --------------------------------------------------------------------------
# Dividend reinvestment
# --------------------------------------------------------------------------


def dividend_lots(tr: TaxReturn, forex: ForexTable) -> Tuple[List[Lot], List[str]]:
    """Lots created by reinvesting dividends.

    Each reinvestment is a fresh acquisition: its own date, its own 24-month
    clock, and a cost equal to the gross dividend that was already taxed. A
    quarterly dividend reinvested for three years leaves twelve small lots, most
    of them still short term when the position is finally sold. That is why a
    "long-held" RSU position so often produces short-term gains nobody expected.
    """
    lots: List[Lot] = []
    warnings: List[str] = []

    for dividend in tr.dividends:
        if not dividend.is_reinvested or dividend.shares_acquired <= 0:
            continue
        if not dividend.pay_date:
            warnings.append(
                f"A reinvested dividend for {dividend.symbol or 'a holding'} has "
                "no payment date, so its holding period cannot be worked out. "
                "Fill the date in."
            )
            continue
        try:
            if dividend.forex_rate_override:
                rate_value = dividend.forex_rate_override
            else:
                rate_value = forex.rate_for(
                    dividend.pay_date, dividend.currency, SALARY_AND_OTHER_SOURCES
                ).value
        except RateUnavailable as exc:
            warnings.append(str(exc))
            continue

        # The cost of the new lot is the gross dividend, because that is the
        # amount brought to tax. Using the net-of-withholding figure would
        # understate the basis and overtax the eventual sale.
        cost_fx = (
            dividend.shares_acquired * dividend.reinvest_price_per_share_fx
            if dividend.reinvest_price_per_share_fx > 0
            else dividend.gross_amount_fx
        )
        lots.append(Lot(
            symbol=dividend.symbol,
            acquired=dividend.pay_date,
            shares=dividend.shares_acquired,
            cost_inr=cost_fx * rate_value,
            cost_fx=cost_fx,
            currency=dividend.currency,
            origin="dividend_reinvest",
            country_code=dividend.country_code,
            source_document=dividend.source_document,
        ))

    if lots:
        warnings.append(
            f"{len(lots)} lot(s) were created by dividend reinvestment. Each one "
            "starts its own 24-month holding period, so a position you have held "
            "for years can still throw off short-term gains on the units bought "
            "by recent dividends."
        )
    return lots, warnings


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------


@dataclass
class SaleEvent:
    """A disposal that still needs matching against lots."""

    symbol: str
    sale_date: Optional[date]
    shares: Decimal
    price_per_share_fx: Decimal
    currency: str = "USD"
    fees_fx: Decimal = D(0)
    country_code: str = "2"
    source_document: str = ""


def classify_foreign_gain(
    purchase: Optional[date], sale: Optional[date]
) -> str:
    """Long term only after 24 months — no securities transaction tax abroad."""
    if purchase is None or sale is None:
        return "stcg_slab_foreign"
    months = (sale.year - purchase.year) * 12 + (sale.month - purchase.month)
    if sale.day < purchase.day:
        months -= 1
    return (
        "ltcg_112_foreign" if months >= LONG_TERM_MONTHS_FOREIGN
        else "stcg_slab_foreign"
    )


def match_sales_to_lots(
    sales: List[SaleEvent],
    lots: List[Lot],
    forex: ForexTable,
    method: str = "fifo",
) -> Tuple[List[CapitalGainItem], List[str]]:
    """Match disposals against lots and emit capital-gain rows.

    First in, first out. It is the only method the department has never argued
    with, and with RSU tranches and reinvested dividends interleaving it is also
    the only one a taxpayer can reconstruct three years later.
    """
    items: List[CapitalGainItem] = []
    warnings: List[str] = []

    pool: Dict[str, List[Lot]] = {}
    for lot in lots:
        pool.setdefault(lot.symbol.upper(), []).append(
            Lot(**{**lot.__dict__})
        )
    for holdings in pool.values():
        holdings.sort(key=lambda l: l.acquired or date.min)

    for sale in sorted(sales, key=lambda s: s.sale_date or date.min):
        remaining = sale.shares
        holdings = pool.get(sale.symbol.upper(), [])

        if not holdings:
            warnings.append(
                f"{sale.shares} share(s) of {sale.symbol} were sold on "
                f"{sale.sale_date} but no vesting or purchase lot was found to "
                "match them against. The whole sale value will be treated as "
                "gain until you add the acquisition."
            )

        try:
            sale_rate = forex.rate_for(
                sale.sale_date, sale.currency, CAPITAL_GAINS
            ) if sale.sale_date else None
        except RateUnavailable as exc:
            warnings.append(str(exc))
            continue
        if sale_rate is None:
            warnings.append(
                f"A sale of {sale.symbol} has no date, so no exchange rate can "
                "be applied. Fill the date in."
            )
            continue

        while remaining > 0 and holdings:
            lot = holdings[0]
            used = min(remaining, lot.shares)

            # Take the per-share figures before the lot is decremented — they
            # are derived from ``lot.shares``, so reading them afterwards gives
            # the wrong basis for every partial disposal.
            cost_per_share_inr = lot.cost_per_share_inr
            cost_per_share_fx = lot.cost_per_share_fx

            proceeds_fx = used * sale.price_per_share_fx
            fees_share = (
                sale.fees_fx * (used / sale.shares) if sale.shares else D(0)
            )
            cost_inr = cost_per_share_inr * used

            if lot.acquired:
                description = (
                    f"{sale.symbol} — {used} share(s) from "
                    f"{_origin_label(lot.origin)} {lot.acquired:%d %b %Y}"
                )
            else:
                description = f"{sale.symbol} — {used} share(s)"

            items.append(CapitalGainItem(
                category=classify_foreign_gain(lot.acquired, sale.sale_date),
                description=description,
                sale_date=sale.sale_date,
                purchase_date=lot.acquired,
                # Rule 115 converts the sale at the month before transfer. The
                # cost stays at the rupee value already brought to tax at
                # vesting or reinvestment — converting it again at today's rate
                # would manufacture a currency gain that was never income.
                sale_consideration=proceeds_fx * sale_rate.value,
                cost_of_acquisition=cost_inr,
                transfer_expenses=fees_share * sale_rate.value,
                is_foreign=True,
                currency=sale.currency,
                sale_consideration_fx=proceeds_fx,
                cost_of_acquisition_fx=cost_per_share_fx * used,
                forex_rate_sale=sale_rate.value,
                country_code=sale.country_code,
                lot_origin=lot.origin,
                source_document=sale.source_document,
            ))

            lot.shares -= used
            lot.cost_inr -= cost_inr
            lot.cost_fx -= cost_per_share_fx * used
            remaining -= used
            if lot.shares <= 0:
                holdings.pop(0)

        if remaining > 0:
            # Unmatched shares: report the proceeds with a nil basis rather than
            # silently dropping them, and say so loudly.
            proceeds_fx = remaining * sale.price_per_share_fx
            items.append(CapitalGainItem(
                category="stcg_slab_foreign",
                description=f"{sale.symbol} — {remaining} share(s), acquisition "
                            "not matched",
                sale_date=sale.sale_date,
                sale_consideration=proceeds_fx * sale_rate.value,
                cost_of_acquisition=D(0),
                is_foreign=True,
                currency=sale.currency,
                sale_consideration_fx=proceeds_fx,
                forex_rate_sale=sale_rate.value,
                country_code=sale.country_code,
                source_document=sale.source_document,
            ))

    return items, warnings


def _origin_label(origin: str) -> str:
    return {
        "rsu_vest": "vesting on",
        "espp": "ESPP purchase on",
        "dividend_reinvest": "dividend reinvested on",
        "purchase": "purchase on",
    }.get(origin, "acquired")


def remaining_holdings(
    sales: List[SaleEvent], lots: List[Lot]
) -> List[Lot]:
    """What is still held at the end — the closing position for Schedule FA."""
    pool: Dict[str, List[Lot]] = {}
    for lot in lots:
        pool.setdefault(lot.symbol.upper(), []).append(Lot(**{**lot.__dict__}))
    for holdings in pool.values():
        holdings.sort(key=lambda l: l.acquired or date.min)

    for sale in sorted(sales, key=lambda s: s.sale_date or date.min):
        remaining = sale.shares
        holdings = pool.get(sale.symbol.upper(), [])
        while remaining > 0 and holdings:
            lot = holdings[0]
            used = min(remaining, lot.shares)
            per_share = lot.cost_per_share_inr
            per_share_fx = lot.cost_per_share_fx
            lot.shares -= used
            lot.cost_inr -= per_share * used
            lot.cost_fx -= per_share_fx * used
            remaining -= used
            if lot.shares <= 0:
                holdings.pop(0)

    return [lot for holdings in pool.values() for lot in holdings if lot.shares > 0]
