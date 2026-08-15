"""Quarterly vesting, advance tax, and the proviso to section 234C.

Expected values derived by hand from sections 208, 211 and 234C.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.foreign.pipeline import apply_foreign
from app.foreign.vesting import (
    add_months,
    expand_schedule,
    summarise_by_quarter,
    vests_in_year,
)
from app.itr import json_builder
from app.money import D
from app.planner import build_planner
from app.schemas import (
    CapitalGainItem,
    DividendReceipt,
    ForeignSale,
    RSUVest,
    SalaryIncome,
    TaxPayment,
    TaxReturn,
    VestingSchedule,
)
from app.tax.advance_tax import DeferrableItem, build_plan, section_234c
from app.tax.engine import compare_regimes, compute
from app.tax.rules import get_ay

AY = get_ay("2026-27")
RATES = {
    month: {"USD": "88.00"}
    for month in ("2025-05", "2025-08", "2025-11", "2025-12",
                  "2026-01", "2026-02", "2026-03")
}


def base_return(**kwargs) -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27", regime_choice="new",
                   filing_date=date(2026, 7, 25), **kwargs)
    tr.taxpayer.pan = "ABCDE1234F"
    tr.taxpayer.date_of_birth = date(1988, 5, 14)
    tr.foreign_settings.forex_overrides = dict(RATES)
    tr.foreign_settings.form67_filed = True
    return tr


# --------------------------------------------------------------------------
# Date arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start, months, expected",
    [
        (date(2025, 9, 15), 3, date(2025, 12, 15)),
        (date(2025, 12, 15), 3, date(2026, 3, 15)),
        # A grant vesting on the 31st clamps rather than rolling into March,
        # which would move it into a different quarter.
        (date(2025, 8, 31), 6, date(2026, 2, 28)),
        (date(2024, 8, 31), 6, date(2025, 2, 28)),
        (date(2025, 1, 31), 1, date(2025, 2, 28)),
    ],
)
def test_month_arithmetic_clamps_to_the_month_end(start, months, expected):
    assert add_months(start, months) == expected


# --------------------------------------------------------------------------
# Expanding a vesting schedule
# --------------------------------------------------------------------------


def quarterly_grant(**kwargs) -> VestingSchedule:
    defaults = dict(
        grant_id="G-1", symbol="ACME", grant_date=date(2024, 9, 15),
        total_shares=D(400), frequency="quarterly",
        first_vest_date=date(2025, 9, 15), tranches=16,
        cliff_shares=D(100), estimated_fmv_per_share_fx=D(170),
        sell_to_cover_fraction=D("0.31"),
    )
    defaults.update(kwargs)
    return VestingSchedule(**defaults)


def test_a_quarterly_grant_expands_to_one_vest_per_quarter():
    result = expand_schedule(quarterly_grant(), as_of=date(2026, 1, 1))
    assert len(result.vests) == 16
    dates = [v.vest_date for v in result.vests[:4]]
    assert dates == [
        date(2025, 9, 15), date(2025, 12, 15),
        date(2026, 3, 15), date(2026, 6, 15),
    ]


def test_the_tranches_add_back_to_the_grant_exactly():
    """Rounding must not lose or invent a share."""
    grant = quarterly_grant(total_shares=D(333), cliff_shares=D(83), tranches=13)
    result = expand_schedule(grant, as_of=date(2026, 1, 1))
    assert sum(v.shares_vested for v in result.vests) == D(333)


def test_a_cliff_vests_a_larger_first_tranche():
    result = expand_schedule(quarterly_grant(), as_of=date(2026, 1, 1))
    assert result.vests[0].shares_vested == D(100)
    assert result.vests[1].shares_vested == D(20)   # 300 over 15 tranches


def test_tranches_with_a_known_price_are_real_and_the_rest_projected():
    grant = quarterly_grant(actual_fmv={
        "2025-09-15": "150.00", "2025-12-15": "162.30",
    })
    result = expand_schedule(grant, as_of=date(2026, 1, 20))
    assert len(result.actual) == 2
    assert len(result.projected) == 14
    assert result.actual[0].fmv_per_share_fx == D("150.00")
    assert result.projected[0].fmv_per_share_fx == D(170)


def test_monthly_and_annual_frequencies():
    monthly = expand_schedule(
        quarterly_grant(frequency="monthly", tranches=3, cliff_shares=D(0)),
        as_of=date(2026, 1, 1),
    )
    assert [v.vest_date for v in monthly.vests] == [
        date(2025, 9, 15), date(2025, 10, 15), date(2025, 11, 15),
    ]
    annual = expand_schedule(
        quarterly_grant(frequency="annual", tranches=2, cliff_shares=D(0)),
        as_of=date(2026, 1, 1),
    )
    assert annual.vests[1].vest_date == date(2026, 9, 15)


def test_a_schedule_with_no_first_vest_date_is_reported_not_guessed():
    result = expand_schedule(quarterly_grant(first_vest_date=None))
    assert not result.vests
    assert result.warnings


# --------------------------------------------------------------------------
# Projections must never reach the return
# --------------------------------------------------------------------------


def test_only_this_years_tranches_are_brought_in():
    """A four-year grant runs to 2029; this return covers FY 2025-26."""
    tr = base_return()
    tr.vesting_schedules = [quarterly_grant(actual_fmv={
        "2025-09-15": "150.00", "2025-12-15": "162.30", "2026-03-15": "158.90",
    })]
    prepared, _ = apply_foreign(tr)
    assert all(
        AY.fy_start <= v.vest_date <= AY.fy_end for v in prepared.rsu_vests
    )
    assert len(prepared.rsu_vests) == 3


def test_a_projected_tranche_is_excluded_from_the_return():
    tr = base_return()
    tr.vesting_schedules = [quarterly_grant(actual_fmv={"2025-09-15": "150.00"})]
    prepared, foreign = apply_foreign(tr)
    assert all(not v.is_projected for v in prepared.rsu_vests)
    assert len(prepared.rsu_vests) == 1


def test_the_planner_does_include_projections():
    tr = base_return()
    tr.vesting_schedules = [quarterly_grant(actual_fmv={"2025-09-15": "150.00"})]
    prepared, foreign = apply_foreign(tr, include_projected=True)
    assert any(v.is_projected for v in prepared.rsu_vests)


def test_no_projected_figure_reaches_the_itr_json():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", employer_tan="BLRA12345B",
                                salary_17_1=D(2_000_000))]
    tr.vesting_schedules = [quarterly_grant(actual_fmv={"2025-09-15": "150.00"})]

    comparison = compare_regimes(tr)
    prepared = comparison.prepared
    itr2 = json_builder.build(prepared, comparison.chosen, "ITR-2")["ITR"]["ITR2"]

    # Only the one real tranche of 100 shares should be reflected anywhere.
    real_perquisite = D(100) * D(150) * D(88)
    assert prepared.salaries[0].perquisites_17_2 <= real_perquisite
    assert itr2["ScheduleS"]["TotalGrossSalary"] > 0


def test_a_hand_entered_vest_wins_over_the_generated_one():
    tr = base_return()
    tr.vesting_schedules = [quarterly_grant(actual_fmv={"2025-09-15": "150.00"})]
    tr.rsu_vests = [RSUVest(
        symbol="ACME", vest_date=date(2025, 9, 15), shares_vested=D(100),
        fmv_per_share_fx=D("149.55"),
    )]
    prepared, _ = apply_foreign(tr)
    matching = [v for v in prepared.rsu_vests if v.vest_date == date(2025, 9, 15)]
    assert len(matching) == 1
    assert matching[0].fmv_per_share_fx == D("149.55")


# --------------------------------------------------------------------------
# Bucketing by quarter
# --------------------------------------------------------------------------


def test_vests_land_in_the_right_instalment_window():
    vests = [
        RSUVest(symbol="A", vest_date=date(2025, 5, 1), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2025, 6, 15), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2025, 6, 16), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2025, 12, 15), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2025, 12, 16), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2026, 3, 31), shares_vested=D(1)),
    ]
    buckets = summarise_by_quarter(vests, AY.fy_start, AY.fy_end)
    assert len(buckets[1]) == 2       # up to and including 15 June
    assert len(buckets[2]) == 1
    assert len(buckets[3]) == 1
    assert len(buckets[4]) == 2


def test_vests_outside_the_year_are_not_bucketed():
    vests = [
        RSUVest(symbol="A", vest_date=date(2027, 6, 15), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2024, 6, 15), shares_vested=D(1)),
    ]
    buckets = summarise_by_quarter(vests, AY.fy_start, AY.fy_end)
    assert sum(len(v) for v in buckets.values()) == 0


def test_vests_in_year_filters_both_ends():
    vests = [
        RSUVest(symbol="A", vest_date=date(2025, 3, 31), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2025, 4, 1), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2026, 3, 31), shares_vested=D(1)),
        RSUVest(symbol="A", vest_date=date(2026, 4, 1), shares_vested=D(1)),
    ]
    assert len(vests_in_year(vests, AY.fy_start, AY.fy_end)) == 2


# --------------------------------------------------------------------------
# The proviso to section 234C
# --------------------------------------------------------------------------


def test_a_december_gain_is_not_charged_for_the_june_instalment():
    """The whole point of the proviso."""
    deferrable = [DeferrableItem(
        kind="capital_gains", label="Share sale", arising_on=date(2025, 12, 10),
        income=D(2_000_000), tax=D(400_000),
    )]
    plan = build_plan(
        AY, total_tax_liability=D(400_000), tds_and_tcs=D(0),
        deferrable=deferrable, payments=[], as_of=AY.fy_end,
    )
    first, second, third, fourth = plan.instalments
    assert first.total_required == D(0)
    assert second.total_required == D(0)
    # From the quarter it arose in, the whole of the tax is due.
    assert third.total_required == D(400_000)
    assert fourth.total_required == D(400_000)


def test_the_proviso_charges_the_full_tax_from_the_quarter_it_arose():
    interest, notes = section_234c(
        AY, D(400_000), [],
        [DeferrableItem("capital_gains", "Sale", date(2025, 12, 10),
                        D(2_000_000), D(400_000))],
    )
    # 4,00,000 short at 15 Dec for 3 months, and again at 15 Mar for 1 month.
    assert interest == D(4_000) * D(3) + D(4_000) * D(1)
    assert interest == D(16_000)


def test_paying_on_time_after_the_gain_costs_nothing():
    interest, _ = section_234c(
        AY, D(400_000),
        [(date(2025, 12, 15), D(400_000))],
        [DeferrableItem("capital_gains", "Sale", date(2025, 12, 10),
                        D(2_000_000), D(400_000))],
    )
    assert interest == D(0)


def test_regular_income_gets_no_such_relief():
    """Salary and interest still follow the plain 15/45/75/100 schedule."""
    plan = build_plan(
        AY, total_tax_liability=D(400_000), tds_and_tcs=D(0),
        deferrable=[], payments=[], as_of=AY.fy_end,
    )
    assert plan.instalments[0].total_required == D(60_000)     # 15%
    assert plan.instalments[1].total_required == D(180_000)    # 45%
    assert plan.instalments[2].total_required == D(300_000)    # 75%
    assert plan.instalments[3].total_required == D(400_000)


def test_a_gain_arising_after_15_march_is_left_out_of_every_instalment():
    plan = build_plan(
        AY, total_tax_liability=D(100_000), tds_and_tcs=D(0),
        deferrable=[DeferrableItem("capital_gains", "Late sale",
                                   date(2026, 3, 25), D(500_000), D(100_000))],
        payments=[], as_of=AY.fy_end,
    )
    assert all(i.deferrable_required == D(0) for i in plan.instalments)
    assert any("31 March" in note for note in plan.notes)


def test_dividends_get_the_same_relief_as_capital_gains():
    """Clause (d) of the proviso, as substituted by the Finance Act 2021."""
    plan = build_plan(
        AY, total_tax_liability=D(100_000), tds_and_tcs=D(0),
        deferrable=[DeferrableItem("dividend", "Dividend",
                                   date(2025, 12, 10), D(300_000), D(100_000))],
        payments=[], as_of=AY.fy_end,
    )
    assert plan.instalments[0].total_required == D(0)
    assert plan.instalments[2].total_required == D(100_000)


def test_the_engine_applies_the_proviso_to_a_real_return():
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme", employer_tan="BLRA12345B",
                                salary_17_1=D(2_000_000))]
    tr.taxes_paid.payments = [TaxPayment(
        kind="tds_salary", deductor_tan="BLRA12345B", amount=D(192_400))]
    tr.capital_gains = [CapitalGainItem(
        category="stcg_111a", description="Sale", sale_date=date(2025, 12, 10),
        sale_consideration=D(3_000_000), cost_of_acquisition=D(1_000_000))]

    comp = compute(tr, "new")
    assert comp.deferrable
    assert comp.deferrable[0].arising_on == date(2025, 12, 10)
    assert any("proviso" in note for note in comp.interest.notes)


def test_the_relief_reported_is_the_net_figure():
    """Netting matters: the proviso also makes the later instalment stricter."""
    plan = build_plan(
        AY, total_tax_liability=D(416_000), tds_and_tcs=D(0),
        deferrable=[DeferrableItem("capital_gains", "Sale",
                                   date(2025, 12, 10), D(2_000_000), D(416_000))],
        payments=[], as_of=AY.fy_end,
    )
    naive = D(1_497) + D(4_491) + D(9_360) + D(4_160)
    assert plan.interest_234c == D(16_640)
    assert plan.interest_waived_by_proviso == naive - D(16_640)


# --------------------------------------------------------------------------
# Liability thresholds
# --------------------------------------------------------------------------


def test_advance_tax_is_not_payable_below_10000():
    plan = build_plan(
        AY, total_tax_liability=D(9_000), tds_and_tcs=D(0),
        deferrable=[], payments=[], as_of=date(2025, 6, 1),
    )
    assert plan.liable is False
    assert "208" in plan.exemption_reason


def test_tds_reduces_the_advance_tax_base():
    plan = build_plan(
        AY, total_tax_liability=D(500_000), tds_and_tcs=D(450_000),
        deferrable=[], payments=[], as_of=date(2025, 6, 1),
    )
    assert plan.assessed_tax == D(50_000)
    assert plan.instalments[0].total_required == D(7_500)


def test_a_senior_citizen_without_business_income_is_exempt():
    plan = build_plan(
        AY, total_tax_liability=D(500_000), tds_and_tcs=D(0),
        deferrable=[], payments=[], as_of=date(2025, 6, 1),
        senior_without_business=True,
    )
    assert plan.liable is False
    assert "207(2)" in plan.exemption_reason


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------


def planner_return() -> TaxReturn:
    tr = base_return()
    tr.salaries = [SalaryIncome(employer_name="Acme India",
                                employer_tan="BLRA12345B",
                                salary_17_1=D(3_000_000))]
    tr.taxes_paid.payments = [TaxPayment(
        kind="tds_salary", deductor_tan="BLRA12345B", amount=D(430_000))]
    tr.vesting_schedules = [quarterly_grant(actual_fmv={
        "2025-09-15": "150.00", "2025-12-15": "162.30",
    })]
    tr.foreign_sales = [ForeignSale(
        symbol="ACME", sale_date=date(2025, 12, 20), shares=D(60),
        price_per_share_fx=D(175))]
    return tr


def test_the_planner_names_the_next_date_and_amount():
    result = build_planner(planner_return(), as_of=date(2025, 11, 20))
    assert result.next_due == date(2025, 12, 15)
    assert result.pay_now > 0


def test_the_planner_splits_regular_from_deferrable():
    result = build_planner(planner_return(), as_of=date(2025, 11, 20))
    assert result.plan.regular_tax > 0
    assert result.plan.deferrable_tax > 0
    assert (
        result.plan.regular_tax + result.plan.deferrable_tax
        == result.plan.assessed_tax
    )


def test_recording_a_payment_reduces_the_next_amount():
    tr = planner_return()
    before = build_planner(tr, as_of=date(2025, 11, 20))
    tr.taxes_paid.payments.append(TaxPayment(
        kind="advance_tax", amount=before.pay_now,
        payment_date=date(2025, 12, 10)))
    after = build_planner(tr, as_of=date(2026, 1, 5))
    assert after.pay_now < before.plan.assessed_tax
    assert after.next_due == date(2026, 3, 15)


def test_the_planner_flags_projected_value_separately():
    result = build_planner(planner_return(), as_of=date(2025, 11, 20))
    assert result.has_projections
    assert result.projected_vest_value > 0
    assert any("not vested yet" in w for w in result.warnings)


def test_every_quarter_is_shown_with_a_status():
    result = build_planner(planner_return(), as_of=date(2025, 11, 20))
    assert len(result.quarters) == 4
    statuses = [q.status for q in result.quarters]
    assert statuses[0] in ("met", "missed")
    assert "upcoming" in statuses or "due" in statuses


def test_the_quarter_view_explains_what_happened_in_it():
    result = build_planner(planner_return(), as_of=date(2025, 11, 20))
    events = [event for q in result.quarters for event in q.events]
    assert any("vested" in event for event in events)


def test_the_challan_names_the_right_minor_head():
    from app.planner import record_payment_hint

    result = build_planner(planner_return(), as_of=date(2025, 11, 20))
    challan = record_payment_hint(result.plan)
    assert "100" in challan["minor_head_advance"]
    assert challan["assessment_year"] == "2026-27"
    assert "BSR" in challan["after"]
