"""The opening questionnaire: which form, and what to go and fetch.

The form prediction has to agree with ``itr.selector`` — the same rules
answered twice would drift, and the copy the user sees first would be the one
nobody tested. So these tests check the prediction against the selector's own
verdict as well as against the statute.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from app.questionnaire import QUESTIONS, build_guidance, grouped_questions

os.environ.setdefault("ITR_DATA_DIR", tempfile.mkdtemp(prefix="itr-quiz-"))

SALARIED = {"resident": True, "salary": True}
TRADER = {**SALARIED, "fno": True, "intraday": True, "equity_delivery": True}
RSU_TRADER = {**TRADER, "foreign_equity": True, "foreign_dividend": True}


def documents(guidance) -> list:
    return [item.name for items in guidance.documents.values() for item in items]


def text_of(guidance) -> str:
    return " ".join(
        item.name + " " + item.where + " " + item.why
        for items in guidance.documents.values() for item in items
    ) + " ".join(guidance.warnings) + " ".join(guidance.deadlines)


# --------------------------------------------------------------------------
# Which form
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answers, expected",
    [
        ({"resident": True, "salary": True}, "ITR-1"),
        ({"resident": True, "salary": True, "other_income": True}, "ITR-1"),
        # Delivery equity beyond the ₹1.25 lakh 112A allowance is not knowable
        # from a yes/no, so the selector escalates — which is the safe way to
        # be wrong.
        ({"resident": True, "salary": True, "equity_delivery": True}, "ITR-1"),
        ({"resident": True, "salary": True, "fno": True}, "ITR-3"),
        ({"resident": True, "intraday": True}, "ITR-3"),
        ({"resident": True, "salary": True, "foreign_equity": True}, "ITR-2"),
        ({"resident": True, "salary": True, "winnings": True}, "ITR-2"),
        ({"resident": True, "salary": True, "director_or_unlisted": True}, "ITR-2"),
        ({"resident": True, "salary": True, "multiple_houses": True,
          "house_property": True}, "ITR-2"),
        ({"resident": False, "salary": True}, "ITR-2"),
    ],
)
def test_the_form_prediction(answers, expected):
    assert build_guidance(answers, "2026-27").form == expected


def test_the_prediction_agrees_with_the_selector():
    """Two implementations of the same rules would drift."""
    from app.itr.selector import select_form
    from app.questionnaire import _skeleton

    for answers in (SALARIED, TRADER, RSU_TRADER,
                    {"resident": True, "winnings": True}):
        guidance = build_guidance(answers, "2026-27")
        assert guidance.form == select_form(_skeleton(answers, "2026-27")).form


def test_trading_is_reported_as_business_income_not_capital_gains():
    guidance = build_guidance(TRADER, "2026-27")
    assert guidance.form == "ITR-3"
    assert any("43(5)" in reason for reason in guidance.form_reasons)


# --------------------------------------------------------------------------
# What to fetch
# --------------------------------------------------------------------------


def test_everyone_is_told_to_get_26as_and_the_ais():
    names = documents(build_guidance(SALARIED, "2026-27"))
    assert any("26AS" in name for name in names)
    assert any("Annual Information Statement" in name for name in names)


def test_a_trader_is_told_to_get_the_segment_wise_pnl_and_the_charges():
    names = documents(build_guidance(TRADER, "2026-27"))
    assert any("segment-wise" in name for name in names)
    assert any("charges and brokerage" in name for name in names)


def test_charges_are_flagged_as_deductible():
    """The compensation for losing the concessional capital-gains rate, and
    the thing traders most often leave unclaimed."""
    blob = text_of(build_guidance(TRADER, "2026-27"))
    assert "deductible against business income" in blob


def test_rsus_pull_in_form_12ba_and_the_schedule_fa_details():
    names = documents(build_guidance(RSU_TRADER, "2026-27"))
    assert any("12BA" in name for name in names)
    assert any("registered address" in name for name in names)
    assert any("31 December" in name for name in names)
    assert any("SBI TT buying rate" in name for name in names)


def test_form_67_is_demanded_before_the_return_not_with_it():
    guidance = build_guidance(RSU_TRADER, "2026-27")
    form67 = next(
        item for items in guidance.documents.values() for item in items
        if item.name == "Form 67"
    )
    assert "BEFORE" in form67.where
    assert any("before the return is filed" in d for d in guidance.deadlines)


def test_selling_old_shares_asks_for_the_31_january_2018_price():
    """Section 55(2)(ac). Without it the cost is understated and tax is paid
    on a gain that was grandfathered away."""
    names = documents(build_guidance(
        {**SALARIED, "equity_delivery": True}, "2026-27"
    ))
    assert any("31 January 2018" in name for name in names)


def test_deduction_proofs_appear_only_if_the_old_regime_is_wanted():
    without = documents(build_guidance(SALARIED, "2026-27"))
    assert not any("80C" in name for name in without)

    with_old = documents(build_guidance({**SALARIED, "old_regime": True}, "2026-27"))
    assert any("80C" in name for name in with_old)
    assert any("80G" in name for name in with_old)


def test_two_employers_gets_the_warning_that_costs_people_money():
    guidance = build_guidance({**SALARIED, "multiple_employers": True}, "2026-27")
    assert any("standard deduction" in w for w in guidance.warnings)


def test_a_trader_is_warned_about_form_10iea():
    guidance = build_guidance(TRADER, "2026-27")
    assert any("10-IEA" in w for w in guidance.warnings)
    assert any("10-IEA" in d for d in guidance.deadlines)


def test_foreign_holdings_carry_the_black_money_act_warning():
    guidance = build_guidance(RSU_TRADER, "2026-27")
    assert any("Black Money Act" in w for w in guidance.warnings)
    assert any("no threshold" in w for w in guidance.warnings)


def test_the_carry_forward_and_verification_deadlines_are_always_stated():
    guidance = build_guidance(SALARIED, "2026-27")
    blob = " ".join(guidance.deadlines)
    assert "carry forward" in blob
    assert "30 days" in blob


def test_an_empty_questionnaire_still_produces_the_universal_list():
    guidance = build_guidance({}, "2026-27")
    assert guidance.document_count > 0
    assert any("26AS" in name for name in documents(guidance))


# --------------------------------------------------------------------------
# Through the web
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_a_new_return_opens_on_the_questionnaire_not_the_upload_page(client):
    response = client.post("/returns/new", data={"assessment_year": "2026-27"},
                           follow_redirects=False)
    assert response.headers["location"].endswith("/start")


def test_answering_stores_the_profile_and_shows_the_checklist(client):
    return_id = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    ).headers["location"].split("/")[2]

    client.post(f"/returns/{return_id}/start", data={
        "resident": "on", "salary": "on", "fno": "on", "foreign_equity": "on",
    }, follow_redirects=False)

    page = client.get(f"/returns/{return_id}/start").text
    assert "You are filing ITR-3" in page
    assert "Form 12BA" in page

    # And the upload page carries it forward.
    assert "Your checklist" in client.get(f"/returns/{return_id}/documents").text


def test_an_unticked_box_is_a_no_not_a_missing_answer(client):
    """Checkboxes send nothing when unticked. Reading only what arrived would
    make "no" indistinguishable from "not asked", and re-answering to remove a
    segment would leave it set."""
    from app.db import ReturnRecord, get_session

    return_id = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    ).headers["location"].split("/")[2]

    client.post(f"/returns/{return_id}/start",
                data={"resident": "on", "fno": "on"}, follow_redirects=False)
    client.post(f"/returns/{return_id}/start",
                data={"resident": "on"}, follow_redirects=False)

    stored = get_session().get(ReturnRecord, return_id).load()
    assert stored.profile.answered is True
    assert stored.profile.answers["fno"] is False
    assert set(stored.profile.answers) == {q.key for q in QUESTIONS}


def test_saying_you_are_not_resident_sets_the_status(client):
    from app.db import ReturnRecord, get_session

    return_id = client.post(
        "/returns/new", data={"assessment_year": "2026-27"},
        follow_redirects=False,
    ).headers["location"].split("/")[2]
    client.post(f"/returns/{return_id}/start", data={"salary": "on"},
                follow_redirects=False)

    stored = get_session().get(ReturnRecord, return_id).load()
    assert stored.taxpayer.residential_status == "NRI"


def test_every_question_belongs_to_a_displayed_group():
    shown = {q.key for _, questions in grouped_questions() for q in questions}
    assert shown == {q.key for q in QUESTIONS}
