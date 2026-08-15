"""ITR-3, the form a trader files.

What these tests can check is that the figures inside the JSON are right and
reconcile with each other. What they cannot check is that the element names are
the ones the department's utility expects — no file this builder produces has
ever been imported. That is said in the module docstring too, and it is why the
filing pack still carries every figure in readable form.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.itr.json_builder import build, build_itr3, to_json_bytes
from app.itr.selector import select_form
from app.money import D
from app.schemas import (
    BankAccount,
    BroughtForwardLoss,
    CapitalGainItem,
    SalaryIncome,
    TaxReturn,
    TradingSegment,
)
from app.tax.engine import compute
from app.tax.rules import get_ay

AY = get_ay("2026-27")


def trader() -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27")
    tr.taxpayer.name = "Asha Ramanathan"
    tr.taxpayer.pan = "ABCDE1234F"
    tr.taxpayer.city = "Chennai"
    tr.taxpayer.state_code = "33"
    tr.taxpayer.pincode = "600001"
    tr.taxpayer.bank_accounts = [BankAccount(
        ifsc="ICIC0000001", bank_name="ICICI Bank",
        account_number="000000000001", is_primary_refund_account=True,
    )]
    tr.salaries = [SalaryIncome(
        employer_name="Acme", salary_17_1=D("3000000"), tds_deducted=D("500000")
    )]
    tr.trading_segments = [
        TradingSegment(segment="equity_fo", gross_profit=D("121000"),
                       turnover=D("499000"), brokerage=D("5100"),
                       securities_transaction_tax=D("6400"), gst=D("1370")),
        TradingSegment(segment="equity_intraday", gross_profit=D("-27000"),
                       turnover=D("63000"), brokerage=D("2100")),
    ]
    tr.capital_gains = [CapitalGainItem(
        category="ltcg_112a", sale_date=date(2025, 8, 12),
        purchase_date=date(2023, 5, 10),
        sale_consideration=D("172000"), cost_of_acquisition=D("140000"),
    )]
    return tr


def itr3(tr: TaxReturn, regime: str = "new") -> dict:
    comp = compute(tr, regime, AY)
    return build_itr3(tr, comp)["ITR"]["ITR3"]


# --------------------------------------------------------------------------


def test_a_trader_gets_itr3_and_it_now_builds():
    tr = trader()
    decision = select_form(tr)
    assert decision.form == "ITR-3"
    assert decision.supported is True

    comp = compute(tr, "new", AY)
    payload = build(tr, comp, "ITR-3")
    assert "ITR3" in payload["ITR"]
    assert to_json_bytes(payload)          # serialises without complaint


def test_every_schedule_a_trader_needs_is_present():
    present = set(itr3(trader()))
    for schedule in (
        "PartA_GEN1", "PartA_PL", "ScheduleS", "ScheduleBP", "ScheduleOI",
        "ScheduleCGFor23", "ScheduleOS", "ScheduleCYLA", "ScheduleCFL",
        "ScheduleVIA", "ScheduleSI", "PartB-TI", "PartB_TTI", "Verification",
    ):
        assert schedule in present, schedule


def test_schedule_bp_keeps_speculation_out_of_the_business_total():
    """Section 73 is the reason these cannot be one number. A speculative loss
    is reported and carried on its own line, never netted into the business
    figure — which is exactly what a broker summary invites you to do."""
    bp = itr3(trader())["ScheduleBP"]

    # F&O 1,21,000 less 5,100 + 6,400 + 1,370 of charges.
    assert bp["BusinessIncOthThanSpecAndSpecifiedBus"][
        "IncomeOthThanSpecAndSpecifiedBus"] == 108130
    assert bp["IncChargeableUnderProfessionOrBusiness"] == 108130

    # Intraday -27,000 less 2,100 of brokerage.
    assert bp["SpecBusinessInc"]["NetProfitLossFrmSpecBus"] == -29100
    assert bp["SpecBusinessInc"]["IncomeFrmSpecBus"] == 0
    assert bp["SpecBusinessInc"]["SpecBusLossCF"] == 29100

    # Turnover for the section 44AB test is both segments together.
    assert bp["TotTurnoverForAudit"] == 562000


def test_the_no_books_block_carries_turnover_and_expenses():
    """A trader running an F&O book off a broker statement keeps no books in
    the section 44AA sense, and the form has a short section for that."""
    pl = itr3(trader())["PartA_PL"]

    assert pl["NoAccountCase"] == {
        "GrossReceipts": 499000, "GrossProfit": 121000,
        "Expenses": 12870, "NetProfit": 108130,
    }
    assert pl["NoAccountCaseSpeculative"] == {
        "GrossReceipts": 63000, "GrossProfit": -27000,
        "Expenses": 2100, "NetProfit": -29100,
    }


def test_the_speculation_loss_reaches_schedule_cfl():
    cfl = itr3(trader())["ScheduleCFL"]
    assert cfl["LossCFFromCurrYr"]["TotalSpecLossCF"] == 29100
    assert cfl["LossCFFromCurrYr"]["TotalBusLossCF"] == 0


def test_brought_forward_losses_reach_schedule_cfl():
    tr = trader()
    tr.brought_forward_losses = [BroughtForwardLoss(
        assessment_year="2025-26", business_loss=D("400000"),
        speculative_loss=D("150000"),
    )]
    cfl = itr3(tr)["ScheduleCFL"]["TotalOfBFLossesEarlierYrs"]["LossSummaryDetail"]
    assert cfl["TotalBusLossCF"] == 400000
    assert cfl["TotalSpecLossCF"] == 150000


def test_schedule_oi_records_the_audit_answer():
    assert itr3(trader())["ScheduleOI"]["AuditRequired"] == "N"

    tr = trader()
    tr.trading_segments[0].turnover = D("150000000")     # ₹15 crore
    assert itr3(tr)["ScheduleOI"]["AuditRequired"] == "Y"


def test_part_b_ti_reconciles_with_the_computation():
    tr = trader()
    comp = compute(tr, "new", AY)
    ti = build_itr3(tr, comp)["ITR"]["ITR3"]["PartB-TI"]

    assert ti["Salaries"] == int(comp.salary)
    assert ti["ProfBusGain"]["TotProfBusGain"] == int(comp.business)
    assert ti["GrossTotalIncome"] == int(comp.gross_total_income)
    assert ti["TotalIncome"] == int(comp.total_income_rounded)


# --------------------------------------------------------------------------
# The due date an audit case actually has
# --------------------------------------------------------------------------


def test_an_audit_case_filed_in_september_is_not_belated():
    """139(1) allows 31 October where the accounts are audited. Measuring
    against the non-audit date declared a punctual return belated, which is a
    materially different thing to tell the department."""
    tr = trader()
    tr.filing_date = date(2026, 9, 15)
    tr.trading_segments[0].turnover = D("150000000")

    comp = compute(tr, "new", AY)
    status = build_itr3(tr, comp)["ITR"]["ITR3"]["PartA_GEN1"]["FilingStatus"]
    assert status["ReturnFileSec"] == 11          # 139(1), on time


def test_the_same_date_without_an_audit_is_belated():
    tr = trader()
    tr.filing_date = date(2026, 9, 15)
    comp = compute(tr, "new", AY)
    status = build_itr3(tr, comp)["ITR"]["ITR3"]["PartA_GEN1"]["FilingStatus"]
    assert status["ReturnFileSec"] == 12          # 139(4), belated


def test_the_old_regime_carries_the_form_10iea_acknowledgement():
    """With business income the opt-out is made on Form 10-IEA, and the
    acknowledgement belongs on the return."""
    tr = trader()
    tr.business.form_10iea_ack = "123456789012345"
    status = itr3(tr, "old")["PartA_GEN1"]["FilingStatus"]
    assert status["NewTaxRegime"] == "N"
    assert status["Form10IEAAckNo"] == "123456789012345"


# --------------------------------------------------------------------------
# The loss schedules ITR-2 was also missing
# --------------------------------------------------------------------------


def test_itr2_now_carries_the_loss_schedules_too():
    from app.itr.json_builder import build_itr2
    from app.schemas import HouseProperty

    tr = TaxReturn(assessment_year="2026-27")
    tr.taxpayer.name = "Asha Ramanathan"
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D("2000000"))]
    tr.house_properties = [HouseProperty(
        property_type="LOP", annual_rent_received=D("240000"),
        interest_24b=D("800000"),
    )]
    comp = compute(tr, "old", AY)
    itr2 = build_itr2(tr, comp)["ITR"]["ITR2"]

    assert "ScheduleCYLA" in itr2
    assert itr2["ScheduleCYLA"]["TotalLossSetOff"]["TotHPlossCurYrSetoff"] == 200000
    assert itr2["ScheduleCFL"]["LossCFFromCurrYr"]["HouseProperty"] == 432000
