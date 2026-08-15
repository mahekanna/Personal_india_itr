"""Turn RSU, dividend and sale records into ordinary return entries.

Everything foreign eventually has to become something the rest of the system
already understands: a perquisite inside salary, rows in the capital-gains
schedule, a line of other-sources income, a credit against tax, and a set of
Schedule FA rows. This module does that conversion once, before the engine
runs, so the tax computation itself never has to know what an RSU is.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from ..money import D
from ..schemas import ForeignTaxPayment, TaxReturn
from . import dividends as dividend_module
from . import rsu as rsu_module
from . import vesting as vesting_module
from .forex import ForexTable
from .schedule_fa import ScheduleFAResult, build_schedule_fa


@dataclass
class ForeignResult:
    """Everything the foreign schedules produced, kept for display."""

    vests: rsu_module.ForeignEquityResult = field(
        default_factory=rsu_module.ForeignEquityResult
    )
    dividends: dividend_module.DividendResult = field(
        default_factory=dividend_module.DividendResult
    )
    schedule_fa: ScheduleFAResult = field(default_factory=ScheduleFAResult)
    lots: List[rsu_module.Lot] = field(default_factory=list)
    closing_lots: List[rsu_module.Lot] = field(default_factory=list)
    capital_gain_items: List = field(default_factory=list)
    foreign_tax_payments: List[ForeignTaxPayment] = field(default_factory=list)
    perquisite_added_to_salary: Decimal = D(0)
    # Tranches generated from a vesting schedule that have not vested yet.
    projected_vests: List = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def has_anything(self) -> bool:
        return bool(
            self.vests.vests or self.dividends.lines
            or self.capital_gain_items or self.schedule_fa.rows
        )


def apply_foreign(
    tr: TaxReturn, include_projected: bool = False
) -> tuple[TaxReturn, ForeignResult]:
    """Fold foreign records into a copy of the return.

    The original is left alone, so the user's own entries are never rewritten
    by a re-run and the wizard can show both the raw records and their effect.

    ``include_projected`` brings in tranches that have not vested yet and sales
    the user only intends to make. That is right for the advance-tax planner and
    wrong for everything else: a projection must never reach the return or the
    ITR JSON.
    """
    result = ForeignResult()
    if not (tr.rsu_vests or tr.dividends or tr.foreign_sales
            or tr.foreign_holdings or tr.vesting_schedules):
        return tr, result

    working = deepcopy(tr)
    forex = ForexTable(tr.foreign_settings.forex_overrides)

    # -- 0. Expand any vesting schedules into individual tranches -----------
    if working.vesting_schedules:
        from ..tax.rules import get_ay

        ay = get_ay(working.assessment_year)
        expansion = vesting_module.expand_all(
            working, window=(ay.fy_start, ay.fy_end)
        )
        result.warnings.extend(expansion.warnings)

        # A four-year grant produces tranches into the 2029 financial year.
        # Only the ones falling inside the year being computed belong here;
        # the rest are next year's problem, and next year's return.
        in_year = vesting_module.vests_in_year(
            expansion.vests, ay.fy_start, ay.fy_end
        )
        result.projected_vests = [v for v in in_year if v.is_projected]
        working.rsu_vests.extend([v for v in in_year if not v.is_projected])
        if include_projected:
            working.rsu_vests.extend(result.projected_vests)

    # Projections are stripped unless this is a planning run.
    if not include_projected:
        working.rsu_vests = [v for v in working.rsu_vests if not v.is_projected]
        working.foreign_sales = [
            s for s in working.foreign_sales if not s.is_projected
        ]

    # -- 1. Vesting: salary perquisite, and the lots it creates -------------
    vests = rsu_module.compute_vests(working, forex)
    result.vests = vests
    result.warnings.extend(vests.warnings)

    perquisite = rsu_module.perquisite_to_add_to_salary(vests)
    if perquisite > 0:
        _add_perquisite_to_salary(working, perquisite)
        result.perquisite_added_to_salary = perquisite

    # -- 2. Dividends: other-sources income, and reinvestment lots ----------
    dividend_result = dividend_module.compute_dividends(working, forex)
    result.dividends = dividend_result
    result.warnings.extend(dividend_result.warnings)
    working.other_sources.foreign_dividend_income += dividend_result.total_gross_inr

    drip_lots, drip_warnings = rsu_module.dividend_lots(working, forex)
    result.warnings.extend(drip_warnings)

    # -- 3. Lots, and the sales matched against them ------------------------
    lots = vests.lots + drip_lots
    result.lots = lots

    sales = [
        rsu_module.SaleEvent(
            symbol=sale.symbol,
            sale_date=sale.sale_date,
            shares=sale.shares,
            price_per_share_fx=sale.price_per_share_fx,
            currency=sale.currency,
            fees_fx=sale.fees_fx,
            country_code=sale.country_code,
            source_document=sale.source_document,
        )
        for sale in working.foreign_sales
        if sale.shares > 0
    ]

    # Shares the broker sold on the vesting date to fund withholding are a
    # disposal like any other. The gain is usually a rounding error, because the
    # sale price and the cost basis are both the vesting-day value — but it is a
    # transfer, it belongs in Schedule CG, and its proceeds show up in the AIS,
    # so leaving it out invites a reconciliation query.
    sales.extend(
        rsu_module.SaleEvent(
            symbol=vest.symbol,
            sale_date=vest.vest_date,
            shares=vest.shares_sold_to_cover,
            price_per_share_fx=vest.sale_price_per_share_fx,
            currency=vest.currency,
            country_code=vest.country_code,
            source_document=vest.source_document or "Sell-to-cover on vesting",
        )
        for vest in working.rsu_vests
        if vest.shares_sold_to_cover > 0 and vest.sale_price_per_share_fx > 0
    )

    if sales:
        items, warnings = rsu_module.match_sales_to_lots(
            sales, lots, forex, working.foreign_settings.lot_matching
        )
        result.capital_gain_items = items
        result.warnings.extend(warnings)
        working.capital_gains.extend(items)

    result.closing_lots = rsu_module.remaining_holdings(sales, lots)

    # -- 4. Foreign tax paid, for Form 67 and section 90 --------------------
    payments = dividend_module.to_foreign_tax_payments(dividend_result)
    payments.extend(working.foreign_taxes)
    result.foreign_tax_payments = payments

    # -- 5. Schedule FA, on the calendar year -------------------------------
    by_symbol: Dict[str, Decimal] = {}
    for line in dividend_result.lines:
        key = (line.receipt.symbol or "").upper()
        by_symbol[key] = by_symbol.get(key, D(0)) + line.gross_inr

    result.schedule_fa = build_schedule_fa(
        working, lots, sales, forex, by_symbol
    )
    result.warnings.extend(result.schedule_fa.warnings)
    working.foreign_assets = result.schedule_fa.rows

    # -- 6. Rates that still need checking ----------------------------------
    _warn_about_rates(working, forex, result)

    if result.capital_gain_items or result.schedule_fa.rows:
        working.taxpayer.has_foreign_assets = True

    return working, result


def _add_perquisite_to_salary(tr: TaxReturn, amount: Decimal) -> None:
    """Put the vesting perquisite where section 17(2) says it belongs."""
    from ..schemas import SalaryIncome

    if not tr.salaries:
        tr.salaries.append(
            SalaryIncome(employer_name="Employer (RSU perquisite)")
        )
    tr.salaries[0].perquisites_17_2 += amount


def _warn_about_rates(
    tr: TaxReturn, forex: ForexTable, result: ForeignResult
) -> None:
    dates = (
        [v.vest_date for v in tr.rsu_vests]
        + [d.pay_date for d in tr.dividends]
        + [s.sale_date for s in tr.foreign_sales]
    )
    missing = forex.months_needing_a_rate(dates)
    if missing:
        result.warnings.append(
            "No exchange rate is on file for "
            + ", ".join(missing)
            + ". Rule 115 needs the SBI TT buying rate as on the last day of "
            "each of those months — enter them on the Foreign income page."
        )

    provisional = forex.provisional_months(dates)
    if provisional:
        result.warnings.append(
            "These months used a built-in reference rate rather than a verified "
            "one: " + ", ".join(provisional) + ". They are approximations. Look "
            "up the published SBI TT buying rate for each and enter it before "
            "you file — the perquisite value, the capital gain and the TDS "
            "reconciliation all move with it."
        )
