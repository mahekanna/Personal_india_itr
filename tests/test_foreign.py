"""RSUs, dividends, reinvestment, the foreign tax credit and Schedule FA.

Expected values here were worked out by hand from the statute and the rules,
the same way as in ``test_tax_engine.py``.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.foreign.dividends import compute_dividends
from app.foreign.forex import ForexTable, RateUnavailable, preceding_month
from app.foreign.ftc import compute_ftc
from app.foreign.pipeline import apply_foreign
from app.foreign.rsu import classify_foreign_gain, compute_vests
from app.foreign.schedule_fa import calendar_period
from app.itr.selector import select_form
from app.money import D
from app.reconcile import reconcile
from app.schemas import (
    DividendReceipt,
    ForeignHolding,
    ForeignSale,
    RSUVest,
    SalaryIncome,
    TaxReturn,
)
from app.tax.engine import compare_regimes, compute

# A rate table with everything pinned, so no test depends on a built-in guess.
RATES = {
    month: {"USD": rate}
    for month, rate in {
        "2025-08": "88.00", "2025-11": "89.00", "2025-12": "90.00",
        "2026-01": "90.00", "2026-02": "90.00", "2023-08": "82.00",
        "2023-12": "83.00", "2025-05": "85.00", "2025-06": "85.00",
    }.items()
}


def base_return(**kwargs) -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27", filing_date=date(2026, 7, 25),
                   **kwargs)
    tr.taxpayer.pan = "ABCDE1234F"
    tr.taxpayer.name = "Asha Ramanathan"
    tr.taxpayer.residential_status = "RES"
    tr.foreign_settings.forex_overrides = dict(RATES)
    return tr


# --------------------------------------------------------------------------
# Rule 115
# --------------------------------------------------------------------------


def test_rule_115_uses_the_month_before_the_transaction():
    """A vest on 15 September converts at the 31 August rate."""
    assert preceding_month(date(2025, 9, 15)) == "2025-08"
    assert preceding_month(date(2025, 1, 3)) == "2024-12"


def test_a_user_supplied_rate_beats_the_built_in_one():
    table = ForexTable(overrides={"2025-08": {"USD": "88.4213"}})
    rate = table.rate_for(date(2025, 9, 15))
    assert rate.value == D("88.4213")
    assert rate.provisional is False


def test_a_missing_rate_names_the_month_it_needs():
    table = ForexTable(overrides={})
    with pytest.raises(RateUnavailable) as caught:
        table.rate_for_month("1999-01", "USD")
    assert "1999-01" in str(caught.value)


# --------------------------------------------------------------------------
# Vesting
# --------------------------------------------------------------------------


def test_vesting_perquisite_is_fmv_times_the_rule_115_rate():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150),
    )]
    result = compute_vests(tr, ForexTable(RATES))
    # 100 × $150 × 88.00
    assert result.total_perquisite_inr == D(1_320_000)


def test_perquisite_already_in_form16_is_not_added_twice():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000),
                                perquisites_17_2=D(1_320_000))]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), included_in_form16=True,
    )]
    prepared, foreign = apply_foreign(tr)
    assert foreign.perquisite_added_to_salary == D(0)
    assert prepared.salaries[0].perquisites_17_2 == D(1_320_000)


def test_a_vest_missing_from_form16_is_added_to_salary():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), included_in_form16=False,
    )]
    prepared, foreign = apply_foreign(tr)
    assert foreign.perquisite_added_to_salary == D(1_320_000)
    assert prepared.salaries[0].perquisites_17_2 == D(1_320_000)


def test_cost_basis_is_the_perquisite_already_taxed():
    """Section 49(2AA) — otherwise the same rupee is taxed twice."""
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150),
    )]
    _, foreign = apply_foreign(tr)
    lot = foreign.lots[0]
    assert lot.shares == D(100)
    assert lot.cost_inr == D(1_320_000)


def test_sell_to_cover_reaches_the_capital_gains_schedule():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), shares_sold_to_cover=D(30),
        sale_price_per_share_fx=D("150.40"),
    )]
    _, foreign = apply_foreign(tr)
    assert len(foreign.capital_gain_items) == 1
    item = foreign.capital_gain_items[0]
    # 30 × ($150.40 − $150.00) × 88.00
    assert item.net_gain == D("1056.00")
    assert sum(lot.shares for lot in foreign.closing_lots) == D(70)


# --------------------------------------------------------------------------
# Holding period — the 24-month trap
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "purchase, sale, expected",
    [
        # A US share is not listed on a recognised Indian exchange, so it needs
        # 24 months, not 12.
        (date(2024, 1, 15), date(2025, 6, 15), "stcg_slab_foreign"),   # 17 mo
        (date(2024, 1, 15), date(2025, 12, 15), "stcg_slab_foreign"),  # 23 mo
        (date(2024, 1, 15), date(2026, 1, 14), "stcg_slab_foreign"),   # 23 mo
        (date(2024, 1, 15), date(2026, 1, 15), "ltcg_112_foreign"),    # 24 mo
        (date(2023, 1, 15), date(2026, 1, 15), "ltcg_112_foreign"),    # 36 mo
    ],
)
def test_foreign_shares_turn_long_term_only_at_24_months(purchase, sale, expected):
    assert classify_foreign_gain(purchase, sale) == expected


def test_an_18_month_hold_is_taxed_at_slab_rates_not_12_5_percent():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(3_000_000))]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2024, 6, 15), shares_vested=D(100),
        fmv_per_share_fx=D(100), included_in_form16=True,
    )]
    tr.foreign_settings.forex_overrides["2024-05"] = {"USD": "83.00"}
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2025, 12, 15), shares=D(100),
        price_per_share_fx=D(150),
    )]
    _, foreign = apply_foreign(tr)
    item = foreign.capital_gain_items[0]
    assert item.category == "stcg_slab_foreign"

    comp = compute(tr, "new")
    # A slab-rate gain never becomes a special-rate slice.
    assert not any(s.code.startswith("ltcg_112_foreign")
                   for s in comp.special_slices)


def test_a_long_held_lot_gets_the_12_5_percent_rate():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(3_000_000))]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(100), included_in_form16=True,
    )]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(100),
        price_per_share_fx=D(150),
    )]
    _, foreign = apply_foreign(tr)
    assert foreign.capital_gain_items[0].category == "ltcg_112_foreign"

    comp = compute(tr, "new")
    slice_ = next(s for s in comp.special_slices if s.code == "ltcg_112_foreign")
    assert slice_.rate == D("0.125")
    # No ₹1.25 lakh shelter — that belongs to section 112A, which needs STT.
    assert slice_.statutory_exemption == D(0)


def test_foreign_gains_never_get_the_112a_exemption():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(100), included_in_form16=True,
    )]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(100),
        price_per_share_fx=D(200),
    )]
    comp = compare_regimes(tr).new
    slice_ = next(s for s in comp.special_slices if s.code == "ltcg_112_foreign")
    assert slice_.statutory_exemption == D(0)


# --------------------------------------------------------------------------
# FIFO matching
# --------------------------------------------------------------------------


def test_fifo_takes_the_oldest_lot_first():
    tr = base_return()
    tr.rsu_vests = [
        RSUVest(symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(50),
                fmv_per_share_fx=D(100), included_in_form16=True),
        RSUVest(symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(50),
                fmv_per_share_fx=D(150), included_in_form16=True),
    ]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(50),
        price_per_share_fx=D(180),
    )]
    _, foreign = apply_foreign(tr)
    item = foreign.capital_gain_items[0]
    # The 2023 lot goes first, so the gain is long term and its cost is
    # 50 × $100 × 82.00 (August 2023 rate).
    assert item.category == "ltcg_112_foreign"
    assert item.cost_of_acquisition == D(410_000)


def test_a_sale_spanning_two_lots_splits_into_two_rows():
    tr = base_return()
    tr.rsu_vests = [
        RSUVest(symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(40),
                fmv_per_share_fx=D(100), included_in_form16=True),
        RSUVest(symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(40),
                fmv_per_share_fx=D(150), included_in_form16=True),
    ]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(60),
        price_per_share_fx=D(180),
    )]
    _, foreign = apply_foreign(tr)
    assert len(foreign.capital_gain_items) == 2
    categories = [item.category for item in foreign.capital_gain_items]
    assert categories == ["ltcg_112_foreign", "stcg_slab_foreign"]


def test_selling_more_than_was_acquired_is_flagged_not_swallowed():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(10),
        fmv_per_share_fx=D(150), included_in_form16=True,
    )]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(40),
        price_per_share_fx=D(180),
    )]
    _, foreign = apply_foreign(tr)
    unmatched = [i for i in foreign.capital_gain_items
                 if i.cost_of_acquisition == 0]
    assert unmatched
    assert any("not matched" in i.description for i in foreign.capital_gain_items)


# --------------------------------------------------------------------------
# Dividends and reinvestment
# --------------------------------------------------------------------------


def test_dividend_is_taxed_on_the_gross_not_the_net():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(100),
        foreign_tax_withheld_fx=D(25),
    )]
    result = compute_dividends(tr, ForexTable(RATES))
    assert result.total_gross_inr == D(8_900)        # $100 × 89.00
    assert result.total_foreign_tax_inr == D("2225") # $25 × 89.00


def test_a_reinvested_dividend_is_still_income_this_year():
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(100),
        is_reinvested=True, shares_acquired=D(1),
        reinvest_price_per_share_fx=D(100),
    )]
    prepared, foreign = apply_foreign(tr)
    assert prepared.other_sources.foreign_dividend_income == D(8_900)
    assert foreign.dividends.reinvested_count == 1


def test_reinvestment_creates_a_lot_with_its_own_holding_clock():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(100), included_in_form16=True,
    )]
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(200),
        is_reinvested=True, shares_acquired=D(2),
        reinvest_price_per_share_fx=D(100),
    )]
    _, foreign = apply_foreign(tr)
    drip = [lot for lot in foreign.lots if lot.origin == "dividend_reinvest"]
    assert len(drip) == 1
    assert drip[0].acquired == date(2025, 12, 10)
    # The cost is the gross dividend, converted at the November rate.
    assert drip[0].cost_inr == D(17_800)


def test_a_long_held_position_can_still_throw_short_term_gains():
    """The point of tracking reinvestment lots at all."""
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(10),
        fmv_per_share_fx=D(100), included_in_form16=True,
    )]
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(200),
        is_reinvested=True, shares_acquired=D(2),
        reinvest_price_per_share_fx=D(100),
    )]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(12),
        price_per_share_fx=D(180),
    )]
    _, foreign = apply_foreign(tr)
    categories = {item.category for item in foreign.capital_gain_items}
    # The original tranche is long term; the units the dividend bought are not.
    assert categories == {"ltcg_112_foreign", "stcg_slab_foreign"}


def test_withholding_above_the_treaty_rate_is_not_creditable():
    """Article 10 of the India-US treaty caps dividend withholding at 25%."""
    tr = base_return()
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(100),
        foreign_tax_withheld_fx=D(30),      # 30%, above the treaty cap
    )]
    result = compute_dividends(tr, ForexTable(RATES))
    assert result.total_foreign_tax_inr == D(2_670)   # $30
    assert result.total_creditable_inr == D("2225")   # only $25
    assert any("W-8BEN" in w for w in result.warnings)


# --------------------------------------------------------------------------
# Foreign tax credit
# --------------------------------------------------------------------------


def test_credit_is_capped_at_the_indian_tax_on_the_same_income():
    """Rule 128(2) — the excess is neither refunded nor carried forward."""
    tr = base_return()
    tr.foreign_settings.form67_filed = True
    from app.schemas import ForeignTaxPayment

    payments = [ForeignTaxPayment(
        income_inr=D(100_000), tax_paid_inr=D(25_000),
        nature_of_income="Dividend",
    )]
    result = compute_ftc(
        tr, payments, total_income=D(1_000_000),
        tax_before_credit=D(100_000),      # a 10% average rate
    )
    assert result.total_credit == D(10_000)
    assert result.total_forfeited == D(15_000)


def test_credit_never_exceeds_the_foreign_tax_actually_paid():
    tr = base_return()
    tr.foreign_settings.form67_filed = True
    from app.schemas import ForeignTaxPayment

    payments = [ForeignTaxPayment(income_inr=D(100_000), tax_paid_inr=D(5_000))]
    result = compute_ftc(
        tr, payments, total_income=D(1_000_000),
        tax_before_credit=D(300_000),      # a 30% average rate
    )
    assert result.total_credit == D(5_000)
    assert result.total_forfeited == D(0)


def test_missing_form_67_is_called_out():
    tr = base_return()
    tr.foreign_settings.form67_filed = False
    from app.schemas import ForeignTaxPayment

    result = compute_ftc(
        tr, [ForeignTaxPayment(income_inr=D(100_000), tax_paid_inr=D(25_000))],
        total_income=D(1_000_000), tax_before_credit=D(100_000),
    )
    assert any("Form 67" in w for w in result.warnings)


def test_the_credit_reduces_the_tax_actually_payable():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.foreign_settings.form67_filed = True
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10),
        gross_amount_fx=D(1_000), foreign_tax_withheld_fx=D(250),
    )]
    comp = compare_regimes(tr).new
    assert comp.relief_90_91 > 0
    assert comp.ftc is not None


# --------------------------------------------------------------------------
# Schedule FA
# --------------------------------------------------------------------------


def test_schedule_fa_reports_the_calendar_year_not_the_financial_year():
    start, end, year = calendar_period("2026-27")
    assert (start, end, year) == (date(2025, 1, 1), date(2025, 12, 31), "2025")


def test_an_acquisition_after_31_december_is_left_for_next_year():
    tr = base_return()
    tr.rsu_vests = [
        RSUVest(symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
                fmv_per_share_fx=D(150), included_in_form16=True),
        # This one is in FY 2025-26 but in calendar 2026.
        RSUVest(symbol="ACME", vest_date=date(2026, 2, 15), shares_vested=D(100),
                fmv_per_share_fx=D(160), included_in_form16=True),
    ]
    tr.foreign_holdings = [ForeignHolding(
        symbol="ACME", entity_name="Acme Inc", entity_address="1 Market St",
        entity_zip="94105", year_end_price_fx=D(160), peak_price_fx=D(190),
    )]
    _, foreign = apply_foreign(tr)
    row = next(r for r in foreign.schedule_fa.rows if r.table == "A3")
    # Only the September tranche: 100 × $160 × 90.00.
    assert row.closing_value == D(1_440_000)


def test_a_broker_account_produces_a_table_a2_row():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), included_in_form16=True,
    )]
    tr.foreign_holdings = [ForeignHolding(
        symbol="ACME", entity_name="Acme Inc", entity_address="1 Market St",
        entity_zip="94105", broker_name="E*TRADE",
        broker_account_number="X1234", year_end_price_fx=D(160),
    )]
    _, foreign = apply_foreign(tr)
    tables = {row.table for row in foreign.schedule_fa.rows}
    assert tables == {"A2", "A3"}


def test_a_holding_with_no_address_is_flagged():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), included_in_form16=True,
    )]
    _, foreign = apply_foreign(tr)
    assert any("address" in w for w in foreign.schedule_fa.warnings)


# --------------------------------------------------------------------------
# Form selection and reconciliation
# --------------------------------------------------------------------------


def test_holding_rsus_rules_out_itr1():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(1_200_000))]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(10),
        fmv_per_share_fx=D(150),
    )]
    decision = select_form(tr)
    assert decision.form == "ITR-2"
    assert any("Schedule FA" in reason for reason in decision.disqualifications)


def test_an_empty_schedule_fa_with_foreign_holdings_is_an_error():
    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(10),
        fmv_per_share_fx=D(150),
    )]
    report = reconcile(tr, [])
    assert any("Schedule FA" in f.title for f in report.errors)


def test_claiming_credit_without_form_67_is_an_error():
    tr = base_return()
    tr.foreign_settings.form67_filed = False
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(100),
        foreign_tax_withheld_fx=D(25),
    )]
    report = reconcile(tr, [])
    assert any("Form 67" in f.title for f in report.errors)


def test_rsu_vests_with_no_form16_perquisite_are_queried():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(150), included_in_form16=True,
    )]
    report = reconcile(tr, [])
    assert any("no perquisite" in f.title for f in report.warnings)


# --------------------------------------------------------------------------
# The ITR-2 JSON
# --------------------------------------------------------------------------


def test_itr2_json_carries_schedule_fa_and_the_credit_schedules():
    from app.itr import json_builder

    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", employer_tan="BLRA12345B",
                                salary_17_1=D(2_000_000))]
    tr.foreign_settings.form67_filed = True
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2023, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D(100), included_in_form16=True,
    )]
    tr.dividends = [DividendReceipt(
        symbol="ACME", pay_date=date(2025, 12, 10), gross_amount_fx=D(1_000),
        foreign_tax_withheld_fx=D(250),
    )]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 1, 20), shares=D(50),
        price_per_share_fx=D(180),
    )]
    tr.foreign_holdings = [ForeignHolding(
        symbol="ACME", entity_name="Acme Inc", entity_address="1 Market St",
        entity_zip="94105", broker_name="E*TRADE", year_end_price_fx=D(160),
    )]

    comparison = compare_regimes(tr)
    prepared = comparison.prepared
    itr2 = json_builder.build(prepared, comparison.chosen, "ITR-2")["ITR"]["ITR2"]

    assert "ScheduleFA" in itr2
    assert itr2["ScheduleFA"]["DetailsForeignEquityDebtInterest"]
    assert "ScheduleFSI" in itr2
    assert "ScheduleTR1" in itr2
    # The schema takes whole rupees, rounded rather than truncated.
    from app.money import rupees

    relief = itr2["PartB_TTI"]["ComputationOfTaxLiability"]["TaxRelief"]
    assert relief["Section90"] == int(rupees(comparison.chosen.relief_90_91))
    assert relief["Section90"] > 0


# --------------------------------------------------------------------------
# The US broker parsers
# --------------------------------------------------------------------------


def test_vest_export_is_recognised():
    from app.parsers.registry import parse_document

    csv = (
        "E*TRADE Benefit History\n\n"
        "Symbol,Grant Number,Vest Date,Shares Vested,Fair Market Value,"
        "Shares Sold to Cover,Sale Price\n"
        "ACME,G-1,2025-09-15,100,150.00,32,150.40\n"
    )
    extraction = parse_document(csv.encode(), "etrade_benefit_history.csv")
    assert extraction.document_type == "us_equity"
    vest = extraction.rsu_vests[0]
    assert vest["shares_vested"] == D(100)
    assert vest["fmv_per_share_fx"] == D("150.00")
    assert vest["shares_sold_to_cover"] == D(32)


def test_dividend_export_detects_reinvestment():
    from app.parsers.registry import parse_document

    csv = (
        "Fidelity 1099-DIV\n"
        "Symbol,Pay Date,Dividend Amount,Federal Income Tax Withheld,"
        "Shares Purchased,Reinvestment Price,Action\n"
        "ACME,2025-12-10,70.00,17.50,0.4516,155.00,Reinvest\n"
    )
    extraction = parse_document(csv.encode(), "fidelity_1099div.csv")
    dividend = extraction.dividends[0]
    assert dividend["is_reinvested"] is True
    assert dividend["gross_amount_fx"] == D("70.00")
    assert dividend["foreign_tax_withheld_fx"] == D("17.50")


def test_sale_export_derives_a_price_from_the_proceeds():
    from app.parsers.registry import parse_document

    csv = (
        "Schwab Realized Gain / Loss 1099-B\n"
        "Symbol,Date Acquired,Date Sold,Quantity Sold,Proceeds,Cost Basis,Commission\n"
        "ACME,2025-09-15,2026-01-20,40,7200.00,6000.00,5.00\n"
    )
    extraction = parse_document(csv.encode(), "schwab_1099b.csv")
    sale = extraction.foreign_sales[0]
    assert sale["shares"] == D(40)
    assert sale["price_per_share_fx"] == D(180)


def test_dollar_signs_do_not_break_the_amounts():
    from app.parsers.registry import parse_document

    csv = (
        "Morgan Stanley StockPlan Connect\n"
        "Symbol,Vest Date,Shares Vested,Fair Market Value\n"
        'ACME,09/15/2025,100,"$150.00"\n'
    )
    extraction = parse_document(csv.encode(), "stockplan_releases.csv")
    assert extraction.rsu_vests[0]["fmv_per_share_fx"] == D("150.00")


def test_an_indian_broker_file_is_not_mistaken_for_a_us_one():
    from app.parsers.registry import parse_document

    csv = (
        "Zerodha Tradewise\n\n"
        "Symbol,Quantity,Buy Date,Buy Value,Sell Date,Sell Value\n"
        "INFY,100,2021-06-10,500000,2025-09-15,825000\n"
    )
    extraction = parse_document(csv.encode(), "zerodha_pnl.csv")
    assert extraction.document_type == "broker_pnl"
    assert extraction.capital_gains[0]["category"] == "ltcg_112a"
