"""Schedule FA — disclosure of foreign assets.

Two things make this schedule different from everything else in the return, and
both catch people out every year.

**It runs on the calendar year.** Not the Indian financial year. For AY 2026-27
the reporting period is 1 January to 31 December 2025. A vest in February 2026
belongs to *next* year's Schedule FA even though it is this year's salary.

**There is no threshold.** A resident and ordinarily resident who held any
foreign asset at any moment in that calendar year must report it, whatever it
was worth, and whether or not it produced a rupee of income. Ten shares left in
an old E*TRADE account count. The penalty for omitting one is a flat ₹10 lakh
per year under sections 42 and 43 of the Black Money Act — assessed on the
non-disclosure itself, not on any tax avoided, so it applies just as fully when
the correct tax was paid.

An RSU holder normally files two tables: **A2** for the custodial account with
the broker, and **A3** for the shares themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from ..money import D, non_negative
from ..schemas import ForeignAsset, ForeignHolding, TaxReturn
from .forex import ForexTable, RateUnavailable
from .rsu import Lot, SaleEvent


@dataclass
class ScheduleFAResult:
    rows: List[ForeignAsset] = field(default_factory=list)
    calendar_year: str = ""
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    warnings: List[str] = field(default_factory=list)
    estimates: List[str] = field(default_factory=list)


def calendar_period(assessment_year: str) -> tuple[date, date, str]:
    """The calendar year Schedule FA covers for a given assessment year.

    AY 2026-27 reports on FY 2025-26, whose Schedule FA period is calendar
    2025 — the calendar year ending inside that financial year.
    """
    start_fy_year = int(assessment_year.split("-")[0]) - 1   # AY 2026-27 -> 2025
    year = start_fy_year
    return date(year, 1, 1), date(year, 12, 31), str(year)


def build_schedule_fa(
    tr: TaxReturn,
    lots: List[Lot],
    sales: List[SaleEvent],
    forex: ForexTable,
    dividend_income_by_symbol: Optional[Dict[str, Decimal]] = None,
) -> ScheduleFAResult:
    start, end, year = calendar_period(tr.assessment_year)
    result = ScheduleFAResult(
        calendar_year=year, period_start=start, period_end=end
    )
    dividends = dividend_income_by_symbol or {}
    holdings = {h.symbol.upper(): h for h in tr.foreign_holdings}

    symbols = {lot.symbol.upper() for lot in lots if lot.symbol}
    symbols |= {sale.symbol.upper() for sale in sales if sale.symbol}
    symbols |= set(holdings)
    symbols.discard("")

    if not symbols:
        return result

    try:
        closing_rate = forex.rate_for_month(f"{year}-12", "USD")
    except RateUnavailable:
        closing_rate = None
        result.warnings.append(
            f"No December {year} exchange rate is on file, so the Schedule FA "
            "closing values cannot be converted. Enter it on the Foreign income "
            "page."
        )

    account_rows: Dict[str, ForeignAsset] = {}

    for symbol in sorted(symbols):
        holding = holdings.get(symbol, ForeignHolding(symbol=symbol))

        # -- position movement within the calendar year --------------------
        acquired_in_year = [
            lot for lot in lots
            if lot.symbol.upper() == symbol and lot.acquired
            and start <= lot.acquired <= end
        ]
        # Shares bought before the calendar year *and still held when it
        # began*. Disposals from earlier years have to come off: without them
        # a position closed in 2024 kept reporting itself in calendar 2025,
        # with an opening balance and a peak value it no longer had.
        held_before = non_negative(
            holding.opening_shares
            + sum(
                (lot.shares for lot in lots
                 if lot.symbol.upper() == symbol and lot.acquired
                 and lot.acquired < start),
                D(0),
            )
            - sum(
                (sale.shares for sale in sales
                 if sale.symbol.upper() == symbol and sale.sale_date
                 and sale.sale_date < start),
                D(0),
            )
        )
        sold_in_year = [
            sale for sale in sales
            if sale.symbol.upper() == symbol and sale.sale_date
            and start <= sale.sale_date <= end
        ]
        sold_shares = sum((sale.shares for sale in sold_in_year), D(0))
        added_shares = sum((lot.shares for lot in acquired_in_year), D(0))
        closing_shares = non_negative(held_before + added_shares - sold_shares)

        if held_before <= 0 and added_shares <= 0:
            continue

        first_acquired = min(
            (lot.acquired for lot in lots
             if lot.symbol.upper() == symbol and lot.acquired),
            default=None,
        )

        # -- valuation ------------------------------------------------------
        initial_value = holding.opening_value_inr + sum(
            (lot.cost_inr for lot in acquired_in_year), D(0)
        )

        peak_shares = held_before + added_shares      # before any disposals
        peak_value = D(0)
        if holding.peak_price_fx > 0 and closing_rate:
            peak_value = peak_shares * holding.peak_price_fx * closing_rate.value
        else:
            # Without a price history the best defensible floor is the higher
            # of what was put in and what it was worth at the close.
            peak_value = initial_value
            result.estimates.append(
                f"{symbol}: no peak price was supplied, so the peak value has "
                "been approximated. Schedule FA wants the highest value during "
                f"calendar {year} — enter the year's high on the Foreign income "
                "page."
            )

        closing_value = D(0)
        if closing_shares > 0 and holding.year_end_price_fx > 0 and closing_rate:
            closing_value = (
                closing_shares * holding.year_end_price_fx * closing_rate.value
            )
        elif closing_shares > 0:
            result.estimates.append(
                f"{symbol}: no 31 December {year} price was supplied, so the "
                "closing value is shown as nil. Enter the year-end price."
            )
        peak_value = max(peak_value, closing_value)

        gross_proceeds = D(0)
        for sale in sold_in_year:
            try:
                rate = forex.rate_for(sale.sale_date, sale.currency)
                gross_proceeds += sale.shares * sale.price_per_share_fx * rate.value
            except RateUnavailable:
                continue

        # -- Table A3: the equity interest ---------------------------------
        result.rows.append(ForeignAsset(
            table="A3",
            country_code=holding.country_code,
            country_name=holding.country_name,
            entity_name=holding.entity_name or symbol,
            entity_address=holding.entity_address,
            entity_zip=holding.entity_zip,
            nature_of_entity=holding.nature_of_entity,
            nature_of_interest="Direct",
            date_acquired=first_acquired,
            initial_investment=initial_value,
            peak_value=peak_value,
            closing_value=closing_value,
            gross_income_accrued=dividends.get(symbol, D(0)) + gross_proceeds,
            nature_of_income="Dividend and sale proceeds",
            income_offered_amount=dividends.get(symbol, D(0)),
            income_offered_schedule="Schedule OS and Schedule CG",
            calendar_year=year,
        ))

        # -- Table A2: the custodial account holding them -------------------
        if holding.broker_name:
            key = f"{holding.broker_name}|{holding.broker_account_number}"
            row = account_rows.get(key)
            if row is None:
                row = ForeignAsset(
                    table="A2",
                    country_code=holding.country_code,
                    country_name=holding.country_name,
                    entity_name=holding.broker_name,
                    entity_address=holding.broker_address,
                    entity_zip=holding.broker_zip,
                    nature_of_entity="Custodial account",
                    nature_of_interest=holding.broker_account_number,
                    date_acquired=first_acquired,
                    nature_of_income="Dividend and sale proceeds",
                    income_offered_schedule="Schedule OS and Schedule CG",
                    calendar_year=year,
                )
                account_rows[key] = row
            row.initial_investment += initial_value
            row.peak_value += peak_value
            row.closing_value += closing_value
            row.gross_income_accrued += dividends.get(symbol, D(0)) + gross_proceeds
            row.income_offered_amount += dividends.get(symbol, D(0))

    result.rows.extend(account_rows.values())
    _add_warnings(tr, result)
    return result


def _add_warnings(tr: TaxReturn, result: ScheduleFAResult) -> None:
    if not result.rows:
        return

    result.warnings.append(
        f"Schedule FA covers calendar {result.calendar_year} "
        f"({result.period_start:%d %b %Y} to {result.period_end:%d %b %Y}), not "
        "the financial year. Anything that vested or was bought in January to "
        "March 2026 belongs in next year's Schedule FA, even though it is this "
        "year's salary."
    )

    if tr.taxpayer.residential_status != "RES":
        result.warnings.append(
            "Schedule FA is required only of a resident and ordinarily "
            "resident. The return is marked otherwise, so these rows are shown "
            "for reference — confirm your residential status before filing."
        )
        return

    if not tr.taxpayer.has_foreign_assets:
        result.warnings.append(
            "Foreign assets were found but the 'I hold foreign assets' box on "
            "the Income page is unticked. It has to be ticked: it is what "
            "moves the return to ITR-2 and opens Schedule FA."
        )

    missing_address = [
        row.entity_name for row in result.rows
        if row.table == "A3" and not row.entity_address
    ]
    if missing_address:
        result.warnings.append(
            "No registered address is on file for "
            + ", ".join(missing_address[:4])
            + ". Schedule FA asks for the entity's address and ZIP code, and "
            "the portal will not accept the row without them."
        )
