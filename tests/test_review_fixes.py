"""Defects found by reading the code against the statute, each pinned here.

Every expected figure below was worked out by hand from the section it cites,
not read off the implementation. Where a number is long, the arithmetic is in
the test so the next person can check it without rebuilding the derivation.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.foreign.dividends import expand_schedules
from app.foreign.rsu import Lot, classify_foreign_gain
from app.foreign.vesting import expand_schedule
from app.itr.selector import select_form
from app.money import D
from app.schemas import (
    BroughtForwardLoss,
    CapitalGainItem,
    DividendReceipt,
    DividendSchedule,
    HouseProperty,
    SalaryIncome,
    TaxReturn,
    VestingSchedule,
)
from app.tax.engine import compute
from app.tax.rules import get_ay

AY = get_ay("2026-27")


def salaried(gross: str, ay: str = "2026-27") -> TaxReturn:
    tr = TaxReturn(assessment_year=ay)
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(gross))]
    return tr


# --------------------------------------------------------------------------
# Surcharge: the 15% cap covers dividends, and does not cover winnings
# --------------------------------------------------------------------------


def test_surcharge_on_dividend_income_is_capped_at_fifteen_percent():
    """The proviso to Paragraph A of Part I of the First Schedule caps
    surcharge at 15% on "income by way of dividend" as well as on sections
    111A, 112 and 112A. Dividends are taxed at slab rates, so the cap has to
    be applied to a slice of the slab-rate tax — which is why it was missed."""
    tr = salaried("23000000")
    tr.other_sources.dividend_income = D("2000000")
    comp = compute(tr, "new", AY)

    # Total income 2,49,25,000 puts this in the 25% band.
    assert comp.total_income == D("24925000")
    assert comp.dividend_in_normal_income == D("2000000")

    # The dividend is the top slice of slab-rate income, so it bears the 30%
    # marginal rate: 20,00,000 x 30% = 6,00,000 of the 70,57,500 total.
    dividend_tax = D("600000")
    assert comp.tax_before_rebate == D("7057500")
    expected = (comp.tax_before_rebate - dividend_tax) * D("0.25") \
        + dividend_tax * D("0.15")
    assert comp.surcharge == expected
    # 60,000 less than charging 25% on the whole of it.
    assert comp.tax_before_rebate * D("0.25") - comp.surcharge == D("60000")


def test_winnings_do_not_get_the_fifteen_percent_surcharge_cap():
    """Section 115BB is not on the list the proviso protects, so winnings bear
    the full 25%. Treating them as capped understated the surcharge."""
    tr = salaried("23000000")
    tr.other_sources.winnings_115bb = D("2000000")
    comp = compute(tr, "new", AY)

    assert comp.total_income == D("24925000")
    assert comp.surcharge == comp.tax_before_rebate * D("0.25")


def test_the_cap_is_inert_below_the_two_crore_threshold():
    """Below ₹2 crore the band rate is already 15% or less, so the split must
    make no difference at all."""
    tr = salaried("8000000")
    tr.other_sources.dividend_income = D("500000")
    comp = compute(tr, "new", AY)
    assert comp.surcharge == comp.tax_after_rebate * D("0.10")


# --------------------------------------------------------------------------
# Section 234C: income with no date must not be waived
# --------------------------------------------------------------------------


def _gain_return(sale_date) -> TaxReturn:
    tr = salaried("1500000")
    tr.filing_date = date(2026, 7, 1)
    tr.capital_gains = [CapitalGainItem(
        category="ltcg_112a", sale_date=sale_date,
        sale_consideration=D("5000000"), cost_of_acquisition=D("1000000"),
    )]
    return tr


def test_an_undated_capital_gain_follows_the_ordinary_instalments():
    """The proviso to section 234C turns on when the income arose. With no
    date it cannot be applied — and an item carrying no date used to be read
    as "arose after 15 March" and waived in full."""
    comp = compute(_gain_return(None), "new", AY)

    # Assessed tax 6,61,375. Nothing was paid, so each instalment is short by
    # its full cumulative fraction, charged for the months the section sets:
    #   15% x 6,61,375 = 99,206 -> 99,200 x 1% x 3 =  2,976
    #   45%            = 2,97,618 -> 2,97,600 x 1% x 3 =  8,928
    #   75%            = 4,96,031 -> 4,96,000 x 1% x 3 = 14,880
    #  100%            = 6,61,375 -> 6,61,300 x 1% x 1 =  6,613
    assert comp.total_tax_liability == D("661375")
    assert comp.interest.section_234c == D(2976 + 8928 + 14880 + 6613)
    assert comp.deferrable == []


def test_a_dated_gain_after_15_march_still_gets_the_relief():
    """The relief itself must survive the fix — only the undated case changes."""
    comp = compute(_gain_return(date(2026, 3, 20)), "new", AY)
    assert len(comp.deferrable) == 1
    # Regular tax is 1,07,250, and only that follows the schedule:
    #   480 + 1,446 + 2,412 + 1,072
    assert comp.interest.section_234c == D(480 + 1446 + 2412 + 1072)
    assert comp.interest.section_234c < D("33397")


def test_an_undated_dividend_receipt_is_not_apportioned_income():
    """compute_dividends skips a receipt with no pay date, so counting it in
    the section 234C attribution shared the converted total against income
    that never entered the return."""
    tr = salaried("1500000")
    tr.dividends = [
        DividendReceipt(symbol="ACME", currency="USD", pay_date=date(2025, 8, 20),
                        gross_amount_fx=D("1000"), forex_rate_override=D("85")),
        DividendReceipt(symbol="ACME", currency="USD", pay_date=None,
                        gross_amount_fx=D("9000"), forex_rate_override=D("85")),
    ]
    comp = compute(tr, "new", AY)
    dividends = [item for item in comp.deferrable if item.kind == "dividend"]
    assert len(dividends) == 1
    # The dated receipt is the whole of the foreign dividend income, not a
    # tenth of it.
    assert dividends[0].income == D("85000")


# --------------------------------------------------------------------------
# Chapter VI-A
# --------------------------------------------------------------------------


def test_80dd_is_a_flat_deduction_not_a_reimbursement():
    """Section 80DD allows ₹75,000 "in respect of" the expenditure. Someone
    who spent ₹10,000 still gets the whole ₹75,000."""
    tr = salaried("1500000")
    tr.deductions.s80dd = D("10000")
    comp = compute(tr, "old", AY)
    line = next(l for l in comp.deduction_detail.lines if l.section == "80DD")
    assert line.claimed == D("10000")
    assert line.allowed == D("75000")


def test_80u_severe_disability_is_flat_too():
    tr = salaried("1500000")
    tr.deductions.s80u = D("5000")
    tr.deductions.s80u_severe = True
    comp = compute(tr, "old", AY)
    line = next(l for l in comp.deduction_detail.lines if l.section == "80U")
    assert line.allowed == D("125000")


def test_80g_qualifying_limit_runs_on_income_net_of_other_deductions():
    """"Adjusted gross total income" is gross total income less the other
    Chapter VI-A deductions. Measuring the 10% against gross overstates it."""
    tr = salaried("1050000")
    tr.deductions.s80c = D("150000")
    tr.deductions.s80g_50pct_with_limit = D("200000")
    comp = compute(tr, "old", AY)

    # GTI 10,00,000 less 80C 1,50,000 = 8,50,000; 10% of that is 85,000;
    # the donation is in the 50% category, so half of 85,000 is allowed.
    assert comp.gross_total_income == D("1000000")
    line = next(l for l in comp.deduction_detail.lines if l.section == "80G")
    assert line.allowed == D("42500")


def test_80gg_uses_the_same_net_base():
    tr = salaried("1050000")
    tr.deductions.s80c = D("150000")
    tr.deductions.rent_paid_annual = D("120000")
    comp = compute(tr, "old", AY)
    line = next(l for l in comp.deduction_detail.lines if l.section == "80GG")
    # Least of 60,000, 25% of 8,50,000 = 2,12,500, and rent less 10% of
    # 8,50,000 = 1,20,000 - 85,000 = 35,000. Measured against the gross
    # 10,00,000 instead, the third term would have been only 20,000 — here the
    # correct base works in the taxpayer's favour.
    assert line.allowed == D("35000")


# --------------------------------------------------------------------------
# Losses
# --------------------------------------------------------------------------


def test_a_house_property_loss_bigger_than_the_other_heads_is_carried_forward():
    """Section 71(3A) caps the set-off at ₹2,00,000, and section 71B carries
    the balance. What is not absorbed by income that actually exists is part
    of that balance — clamping gross total income at zero destroyed it."""
    tr = salaried("125000")
    tr.house_properties = [HouseProperty(
        property_type="LOP", annual_rent_received=D(0), interest_24b=D("300000")
    )]
    comp = compute(tr, "old", AY)

    # Salary after section 16(ia) is 75,000. Of the 3,00,000 loss, 2,00,000 is
    # available for set-off but only 75,000 can be absorbed; 1,00,000 was
    # already beyond the section 71(3A) cap.
    assert comp.gross_total_income == D(0)
    assert comp.carried_forward["house_property"] == D("225000")


def test_a_brought_forward_house_property_loss_is_actually_set_off():
    """The field existed on the schema and nothing read it, so a carried-over
    loss quietly did nothing at all."""
    tr = salaried("1000000")
    tr.house_properties = [HouseProperty(
        property_type="LOP", annual_rent_received=D("500000")
    )]
    tr.brought_forward_losses = [
        BroughtForwardLoss(assessment_year="2025-26",
                           house_property_loss=D("200000"))
    ]
    comp = compute(tr, "old", AY)

    # Annual value 5,00,000 less 30% = 3,50,000, less the 2,00,000 brought
    # forward = 1,50,000. Salary 10,00,000 less 50,000 = 9,50,000.
    assert comp.house_property == D("150000")
    assert comp.gross_total_income == D("1100000")


def test_a_brought_forward_loss_only_meets_its_own_head():
    """Section 71B and section 72 both confine the set-off to the same head."""
    tr = salaried("1000000")
    tr.brought_forward_losses = [
        BroughtForwardLoss(assessment_year="2025-26",
                           house_property_loss=D("200000"))
    ]
    comp = compute(tr, "old", AY)
    assert comp.gross_total_income == D("950000")
    assert comp.carried_forward["house_property_brought_forward"] == D("200000")


# --------------------------------------------------------------------------
# Holding period
# --------------------------------------------------------------------------


def test_exactly_twenty_four_months_is_still_short_term():
    """Section 2(42A): short term where held for "not more than" 24 months."""
    assert classify_foreign_gain(date(2024, 1, 15), date(2026, 1, 15)) \
        == "stcg_slab_foreign"
    assert classify_foreign_gain(date(2024, 1, 15), date(2026, 1, 16)) \
        == "ltcg_112_foreign"


# --------------------------------------------------------------------------
# Dividend schedules
# --------------------------------------------------------------------------


def _acme_lots() -> list:
    return [Lot(symbol="ACME", acquired=date(2025, 1, 1), shares=D(1000),
                cost_inr=D(0), cost_fx=D(0), currency="USD", origin="purchase")]


def test_a_reinvested_schedule_compounds_the_position():
    """The docstring promised compounding and the code added nothing, so every
    payment after the first was sized against a holding that never grew."""
    tr = TaxReturn(assessment_year="2026-27")
    tr.dividend_schedules = [DividendSchedule(
        symbol="ACME", currency="USD", frequency="quarterly",
        first_pay_date=date(2025, 5, 15), payments=4,
        dividend_per_share_fx=D("1.00"), reinvested=True,
        reinvest_price_per_share_fx=D("100"),
    )]
    generated, _ = expand_schedules(
        tr, _acme_lots(), [], date(2025, 4, 1), date(2026, 3, 31)
    )

    # 1,000 shares at $1 buys 10 more at $100, so the next payment is on 1,010.
    assert [g.shares_held for g in generated] == [
        D("1000"), D("1010.00"), D("1020.1000"), D("1030.301000"),
    ]
    assert generated[0].shares_acquired == D("10.00")


def test_reinvestment_without_a_price_says_so_rather_than_guessing():
    tr = TaxReturn(assessment_year="2026-27")
    tr.dividend_schedules = [DividendSchedule(
        symbol="ACME", currency="USD", frequency="quarterly",
        first_pay_date=date(2025, 5, 15), payments=2,
        dividend_per_share_fx=D("1.00"), reinvested=True,
    )]
    generated, warnings = expand_schedules(
        tr, _acme_lots(), [], date(2025, 4, 1), date(2026, 3, 31)
    )
    assert all(g.shares_acquired == D(0) for g in generated)
    assert any("reinvestment price" in w for w in warnings)


def test_section_194_tds_waits_for_the_ten_thousand_threshold():
    """Section 194 bites once the payer has paid more than ₹10,000 in the
    year. Crediting 10% on every payment claims tax nobody deducted."""
    tr = TaxReturn(assessment_year="2026-27")
    tr.dividend_schedules = [DividendSchedule(
        symbol="INFY", currency="INR", frequency="quarterly",
        first_pay_date=date(2025, 5, 15), payments=4,
        dividend_per_share_fx=D("4"),
    )]
    lots = [Lot(symbol="INFY", acquired=date(2025, 1, 1), shares=D(1000),
                cost_inr=D(0), cost_fx=D(0), currency="INR", origin="purchase")]
    generated, _ = expand_schedules(
        tr, lots, [], date(2025, 4, 1), date(2026, 3, 31)
    )

    # ₹4,000 a quarter: the third payment takes the year past ₹10,000.
    assert [g.tds_deducted for g in generated] == [
        D(0), D(0), D("400.00"), D("400.00"),
    ]


# --------------------------------------------------------------------------
# Vesting schedules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"tranches": 1}, [D(100)]),
        # A single tranche that is also the cliff has to vest the whole grant.
        ({"tranches": 1, "cliff_shares": D(25)}, [D(100)]),
        # A cliff larger than the grant is a typo, not 999 free shares.
        ({"cliff_shares": D(999)}, [D(100)]),
        ({"cliff_shares": D(25)}, [D(25), D(25), D(25), D(25)]),
    ],
)
def test_a_schedule_never_vests_more_or_less_than_the_grant(override, expected):
    fields = dict(
        symbol="ACME", total_shares=D(100), frequency="quarterly",
        first_vest_date=date(2025, 6, 15), tranches=4,
        estimated_fmv_per_share_fx=D(100), sell_to_cover_fraction=D(0),
    )
    fields.update(override)
    result = expand_schedule(VestingSchedule(**fields), as_of=date(2026, 3, 31))
    assert [v.shares_vested for v in result.vests] == expected
    assert sum(v.shares_vested for v in result.vests) == D(100)


# --------------------------------------------------------------------------
# Form selection
# --------------------------------------------------------------------------


def test_one_high_rent_property_still_breaks_the_itr1_ceiling():
    """ITR-1 allows one house property, so the ₹50 lakh test has to count the
    income from it. Leaving it out picked ITR-1 for a return that could not
    lawfully go on one."""
    tr = salaried("2000000")
    tr.house_properties = [HouseProperty(
        property_type="LOP", annual_rent_received=D("6000000")
    )]
    decision = select_form(tr)
    assert decision.form == "ITR-2"
    assert any("50 lakh" in reason for reason in decision.disqualifications)


def test_a_modest_let_out_property_still_allows_itr1():
    tr = salaried("1200000")
    tr.house_properties = [HouseProperty(
        property_type="LOP", annual_rent_received=D("300000")
    )]
    assert select_form(tr).form == "ITR-1"


# --------------------------------------------------------------------------
# ITR JSON internal consistency
# --------------------------------------------------------------------------


def test_schedule_os_reports_foreign_dividends_too():
    from app.itr.json_builder import build_itr2

    tr = salaried("1500000")
    tr.taxpayer.name = "Asha Ramanathan"
    tr.other_sources.dividend_income = D("50000")
    tr.other_sources.foreign_dividend_income = D("150000")
    comp = compute(tr, "new", AY)
    payload = build_itr2(tr, comp)["ITR"]["ITR2"]

    gross = payload["ScheduleOS"]["IncOthThanOwnRaceHorse"]["DividendGross"]
    assert gross == 200000


def test_schedule_cg_totals_include_slab_rate_and_foreign_gains():
    from app.itr.json_builder import build_itr2

    tr = salaried("1500000")
    tr.capital_gains = [
        CapitalGainItem(category="stcg_slab_foreign", sale_date=date(2025, 9, 1),
                        sale_consideration=D("500000"),
                        cost_of_acquisition=D("300000")),
        CapitalGainItem(category="ltcg_112_foreign", sale_date=date(2025, 9, 1),
                        sale_consideration=D("900000"),
                        cost_of_acquisition=D("400000")),
    ]
    comp = compute(tr, "new", AY)
    cg = build_itr2(tr, comp)["ITR"]["ITR2"]["ScheduleCGFor23"]

    assert cg["ShortTermCapGainFor23"]["TotalSTCG"] == 200000
    assert cg["LongTermCapGain23"]["TotalLTCG"] == 500000
    assert cg["SumOfCGIncm"] == 700000


def test_the_salary_schedule_adds_up_under_the_new_regime():
    """Recomputing the section 16 figures in the JSON builder disagreed with
    the engine whenever the new regime withdrew an exemption."""
    from app.itr.json_builder import build_itr2

    tr = salaried("2000000")
    tr.salaries[0].exempt_allowances = {"hra": D("300000")}
    tr.salaries[0].professional_tax = D("2400")
    comp = compute(tr, "new", AY)
    schedule_s = build_itr2(tr, comp)["ITR"]["ITR2"]["ScheduleS"]

    # The new regime withdraws HRA and section 16(iii) alike, so both report
    # nil and the head total is gross less the ₹75,000 standard deduction.
    assert schedule_s["AllwncExemptUs10"] == 0
    assert schedule_s["DeductionUs16ia"] == 75000
    assert schedule_s["DeductionUnderSection16"] == 75000
    assert (
        schedule_s["NetSalary"] - schedule_s["DeductionUnderSection16"]
        == schedule_s["TotIncUnderHeadSalaries"]
    )


# --------------------------------------------------------------------------
# Routes that took a form value without checking it
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    import os
    import tempfile

    os.environ.setdefault("ITR_DATA_DIR", tempfile.mkdtemp(prefix="itr-review-"))
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


ALL_PAGES = ("documents", "review", "income", "foreign", "compare", "file",
             "planner")


def _new(client, assessment_year: str = "2026-27") -> str:
    response = client.post("/returns/new",
                           data={"assessment_year": assessment_year},
                           follow_redirects=False)
    return response.headers["location"].split("/")[2]


def test_an_unrecognised_regime_on_the_compare_page_is_not_a_500(client):
    """Assignment is validated, so this was the one route still handing a raw
    form string to the model — on the page whose only job is to set it."""
    return_id = _new(client)
    response = client.post(f"/returns/{return_id}/compare",
                           data={"regime_choice": "martian"},
                           follow_redirects=False)
    assert response.status_code == 303
    for page in ALL_PAGES:
        assert client.get(f"/returns/{return_id}/{page}").status_code == 200


def test_creating_a_return_in_an_unsupported_year_falls_back(client):
    """The income page has checked this for a while; the page that creates the
    return did not, so four pages rendered and the fifth blew up."""
    return_id = _new(client, "1899-00")
    for page in ALL_PAGES:
        assert client.get(f"/returns/{return_id}/{page}").status_code == 200


@pytest.mark.parametrize(
    "body, expected",
    [
        ('{"assessment_year":"1899-00","salary":1500000}', 200),
        ('{"salary":"NaN"}', 200),
        ('{"salary":1200000,"ltcg_112a":"1e999"}', 200),
        ("not json at all", 400),
        ("[1,2,3]", 400),
    ],
)
def test_the_quick_compare_api_answers_or_refuses_but_never_500s(
    client, body, expected
):
    response = client.post("/api/quick-compare", content=body,
                           headers={"content-type": "application/json"})
    assert response.status_code == expected


# --------------------------------------------------------------------------
# Batch parsing
# --------------------------------------------------------------------------


def test_a_password_protected_file_is_retried_once_the_pan_is_known():
    """parse_many documented two passes and made one, so whether the AIS
    parsed depended on whether the Form 16 happened to be uploaded first."""
    from app.parsers import registry
    from app.parsers.base import Extraction, Fact, PasswordRequired

    seen: list = []

    def fake_parse(raw, filename, *, pan="", date_of_birth=None, forced_type=""):
        seen.append((filename, pan))
        if filename == "form16.pdf":
            out = Extraction(document_type="form16", source_filename=filename)
            out.facts.append(Fact(path="taxpayer.pan", label="PAN",
                                  value="ABCDE1234F"))
            return out
        if not pan:
            raise PasswordRequired("locked")
        return Extraction(document_type="ais", source_filename=filename)

    original = registry.parse_document
    registry.parse_document = fake_parse
    try:
        # The locked file comes first, before anything has revealed the PAN.
        results = registry.parse_many(
            [("ais.pdf", b"x"), ("form16.pdf", b"y")]
        )
    finally:
        registry.parse_document = original

    assert [r.document_type for r in results] == ["ais", "form16"]
    assert ("ais.pdf", "ABCDE1234F") in seen


def test_a_file_that_stays_locked_keeps_its_original_message():
    from app.parsers import registry
    from app.parsers.base import Extraction, Fact, PasswordRequired

    def fake_parse(raw, filename, *, pan="", date_of_birth=None, forced_type=""):
        if filename == "form16.pdf":
            out = Extraction(document_type="form16", source_filename=filename)
            out.facts.append(Fact(path="taxpayer.pan", label="PAN",
                                  value="ABCDE1234F"))
            return out
        raise PasswordRequired("This PDF is password protected.")

    original = registry.parse_document
    registry.parse_document = fake_parse
    try:
        results = registry.parse_many([("ais.pdf", b"x"), ("form16.pdf", b"y")])
    finally:
        registry.parse_document = original

    assert results[0].needs_password is True
    assert any("password protected" in w for w in results[0].warnings)
