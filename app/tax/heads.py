"""Head-wise income computation: salary, house property, business, capital
gains and other sources.

Each function returns both a number and a line-by-line breakdown, because a tax
computation the user cannot audit is a tax computation the user cannot trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..money import D, non_negative, rupees
from ..schemas import (
    CapitalGainItem,
    HouseProperty,
    OtherSourcesIncome,
    TaxReturn,
)
from .rules import AssessmentYear, RegimeRules

# Section 10 exemptions that the new regime withdraws. Anything not on this
# list (gratuity, leave encashment, retrenchment compensation, VRS, PF, the
# transport allowance for a disabled employee, conveyance for official duty)
# survives in both regimes.
_EXEMPTIONS_DENIED_IN_NEW_REGIME = {
    "hra",
    "lta",
    "children_education_allowance",
    "hostel_allowance",
    "academic_allowance",
    "uniform_allowance",
    "helper_allowance",
    "special_allowance_10_14",
    "food_coupons",
}


@dataclass
class Line:
    """One row of the computation sheet."""

    label: str
    amount: Decimal
    note: str = ""
    is_subtotal: bool = False


@dataclass
class HeadResult:
    total: Decimal
    lines: List[Line] = field(default_factory=list)
    # Losses that could not be absorbed this year.
    carried_forward: Dict[str, Decimal] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Salary
# --------------------------------------------------------------------------


def compute_salary(tr: TaxReturn, regime: RegimeRules) -> HeadResult:
    lines: List[Line] = []
    gross_total = D(0)
    exempt_total = D(0)
    section_16_total = D(0)

    for salary in tr.salaries:
        employer = salary.employer_name or "Employer"
        gross = salary.gross_salary
        gross_total += gross
        lines.append(Line(f"{employer} — gross salary u/s 17", gross))

        for name, amount in salary.exempt_allowances.items():
            key = name.strip().lower().replace(" ", "_")
            if regime.key == "new" and key in _EXEMPTIONS_DENIED_IN_NEW_REGIME:
                lines.append(
                    Line(f"  {name} — not exempt under the new regime", D(0),
                         note="Section 10 exemption withdrawn by section 115BAC")
                )
                continue
            exempt_total += amount
            lines.append(Line(f"  Less: {name} exempt u/s 10", -amount))

        # Section 16(iii) professional tax and 16(ii) entertainment allowance
        # are old-regime only.
        if regime.key == "old":
            if salary.professional_tax:
                section_16_total += salary.professional_tax
                lines.append(
                    Line("  Less: professional tax u/s 16(iii)",
                         -salary.professional_tax)
                )
            if salary.entertainment_allowance:
                allowed = min(salary.entertainment_allowance, D("5000"))
                section_16_total += allowed
                lines.append(
                    Line("  Less: entertainment allowance u/s 16(ii)", -allowed)
                )

    net_before_std = gross_total - exempt_total - section_16_total

    standard_deduction = D(0)
    if tr.salaries:
        # Section 16(ia) is capped at the salary itself.
        standard_deduction = min(
            regime.standard_deduction_salary, non_negative(net_before_std)
        )
        lines.append(
            Line("Less: standard deduction u/s 16(ia)", -standard_deduction,
                 is_subtotal=False)
        )

    total = non_negative(net_before_std - standard_deduction)
    lines.append(Line("Income chargeable under the head Salaries", total,
                      is_subtotal=True))
    return HeadResult(total=total, lines=lines)


# --------------------------------------------------------------------------
# House property
# --------------------------------------------------------------------------


def compute_house_property(
    tr: TaxReturn, regime: RegimeRules
) -> Tuple[HeadResult, Decimal]:
    """Returns the head result and the loss available for inter-head set-off.

    Section 71(3A) caps the set-off against other heads at ₹2,00,000; the
    balance is carried forward for eight years under section 71B. Under the
    new regime no set-off is allowed at all — section 115BAC(2)(i).
    """
    lines: List[Line] = []
    head_total = D(0)

    for prop in tr.house_properties:
        share = prop.ownership_share if prop.ownership_share > 0 else D(1)
        label = prop.address or prop.property_type
        is_self_occupied = prop.property_type == "SOP"

        if is_self_occupied:
            gav = D(0)
            lines.append(Line(f"{label} (self-occupied) — annual value", D(0)))
            net_annual_value = D(0)
            std_deduction = D(0)
        else:
            gross_rent = prop.annual_rent_received - prop.unrealised_rent
            gav = non_negative(gross_rent) * share
            municipal = prop.municipal_taxes_paid * share
            net_annual_value = non_negative(gav - municipal)
            std_deduction = net_annual_value * D("0.30")
            lines.append(Line(f"{label} — gross annual value", gav))
            if municipal:
                lines.append(Line("  Less: municipal taxes paid", -municipal))
            lines.append(Line("  Less: standard deduction @30% u/s 24(a)",
                              -std_deduction))

        interest = (prop.interest_24b + prop.pre_construction_interest) * share
        if is_self_occupied:
            allowed_interest = min(interest, regime.self_occupied_interest_cap)
            if interest > allowed_interest:
                note = (
                    "Interest on a self-occupied house is not deductible under "
                    "the new regime"
                    if regime.key == "new"
                    else "Capped at ₹2,00,000 by the proviso to section 24(b)"
                )
                lines.append(
                    Line("  Interest u/s 24(b) disallowed",
                         -(interest - allowed_interest) * -1, note=note)
                )
            interest = allowed_interest

        if interest:
            lines.append(Line("  Less: interest on borrowed capital u/s 24(b)",
                              -interest))

        property_income = net_annual_value - std_deduction - interest
        lines.append(Line(f"  Net income from {label}", property_income,
                          is_subtotal=True))
        head_total += property_income

    setoff_available = D(0)
    carried_forward = D(0)

    if head_total < 0:
        loss = -head_total
        if not regime.allow_house_property_setoff:
            setoff_available = D(0)
            carried_forward = loss
            lines.append(
                Line("House property loss — set-off not available", D(0),
                     note="Section 115BAC(2)(i) bars set-off against other heads")
            )
        else:
            setoff_available = min(loss, D("200000"))
            carried_forward = loss - setoff_available
            if carried_forward > 0:
                lines.append(
                    Line("House property loss carried forward u/s 71B",
                         carried_forward,
                         note="Set-off against other heads is capped at "
                              "₹2,00,000 by section 71(3A)")
                )

    reported_total = head_total if head_total >= 0 else -setoff_available
    lines.append(Line("Income from house property", reported_total,
                      is_subtotal=True))

    result = HeadResult(total=reported_total, lines=lines)
    if carried_forward:
        result.carried_forward["house_property"] = carried_forward
    return result, setoff_available


# --------------------------------------------------------------------------
# Business (presumptive)
# --------------------------------------------------------------------------


def compute_business(tr: TaxReturn) -> HeadResult:
    lines: List[Line] = []
    business = tr.business
    total = D(0)

    if business.scheme == "44AD":
        digital = business.gross_turnover_digital * D("0.06")
        cash = business.gross_turnover_cash * D("0.08")
        if business.gross_turnover_digital:
            lines.append(Line("Presumptive income @6% on digital receipts u/s 44AD",
                              digital))
        if business.gross_turnover_cash:
            lines.append(Line("Presumptive income @8% on cash receipts u/s 44AD",
                              cash))
        total = digital + cash
    elif business.scheme == "44ADA":
        total = business.gross_receipts_44ada * D("0.50")
        lines.append(Line("Presumptive income @50% of gross receipts u/s 44ADA",
                          total))
    elif business.scheme == "44AE":
        total = business.declared_income_44ae
        lines.append(Line("Presumptive income u/s 44AE", total))

    if business.higher_declared_income is not None and business.higher_declared_income > total:
        lines.append(Line("Higher income voluntarily declared",
                          business.higher_declared_income - total))
        total = business.higher_declared_income

    if total:
        lines.append(Line("Profits and gains of business or profession", total,
                          is_subtotal=True))
    return HeadResult(total=total, lines=lines)


# --------------------------------------------------------------------------
# Capital gains
# --------------------------------------------------------------------------


@dataclass
class CapitalGainBucket:
    code: str
    label: str
    rate: Optional[Decimal]
    gross_gain: Decimal = D(0)
    exemption_54: Decimal = D(0)
    statutory_exemption: Decimal = D(0)   # the ₹1.25 lakh under section 112A
    losses_set_off: Decimal = D(0)
    taxable: Decimal = D(0)


@dataclass
class CapitalGainResult(HeadResult):
    buckets: Dict[str, CapitalGainBucket] = field(default_factory=dict)
    # Gains that fall into the slab-rate pool rather than a special rate.
    slab_taxable: Decimal = D(0)


def compute_capital_gains(tr: TaxReturn, ay: AssessmentYear) -> CapitalGainResult:
    """Bucket every transaction, set off losses, apply exemptions.

    Loss set-off follows section 74: a short-term capital loss may be set off
    against any capital gain, while a long-term loss may only go against a
    long-term gain. Within those constraints we set off against the
    highest-taxed bucket first, which is the taxpayer's prerogative.
    """
    lines: List[Line] = []
    buckets: Dict[str, CapitalGainBucket] = {}

    for code, rule in ay.capital_gains.items():
        buckets[code] = CapitalGainBucket(
            code=code, label=rule.label, rate=rule.rate
        )

    resident = tr.taxpayer.residential_status == "RES"

    for item in tr.capital_gains:
        rule = ay.capital_gains.get(item.category)
        if rule is None:
            continue
        category = item.category
        gain = item.net_gain
        note = item.source_document

        if rule.grandfathered_indexation_option:
            category, gain, note = _choose_indexation_option(
                item, rule, gain, resident, note
            )

        bucket = buckets[category]
        bucket.gross_gain += gain
        bucket.exemption_54 += item.exemption_amount
        if item.description:
            lines.append(
                Line(f"  {item.description} ({buckets[category].label})", gain,
                     note=note)
            )

    # Section 54/54F/54EC exemptions genuinely reduce the gain. The ₹1.25 lakh
    # under section 112A does not: section 112A(2) makes it a threshold in the
    # *rate* computation, so the full gain still enters total income and counts
    # towards the 87A ceiling and the surcharge thresholds. It is recorded on
    # the bucket and consumed by the engine at the tax stage.
    for code, bucket in buckets.items():
        rule = ay.capital_gains[code]
        bucket.taxable = bucket.gross_gain - bucket.exemption_54
        bucket.statutory_exemption = rule.exemption

    _set_off_capital_losses(buckets, tr, lines)

    slab_taxable = D(0)
    special_total = D(0)
    for code, bucket in buckets.items():
        if bucket.taxable <= 0:
            continue
        if bucket.rate is None:
            slab_taxable += bucket.taxable
        else:
            special_total += bucket.taxable
        lines.append(Line(f"{bucket.label}", bucket.taxable, is_subtotal=True))

    total = slab_taxable + special_total
    lines.append(Line("Income under the head Capital Gains", total,
                      is_subtotal=True))

    result = CapitalGainResult(total=total, lines=lines)
    result.buckets = buckets
    result.slab_taxable = slab_taxable

    for code, bucket in buckets.items():
        if bucket.taxable < 0:
            key = "ltcl" if code.startswith("ltcg") else "stcl"
            result.carried_forward[key] = (
                result.carried_forward.get(key, D(0)) - bucket.taxable
            )
    return result


_INDEXATION_CUTOFF = _date(2024, 7, 23)


def _choose_indexation_option(
    item: CapitalGainItem,
    rule,
    unindexed_gain: Decimal,
    resident: bool,
    note: str,
) -> Tuple[str, Decimal, str]:
    """Pick the cheaper of 12.5% flat and 20% with indexation.

    The option is available only to a resident individual or HUF, and only for
    land or building acquired before 23 July 2024 — fifth proviso to section
    112(1). The comparison is on *tax*, not on gain, so a larger indexed gain
    can still lose to a smaller flat-rate one.
    """
    eligible = (
        resident
        and item.indexed_cost_of_acquisition is not None
        and item.purchase_date is not None
        and item.purchase_date < _INDEXATION_CUTOFF
    )
    if not eligible:
        return item.category, unindexed_gain, note

    indexed_gain = non_negative(
        item.sale_consideration
        - item.indexed_cost_of_acquisition
        - item.transfer_expenses
    )
    flat_tax = non_negative(unindexed_gain) * rule.rate
    indexed_tax = indexed_gain * (rule.indexed_rate or rule.rate)

    if indexed_tax < flat_tax:
        return (
            "ltcg_112_property_indexed",
            indexed_gain,
            (note + " · " if note else "")
            + "20% with indexation chosen — cheaper than 12.5% flat",
        )
    return item.category, unindexed_gain, note


def _set_off_capital_losses(
    buckets: Dict[str, CapitalGainBucket],
    tr: TaxReturn,
    lines: List[Line],
) -> None:
    """Section 70/74 set-off, current-year first, then brought forward."""

    def positive(codes: List[str]) -> List[CapitalGainBucket]:
        # Highest rate first so the loss shelters the most expensive gain.
        chosen = [buckets[c] for c in codes if buckets[c].taxable > 0]
        return sorted(chosen, key=lambda b: b.rate or D("0.30"), reverse=True)

    long_codes = [c for c in buckets if c.startswith("ltcg")]
    short_codes = [c for c in buckets if c.startswith("stcg")]

    def absorb(loss: Decimal, targets: List[CapitalGainBucket], label: str) -> Decimal:
        for bucket in targets:
            if loss <= 0:
                break
            used = min(loss, bucket.taxable)
            bucket.taxable -= used
            bucket.losses_set_off += used
            loss -= used
            lines.append(Line(f"  Less: {label} set off against {bucket.label}",
                              -used))
        return loss

    # Current-year long-term losses go only against long-term gains.
    for code in long_codes:
        bucket = buckets[code]
        if bucket.taxable < 0:
            remaining = absorb(-bucket.taxable,
                               positive([c for c in long_codes if c != code]),
                               "long-term capital loss")
            bucket.taxable = -remaining

    # Current-year short-term losses may go against any capital gain.
    for code in short_codes:
        bucket = buckets[code]
        if bucket.taxable < 0:
            remaining = absorb(
                -bucket.taxable,
                positive([c for c in long_codes + short_codes if c != code]),
                "short-term capital loss",
            )
            bucket.taxable = -remaining

    # Brought-forward losses under section 74.
    bf_ltcl = sum((l.ltcl for l in tr.brought_forward_losses), D(0))
    bf_stcl = sum((l.stcl for l in tr.brought_forward_losses), D(0))
    if bf_ltcl:
        left = absorb(bf_ltcl, positive(long_codes),
                      "brought-forward long-term capital loss")
        if left:
            lines.append(Line("Brought-forward long-term loss carried forward",
                              left))
    if bf_stcl:
        left = absorb(bf_stcl, positive(long_codes + short_codes),
                      "brought-forward short-term capital loss")
        if left:
            lines.append(Line("Brought-forward short-term loss carried forward",
                              left))


# --------------------------------------------------------------------------
# Other sources
# --------------------------------------------------------------------------


def compute_other_sources(
    tr: TaxReturn, regime: RegimeRules
) -> Tuple[HeadResult, Decimal]:
    """Returns the head result and the winnings taxed at the flat 115BB rate."""
    src: OtherSourcesIncome = tr.other_sources
    lines: List[Line] = []

    components = [
        ("Interest from savings bank accounts", src.savings_bank_interest),
        ("Interest from fixed and recurring deposits", src.fixed_deposit_interest),
        ("Other interest income", src.other_interest),
        ("Interest on income-tax refund", src.income_tax_refund_interest),
        ("Dividend income", src.dividend_income),
        ("Taxable gifts u/s 56(2)(x)", src.gifts_taxable),
        ("Any other income", src.other_income),
    ]
    total = D(0)
    for label, amount in components:
        if amount:
            lines.append(Line(label, amount))
            total += amount

    if src.family_pension:
        lines.append(Line("Family pension received", src.family_pension))
        deduction = min(
            src.family_pension * regime.family_pension_deduction_fraction,
            regime.family_pension_deduction_cap,
        )
        lines.append(Line("Less: deduction u/s 57(iia)", -deduction))
        total += src.family_pension - deduction

    if src.section_57_deductions and regime.key == "old":
        lines.append(Line("Less: expenses u/s 57", -src.section_57_deductions))
        total -= src.section_57_deductions

    winnings = src.winnings_115bb
    if winnings:
        lines.append(
            Line("Winnings from lotteries, games and betting u/s 115BB", winnings,
                 note="Taxed at a flat 30%; no deduction or rebate is allowed")
        )
        total += winnings

    total = non_negative(total)
    lines.append(Line("Income from other sources", total, is_subtotal=True))
    return HeadResult(total=total, lines=lines), winnings
