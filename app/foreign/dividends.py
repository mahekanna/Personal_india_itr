"""Dividends — foreign and domestic — and how they follow the position.

Since the Finance Act 2020 removed the dividend distribution tax, a dividend is
ordinary income taxed at slab rates. There is no 12.5% concession and no
₹1.25 lakh shelter. A US dividend is taxed in full in India on the **gross**
amount, before the 25% the US withholds under Article 10 of the treaty; the
withheld tax comes back as a credit under section 90, not as a deduction.

Three things people get wrong:

* Netting off the withholding and declaring only what reached the account. That
  understates income by a quarter and forfeits the credit.
* Treating a reinvested dividend as not-yet-income. It is income on the payment
  date whether it arrives as cash or as more shares.
* Sizing the dividend by the position held *today*. Entitlement is fixed on the
  record date, and a holding that grows every quarter as tranches vest — and
  again whenever a dividend is reinvested — held a different number of shares on
  each of them. This module reads the same lot ledger the capital-gains matching
  uses, so the position it works from is the real one.

An Indian dividend is the same income under a different set of plumbing: no
currency conversion, no foreign credit, and TDS at 10% under section 194 once
the payer has paid more than ₹10,000 in the year — a threshold the Finance Act
2025 raised from ₹5,000.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..money import D, non_negative
from ..schemas import (
    DividendReceipt,
    DividendSchedule,
    ForeignTaxPayment,
    TaxReturn,
)
from .forex import SALARY_AND_OTHER_SOURCES, ForexTable, RateUnavailable
from .rsu import Lot, SaleEvent, shares_held_on, symbols_held

# Article 10 of the India-US treaty caps withholding on dividends paid to an
# individual resident of India at 25%. Anything above that is not creditable —
# it has to be reclaimed from the IRS instead.
US_TREATY_DIVIDEND_CAP = D("0.25")

# Section 194: TDS on a dividend from an Indian company, once the payer has
# paid more than this in the year. Raised from ₹5,000 by the Finance Act 2025.
SECTION_194_THRESHOLD = D("10000")
SECTION_194_RATE = D("0.10")

_MONTHS_BETWEEN = {
    "monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12,
}


@dataclass
class DividendLine:
    receipt: DividendReceipt
    gross_inr: Decimal
    foreign_tax_inr: Decimal
    rate_used: Optional[Decimal]
    creditable_inr: Decimal
    excess_withholding_inr: Decimal = D(0)
    # What the holdings ledger says was held on the record date, and what the
    # declared amount implies. A gap between them is worth knowing about.
    shares_on_record_date: Optional[Decimal] = None
    implied_shares: Optional[Decimal] = None
    error: str = ""

    @property
    def is_domestic(self) -> bool:
        return self.receipt.is_domestic


@dataclass
class DividendResult:
    lines: List[DividendLine] = field(default_factory=list)
    total_gross_inr: Decimal = D(0)          # foreign only
    total_domestic_inr: Decimal = D(0)
    total_foreign_tax_inr: Decimal = D(0)
    total_creditable_inr: Decimal = D(0)
    total_tds_194: Decimal = D(0)
    reinvested_count: int = 0
    reinvested_value_inr: Decimal = D(0)
    generated: List[DividendReceipt] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def total_all_inr(self) -> Decimal:
        return self.total_gross_inr + self.total_domestic_inr


# --------------------------------------------------------------------------
# Generating payments from a schedule and the holdings ledger
# --------------------------------------------------------------------------


def expand_schedules(
    tr: TaxReturn,
    lots: List[Lot],
    sales: List[SaleEvent],
    fy_start: Optional[date] = None,
    fy_end: Optional[date] = None,
) -> Tuple[List[DividendReceipt], List[str]]:
    """Turn declared per-share rates into payments sized by the real position.

    Payments are walked in date order, and a reinvested one adds to the running
    position before the next is sized — because that is what actually happens.
    Three years of reinvested quarterly dividends compound, and a schedule that
    ignored that would understate the later payments.

    A payment already entered by hand, or imported from a 1099-DIV, always wins:
    the schedule fills gaps, it does not overwrite.
    """
    generated: List[DividendReceipt] = []
    warnings: List[str] = []

    existing = {
        (d.symbol.upper(), d.pay_date)
        for d in tr.dividends if d.pay_date
    }
    # Extra shares acquired by reinvesting the payments generated here.
    reinvested: Dict[str, Decimal] = {}

    pending: List[Tuple[date, DividendSchedule, int]] = []
    for schedule in tr.dividend_schedules:
        if not schedule.first_pay_date or schedule.payments <= 0:
            warnings.append(
                f"Dividend schedule for {schedule.symbol or 'a holding'} needs a "
                "first payment date and a number of payments."
            )
            continue
        step = _MONTHS_BETWEEN.get(schedule.frequency, 3)
        for index in range(schedule.payments):
            pending.append((_add_months(schedule.first_pay_date, index * step),
                            schedule, index))

    for pay_date, schedule, _index in sorted(pending, key=lambda row: row[0]):
        if fy_start and fy_end and not (fy_start <= pay_date <= fy_end):
            continue
        key = schedule.symbol.upper()
        if (key, pay_date) in existing:
            continue

        record_date = pay_date - timedelta(days=schedule.record_date_lead_days)
        declared = schedule.declared_rates.get(pay_date.isoformat())
        rate_per_share = D(declared) if declared is not None else schedule.dividend_per_share_fx
        if rate_per_share <= 0:
            continue

        held = shares_held_on(lots, sales, schedule.symbol, record_date)
        held += reinvested.get(key, D(0))
        if held <= 0:
            continue

        gross = held * rate_per_share
        withheld = (
            gross * schedule.withholding_rate
            if schedule.currency.upper() != "INR"
            else D(0)
        )
        tds = (
            gross * SECTION_194_RATE
            if schedule.currency.upper() == "INR" else D(0)
        )

        receipt = DividendReceipt(
            symbol=schedule.symbol,
            company_name=schedule.company_name,
            pay_date=pay_date,
            record_date=record_date,
            gross_amount_fx=gross,
            dividend_per_share_fx=rate_per_share,
            shares_held=held,
            foreign_tax_withheld_fx=withheld,
            tds_deducted=tds,
            currency=schedule.currency,
            country_code=schedule.country_code,
            is_reinvested=schedule.reinvested,
            source_document=f"Dividend schedule — {schedule.symbol}",
        )

        if schedule.reinvested:
            # Reinvestment buys at whatever the price was; without one on file
            # the share count cannot be worked out, so only the income is
            # recorded and the lot is left for the user to complete.
            reinvested[key] = reinvested.get(key, D(0))

        generated.append(receipt)

    if generated:
        estimated = [r for r in generated
                     if r.pay_date.isoformat() not in _all_declared(tr)]
        if estimated:
            warnings.append(
                f"{len(generated)} dividend payment(s) were worked out from your "
                "schedules and the shares actually held on each record date. "
                f"{len(estimated)} of them use the standard per-share rate rather "
                "than a declared one — replace those with the rate the company "
                "actually declared before filing."
            )
    return generated, warnings


def _all_declared(tr: TaxReturn) -> set:
    out = set()
    for schedule in tr.dividend_schedules:
        out.update(schedule.declared_rates)
    return out


def _add_months(start: date, months: int) -> date:
    import calendar

    index = start.month - 1 + months
    year = start.year + index // 12
    month = index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# --------------------------------------------------------------------------
# Computing what was received
# --------------------------------------------------------------------------


def compute_dividends(
    tr: TaxReturn,
    forex: ForexTable,
    lots: Optional[List[Lot]] = None,
    sales: Optional[List[SaleEvent]] = None,
) -> DividendResult:
    result = DividendResult()

    for receipt in tr.dividends:
        if receipt.gross_amount_fx <= 0:
            continue
        if not receipt.pay_date:
            result.warnings.append(
                f"A dividend from {receipt.symbol or 'a holding'} has no payment "
                "date, so Rule 115 cannot pick an exchange rate for it, and the "
                "section 234C relief for dividend income cannot be applied."
            )
            continue

        # ---- Indian dividends need no conversion --------------------------
        if receipt.is_domestic:
            result.lines.append(DividendLine(
                receipt=receipt, gross_inr=receipt.gross_amount_fx,
                foreign_tax_inr=D(0), rate_used=D(1), creditable_inr=D(0),
                shares_on_record_date=_held(receipt, lots, sales),
                implied_shares=receipt.implied_shares or None,
            ))
            result.total_domestic_inr += receipt.gross_amount_fx
            result.total_tds_194 += receipt.tds_deducted
            if receipt.is_reinvested:
                result.reinvested_count += 1
                result.reinvested_value_inr += receipt.gross_amount_fx
            continue

        # ---- Foreign dividends --------------------------------------------
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

        treaty_ceiling = receipt.gross_amount_fx * US_TREATY_DIVIDEND_CAP
        excess_fx = max(D(0), receipt.foreign_tax_withheld_fx - treaty_ceiling)
        creditable_inr = (receipt.foreign_tax_withheld_fx - excess_fx) * rate

        result.lines.append(DividendLine(
            receipt=receipt, gross_inr=gross_inr,
            foreign_tax_inr=foreign_tax_inr, rate_used=rate,
            creditable_inr=creditable_inr,
            excess_withholding_inr=excess_fx * rate,
            shares_on_record_date=_held(receipt, lots, sales),
            implied_shares=receipt.implied_shares or None,
        ))
        result.total_gross_inr += gross_inr
        result.total_foreign_tax_inr += foreign_tax_inr
        result.total_creditable_inr += creditable_inr

        if receipt.is_reinvested:
            result.reinvested_count += 1
            result.reinvested_value_inr += gross_inr

    _add_warnings(tr, result, lots, sales)
    return result


def _held(
    receipt: DividendReceipt,
    lots: Optional[List[Lot]],
    sales: Optional[List[SaleEvent]],
) -> Optional[Decimal]:
    if lots is None or not receipt.symbol:
        return None
    when = receipt.record_date or receipt.pay_date
    if when is None:
        return None
    # A symbol the lot ledger has never heard of — an Indian holding, or a
    # position bought outside the plan — is unknown, not zero. Reporting nil
    # would read as "you held none", which is a different claim entirely.
    if receipt.symbol.upper() not in symbols_held(lots):
        return None
    return shares_held_on(lots, sales or [], receipt.symbol, when)


# --------------------------------------------------------------------------


def _add_warnings(
    tr: TaxReturn,
    result: DividendResult,
    lots: Optional[List[Lot]],
    sales: Optional[List[SaleEvent]],
) -> None:
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

    _check_against_holdings(result)
    _check_for_silent_holdings(tr, result, lots, sales)
    _check_section_194(result)


def _check_against_holdings(result: DividendResult) -> None:
    """Does the amount declared match the shares actually held?"""
    mismatched = []
    for line in result.lines:
        if line.shares_on_record_date is None or not line.implied_shares:
            continue
        held = line.shares_on_record_date
        implied = line.implied_shares
        if held <= 0:
            continue
        # A whole share of difference is worth a look; rounding is not.
        if abs(implied - held) > max(D(1), held * D("0.02")):
            mismatched.append((line, held, implied))

    for line, held, implied in mismatched[:4]:
        result.warnings.append(
            f"{line.receipt.symbol}: the dividend paid on "
            f"{line.receipt.pay_date:%d %b %Y} implies {implied:.2f} shares at "
            f"{line.receipt.currency} {line.receipt.dividend_per_share_fx} each, "
            f"but the holdings ledger shows {held:.2f} held on the record date. "
            "Either a vesting tranche or a purchase is missing, or the payment "
            "covers a position this return does not know about."
        )


def _check_for_silent_holdings(
    tr: TaxReturn,
    result: DividendResult,
    lots: Optional[List[Lot]],
    sales: Optional[List[SaleEvent]],
) -> None:
    """A holding that paid nothing all year is worth a second look."""
    if not lots:
        return
    paid = {line.receipt.symbol.upper() for line in result.lines}
    scheduled = {s.symbol.upper() for s in tr.dividend_schedules}
    silent = [
        symbol for symbol in symbols_held(lots)
        if symbol not in paid and symbol not in scheduled
    ]
    if silent:
        result.warnings.append(
            "No dividend is recorded for " + ", ".join(silent[:5])
            + ". If the company pays one, the payments are in your broker's "
            "1099-DIV or activity statement and the AIS will have them too — "
            "and income the AIS shows but the return omits is the commonest "
            "reason a notice arrives."
        )


def _check_section_194(result: DividendResult) -> None:
    """Indian dividends above ₹10,000 from one payer should show TDS."""
    by_symbol: Dict[str, Decimal] = {}
    tds_by_symbol: Dict[str, Decimal] = {}
    for line in result.lines:
        if not line.is_domestic:
            continue
        key = line.receipt.symbol.upper() or "—"
        by_symbol[key] = by_symbol.get(key, D(0)) + line.gross_inr
        tds_by_symbol[key] = tds_by_symbol.get(key, D(0)) + line.receipt.tds_deducted

    missing = [
        symbol for symbol, gross in by_symbol.items()
        if gross > SECTION_194_THRESHOLD and tds_by_symbol.get(symbol, D(0)) <= 0
    ]
    if missing:
        result.warnings.append(
            "Dividends above ₹10,000 are recorded from "
            + ", ".join(missing[:5])
            + " with no TDS. Section 194 requires 10% once a payer crosses "
            "₹10,000 in the year — the threshold the Finance Act 2025 raised "
            "from ₹5,000. Check Form 26AS: unclaimed TDS is a refund forgone."
        )


# --------------------------------------------------------------------------
# Section 57(1): interest on money borrowed to buy the shares
# --------------------------------------------------------------------------


def section_57_interest_allowed(
    dividend_income: Decimal, interest_paid: Decimal
) -> Decimal:
    """Capped at 20% of the dividend income, and nothing else is deductible.

    The proviso to section 57(i) allows interest on borrowed capital against
    dividend and mutual-fund income up to a fifth of that income. Collection
    charges, advisory fees and demat charges are all disallowed outright.
    """
    if dividend_income <= 0 or interest_paid <= 0:
        return D(0)
    return min(interest_paid, dividend_income * D("0.20"))


# --------------------------------------------------------------------------


def to_foreign_tax_payments(result: DividendResult) -> List[ForeignTaxPayment]:
    """Roll the foreign dividend lines up into Form 67 / Schedule TR entries."""
    by_country: dict = {}
    for line in result.lines:
        if line.is_domestic:
            continue
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


def to_tds_payments(result: DividendResult) -> List[dict]:
    """Section 194 TDS becomes an ordinary tax credit on the return."""
    rows: List[dict] = []
    for line in result.lines:
        if not line.is_domestic or line.receipt.tds_deducted <= 0:
            continue
        rows.append({
            "kind": "tds_other",
            "deductor_name": line.receipt.company_name or line.receipt.symbol,
            "amount": line.receipt.tds_deducted,
            "source_document": line.receipt.source_document
            or "Dividend TDS u/s 194",
        })
    return rows
