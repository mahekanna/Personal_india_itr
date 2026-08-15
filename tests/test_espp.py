"""Employee Stock Purchase Plans.

The discount is salary under section 17(2)(vi); the cost basis on sale is the
fair market value already taxed, under section 49(2AA). Expected values derived
by hand from those two sections.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.foreign.espp import compute_espp, perquisite_to_add_to_salary
from app.foreign.forex import ForexTable
from app.foreign.pipeline import apply_foreign
from app.itr.selector import select_form
from app.money import D
from app.parsers.registry import parse_document
from app.reconcile import reconcile
from app.schemas import ESPPPurchase, ForeignSale, SalaryIncome, TaxReturn
from app.tax.engine import compare_regimes, compute

RATES = {
    month: {"USD": "88.00"}
    for month in ("2025-05", "2025-08", "2025-11", "2025-12",
                  "2026-01", "2026-02", "2026-03")
}


def base_return(**kwargs) -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27", regime_choice="new",
                   filing_date=date(2026, 7, 25), **kwargs)
    tr.taxpayer.pan = "ABCDE1234F"
    tr.taxpayer.residential_status = "RES"
    tr.foreign_settings.forex_overrides = dict(RATES)
    tr.foreign_settings.form67_filed = True
    return tr


def lookback_purchase(**kwargs) -> ESPPPurchase:
    """A standard 15% plan with a lookback, after the price rose.

    Paid 85% of the $150 offering-start price; the shares were worth $200 on
    the purchase date. The real discount is 36.25%, not 15%.
    """
    defaults = dict(
        symbol="ACME", offering_start_date=date(2025, 6, 1),
        purchase_date=date(2025, 12, 1), shares_purchased=D(100),
        fmv_per_share_fx=D(200), price_paid_per_share_fx=D("127.50"),
        offering_price_fx=D(150), contributions_fx=D(12_750),
        included_in_form16=True,
    )
    defaults.update(kwargs)
    return ESPPPurchase(**defaults)


# --------------------------------------------------------------------------
# The perquisite
# --------------------------------------------------------------------------


def test_the_discount_is_fmv_less_what_you_paid():
    purchase = lookback_purchase()
    assert purchase.discount_per_share_fx == D("72.50")
    assert purchase.total_discount_fx == D(7_250)


def test_the_perquisite_is_converted_at_the_month_before_purchase():
    """Rule 115 — a December purchase uses the 30 November rate."""
    tr = base_return()
    tr.espp_purchases = [lookback_purchase()]
    result = compute_espp(tr, ForexTable(RATES))
    assert result.total_perquisite_inr == D(7_250) * D(88)
    assert result.total_perquisite_inr == D(638_000)


def test_the_lookback_produces_far_more_than_the_headline_discount():
    purchase = lookback_purchase()
    # 72.50 / 200 — a headline 15% plan delivering 36.25%, because the price
    # rose over the offering period and the lookback priced off the low.
    assert purchase.discount_fraction == D("0.3625")


def test_buying_above_market_creates_no_perquisite():
    """A negative discount is not a deduction."""
    tr = base_return()
    tr.espp_purchases = [lookback_purchase(
        fmv_per_share_fx=D(100), price_paid_per_share_fx=D(120))]
    result = compute_espp(tr, ForexTable(RATES))
    assert result.total_perquisite_inr == D(0)
    assert any("above the fair market value" in w for w in result.warnings)


def test_a_purchase_already_in_form16_is_not_added_to_salary_again():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000),
                                perquisites_17_2=D(638_000))]
    tr.espp_purchases = [lookback_purchase(included_in_form16=True)]
    prepared, foreign = apply_foreign(tr)
    assert foreign.perquisite_added_to_salary == D(0)
    assert prepared.salaries[0].perquisites_17_2 == D(638_000)


def test_a_purchase_missing_from_form16_is_added():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.espp_purchases = [lookback_purchase(included_in_form16=False)]
    prepared, foreign = apply_foreign(tr)
    assert foreign.perquisite_added_to_salary == D(638_000)
    assert prepared.salaries[0].perquisites_17_2 == D(638_000)


def test_contributions_are_not_deductible():
    """They came out of salary that was already taxed."""
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.espp_purchases = [lookback_purchase(
        included_in_form16=False, contributions_fx=D(12_750))]
    comp = compare_regimes(tr).new
    # Gross salary rises by the perquisite alone; nothing is netted off for the
    # ₹11,22,000 of contributions.
    assert comp.salary == D(2_000_000) + D(638_000) - D(75_000)


def test_espp_and_rsu_perquisites_add_together():
    from app.schemas import RSUVest

    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.espp_purchases = [lookback_purchase(included_in_form16=False)]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 12, 1), shares_vested=D(10),
        fmv_per_share_fx=D(200), included_in_form16=False)]
    _, foreign = apply_foreign(tr)
    assert foreign.perquisite_added_to_salary == D(638_000) + D(176_000)


# --------------------------------------------------------------------------
# The cost basis — section 49(2AA)
# --------------------------------------------------------------------------


def test_the_cost_basis_is_the_fair_market_value_not_the_price_paid():
    tr = base_return()
    tr.espp_purchases = [lookback_purchase()]
    result = compute_espp(tr, ForexTable(RATES))
    item = result.results[0]
    assert item.cost_basis_inr == D(200) * D(100) * D(88)      # ₹17,60,000
    assert item.amount_paid_inr == D("127.50") * D(100) * D(88)  # ₹11,22,000
    assert item.cost_basis_inr > item.amount_paid_inr


def test_the_lot_carries_the_fair_market_value_basis():
    tr = base_return()
    tr.espp_purchases = [lookback_purchase()]
    _, foreign = apply_foreign(tr)
    lot = next(l for l in foreign.lots if l.origin == "espp")
    assert lot.shares == D(100)
    assert lot.cost_inr == D(1_760_000)
    assert lot.acquired == date(2025, 12, 1)


def test_selling_uses_the_fair_market_value_basis():
    """The whole point: the discount must not be taxed a second time."""
    tr = base_return()
    tr.espp_purchases = [lookback_purchase()]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 2, 10), shares=D(100),
        price_per_share_fx=D(210))]
    _, foreign = apply_foreign(tr)

    item = foreign.capital_gain_items[0]
    assert item.lot_origin == "espp"
    assert item.sale_consideration == D(210) * D(100) * D(88)   # ₹18,48,000
    assert item.cost_of_acquisition == D(1_760_000)
    assert item.net_gain == D(88_000)

    # Using the price actually paid would have inflated the gain by exactly the
    # perquisite already taxed as salary.
    wrong_gain = item.sale_consideration - D(1_122_000)
    assert wrong_gain - item.net_gain == D(638_000)


def test_an_espp_lot_turns_long_term_at_24_months_from_purchase():
    tr = base_return()
    tr.foreign_settings.forex_overrides["2023-11"] = {"USD": "83.00"}
    tr.espp_purchases = [lookback_purchase(
        purchase_date=date(2023, 12, 1), offering_start_date=date(2023, 6, 1))]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 2, 10), shares=D(100),
        price_per_share_fx=D(210))]
    _, foreign = apply_foreign(tr)
    assert foreign.capital_gain_items[0].category == "ltcg_112_foreign"


def test_an_espp_lot_sold_inside_24_months_is_at_slab_rates():
    tr = base_return()
    tr.espp_purchases = [lookback_purchase()]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 2, 10), shares=D(100),
        price_per_share_fx=D(210))]
    _, foreign = apply_foreign(tr)
    assert foreign.capital_gain_items[0].category == "stcg_slab_foreign"


def test_espp_and_rsu_lots_are_matched_together_fifo():
    from app.schemas import RSUVest

    tr = base_return()
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(50),
        fmv_per_share_fx=D(150), included_in_form16=True)]
    tr.espp_purchases = [lookback_purchase(shares_purchased=D(50))]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2026, 2, 10), shares=D(80),
        price_per_share_fx=D(210))]
    _, foreign = apply_foreign(tr)

    assert len(foreign.capital_gain_items) == 2
    origins = [item.lot_origin for item in foreign.capital_gain_items]
    # The September vest is older, so it goes first.
    assert origins == ["rsu_vest", "espp"]


# --------------------------------------------------------------------------
# Projections and form selection
# --------------------------------------------------------------------------


def test_a_projected_purchase_stays_out_of_the_return():
    tr = base_return()
    tr.espp_purchases = [lookback_purchase(is_projected=True)]
    prepared, _ = apply_foreign(tr)
    assert prepared.espp_purchases == []


def test_the_planner_does_include_a_projected_purchase():
    tr = base_return()
    tr.espp_purchases = [lookback_purchase(is_projected=True)]
    prepared, _ = apply_foreign(tr, include_projected=True)
    assert len(prepared.espp_purchases) == 1


def test_holding_espp_shares_rules_out_itr1():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(1_200_000))]
    tr.espp_purchases = [lookback_purchase()]
    decision = select_form(tr)
    assert decision.form == "ITR-2"


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_a_purchase_with_no_fair_market_value_is_an_error():
    tr = base_return()
    tr.espp_purchases = [lookback_purchase(fmv_per_share_fx=D(0))]
    report = reconcile(tr, [])
    assert any("no fair market value" in f.title for f in report.errors)


def test_a_discount_with_no_form16_perquisite_is_queried():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(2_000_000))]
    tr.espp_purchases = [lookback_purchase(included_in_form16=True)]
    report = reconcile(tr, [])
    assert any("ESPP" in f.title for f in report.warnings)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


ESPP_CSV = (
    "E*TRADE - Employee Stock Purchase Plan - Purchase Confirmation\n\n"
    "Symbol,Offering Date,Purchase Date,Qty. Purchased,Purchase Price,"
    "Market Value Per Share On Purchase Date,Grant Date Market Value,Contributions\n"
    "ACME,2025-06-01,2025-12-01,100,127.50,200.00,150.00,12750.00\n"
)


def test_an_espp_confirmation_is_recognised():
    extraction = parse_document(ESPP_CSV.encode(), "etrade_espp_purchase.csv")
    assert extraction.document_type == "us_equity"
    purchase = extraction.espp_purchases[0]
    assert purchase["shares_purchased"] == D(100)
    assert purchase["price_paid_per_share_fx"] == D("127.50")
    assert purchase["fmv_per_share_fx"] == D("200.00")
    assert purchase["offering_price_fx"] == D("150.00")
    assert purchase["purchase_date"] == date(2025, 12, 1)


def test_an_espp_file_produces_no_vest_rows():
    extraction = parse_document(ESPP_CSV.encode(), "etrade_espp_purchase.csv")
    assert extraction.rsu_vests == []
    assert extraction.foreign_sales == []


def test_a_vesting_file_is_still_read_as_vests_not_espp():
    csv = (
        "E*TRADE Benefit History\n"
        "Symbol,Grant Number,Vest Date,Shares Vested,Fair Market Value,"
        "Shares Sold to Cover,Sale Price\n"
        "ACME,G-1,2025-09-15,100,150.00,32,150.40\n"
    )
    extraction = parse_document(csv.encode(), "etrade_benefit_history.csv")
    assert len(extraction.rsu_vests) == 1
    assert extraction.espp_purchases == []


def test_the_parser_warns_about_the_basis():
    extraction = parse_document(ESPP_CSV.encode(), "etrade_espp_purchase.csv")
    assert any("cost basis" in w for w in extraction.warnings)


def test_the_same_purchase_imported_twice_is_not_doubled():
    from app.merge import apply_extractions

    tr = TaxReturn(assessment_year="2026-27")
    for _ in range(2):
        extraction = parse_document(ESPP_CSV.encode(), "espp.csv")
        apply_extractions(tr, [extraction])
    assert len(tr.espp_purchases) == 1
