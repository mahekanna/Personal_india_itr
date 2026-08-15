"""Which ITR form applies.

Picking the wrong form makes the return defective under section 139(9), so the
rules are applied strictly and every disqualification is reported with its
reason. When in doubt the selector escalates to the more capable form — filing
ITR-2 when ITR-1 would have done is harmless; the reverse is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List

from ..money import D
from ..schemas import TaxReturn

ITR1_INCOME_CEILING = D("5000000")
ITR1_OTHER_SOURCES_CEILING = D("5000000")
# ITR-1 accepts LTCG under section 112A up to ₹1.25 lakh from AY 2025-26.
ITR1_LTCG_CEILING = D("125000")


@dataclass
class FormDecision:
    form: str
    reasons: List[str] = field(default_factory=list)
    disqualifications: List[str] = field(default_factory=list)
    supported: bool = True
    note: str = ""


def select_form(tr: TaxReturn) -> FormDecision:
    reasons: List[str] = []
    blocks: List[str] = []

    total_salary = sum((s.gross_salary for s in tr.salaries), D(0))
    let_out = [p for p in tr.house_properties if p.property_type != "SOP"]
    self_occupied = [p for p in tr.house_properties if p.property_type == "SOP"]

    other_sources_total = (
        tr.other_sources.savings_bank_interest
        + tr.other_sources.fixed_deposit_interest
        + tr.other_sources.other_interest
        + tr.other_sources.dividend_income
        + tr.other_sources.family_pension
        + tr.other_sources.other_income
    )
    # House property has to be in here. ITR-1 permits one property, let out or
    # not, so a single high-rent flat can carry someone past ₹50 lakh on its
    # own — and leaving it out of the test picked ITR-1 for a return that could
    # not lawfully go on it.
    house_property_income = D(0)
    for prop in tr.house_properties:
        share = prop.ownership_share if prop.ownership_share > 0 else D(1)
        if prop.property_type == "SOP":
            net_annual_value = D(0)
        else:
            gross = max(D(0), prop.annual_rent_received - prop.unrealised_rent)
            net_annual_value = max(
                D(0), gross * share - prop.municipal_taxes_paid * share
            )
        house_property_income += (
            net_annual_value
            - net_annual_value * D("0.30")
            - (prop.interest_24b + prop.pre_construction_interest) * share
        )

    rough_income = (
        total_salary + other_sources_total + house_property_income
        + sum((item.net_gain for item in tr.capital_gains), D(0))
    )

    # ---- Hard disqualifications from ITR-1 --------------------------------
    if tr.taxpayer.residential_status != "RES":
        blocks.append(
            "ITR-1 is only for a resident and ordinarily resident individual."
        )
    if len(tr.house_properties) > 1:
        blocks.append("ITR-1 allows only one house property.")
    if len(let_out) > 0 and len(tr.house_properties) > 1:
        blocks.append("More than one property, including a let-out one.")
    if rough_income > ITR1_INCOME_CEILING:
        blocks.append(
            f"Total income of about ₹{rough_income:,.0f} exceeds the ₹50 lakh "
            "ceiling for ITR-1."
        )
    if tr.business.scheme in ("44AD", "44ADA", "44AE"):
        blocks.append(
            "Presumptive business or professional income belongs in ITR-4."
        )
    if tr.taxpayer.has_foreign_assets or tr.foreign_assets or tr.rsu_vests \
            or tr.foreign_holdings or tr.espp_purchases:
        blocks.append(
            "Foreign assets rule out ITR-1 outright. RSUs, ESPP shares or any "
            "overseas holding mean Schedule FA, which only ITR-2 and ITR-3 "
            "carry — and Schedule FA is not optional at any value."
        )
    if tr.dividends or tr.foreign_taxes:
        blocks.append(
            "Foreign income with tax paid abroad needs Schedule FSI and "
            "Schedule TR, which ITR-1 does not have."
        )
    if tr.taxpayer.is_company_director:
        blocks.append("A director of a company cannot file ITR-1.")
    if tr.taxpayer.holds_unlisted_equity:
        blocks.append("Holding unlisted equity shares rules out ITR-1.")
    if tr.other_sources.winnings_115bb > 0:
        blocks.append("Winnings taxable under section 115BB rule out ITR-1.")
    if tr.brought_forward_losses:
        blocks.append("Brought-forward losses cannot be carried in ITR-1.")
    if tr.exempt_income.agricultural_income > D("5000"):
        blocks.append(
            "Agricultural income above ₹5,000 rules out ITR-1."
        )

    # ---- Capital gains ----------------------------------------------------
    non_112a = [
        item for item in tr.capital_gains if item.category != "ltcg_112a"
    ]
    ltcg_112a_total = sum(
        (item.net_gain for item in tr.capital_gains
         if item.category == "ltcg_112a"),
        D(0),
    )
    if non_112a:
        blocks.append(
            "ITR-1 accepts only long-term capital gains under section 112A; "
            "any other capital gain requires ITR-2."
        )
    elif ltcg_112a_total > ITR1_LTCG_CEILING:
        blocks.append(
            f"Section 112A gains of ₹{ltcg_112a_total:,.0f} exceed the "
            "₹1,25,000 that ITR-1 permits."
        )

    # ---- Decide -----------------------------------------------------------
    if tr.business.scheme in ("44AD", "44ADA", "44AE"):
        decision = FormDecision(form="ITR-4")
        decision.reasons.append(
            f"Presumptive income is declared under section {tr.business.scheme}."
        )
        decision.supported = False
        decision.note = (
            "ITR-4 JSON generation is not built yet. The computation and the "
            "filing pack are still produced in full — enter the figures on the "
            "portal directly."
        )
        decision.disqualifications = blocks
        return decision

    if tr.has_business_income and tr.business.scheme == "none":
        decision = FormDecision(form="ITR-3", supported=False)
        decision.reasons.append("Business income is being reported with books of account.")
        decision.note = (
            "ITR-3 is beyond what this system generates. Use the computation "
            "and filing pack with the offline utility."
        )
        return decision

    if not blocks:
        decision = FormDecision(form="ITR-1")
        decision.reasons.append(
            "Resident individual with salary, one house property and other "
            "sources, all within the ITR-1 limits."
        )
        if ltcg_112a_total > 0:
            decision.reasons.append(
                f"Section 112A gains of ₹{ltcg_112a_total:,.0f} are within the "
                "₹1,25,000 that ITR-1 now accepts."
            )
        return decision

    decision = FormDecision(form="ITR-2", disqualifications=blocks)
    decision.reasons.append(
        "ITR-2 covers capital gains, more than one house property, foreign "
        "assets and income above ₹50 lakh — everything except business income."
    )
    return decision
