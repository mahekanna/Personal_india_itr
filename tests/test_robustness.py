"""Bad input must not crash a page, and must never cost the user their data.

Every case here was found by probing the running application rather than by
reasoning about it, and each one was a real failure before the fix it names.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

os.environ.setdefault("ITR_DATA_DIR", tempfile.mkdtemp(prefix="itr-robust-"))

from app.db import ReturnRecord, get_session          # noqa: E402
from app.money import D, MAX_MONEY, inr, round_to_ten, rupees  # noqa: E402
from app.planner import build_planner                 # noqa: E402
from app.schemas import (                             # noqa: E402
    CapitalGainItem,
    DividendReceipt,
    ESPPPurchase,
    ForeignSale,
    RSUVest,
    SalaryIncome,
    TaxReturn,
    VestingSchedule,
)
from app.tax.engine import compare_regimes            # noqa: E402


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def new_return(client) -> str:
    response = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    )
    return response.headers["location"].split("/")[2]


ALL_PAGES = ("documents", "review", "income", "foreign", "compare", "file",
             "planner")


# --------------------------------------------------------------------------
# Money that cannot be quantized
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["-1e999", "1e999", "NaN", "sNaN", "Infinity", "-Infinity", "1e400"]
)
def test_absurd_money_becomes_zero_rather_than_raising(text):
    """Decimal.quantize raises past 28 significant digits, and that surfaced
    inside a Jinja filter, where it is a 500 rather than a wrong number."""
    assert D(text) == D(0)
    assert rupees(text) == D(0)
    assert inr(text) == "0"
    assert round_to_ten(text) == D(0)


def test_the_largest_plausible_figure_still_works():
    """The guard must not reject a real, if enormous, rupee amount."""
    assert D("999999999999999") == D(999_999_999_999_999)
    assert inr("999999999999999") == "99,99,99,99,99,99,999"
    assert MAX_MONEY == D("1e15")


def test_indian_formatting_survives_the_guard():
    assert D("1,00,000") == D(100_000)
    assert D("₹12,34,567.00") == D("1234567.00")


def test_an_absurd_figure_does_not_break_a_page(client):
    return_id = new_return(client)
    client.post(f"/returns/{return_id}/income", data={
        "assessment_year": "2026-27", "business_scheme": "none",
        "os_savings_bank_interest": "-1e999",
        "os_fixed_deposit_interest": "Infinity",
        "ded_s80c": "NaN",
    }, follow_redirects=False)
    for page in ALL_PAGES:
        assert client.get(f"/returns/{return_id}/{page}").status_code == 200


# --------------------------------------------------------------------------
# Values the model does not accept
# --------------------------------------------------------------------------


def test_an_unsupported_assessment_year_is_refused_not_stored(client):
    """It used to be stored, then raise from get_ay on every later page."""
    return_id = new_return(client)
    client.post(f"/returns/{return_id}/income", data={
        "assessment_year": "1899-00", "business_scheme": "none",
    }, follow_redirects=False)

    stored = get_session().get(ReturnRecord, return_id).load()
    assert stored.assessment_year == "2026-27"
    for page in ALL_PAGES:
        assert client.get(f"/returns/{return_id}/{page}").status_code == 200


@pytest.mark.parametrize(
    "field, value, attribute, expected",
    [
        ("regime_choice", "martian", "regime_choice", "auto"),
        ("residential_status", "MARS", "taxpayer.residential_status", "RES"),
        ("business_scheme", "44ZZ", "business.scheme", "none"),
    ],
)
def test_an_unrecognised_choice_falls_back_to_the_default(
    client, field, value, attribute, expected
):
    return_id = new_return(client)
    client.post(f"/returns/{return_id}/income",
                data={field: value}, follow_redirects=False)

    stored = get_session().get(ReturnRecord, return_id).load()
    target = stored
    for part in attribute.split("."):
        target = getattr(target, part)
    assert target == expected


def test_an_unknown_capital_gain_bucket_falls_back(client):
    return_id = new_return(client)
    client.post(f"/returns/{return_id}/income", data={
        "cg_0_sale_consideration": "100000",
        "cg_0_category": "nonexistent_bucket",
        "business_scheme": "none",
    }, follow_redirects=False)

    stored = get_session().get(ReturnRecord, return_id).load()
    assert stored.capital_gains[0].category == "ltcg_112a"
    assert client.get(f"/returns/{return_id}/compare").status_code == 200


# --------------------------------------------------------------------------
# The loader must never destroy what it cannot read
# --------------------------------------------------------------------------


def test_one_unreadable_field_does_not_discard_the_whole_return():
    """This is the failure that looks like the data having vanished."""
    import json

    record = ReturnRecord(assessment_year="2026-27")
    tr = TaxReturn(assessment_year="2026-27")
    tr.taxpayer.name = "Asha Ramanathan"
    tr.taxpayer.pan = "ABCDE1234F"
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D(3_000_000))]
    record.save(tr)

    # Corrupt exactly one field, the way a bad form post used to.
    payload = json.loads(record.payload)
    payload["regime_choice"] = "martian"
    record.payload = json.dumps(payload)

    salvaged = record.load()
    assert salvaged.taxpayer.name == "Asha Ramanathan"
    assert salvaged.salaries[0].salary_17_1 == D(3_000_000)
    assert salvaged.regime_choice == "auto"
    assert any("could not be read back" in note for note in salvaged.notes)


def test_unparseable_json_still_yields_a_usable_return():
    record = ReturnRecord(assessment_year="2026-27")
    record.payload = "{not json at all"
    assert record.load().assessment_year == "2026-27"


def test_assignment_is_validated_so_bad_values_never_persist():
    tr = TaxReturn(assessment_year="2026-27")
    with pytest.raises(Exception):
        tr.regime_choice = "martian"
    assert tr.regime_choice == "auto"


# --------------------------------------------------------------------------
# Degenerate returns
# --------------------------------------------------------------------------


def test_an_entirely_empty_return_computes_and_renders(client):
    return_id = new_return(client)
    for page in ALL_PAGES:
        assert client.get(f"/returns/{return_id}/{page}").status_code == 200
    assert client.get(f"/returns/{return_id}/computation.pdf").status_code == 200


def test_an_empty_return_computes_without_raising():
    tr = TaxReturn(assessment_year="2026-27")
    comparison = compare_regimes(tr)
    assert comparison.chosen.total_tax_liability == D(0)
    assert build_planner(tr).plan.liable is False


def test_everything_undated_does_not_raise():
    tr = TaxReturn(assessment_year="2026-27")
    tr.rsu_vests = [RSUVest(symbol="A", shares_vested=D(10),
                            fmv_per_share_fx=D(100))]
    tr.espp_purchases = [ESPPPurchase(symbol="A", shares_purchased=D(5),
                                      fmv_per_share_fx=D(100),
                                      price_paid_per_share_fx=D(80))]
    tr.dividends = [DividendReceipt(symbol="A", gross_amount_fx=D(50))]
    tr.foreign_sales = [ForeignSale(symbol="A", shares=D(3),
                                    price_per_share_fx=D(120))]
    compare_regimes(tr)
    build_planner(tr)


def test_a_vest_with_no_exchange_rate_on_file_is_reported_not_fatal():
    tr = TaxReturn(assessment_year="2026-27")
    tr.rsu_vests = [RSUVest(symbol="A", vest_date=date(1999, 5, 1),
                            shares_vested=D(10), fmv_per_share_fx=D(100))]
    comparison = compare_regimes(tr)
    assert any("rate" in w.lower() for w in comparison.foreign.warnings)


@pytest.mark.parametrize(
    "override",
    [
        {"total_shares": D(0)},
        {"tranches": 1},
        {"cliff_shares": D(999)},
        {"tranches": 400},
    ],
)
def test_degenerate_vesting_schedules_do_not_raise(override):
    tr = TaxReturn(assessment_year="2026-27")
    fields = dict(
        symbol="A", total_shares=D(100), frequency="quarterly",
        first_vest_date=date(2025, 6, 15), tranches=4,
        estimated_fmv_per_share_fx=D(100),
    )
    fields.update(override)
    tr.vesting_schedules = [VestingSchedule(**fields)]
    compare_regimes(tr)
    build_planner(tr)


# --------------------------------------------------------------------------
# Uploads and output escaping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, payload",
    [
        ("empty.pdf", b""),
        ("notreally.pdf", b"this is not a pdf at all"),
        ("broken.json", b"\xff\xfe\x00garbage"),
        ("noextension", b"hello"),
        ("wide.csv", ('"' + "x" * 50_000 + '"\n').encode()),
    ],
)
def test_a_hostile_upload_is_reported_not_fatal(client, name, payload):
    return_id = new_return(client)
    response = client.post(
        f"/returns/{return_id}/documents",
        files={"files": (name, payload, "application/octet-stream")},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)
    assert client.get(f"/returns/{return_id}/review").status_code == 200


def test_markup_in_a_field_is_escaped_on_the_way_out(client):
    return_id = new_return(client)
    client.post(f"/returns/{return_id}/income", data={
        "name": "<script>alert(1)</script>", "business_scheme": "none",
    }, follow_redirects=False)
    page = client.get(f"/returns/{return_id}/income").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_a_missing_return_is_a_404_not_a_500(client):
    assert client.get("/returns/does-not-exist/income").status_code == 404
