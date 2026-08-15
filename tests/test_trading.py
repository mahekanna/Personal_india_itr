"""Trading income: three segments of one demat account, three heads of income.

The expected figures are worked out by hand from the sections cited. The
recurring theme is that the segments are *not* interchangeable — merging them,
which every broker summary invites, either wastes a loss or claims one the
statute does not allow.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.money import D
from app.schemas import (
    BroughtForwardLoss,
    CapitalGainItem,
    HouseProperty,
    SalaryIncome,
    TaxReturn,
    TradingSegment,
)
from app.tax.engine import compute
from app.tax.rules import get_ay
from app.tax.trading import (
    AUDIT_LIMIT_CASH,
    AUDIT_LIMIT_DIGITAL,
    audit_required,
    turnover_from_trades,
)

AY = get_ay("2026-27")


def salaried(gross: str = "3000000") -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27")
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(gross))]
    return tr


# --------------------------------------------------------------------------
# Turnover — ICAI Guidance Note on Tax Audit, Revised 2023, para 5.10
# --------------------------------------------------------------------------


def test_turnover_is_the_absolute_value_of_every_trade():
    """A profit and an equal loss make turnover, not nil."""
    assert turnover_from_trades([D(10_000), D(-10_000)]) == D(20_000)
    assert turnover_from_trades([D(50_000), D(-20_000), D(5_000)]) == D(75_000)


def test_option_sell_premium_is_added_only_when_asked_for():
    """The eighth edition dropped full sale consideration from the
    computation. The premium goes in only where the trade-by-trade profit has
    not already absorbed it — which a broker's tax P&L always has."""
    assert turnover_from_trades([D(10_000)]) == D(10_000)
    assert turnover_from_trades([D(10_000)], option_sell_premium=D(400_000)) \
        == D(410_000)


# --------------------------------------------------------------------------
# Section 44AB
# --------------------------------------------------------------------------


def test_the_audit_threshold_is_ten_crore_for_a_banked_account():
    """The proviso to section 44AB(a) raises it from ₹1 crore to ₹10 crore
    where cash is under 5% either way — which a broking account always is.
    The ₹1 crore figure is the one most commentary still quotes."""
    assert AUDIT_LIMIT_DIGITAL == D("100000000")
    assert AUDIT_LIMIT_CASH == D("10000000")

    required, _ = audit_required(D("50000000"))          # ₹5 crore
    assert required is False
    required, reason = audit_required(D("120000000"))    # ₹12 crore
    assert required is True
    assert "44AB(a)" in reason


def test_cash_above_five_percent_drops_the_threshold_to_one_crore():
    required, reason = audit_required(
        D("50000000"), cash_receipts_fraction=D("0.10")
    )
    assert required is True
    assert "cash" in reason


def test_an_audit_moves_the_due_date_to_31_october():
    """It was hardcoded to False, which charged a trader a late-filing fee for
    filing on a date that was not late."""
    tr = salaried()
    tr.filing_date = date(2026, 9, 15)      # after 31 July, before 31 October
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("5000000"),
        turnover=D("150000000"),            # ₹15 crore
    )]
    comp = compute(tr, "new", AY)

    assert comp.audit_required is True
    assert comp.interest.section_234f == D(0)
    assert comp.interest.section_234a == D(0)


def test_without_an_audit_the_same_date_is_late():
    tr = salaried()
    tr.filing_date = date(2026, 9, 15)
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("5000000"), turnover=D("9000000"),
    )]
    comp = compute(tr, "new", AY)

    assert comp.audit_required is False
    assert comp.interest.section_234f == D(5_000)


# --------------------------------------------------------------------------
# F&O is business income, not capital gains, and not speculative
# --------------------------------------------------------------------------


def test_fo_profit_is_business_income_at_slab_rates():
    """Clause (d) of the proviso to section 43(5) takes derivatives on a
    recognised exchange out of the definition of a speculative transaction."""
    tr = salaried("0")
    tr.salaries = []
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("1000000"), turnover=D("4000000"),
    )]
    comp = compute(tr, "new", AY)

    assert comp.business == D("1000000")
    assert comp.capital_gains == D(0)
    assert comp.special_slices == []          # nothing at a concessional rate
    assert comp.normal_income == D("1000000")


@pytest.mark.parametrize(
    "segment", ["equity_fo", "currency_fo", "commodity_fo"]
)
def test_every_derivative_segment_is_non_speculative(segment):
    tr = salaried("0")
    tr.salaries = []
    tr.trading_segments = [TradingSegment(
        segment=segment, gross_profit=D("-500000"), turnover=D("2000000"),
    )]
    comp = compute(tr, "new", AY)

    # A non-speculative loss is carried under section 72, not section 73.
    assert comp.carried_forward.get("business") == D("500000")
    assert "speculative_business" not in comp.carried_forward


def test_trading_expenses_are_deductible():
    """The compensation for losing the concessional capital-gains rate."""
    tr = salaried("0")
    tr.salaries = []
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("1000000"), turnover=D("4000000"),
        brokerage=D("40000"), exchange_transaction_charges=D("8000"),
        securities_transaction_tax=D("25000"), gst=D("9000"),
        stamp_duty=D("3000"), other_expenses=D("15000"),
    )]
    comp = compute(tr, "new", AY)
    assert comp.business == D("900000")       # 10,00,000 - 1,00,000


# --------------------------------------------------------------------------
# Section 73: speculation stays in its own ring
# --------------------------------------------------------------------------


def test_an_intraday_loss_does_not_reduce_fo_profit():
    """Section 73 permits a speculation loss against speculative income only.
    Netting it against F&O — which every broker summary invites — claims a
    set-off the statute refuses."""
    tr = salaried()
    tr.trading_segments = [
        TradingSegment(segment="equity_intraday", gross_profit=D("-200000"),
                       turnover=D("500000")),
        TradingSegment(segment="equity_fo", gross_profit=D("300000"),
                       turnover=D("2000000")),
    ]
    comp = compute(tr, "new", AY)

    assert comp.business == D("300000")
    assert comp.carried_forward["speculative_business"] == D("200000")


def test_an_intraday_profit_is_ordinary_business_income():
    tr = salaried("0")
    tr.salaries = []
    tr.trading_segments = [TradingSegment(
        segment="equity_intraday", gross_profit=D("400000"), turnover=D("900000"),
    )]
    comp = compute(tr, "new", AY)
    assert comp.business == D("400000")
    assert not comp.carried_forward


def test_a_brought_forward_speculation_loss_meets_only_speculative_income():
    tr = salaried("0")
    tr.salaries = []
    tr.trading_segments = [
        TradingSegment(segment="equity_intraday", gross_profit=D("150000"),
                       turnover=D("400000")),
        TradingSegment(segment="equity_fo", gross_profit=D("500000"),
                       turnover=D("2000000")),
    ]
    tr.brought_forward_losses = [BroughtForwardLoss(
        assessment_year="2025-26", speculative_loss=D("400000")
    )]
    comp = compute(tr, "new", AY)

    # Only the 1,50,000 of speculative income may be sheltered. The 5,00,000
    # of F&O profit is out of reach.
    assert comp.business == D("500000")
    assert comp.carried_forward["speculative_brought_forward"] == D("250000")


def test_a_brought_forward_speculation_loss_with_no_speculative_income_says_so():
    tr = salaried("0")
    tr.salaries = []
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("500000"), turnover=D("2000000"),
    )]
    tr.brought_forward_losses = [BroughtForwardLoss(
        assessment_year="2025-26", speculative_loss=D("400000")
    )]
    comp = compute(tr, "new", AY)

    assert comp.business == D("500000")
    assert any("no speculative income" in w for w in comp.warnings)


# --------------------------------------------------------------------------
# Section 71(2A): never against salary
# --------------------------------------------------------------------------


def test_an_fo_loss_meets_capital_gains_but_not_salary():
    """The case this system exists for: RSU gains alongside an F&O year in the
    red. The loss must eat the gain, not the salary the RSU vested into."""
    tr = salaried("3000000")
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("-800000"), turnover=D("4500000"),
        brokerage=D("50000"),
    )]
    tr.capital_gains = [CapitalGainItem(
        category="ltcg_112_foreign", sale_date=date(2025, 9, 1),
        sale_consideration=D("1000000"), cost_of_acquisition=D("400000"),
    )]
    comp = compute(tr, "new", AY)

    # Salary 30,00,000 less the 75,000 standard deduction stands untouched.
    assert comp.salary == D("2925000")
    # The whole 6,00,000 gain is absorbed, so no 12.5% slice survives.
    assert comp.capital_gains == D(0)
    assert comp.special_slices == []
    assert comp.loss_set_off["business"] == D("600000")
    # 8,50,000 of loss less the 6,00,000 absorbed.
    assert comp.carried_forward["business"] == D("250000")
    assert comp.gross_total_income == D("2925000")

    # Slab tax on 29,25,000 under the new regime:
    #   4-8L @5% 20,000 | 8-12L @10% 40,000 | 12-16L @15% 60,000
    #   16-20L @20% 80,000 | 20-24L @25% 1,00,000 | 24-29.25L @30% 1,57,500
    assert comp.tax_before_rebate == D("457500")
    assert comp.total_tax_liability == D("475800")     # plus 4% cess


def test_the_loss_is_spent_on_slab_income_before_a_concessional_gain():
    """Sheltering a 12.5% gain while leaving 30% income exposed would be
    giving relief away. Other sources goes first."""
    tr = salaried("0")
    tr.salaries = []
    tr.other_sources.fixed_deposit_interest = D("500000")
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("-300000"), turnover=D("1500000"),
    )]
    tr.capital_gains = [CapitalGainItem(
        category="ltcg_112_foreign", sale_date=date(2025, 9, 1),
        sale_consideration=D("900000"), cost_of_acquisition=D("400000"),
    )]
    comp = compute(tr, "new", AY)

    assert comp.other_sources == D("200000")          # 5,00,000 - 3,00,000
    assert comp.capital_gains == D("500000")          # gain untouched
    assert [s.code for s in comp.special_slices] == ["ltcg_112_foreign"]


def test_a_house_property_loss_also_reduces_the_right_income():
    """Same defect, same fix: netting into the total left the concessional
    slice standing and took the relief out of slab-rate income instead."""
    tr = salaried("0")
    tr.salaries = []
    tr.house_properties = [HouseProperty(
        property_type="LOP", annual_rent_received=D(0), interest_24b=D("200000"),
    )]
    tr.capital_gains = [CapitalGainItem(
        category="ltcg_112_foreign", sale_date=date(2025, 9, 1),
        sale_consideration=D("1000000"), cost_of_acquisition=D("400000"),
    )]
    comp = compute(tr, "old", AY)

    assert comp.loss_set_off["house_property"] == D("200000")
    assert comp.capital_gains == D("400000")          # 6,00,000 - 2,00,000
    slice_ = comp.special_slices[0]
    assert slice_.code == "ltcg_112_foreign"
    assert slice_.income == D("400000")


# --------------------------------------------------------------------------
# Regime mechanics for someone with business income
# --------------------------------------------------------------------------


def test_the_old_regime_with_business_income_warns_about_form_10iea():
    """A salaried taxpayer chooses afresh each year on the return. Someone
    with business income files Form 10-IEA, and section 115BAC(6) lets them
    opt out only once."""
    tr = salaried("800000")
    tr.deductions.s80c = D("150000")
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("400000"), turnover=D("2000000"),
    )]
    comp = compute(tr, "old", AY)
    assert any("10-IEA" in w for w in comp.warnings)
    assert any("115BAC(6)" in w for w in comp.warnings)


def test_the_new_regime_needs_no_such_warning():
    tr = salaried("800000")
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("400000"), turnover=D("2000000"),
    )]
    comp = compute(tr, "new", AY)
    assert not any("10-IEA" in w for w in comp.warnings)


def test_trading_income_alone_makes_this_a_business_return():
    """It drives the due date, the form and the advance-tax rules, whether or
    not the taxpayer ticked the 'I have business income' box."""
    tr = salaried()
    assert tr.has_business_income is False
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("400000"), turnover=D("2000000"),
    )]
    comp = compute(tr, "new", AY)
    assert comp.trading is not None and comp.trading.has_anything
    assert any("ITR-3" in w for w in comp.warnings)


# --------------------------------------------------------------------------
# Form selection
# --------------------------------------------------------------------------


def test_any_trading_segment_forces_itr3():
    """It returned ITR-1 for an F&O trader, which is a defective return under
    section 139(9)."""
    from app.itr.selector import select_form

    tr = salaried()
    tr.trading_segments = [TradingSegment(
        segment="equity_fo", gross_profit=D("400000"), turnover=D("2000000"),
    )]
    decision = select_form(tr)

    assert decision.form == "ITR-3"
    assert decision.supported is True
    assert any("43(5)" in reason for reason in decision.reasons)


def test_delivery_equity_alone_still_allows_the_simpler_forms():
    """Delivery is capital gains and must not be dragged into ITR-3."""
    from app.itr.selector import select_form

    tr = salaried("1200000")
    tr.capital_gains = [CapitalGainItem(
        category="ltcg_112a", sale_date=date(2025, 9, 1),
        sale_consideration=D("200000"), cost_of_acquisition=D("150000"),
    )]
    assert select_form(tr).form == "ITR-1"
