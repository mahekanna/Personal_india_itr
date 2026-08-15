"""End-to-end tests: the web flow, form selection, reconciliation and the JSON."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date

import pytest

os.environ.setdefault(
    "ITR_DATA_DIR", tempfile.mkdtemp(prefix="itr-tests-")
)

from app.itr import json_builder                       # noqa: E402
from app.itr.filing_pack import build_filing_pack      # noqa: E402
from app.itr.selector import select_form               # noqa: E402
from app.merge import apply_extractions                # noqa: E402
from app.money import D                                # noqa: E402
from app.parsers.base import Extraction                # noqa: E402
from app.reconcile import reconcile                    # noqa: E402
from app.schemas import (                              # noqa: E402
    BankAccount,
    CapitalGainItem,
    HouseProperty,
    SalaryIncome,
    TaxPayment,
    TaxReturn,
)
from app.tax.engine import compare_regimes, compute    # noqa: E402

ON_TIME = date(2026, 7, 25)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def salaried_return() -> TaxReturn:
    tr = TaxReturn(assessment_year="2026-27", filing_date=ON_TIME)
    tr.taxpayer.name = "Asha Ramanathan"
    tr.taxpayer.pan = "ABCDE1234F"
    tr.taxpayer.date_of_birth = date(1988, 5, 14)
    tr.taxpayer.city = "Bengaluru"
    tr.taxpayer.bank_accounts = [
        BankAccount(ifsc="HDFC0001234", bank_name="HDFC Bank",
                    account_number="50100123456789",
                    is_primary_refund_account=True)
    ]
    tr.salaries = [
        SalaryIncome(employer_name="Acme Software", employer_tan="BLRA12345B",
                     salary_17_1=D(2_400_000), perquisites_17_2=D(50_000),
                     professional_tax=D(2_400), tds_deducted=D(480_000))
    ]
    tr.other_sources.savings_bank_interest = D(12_000)
    tr.other_sources.fixed_deposit_interest = D(85_000)
    tr.taxes_paid.payments = [
        TaxPayment(kind="tds_salary", deductor_name="Acme Software",
                   deductor_tan="BLRA12345B", amount=D(480_000))
    ]
    return tr


# --------------------------------------------------------------------------
# Form selection
# --------------------------------------------------------------------------


def test_plain_salaried_return_gets_itr1():
    assert select_form(salaried_return()).form == "ITR-1"


def test_small_112a_gain_still_allows_itr1():
    """ITR-1 accepts section 112A gains up to ₹1,25,000."""
    tr = salaried_return()
    tr.capital_gains = [
        CapitalGainItem(category="ltcg_112a", sale_consideration=D(200_000),
                        cost_of_acquisition=D(120_000))
    ]
    assert select_form(tr).form == "ITR-1"


def test_any_stcg_pushes_the_return_to_itr2():
    tr = salaried_return()
    tr.capital_gains = [
        CapitalGainItem(category="stcg_111a", sale_consideration=D(50_000),
                        cost_of_acquisition=D(40_000))
    ]
    decision = select_form(tr)
    assert decision.form == "ITR-2"
    assert decision.disqualifications


def test_income_above_50_lakh_rules_out_itr1():
    tr = salaried_return()
    tr.salaries[0].salary_17_1 = D(6_000_000)
    assert select_form(tr).form == "ITR-2"


def test_two_house_properties_rule_out_itr1():
    tr = salaried_return()
    tr.house_properties = [
        HouseProperty(property_type="SOP"),
        HouseProperty(property_type="LOP", annual_rent_received=D(240_000)),
    ]
    assert select_form(tr).form == "ITR-2"


def test_foreign_assets_rule_out_itr1():
    tr = salaried_return()
    tr.taxpayer.has_foreign_assets = True
    assert select_form(tr).form == "ITR-2"


def test_presumptive_income_selects_itr4_and_says_json_is_unavailable():
    tr = salaried_return()
    tr.business.scheme = "44ADA"
    tr.business.gross_receipts_44ada = D(3_000_000)
    decision = select_form(tr)
    assert decision.form == "ITR-4"
    assert decision.supported is False
    assert decision.note


# --------------------------------------------------------------------------
# ITR JSON
# --------------------------------------------------------------------------


def test_itr1_json_has_the_expected_envelope():
    tr = salaried_return()
    comp = compare_regimes(tr).chosen
    payload = json_builder.build(tr, comp, "ITR-1")

    itr1 = payload["ITR"]["ITR1"]
    assert itr1["Form_ITR1"]["FormName"] == "ITR-1"
    assert itr1["Form_ITR1"]["AssessmentYear"] == "2026"
    assert itr1["PersonalInfo"]["PAN"] == "ABCDE1234F"
    assert itr1["PersonalInfo"]["AssesseeName"]["SurNameOrOrgName"] == "Ramanathan"
    assert itr1["Verification"]["Declaration"]["AssesseeVerPAN"] == "ABCDE1234F"


def test_itr1_json_totals_match_the_computation():
    tr = salaried_return()
    comp = compare_regimes(tr).chosen
    itr1 = json_builder.build(tr, comp, "ITR-1")["ITR"]["ITR1"]

    assert itr1["ITR1_IncomeDeductions"]["TotalIncome"] == int(comp.total_income_rounded)
    assert itr1["ITR1_IncomeDeductions"]["GrossTotIncome"] == int(comp.gross_total_income)
    assert itr1["TaxPaid"]["TaxesPaid"]["TDS"] == int(comp.tds)
    assert itr1["ITR1_TaxComputation"]["NetTaxLiability"] == int(comp.total_tax_liability)


def test_regime_flag_is_carried_into_the_json():
    tr = salaried_return()
    tr.regime_choice = "old"
    comp = compare_regimes(tr).chosen
    itr1 = json_builder.build(tr, comp, "ITR-1")["ITR"]["ITR1"]
    assert itr1["FilingStatus"]["NewTaxRegime"] == "N"


def test_belated_return_is_filed_under_section_139_4():
    tr = salaried_return()
    tr.filing_date = date(2026, 9, 10)
    comp = compare_regimes(tr).chosen
    itr1 = json_builder.build(tr, comp, "ITR-1")["ITR"]["ITR1"]
    assert itr1["FilingStatus"]["ReturnFileSec"] == 12


def test_itr2_json_carries_schedule_si_for_special_rate_income():
    tr = salaried_return()
    tr.capital_gains = [
        CapitalGainItem(category="ltcg_112a", description="INFY",
                        sale_consideration=D(825_000),
                        cost_of_acquisition=D(500_000)),
        CapitalGainItem(category="stcg_111a", description="TCS",
                        sale_consideration=D(300_000),
                        cost_of_acquisition=D(200_000)),
    ]
    comp = compare_regimes(tr).chosen
    itr2 = json_builder.build(tr, comp, "ITR-2")["ITR"]["ITR2"]

    codes = {row["SecCode"] for row in itr2["ScheduleSI"]["SplCodeRateTax"]}
    assert {"1A", "22"} <= codes
    assert itr2["ScheduleSI"]["TotSplRateIncTax"] == int(comp.tax_on_special_income)


def test_json_is_serialisable_and_round_trips():
    tr = salaried_return()
    comp = compare_regimes(tr).chosen
    raw = json_builder.to_json_bytes(json_builder.build(tr, comp, "ITR-1"))
    assert json.loads(raw)["ITR"]["ITR1"]["PersonalInfo"]["PAN"] == "ABCDE1234F"


def test_unsupported_form_raises_a_clear_error():
    tr = salaried_return()
    comp = compare_regimes(tr).chosen
    with pytest.raises(ValueError, match="ITR-4"):
        json_builder.build(tr, comp, "ITR-4")


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_claiming_more_tds_than_26as_shows_is_an_error():
    tr = salaried_return()
    tr.taxes_paid.payments[0].amount = D(500_000)

    extraction = Extraction(document_type="form26as", source_filename="26as.pdf")
    extraction.payments.append({
        "kind": "tds_salary", "deductor_tan": "BLRA12345B",
        "amount": D(480_000),
    })
    report = reconcile(tr, [extraction])
    titles = [f.title for f in report.errors]
    assert any("more TDS than Form 26AS" in t for t in titles)


def test_matching_tds_produces_no_error():
    tr = salaried_return()
    extraction = Extraction(document_type="form26as", source_filename="26as.pdf")
    extraction.payments.append({
        "kind": "tds_salary", "deductor_tan": "BLRA12345B",
        "amount": D(480_000),
    })
    report = reconcile(tr, [extraction])
    assert not [f for f in report.errors if "TDS" in f.title]


def test_income_the_ais_reports_but_the_return_omits_is_an_error():
    tr = salaried_return()
    tr.other_sources.dividend_income = D(0)

    extraction = Extraction(document_type="ais", source_filename="ais.json")
    extraction.add("other_sources.dividend_income", "Dividend income", D(45_000))
    report = reconcile(tr, [extraction])
    assert any("Dividend" in f.title for f in report.errors)


def test_missing_pan_blocks_the_return():
    tr = salaried_return()
    tr.taxpayer.pan = ""
    report = reconcile(tr, [])
    assert any("PAN" in f.title for f in report.errors)


def test_tds_claimed_without_a_tan_is_an_error():
    tr = salaried_return()
    tr.salaries[0].employer_tan = ""
    report = reconcile(tr, [])
    assert any("TAN" in f.title for f in report.errors)


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def test_the_same_form16_uploaded_twice_does_not_double_the_salary():
    tr = TaxReturn(assessment_year="2026-27")

    def extraction():
        out = Extraction(document_type="form16", source_filename="f16.pdf")
        out.salaries.append({
            "employer_name": "Acme", "employer_tan": "BLRA12345B",
            "salary_17_1": D(2_400_000),
        })
        return out

    apply_extractions(tr, [extraction()])
    apply_extractions(tr, [extraction()])
    assert len(tr.salaries) == 1


def test_two_bank_certificates_accumulate_rather_than_overwrite():
    tr = TaxReturn(assessment_year="2026-27")
    for amount in (D(30_000), D(55_000)):
        out = Extraction(document_type="bank_interest", source_filename="b.pdf")
        out.add("other_sources.fixed_deposit_interest", "Deposit interest", amount)
        apply_extractions(tr, [out])
    assert tr.other_sources.fixed_deposit_interest == D(85_000)


def test_unaccepted_facts_are_not_applied():
    tr = TaxReturn(assessment_year="2026-27")
    out = Extraction(document_type="ais", source_filename="ais.json")
    out.add("other_sources.dividend_income", "Dividend", D(33_000))
    apply_extractions(tr, [out], accepted_paths=set())
    assert tr.other_sources.dividend_income == D(0)


# --------------------------------------------------------------------------
# Filing pack
# --------------------------------------------------------------------------


def test_filing_pack_tells_you_to_pay_before_filing_when_tax_is_due():
    tr = salaried_return()
    tr.taxes_paid.payments = []          # nothing paid, so a large balance
    comp = compare_regimes(tr).chosen
    pack = build_filing_pack(tr, comp, "ITR-1")
    assert comp.net_payable > 0
    assert any("self-assessment tax" in step for step in pack.checklist)


def test_filing_pack_always_ends_with_e_verification():
    tr = salaried_return()
    comp = compare_regimes(tr).chosen
    pack = build_filing_pack(tr, comp, "ITR-1")
    assert any("e-verify" in step for step in pack.checklist)


# --------------------------------------------------------------------------
# Web flow
# --------------------------------------------------------------------------


def test_every_wizard_page_renders(client):
    response = client.post(
        "/returns/new", data={"assessment_year": "2026-27", "label": "Test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return_id = response.headers["location"].split("/")[2]

    for page in ("documents", "review", "income", "compare", "file"):
        assert client.get(f"/returns/{return_id}/{page}").status_code == 200


def test_the_income_form_round_trips_through_the_wizard(client):
    return_id = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    ).headers["location"].split("/")[2]

    client.post(f"/returns/{return_id}/income", data={
        "assessment_year": "2026-27", "regime_choice": "auto",
        "filing_date": "2026-07-25", "name": "Asha Ramanathan",
        "pan": "abcde1234f", "date_of_birth": "1988-05-14",
        "residential_status": "RES",
        "salary_0_employer_name": "Acme", "salary_0_employer_tan": "BLRA12345B",
        "salary_0_salary_17_1": "2400000",
        "os_savings_bank_interest": "12000",
        "ded_s80c": "150000",
        "tax_0_kind": "tds_salary", "tax_0_deductor_tan": "BLRA12345B",
        "tax_0_amount": "480000",
        "business_scheme": "none",
    }, follow_redirects=False)

    data = client.get(f"/returns/{return_id}/data.json").json()
    assert data["taxpayer"]["pan"] == "ABCDE1234F"      # upper-cased on save
    assert data["salaries"][0]["salary_17_1"] == "2400000"
    assert len(data["taxes_paid"]["payments"]) == 1


def test_uploading_a_form16_flows_into_the_review_screen(client):
    from tests.test_parsers import FORM16_LINES, _pdf

    return_id = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    ).headers["location"].split("/")[2]

    response = client.post(
        f"/returns/{return_id}/documents",
        files={"files": ("form16.pdf", _pdf(FORM16_LINES), "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get(f"/returns/{return_id}/review")
    assert page.status_code == 200
    assert "BLRA12345B" in page.text


def test_downloads_are_served(client):
    return_id = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    ).headers["location"].split("/")[2]

    client.post(f"/returns/{return_id}/income", data={
        "assessment_year": "2026-27", "regime_choice": "auto",
        "filing_date": "2026-07-25", "name": "Asha R", "pan": "ABCDE1234F",
        "date_of_birth": "1988-05-14", "residential_status": "RES",
        "salary_0_employer_name": "Acme", "salary_0_employer_tan": "BLRA12345B",
        "salary_0_salary_17_1": "1200000", "business_scheme": "none",
    }, follow_redirects=False)

    itr = client.get(f"/returns/{return_id}/itr.json")
    assert itr.status_code == 200
    assert json.loads(itr.content)["ITR"]["ITR1"]["PersonalInfo"]["PAN"]

    pdf = client.get(f"/returns/{return_id}/computation.pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")


def test_quick_compare_api(client):
    response = client.post("/api/quick-compare", json={
        "salary": 1_500_000, "s80c": 150_000, "s80d": 25_000,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["recommended"] in ("new", "old")
    assert D(body["new"]["total_tax"]) > 0


def test_a_missing_return_gives_a_clean_404(client):
    assert client.get("/returns/does-not-exist/income").status_code == 404
