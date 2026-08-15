"""The computation engine.

``compute(return, regime)`` walks the statutory order: heads of income, then
set-off of losses, then Chapter VI-A, then tax on total income, then rebate,
surcharge and cess, then interest, then taxes already paid.

Nothing here knows about the web layer, and nothing here reads a rate from
anywhere but ``rules.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..money import D, non_negative, round_to_ten, rupees
from ..schemas import TaxReturn
from . import heads
from .chapter_via import DeductionResult, compute_deductions
from .interest import InterestResult, compute_interest_and_fee
from .rules import AssessmentYear, RegimeRules, age_band, get_ay

# Winnings under section 115BB are taxed at a flat 30% with no deduction,
# no basic-exemption adjustment and no rebate.
_RATE_115BB = D("0.30")


@dataclass
class SpecialRateSlice:
    code: str
    label: str
    income: Decimal
    rate: Decimal
    tax: Decimal
    # The ₹1.25 lakh threshold under section 112A. It shelters income from the
    # rate but does not leave total income.
    statutory_exemption: Decimal = D(0)
    basic_exemption_used: Decimal = D(0)

    @property
    def chargeable(self) -> Decimal:
        return non_negative(
            self.income - self.statutory_exemption - self.basic_exemption_used
        )


@dataclass
class Computation:
    """A complete, auditable computation for one regime."""

    regime: str
    regime_name: str
    assessment_year: str

    salary: Decimal = D(0)
    house_property: Decimal = D(0)
    business: Decimal = D(0)
    capital_gains: Decimal = D(0)
    other_sources: Decimal = D(0)

    gross_total_income: Decimal = D(0)
    deductions_total: Decimal = D(0)
    total_income: Decimal = D(0)
    total_income_rounded: Decimal = D(0)

    normal_income: Decimal = D(0)
    special_slices: List[SpecialRateSlice] = field(default_factory=list)

    tax_on_normal_income: Decimal = D(0)
    tax_on_special_income: Decimal = D(0)
    tax_before_rebate: Decimal = D(0)
    rebate_87a: Decimal = D(0)
    tax_after_rebate: Decimal = D(0)
    surcharge: Decimal = D(0)
    surcharge_marginal_relief: Decimal = D(0)
    cess: Decimal = D(0)
    relief_89: Decimal = D(0)
    relief_90_91: Decimal = D(0)
    total_tax_liability: Decimal = D(0)

    interest: InterestResult = field(default_factory=InterestResult)
    ftc: Optional[object] = None          # foreign.ftc.FTCResult when relevant

    tds: Decimal = D(0)
    tcs: Decimal = D(0)
    advance_tax: Decimal = D(0)
    self_assessment_tax: Decimal = D(0)
    total_taxes_paid: Decimal = D(0)

    net_payable: Decimal = D(0)      # positive => pay, negative => refund
    refund_due: Decimal = D(0)

    lines: List[heads.Line] = field(default_factory=list)
    deduction_detail: Optional[DeductionResult] = None
    carried_forward: Dict[str, Decimal] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def effective_rate(self) -> Decimal:
        if self.total_income <= 0:
            return D(0)
        return (self.total_tax_liability / self.total_income * D(100)).quantize(
            D("0.01")
        )


# --------------------------------------------------------------------------
# Slab arithmetic
# --------------------------------------------------------------------------


def tax_on_slabs(income: Decimal, bands) -> Decimal:
    """Progressive tax across a slab table."""
    income = non_negative(income)
    tax = D(0)
    lower = D(0)
    for band in bands:
        if band.upto is None:
            tax += non_negative(income - lower) * band.rate
            break
        taxable_here = min(income, band.upto) - lower
        if taxable_here > 0:
            tax += taxable_here * band.rate
        lower = band.upto
        if income <= lower:
            break
    return tax


def basic_exemption_limit(bands) -> Decimal:
    """The top of the nil-rate band."""
    for band in bands:
        if band.rate == 0 and band.upto is not None:
            return band.upto
    return D(0)


# --------------------------------------------------------------------------
# The main entry point
# --------------------------------------------------------------------------


def compute(
    tr: TaxReturn,
    regime_key: str,
    ay: AssessmentYear | None = None,
    foreign=None,
) -> Computation:
    # RSU vests, dividends and foreign sales are folded into ordinary return
    # entries first, so nothing below this line needs to know they exist. The
    # conversion does not depend on the regime, so callers computing both
    # regimes do it once and pass the result in.
    if foreign is None:
        from ..foreign.pipeline import apply_foreign

        tr, foreign = apply_foreign(tr)

    ay = ay or get_ay(tr.assessment_year)
    regime: RegimeRules = ay.regimes[regime_key]
    band_key = age_band(tr.taxpayer.date_of_birth, ay)
    slabs = regime.slabs_by_age[band_key]

    comp = Computation(
        regime=regime_key,
        regime_name=regime.name,
        assessment_year=ay.ay,
    )
    lines: List[heads.Line] = []

    # ---- Heads of income ---------------------------------------------------
    salary_result = heads.compute_salary(tr, regime)
    hp_result, hp_setoff = heads.compute_house_property(tr, regime)
    business_result = heads.compute_business(tr)
    cg_result = heads.compute_capital_gains(tr, ay)
    other_result, winnings = heads.compute_other_sources(tr, regime)

    lines.extend(salary_result.lines)
    lines.extend(hp_result.lines)
    lines.extend(business_result.lines)
    lines.extend(cg_result.lines)
    lines.extend(other_result.lines)

    comp.salary = salary_result.total
    comp.house_property = hp_result.total
    comp.business = business_result.total
    comp.capital_gains = cg_result.total
    comp.other_sources = other_result.total

    for source in (salary_result, hp_result, business_result, cg_result, other_result):
        comp.carried_forward.update(source.carried_forward)

    gti = (
        comp.salary + comp.house_property + comp.business
        + comp.capital_gains + comp.other_sources
    )
    comp.gross_total_income = non_negative(gti)
    lines.append(heads.Line("Gross total income", comp.gross_total_income,
                            is_subtotal=True))

    # ---- Chapter VI-A ------------------------------------------------------
    # Section 112A(6) and its siblings bar Chapter VI-A against special-rate
    # income, so the room available for deductions excludes those slices.
    special_income_total = sum(
        (b.taxable for b in cg_result.buckets.values()
         if b.rate is not None and b.taxable > 0),
        D(0),
    ) + winnings
    deduction_room = non_negative(comp.gross_total_income - special_income_total)

    salary_for_nps = sum((s.salary_17_1 for s in tr.salaries), D(0))
    deductions = compute_deductions(
        tr, regime, ay, deduction_room, salary_for_nps, band_key
    )
    comp.deduction_detail = deductions
    comp.deductions_total = deductions.total
    lines.append(heads.Line("Less: deductions under Chapter VI-A",
                            -comp.deductions_total, is_subtotal=True))

    comp.total_income = non_negative(comp.gross_total_income - comp.deductions_total)
    comp.total_income_rounded = rupees(comp.total_income)
    lines.append(heads.Line("Total income", comp.total_income, is_subtotal=True))

    # ---- Split normal and special-rate income ------------------------------
    normal_income = non_negative(comp.total_income - special_income_total)

    slices: List[SpecialRateSlice] = []
    for code, bucket in cg_result.buckets.items():
        if bucket.rate is None or bucket.taxable <= 0:
            continue
        slices.append(
            SpecialRateSlice(
                code, bucket.label, bucket.taxable, bucket.rate, D(0),
                statutory_exemption=min(bucket.statutory_exemption, bucket.taxable),
            )
        )
    if winnings > 0:
        slices.append(
            SpecialRateSlice("winnings_115bb", "Winnings u/s 115BB", winnings,
                             _RATE_115BB, D(0))
        )
    # Highest rate first: the unused basic exemption should shelter the most
    # expensive income.
    slices.sort(key=lambda s: s.rate, reverse=True)

    # ---- Unused basic exemption against special-rate income ----------------
    # Provisos to sections 111A(1), 112(1)(a) and 112A(2) let a resident set
    # the unexhausted basic exemption against these gains. Winnings under
    # 115BB get no such relief.
    exemption_limit = basic_exemption_limit(slabs)
    if tr.taxpayer.residential_status in ("RES", "RNOR") and normal_income < exemption_limit:
        spare = exemption_limit - normal_income
        for slice_ in slices:
            if spare <= 0:
                break
            if slice_.code == "winnings_115bb":
                continue
            used = min(spare, non_negative(slice_.income - slice_.statutory_exemption))
            slice_.basic_exemption_used = used
            spare -= used
        if any(s.basic_exemption_used for s in slices):
            comp.warnings.append(
                "Part of the basic exemption limit was unused against normal "
                "income and has been set against capital gains, as the provisos "
                "to sections 111A, 112 and 112A permit for residents."
            )

    for slice_ in slices:
        slice_.tax = slice_.chargeable * slice_.rate

    comp.special_slices = slices
    comp.normal_income = normal_income
    comp.tax_on_normal_income = tax_on_slabs(normal_income, slabs)
    comp.tax_on_special_income = sum((s.tax for s in slices), D(0))
    comp.tax_before_rebate = comp.tax_on_normal_income + comp.tax_on_special_income

    # ---- Section 87A rebate ------------------------------------------------
    comp.rebate_87a = _rebate_87a(comp, regime, slices)
    comp.tax_after_rebate = non_negative(comp.tax_before_rebate - comp.rebate_87a)

    # ---- Surcharge ---------------------------------------------------------
    comp.surcharge, comp.surcharge_marginal_relief = _surcharge(
        comp, regime, slabs, slices
    )

    # ---- Cess --------------------------------------------------------------
    comp.cess = (comp.tax_after_rebate + comp.surcharge) * ay.cess_rate

    total_before_relief = comp.tax_after_rebate + comp.surcharge + comp.cess

    # ---- Foreign tax credit — section 90 read with Rule 128 ---------------
    # Computed here rather than earlier because Rule 128 measures the credit
    # against the Indian tax actually attributable to the doubly-taxed income,
    # which is not known until surcharge and cess are on.
    if foreign is not None and foreign.foreign_tax_payments:
        from ..foreign.ftc import compute_ftc

        special_rate = None
        if comp.tax_on_special_income > 0:
            chargeable = sum((s.chargeable for s in slices), D(0))
            if chargeable > 0:
                special_rate = {
                    "capital_gains": comp.tax_on_special_income / chargeable
                }
        comp.ftc = compute_ftc(
            tr, foreign.foreign_tax_payments,
            total_income=comp.total_income,
            tax_before_credit=total_before_relief,
            special_rate_for=special_rate,
        )
        comp.relief_90_91 = comp.ftc.total_credit
        comp.warnings.extend(comp.ftc.warnings)

    comp.total_tax_liability = non_negative(
        total_before_relief - comp.relief_89 - comp.relief_90_91
    )

    # ---- Taxes already paid ------------------------------------------------
    paid = tr.taxes_paid
    comp.tds = paid.total("tds_salary", "tds_other")
    comp.tcs = paid.total("tcs")
    comp.advance_tax = paid.total("advance_tax")
    comp.self_assessment_tax = paid.total("self_assessment")
    comp.total_taxes_paid = (
        comp.tds + comp.tcs + comp.advance_tax + comp.self_assessment_tax
    )

    # ---- Interest and fee --------------------------------------------------
    instalments = [
        (p.payment_date or ay.fy_end, p.amount)
        for p in paid.payments
        if p.kind == "advance_tax"
    ]
    comp.interest = compute_interest_and_fee(
        ay,
        total_tax_liability=comp.total_tax_liability,
        tds_tcs=comp.tds + comp.tcs,
        advance_tax_instalments=instalments,
        self_assessment_paid=comp.self_assessment_tax,
        total_income=comp.total_income,
        filing_date=tr.filing_date,
        is_audit_case=False,
        has_only_pension_or_no_business=not tr.has_business_income,
        is_senior_citizen=band_key in ("senior", "super_senior"),
    )

    net = (
        comp.total_tax_liability + comp.interest.total - comp.total_taxes_paid
    )
    net = round_to_ten(net)
    if net >= 0:
        comp.net_payable = net
        comp.refund_due = D(0)
    else:
        comp.net_payable = D(0)
        comp.refund_due = -net

    comp.lines = lines
    _add_warnings(comp, tr, regime, ay)
    return comp


# --------------------------------------------------------------------------
# Rebate and surcharge
# --------------------------------------------------------------------------


def _rebate_87a(
    comp: Computation, regime: RegimeRules, slices: List[SpecialRateSlice]
) -> Decimal:
    rebate = regime.rebate
    if comp.total_income > rebate.income_ceiling:
        # Marginal relief: just above the ceiling the extra tax may not exceed
        # the extra income.
        if not rebate.marginal_relief:
            return D(0)
        excess_income = comp.total_income - rebate.income_ceiling
        # Relief applies only against tax on slab-rate income.
        rebatable = comp.tax_on_normal_income
        if rebatable > excess_income:
            return non_negative(rebatable - excess_income)
        return D(0)

    # Within the ceiling — work out how much tax the rebate may wipe out.
    if rebate.exclude_all_special:
        rebatable_tax = comp.tax_on_normal_income
    else:
        excluded = sum(
            (s.tax for s in slices if s.code in rebate.excluded_sections), D(0)
        )
        # Winnings under 115BB never qualify for the rebate.
        excluded += sum(
            (s.tax for s in slices if s.code == "winnings_115bb"), D(0)
        )
        rebatable_tax = non_negative(comp.tax_before_rebate - excluded)

    return min(rebatable_tax, rebate.max_rebate)


def _surcharge(
    comp: Computation,
    regime: RegimeRules,
    slabs,
    slices: List[SpecialRateSlice],
) -> Tuple[Decimal, Decimal]:
    """Surcharge with the 15% cap on special-rate income and marginal relief."""
    income = comp.total_income
    band = None
    for candidate in regime.surcharge_bands:
        if income > candidate.threshold:
            band = candidate
    if band is None:
        return D(0), D(0)

    cap = regime.surcharge_cap_on_special_income
    special_rate = min(band.rate, cap)

    # Tax on the capped slice attracts the capped rate; the rest the full rate.
    capped_tax = sum((s.tax for s in slices), D(0))
    normal_tax = non_negative(comp.tax_after_rebate - capped_tax)

    surcharge = normal_tax * band.rate + capped_tax * special_rate

    # ---- Marginal relief ---------------------------------------------------
    # Tax plus surcharge on the income above the threshold may not exceed the
    # tax at the threshold plus the whole of the excess income.
    excess = income - band.threshold
    tax_at_threshold = _tax_at_income(
        band.threshold, comp, regime, slabs, slices
    )
    previous_band = None
    for candidate in regime.surcharge_bands:
        if candidate.threshold < band.threshold:
            previous_band = candidate
    if previous_band is not None:
        prev_rate = min(previous_band.rate, cap)
        tax_at_threshold += tax_at_threshold * prev_rate

    ceiling = tax_at_threshold + excess
    relief = D(0)
    if comp.tax_after_rebate + surcharge > ceiling:
        relieved = non_negative(ceiling - comp.tax_after_rebate)
        relief = surcharge - relieved
        surcharge = relieved

    return surcharge, relief


def _tax_at_income(
    income: Decimal,
    comp: Computation,
    regime: RegimeRules,
    slabs,
    slices: List[SpecialRateSlice],
) -> Decimal:
    """Tax (before surcharge and cess) on a hypothetical lower total income.

    Used only for surcharge marginal relief. The shortfall is taken off the
    slab-rate income first and then off the special-rate slices from the
    lowest rate upward, which mirrors how the relief is worked out in practice.
    """
    reduction = non_negative(comp.total_income - income)
    normal = comp.normal_income
    take_from_normal = min(reduction, normal)
    normal -= take_from_normal
    reduction -= take_from_normal

    tax = tax_on_slabs(normal, slabs)
    for slice_ in sorted(slices, key=lambda s: s.rate):
        chargeable = slice_.chargeable
        take = min(reduction, chargeable)
        reduction -= take
        tax += (chargeable - take) * slice_.rate
    return tax


# --------------------------------------------------------------------------
# Advisory warnings
# --------------------------------------------------------------------------


def _add_warnings(
    comp: Computation, tr: TaxReturn, regime: RegimeRules, ay: AssessmentYear
) -> None:
    if comp.deduction_detail and comp.deduction_detail.disallowed:
        sections = ", ".join(
            sorted({line.section for line in comp.deduction_detail.disallowed})
        )
        comp.warnings.append(
            f"Under {regime.name} these claims fall away: {sections}."
        )
    if comp.interest.section_234f:
        comp.warnings.append(
            f"A late-filing fee of ₹{comp.interest.section_234f:,.0f} is "
            "payable under section 234F."
        )
    if comp.carried_forward:
        detail = ", ".join(
            f"{key.replace('_', ' ')} ₹{value:,.0f}"
            for key, value in comp.carried_forward.items()
        )
        comp.warnings.append(
            f"Losses carried forward to the next year: {detail}. "
            "These survive only if the return is filed by the due date."
        )


# --------------------------------------------------------------------------
# Regime comparison
# --------------------------------------------------------------------------


@dataclass
class RegimeComparison:
    new: Computation
    old: Computation
    recommended: str
    saving: Decimal
    # The prepared return (with RSU perquisite and foreign gains folded in)
    # and what the foreign pass produced, for the review screens.
    prepared: Optional[TaxReturn] = None
    foreign: Optional[object] = None

    @property
    def chosen(self) -> Computation:
        return self.new if self.recommended == "new" else self.old


def compare_regimes(tr: TaxReturn, ay: AssessmentYear | None = None) -> RegimeComparison:
    from ..foreign.pipeline import apply_foreign

    prepared, foreign = apply_foreign(tr)
    ay = ay or get_ay(prepared.assessment_year)
    new = compute(prepared, "new", ay, foreign=foreign)
    old = compute(prepared, "old", ay, foreign=foreign)

    new_cost = new.total_tax_liability + new.interest.total_interest
    old_cost = old.total_tax_liability + old.interest.total_interest

    if tr.regime_choice in ("new", "old"):
        recommended = tr.regime_choice
    else:
        recommended = "new" if new_cost <= old_cost else "old"

    saving = abs(new_cost - old_cost)
    return RegimeComparison(
        new=new, old=old, recommended=recommended, saving=saving,
        prepared=prepared, foreign=foreign,
    )
