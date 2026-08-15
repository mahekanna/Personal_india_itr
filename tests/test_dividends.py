"""Dividends sized against the holdings ledger, and Indian dividends.

Expected values derived by hand. The point of most of these is that a dividend
follows the position held on the *record date*, which for someone whose holding
grows every quarter is a different number every time.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.foreign.dividends import (
    SECTION_194_THRESHOLD,
    compute_dividends,
    expand_schedules,
    section_57_interest_allowed,
    to_tds_payments,
)
from app.foreign.forex import ForexTable
from app.foreign.pipeline import apply_foreign
from app.foreign.rsu import Lot, SaleEvent, shares_held_on
from app.money import D
from app.parsers.registry import parse_document
from app.schemas import (
    DividendReceipt,
    DividendSchedule,
    ESPPPurchase,
    ForeignSale,
    RSUVest,
    SalaryIncome,
    TaxReturn,
)
from app.tax.engine import compare_regimes, compute

MONTHS = (
    "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
)
RATES = {month: {"USD": "88.00"} for month in MONTHS}


def base_return(**kwargs) -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27", regime_choice="new",
                   filing_date=date(2026, 7, 25), **kwargs)
    tr.taxpayer.pan = "ABCDE1234F"
    tr.taxpayer.residential_status = "RES"
    tr.foreign_settings.forex_overrides = dict(RATES)
    tr.foreign_settings.form67_filed = True
    return tr


def growing_position(tr: TaxReturn) -> TaxReturn:
    """100 shares in June, 100 more in September, 50 by ESPP in December."""
    tr.rsu_vests = [
        RSUVest(symbol="ACME", vest_date=date(2025, 6, 15), shares_vested=D(100),
                fmv_per_share_fx=D(150), shares_sold_to_cover=D(0),
                included_in_form16=True),
        RSUVest(symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
                fmv_per_share_fx=D(160), shares_sold_to_cover=D(0),
                included_in_form16=True),
    ]
    tr.espp_purchases = [ESPPPurchase(
        symbol="ACME", purchase_date=date(2025, 12, 1), shares_purchased=D(50),
        fmv_per_share_fx=D(200), price_paid_per_share_fx=D(170),
        included_in_form16=True)]
    return tr


# --------------------------------------------------------------------------
# The holdings ledger
# --------------------------------------------------------------------------


def test_shares_held_reads_the_position_at_a_date():
    lots = [
        Lot("ACME", date(2025, 6, 15), D(100), D(0), D(0), "USD", "rsu_vest"),
        Lot("ACME", date(2025, 9, 15), D(100), D(0), D(0), "USD", "rsu_vest"),
    ]
    sales = [SaleEvent("ACME", date(2025, 10, 1), D(40), D(200))]

    assert shares_held_on(lots, sales, "ACME", date(2025, 6, 1)) == D(0)
    assert shares_held_on(lots, sales, "ACME", date(2025, 6, 15)) == D(100)
    assert shares_held_on(lots, sales, "ACME", date(2025, 9, 20)) == D(200)
    assert shares_held_on(lots, sales, "ACME", date(2025, 11, 1)) == D(160)


def test_shares_held_is_case_insensitive_and_never_negative():
    lots = [Lot("acme", date(2025, 6, 15), D(10), D(0), D(0), "USD", "rsu_vest")]
    sales = [SaleEvent("ACME", date(2025, 7, 1), D(50), D(200))]
    assert shares_held_on(lots, sales, "ACME", date(2025, 6, 20)) == D(10)
    assert shares_held_on(lots, sales, "ACME", date(2025, 8, 1)) == D(0)


# --------------------------------------------------------------------------
# Schedules meeting the ledger
# --------------------------------------------------------------------------


def quarterly_schedule(**kwargs) -> DividendSchedule:
    defaults = dict(
        symbol="ACME", company_name="Acme Inc", frequency="quarterly",
        first_pay_date=date(2025, 5, 20), payments=4,
        dividend_per_share_fx=D("0.70"), record_date_lead_days=14,
        withholding_rate=D("0.25"),
    )
    defaults.update(kwargs)
    return DividendSchedule(**defaults)


def test_a_payment_before_anything_vested_is_not_generated():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule()]
    _, foreign = apply_foreign(tr)
    # The May payment has a 6 May record date; the first tranche vests 15 June.
    assert all(line.receipt.pay_date != date(2025, 5, 20)
               for line in foreign.dividends.lines)


def test_each_payment_is_sized_by_the_position_on_its_record_date():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule()]
    _, foreign = apply_foreign(tr)

    by_date = {line.receipt.pay_date: line for line in foreign.dividends.lines}
    # 20 Aug — only the June tranche is in.
    assert by_date[date(2025, 8, 20)].receipt.shares_held == D(100)
    # 20 Nov — June and September.
    assert by_date[date(2025, 11, 20)].receipt.shares_held == D(200)
    # 20 Feb — and the December ESPP purchase.
    assert by_date[date(2026, 2, 20)].receipt.shares_held == D(250)


def test_the_gross_amount_follows_the_position():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule()]
    _, foreign = apply_foreign(tr)
    by_date = {line.receipt.pay_date: line for line in foreign.dividends.lines}
    assert by_date[date(2025, 11, 20)].receipt.gross_amount_fx == D(200) * D("0.70")
    assert by_date[date(2025, 11, 20)].gross_inr == D(140) * D(88)


def test_a_declared_rate_overrides_the_standard_one():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule(
        declared_rates={"2025-08-20": "0.72", "2025-11-20": "0.75"})]
    _, foreign = apply_foreign(tr)
    by_date = {line.receipt.pay_date: line for line in foreign.dividends.lines}
    assert by_date[date(2025, 8, 20)].receipt.dividend_per_share_fx == D("0.72")
    assert by_date[date(2026, 2, 20)].receipt.dividend_per_share_fx == D("0.70")


def test_a_payment_already_entered_by_hand_is_not_generated_again():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule()]
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 11, 20), record_date=date(2025, 11, 6),
        gross_amount_fx=D(139), foreign_tax_withheld_fx=D("34.75"))]
    _, foreign = apply_foreign(tr)
    november = [line for line in foreign.dividends.lines
                if line.receipt.pay_date == date(2025, 11, 20)]
    assert len(november) == 1
    assert november[0].receipt.gross_amount_fx == D(139)


def test_a_sale_reduces_the_next_payment():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule()]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2025, 10, 1), shares=D(150),
        price_per_share_fx=D(180))]
    _, foreign = apply_foreign(tr)
    by_date = {line.receipt.pay_date: line for line in foreign.dividends.lines}
    assert by_date[date(2025, 11, 20)].receipt.shares_held == D(50)


def test_payments_outside_the_financial_year_are_not_generated():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule(payments=12)]
    _, foreign = apply_foreign(tr)
    assert all(
        date(2025, 4, 1) <= line.receipt.pay_date <= date(2026, 3, 31)
        for line in foreign.dividends.lines
    )


def test_a_schedule_with_no_first_payment_date_is_reported():
    tr = base_return()
    tr.dividend_schedules = [quarterly_schedule(first_pay_date=None)]
    _, foreign = apply_foreign(tr)
    assert any("first payment date" in w for w in foreign.warnings)


# --------------------------------------------------------------------------
# Checking a declared payment against the position
# --------------------------------------------------------------------------


def test_a_dividend_implying_more_shares_than_were_held_is_flagged():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 6, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), shares_sold_to_cover=D(0),
        included_in_form16=True)]
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 11, 20), record_date=date(2025, 11, 6),
        gross_amount_fx=D(350), dividend_per_share_fx=D("0.70"))]
    _, foreign = apply_foreign(tr)
    assert any("implies 500.00 shares" in w for w in foreign.warnings)


def test_a_matching_dividend_raises_nothing():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 6, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), shares_sold_to_cover=D(0),
        included_in_form16=True)]
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 11, 20), record_date=date(2025, 11, 6),
        gross_amount_fx=D(70), dividend_per_share_fx=D("0.70"),
        foreign_tax_withheld_fx=D("17.50"))]
    _, foreign = apply_foreign(tr)
    assert not any("implies" in w for w in foreign.warnings)


def test_a_holding_that_paid_nothing_all_year_is_queried():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 6, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), shares_sold_to_cover=D(0),
        included_in_form16=True)]
    _, foreign = apply_foreign(tr)
    assert any("No dividend is recorded for ACME" in w for w in foreign.warnings)


def test_an_indian_holding_reads_as_unknown_not_zero():
    """The lot ledger tracks foreign equity; it knows nothing of Indian shares."""
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 6, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), shares_sold_to_cover=D(0),
        included_in_form16=True)]
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(24_000), dividend_per_share_fx=D(20))]
    _, foreign = apply_foreign(tr)
    infy = next(l for l in foreign.dividends.lines if l.receipt.symbol == "INFY")
    assert infy.shares_on_record_date is None


# --------------------------------------------------------------------------
# Indian dividends
# --------------------------------------------------------------------------


def test_an_indian_dividend_needs_no_conversion():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(24_000), tds_deducted=D(2_400))]
    result = compute_dividends(tr, ForexTable(RATES))
    assert result.total_domestic_inr == D(24_000)
    assert result.total_gross_inr == D(0)
    assert result.total_creditable_inr == D(0)


def test_indian_and_foreign_dividends_land_in_different_lines():
    tr = base_return()
    tr.dividends = [
        DividendReceipt(symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
                        gross_amount_fx=D(24_000), tds_deducted=D(2_400)),
        DividendReceipt(symbol="ACME", pay_date=date(2025, 11, 20),
                        gross_amount_fx=D(100), foreign_tax_withheld_fx=D(25)),
    ]
    prepared, _ = apply_foreign(tr)
    assert prepared.other_sources.dividend_income == D(24_000)
    assert prepared.other_sources.foreign_dividend_income == D(100) * D(88)


def test_section_194_tds_becomes_a_tax_credit():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(24_000), tds_deducted=D(2_400))]
    prepared, _ = apply_foreign(tr)
    credits = [p for p in prepared.taxes_paid.payments if p.kind == "tds_other"]
    assert len(credits) == 1
    assert credits[0].amount == D(2_400)


def test_a_large_indian_dividend_with_no_tds_is_queried():
    """Section 194 bites above ₹10,000, raised from ₹5,000 for FY 2025-26."""
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(24_000), tds_deducted=D(0))]
    result = compute_dividends(tr, ForexTable(RATES))
    assert any("no TDS" in w for w in result.warnings)


def test_a_small_indian_dividend_needs_no_tds():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(8_000), tds_deducted=D(0))]
    result = compute_dividends(tr, ForexTable(RATES))
    assert not any("no TDS" in w for w in result.warnings)
    assert SECTION_194_THRESHOLD == D(10_000)


def test_an_indian_dividend_gets_the_234c_proviso_when_dated():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", employer_tan="BLRA12345B",
                                salary_17_1=D(2_000_000))]
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 12, 10),
        gross_amount_fx=D(500_000))]
    comp = compare_regimes(tr).new
    dated = [d for d in comp.deferrable if d.arising_on == date(2025, 12, 10)]
    assert dated


# --------------------------------------------------------------------------
# Section 57(i)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dividend, interest, expected",
    [
        (D(100_000), D(50_000), D(20_000)),   # capped at 20%
        (D(100_000), D(15_000), D(15_000)),   # under the cap, allowed in full
        (D(0), D(50_000), D(0)),              # no dividend, no deduction
        (D(100_000), D(0), D(0)),
    ],
)
def test_interest_against_dividend_is_capped_at_20_percent(
    dividend, interest, expected
):
    assert section_57_interest_allowed(dividend, interest) == expected


def test_the_cap_is_applied_and_explained():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(100_000), tds_deducted=D(10_000))]
    tr.other_sources.dividend_interest_expense = D(50_000)
    prepared, foreign = apply_foreign(tr)
    assert prepared.other_sources.section_57_deductions == D(20_000)
    assert any("caps it at 20%" in w for w in foreign.warnings)


def test_the_interest_deduction_survives_the_new_regime():
    """Section 115BAC restricts only clause (iia) of section 57."""
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(1_500_000))]
    tr.dividends = [DividendReceipt(
        symbol="INFY", currency="INR", pay_date=date(2025, 7, 10),
        gross_amount_fx=D(100_000))]
    tr.other_sources.dividend_interest_expense = D(50_000)

    comparison = compare_regimes(tr)
    # The deduction reduces other-sources income under both regimes alike.
    assert comparison.new.other_sources == D(80_000)
    assert comparison.old.other_sources == D(80_000)


# --------------------------------------------------------------------------
# Reinvestment still behaves
# --------------------------------------------------------------------------


def test_a_reinvested_dividend_from_a_schedule_is_still_income():
    tr = growing_position(base_return())
    tr.dividend_schedules = [quarterly_schedule(reinvested=True)]
    prepared, foreign = apply_foreign(tr)
    assert prepared.other_sources.foreign_dividend_income > 0
    assert foreign.dividends.reinvested_count > 0


def test_withholding_above_the_treaty_cap_is_still_caught():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 11, 20), gross_amount_fx=D(100),
        foreign_tax_withheld_fx=D(30))]
    result = compute_dividends(tr, ForexTable(RATES))
    assert result.total_creditable_inr == D(25) * D(88)
    assert any("W-8BEN" in w for w in result.warnings)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


DIV_CSV = (
    "Fidelity Dividends and Interest 1099-DIV\n"
    "Symbol,Record Date,Pay Date,Shares Held,Rate Per Share,Dividend Amount,"
    "Federal Income Tax Withheld,Shares Purchased,Reinvestment Price,Action\n"
    "ACME,2025-11-06,2025-11-20,200,0.75,150.00,37.50,0.88,170.00,Reinvest\n"
)


def test_the_record_date_and_rate_per_share_are_read():
    extraction = parse_document(DIV_CSV.encode(), "fidelity_1099div.csv")
    dividend = extraction.dividends[0]
    assert dividend["record_date"] == date(2025, 11, 6)
    assert dividend["dividend_per_share_fx"] == D("0.75")
    assert dividend["shares_held"] == D(200)
    assert dividend["is_reinvested"] is True


def test_a_parsed_dividend_reconciles_against_the_ledger():
    from app.merge import apply_extractions

    tr = growing_position(base_return())
    apply_extractions(tr, [parse_document(DIV_CSV.encode(), "1099div.csv")])
    _, foreign = apply_foreign(tr)
    line = next(l for l in foreign.dividends.lines
                if l.receipt.pay_date == date(2025, 11, 20))
    assert line.shares_on_record_date == D(200)
    assert line.implied_shares == D(200)
    assert not any("implies" in w for w in foreign.warnings)
