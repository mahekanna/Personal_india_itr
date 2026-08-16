"""The computation engine.

``compute(return, regime)`` walks the statutory order: heads of income, then
set-off of losses, then Chapter VI-A, then tax on total income, then rebate,
surcharge and cess, then interest, then taxes already paid.

Nothing here knows about the web layer, and nothing here reads a rate from
anywhere but ``rules.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..money import D, non_negative, round_to_ten, rupees
from ..schemas import TaxReturn
from . import heads
from .advance_tax import DeferrableItem
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
    # What the salary head actually allowed, for the ITR schedules to report.
    salary_standard_deduction: Decimal = D(0)
    salary_exempt_allowed: Decimal = D(0)
    salary_section_16_other: Decimal = D(0)
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
    # Dividend income sitting inside the slab-rate pool. Taxed at slab rates
    # like any other income, but the surcharge on it is capped at 15%.
    dividend_in_normal_income: Decimal = D(0)

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
    trading: Optional[object] = None       # tax.trading.TradingResult
    audit_required: bool = False
    ftc: Optional[object] = None          # foreign.ftc.FTCResult when relevant
    # Income the proviso to section 234C excuses from the earlier instalments.
    deferrable: List[DeferrableItem] = field(default_factory=list)

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
    # The heads as each was computed, before any inter-head set-off under
    # section 71. The schedules describe the head; Part B-TI reconciles the
    # total after set-off. Both figures are real and they are not the same.
    head_before_setoff: Dict[str, Decimal] = field(default_factory=dict)
    # How much of each head's loss the other heads actually absorbed.
    loss_set_off: Dict[str, Decimal] = field(default_factory=dict)
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

    comp.trading = getattr(business_result, "trading", None)
    # Trading income is business income however the taxpayer thinks of it, and
    # it changes the due date, the form and the regime mechanics.
    has_business = bool(
        tr.has_business_income
        or tr.business.scheme != "none"
        or (comp.trading is not None and comp.trading.has_anything)
    )
    comp.salary = salary_result.total
    comp.salary_standard_deduction = salary_result.standard_deduction
    comp.salary_exempt_allowed = salary_result.exempt_allowed
    comp.salary_section_16_other = salary_result.section_16_other
    comp.house_property = hp_result.total
    comp.business = business_result.total
    comp.capital_gains = cg_result.total
    comp.other_sources = other_result.total

    for source in (salary_result, hp_result, business_result, cg_result, other_result):
        comp.carried_forward.update(source.carried_forward)

    comp.head_before_setoff = {
        "salary": comp.salary,
        "house_property": comp.house_property,
        "business": comp.business,
        "capital_gains": comp.capital_gains,
        "other_sources": comp.other_sources,
    }

    # A house-property loss may only be set off against income that actually
    # exists. Section 71(3A) caps the set-off at ₹2,00,000 — heads.py has
    # already done that — but if the other heads cannot absorb even that much,
    # the balance is carried forward under section 71B rather than vanishing
    # into a clamp at zero.
    if comp.house_property < 0:
        loss = -comp.house_property
        comp.house_property = D(0)
        unabsorbed = _absorb_loss(
            loss, comp, cg_result, lines,
            allow_salary=True, label="house property loss u/s 71",
        )
        absorbed = loss - unabsorbed
        comp.loss_set_off["house_property"] = absorbed
        if unabsorbed > 0:
            comp.carried_forward["house_property"] = (
                comp.carried_forward.get("house_property", D(0)) + unabsorbed
            )
            lines.append(heads.Line(
                "House property loss not absorbed this year — carried forward",
                unabsorbed,
                note="Section 71B: eight assessment years, and only if this "
                     "return is filed by the due date",
            ))
    # Statutory order: current-year inter-head set-off under section 71 first,
    # then the brought-forward losses under sections 71B, 72 and 73.
    _set_off_business_loss(comp, lines, cg_result)
    _set_off_brought_forward(tr, comp, lines, business_result)

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
    # The 15% cap covers dividend income as well as the special-rate capital
    # gains, so the dividend has to be picked out of the slab-rate pool.
    comp.dividend_in_normal_income = _dividend_in_total_income(tr, normal_income)
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
    comp.deferrable = build_deferrable(tr, comp)
    # An audit case files by 31 October rather than 31 July, which moves both
    # the section 234A interest and the section 234F fee. It was hardcoded to
    # False, which charged a trader for being late when they were not.
    comp.audit_required = bool(
        tr.business.books_audited
        or (comp.trading is not None and comp.trading.audit_required)
    )
    comp.interest = compute_interest_and_fee(
        ay,
        total_tax_liability=comp.total_tax_liability,
        tds_tcs=comp.tds + comp.tcs,
        advance_tax_instalments=instalments,
        self_assessment_paid=comp.self_assessment_tax,
        total_income=comp.total_income,
        filing_date=tr.filing_date,
        is_audit_case=comp.audit_required,
        has_business=has_business,
        has_only_pension_or_no_business=not has_business,
        is_senior_citizen=band_key in ("senior", "super_senior"),
        deferrable=comp.deferrable,
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


def _absorb_loss(
    loss: Decimal,
    comp: Computation,
    cg_result,
    lines: List[heads.Line],
    *,
    allow_salary: bool,
    label: str,
) -> Decimal:
    """Set a loss against the other heads, and say which income it consumed.

    Netting the loss into gross total income and leaving the heads alone is not
    good enough. The special-rate capital-gains slices are built from the
    buckets, so a loss that netted away in the total would leave a 12.5% slice
    still standing and quietly take the relief out of slab-rate income instead
    — which, where section 71(2A) bars the loss from salary, is exactly the
    set-off the statute refuses.

    Order of consumption is the taxpayer's prerogative, and the choice made
    here is to spend the loss on the most expensive income first: slab-rate
    income, then the special-rate buckets from the highest rate down. Sheltering
    a 12.5% gain while leaving 30% income exposed would be giving relief away.

    Returns what could not be absorbed.
    """
    remaining = loss

    slab_pools = ["other_sources", "house_property"]
    if allow_salary:
        slab_pools.insert(0, "salary")
    for attribute in slab_pools:
        if remaining <= 0:
            break
        available = non_negative(getattr(comp, attribute))
        used = min(remaining, available)
        if used > 0:
            setattr(comp, attribute, getattr(comp, attribute) - used)
            remaining -= used
            lines.append(heads.Line(
                f"  Less: {label} set off against "
                f"{attribute.replace('_', ' ')}", -used,
            ))

    # Capital gains, slab-rate buckets before the concessional ones.
    if cg_result is not None:
        buckets = sorted(
            (b for b in cg_result.buckets.values() if b.taxable > 0),
            key=lambda b: (b.rate is not None, -(b.rate or D(0))),
        )
        for bucket in buckets:
            if remaining <= 0:
                break
            used = min(remaining, bucket.taxable)
            bucket.taxable -= used
            bucket.losses_set_off += used
            remaining -= used
            lines.append(heads.Line(
                f"  Less: {label} set off against {bucket.label}", -used,
            ))
        comp.capital_gains = sum(
            (b.taxable for b in cg_result.buckets.values() if b.taxable > 0),
            D(0),
        )

    return remaining


def _set_off_business_loss(
    comp: Computation, lines: List[heads.Line], cg_result
) -> None:
    """Section 71(2A): a business loss meets every head except salary.

    An F&O year that ends in the red can be set against capital gains and
    against other sources — including the gain on a US share sale — but never
    against the salary the RSU vested into. What none of those absorb is
    carried forward eight years under section 72.

    Speculation never reaches here: section 73 keeps an intraday loss inside
    its own ring, and ``compute_business`` has already held it back.
    """
    if comp.business >= 0:
        return

    loss = -comp.business
    # The head itself contributes nothing further: what it absorbed has been
    # taken off the other heads, and what it did not is carried forward.
    comp.business = D(0)
    unabsorbed = _absorb_loss(
        loss, comp, cg_result, lines,
        allow_salary=False, label="business loss u/s 71",
    )
    comp.loss_set_off["business"] = loss - unabsorbed

    if unabsorbed > 0:
        comp.carried_forward["business"] = (
            comp.carried_forward.get("business", D(0)) + unabsorbed
        )
        lines.append(heads.Line(
            "Business loss carried forward u/s 72", unabsorbed,
            note="Eight assessment years, and only if this return is filed by "
                 "the due date",
        ))
        if comp.salary > 0:
            comp.warnings.append(
                f"₹{unabsorbed:,.0f} of business loss could not be set off this "
                "year. Section 71(2A) does not permit it against salary, so it "
                "is carried forward for eight years under section 72 — but only "
                "if this return is filed by the due date. A belated return "
                "forfeits the carry-forward entirely."
            )


def _set_off_brought_forward(
    tr: TaxReturn, comp: Computation, lines: List[heads.Line],
    business_result=None,
) -> None:
    """Brought-forward house-property, business and speculation losses.

    Section 71B lets an unabsorbed house-property loss be carried for eight
    years and section 72 a business loss for eight — but each may only be set
    off against income under **the same head**. Section 73 is stricter still: a
    speculation loss meets speculative income and nothing else, and it lapses
    after four years. Capital losses are handled inside ``heads.py``, where the
    bucket-by-bucket ordering of section 74 matters.
    """
    pairs = (
        ("house_property_loss", "house_property",
         "brought-forward house property loss u/s 71B"),
        ("business_loss", "business", "brought-forward business loss u/s 72"),
    )
    for field_name, head, label in pairs:
        brought = sum(
            (getattr(loss, field_name) for loss in tr.brought_forward_losses),
            D(0),
        )
        if brought <= 0:
            continue
        available = non_negative(getattr(comp, head))
        used = min(brought, available)
        if used > 0:
            setattr(comp, head, getattr(comp, head) - used)
            lines.append(heads.Line(f"  Less: {label}", -used))
        left = brought - used
        if left > 0:
            key = f"{head}_brought_forward"
            comp.carried_forward[key] = comp.carried_forward.get(key, D(0)) + left

    # ---- Section 73: speculation, and only speculation ---------------------
    brought_speculative = sum(
        (loss.speculative_loss for loss in tr.brought_forward_losses), D(0)
    )
    if brought_speculative <= 0:
        return

    # It can only meet this year's speculative *profit*, which is the part of
    # the business head that came from intraday trading. Setting it against the
    # F&O profit sitting in the same head would be unlawful.
    speculative_profit = non_negative(
        getattr(business_result, "speculative", D(0))
    )
    used = min(brought_speculative, speculative_profit, non_negative(comp.business))
    if used > 0:
        comp.business -= used
        lines.append(heads.Line(
            "  Less: brought-forward speculation loss u/s 73", -used,
            note="Set off only against this year's speculative income",
        ))
    left = brought_speculative - used
    if left > 0:
        comp.carried_forward["speculative_brought_forward"] = (
            comp.carried_forward.get("speculative_brought_forward", D(0)) + left
        )
        if speculative_profit <= 0:
            comp.warnings.append(
                f"₹{left:,.0f} of brought-forward speculation loss could not be "
                "used: there is no speculative income this year to set it "
                "against, and section 73 does not allow it against F&O profit "
                "or anything else. It lapses four assessment years after the "
                "year it arose."
            )


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


def _dividend_in_total_income(tr: TaxReturn, normal_income: Decimal) -> Decimal:
    """Dividend income, domestic and foreign, as it sits in total income.

    Section 57 expenses set against it come off first, and the result cannot
    exceed the slab-rate pool it is part of.
    """
    src = tr.other_sources
    gross = src.dividend_income + src.foreign_dividend_income
    if gross <= 0:
        return D(0)
    net = non_negative(gross - min(src.section_57_deductions, gross))
    return min(net, non_negative(normal_income))


def _dividend_slab_tax(
    normal_income: Decimal, dividend: Decimal, slabs
) -> Decimal:
    """Tax on the dividend, treating it as the top slice of slab-rate income.

    That is how the department's own utility attributes it, and it is the
    reading that favours the taxpayer, since the top slice bears the highest
    marginal rate and so gets the most out of the 15% cap.
    """
    dividend = min(non_negative(dividend), non_negative(normal_income))
    if dividend <= 0:
        return D(0)
    return non_negative(
        tax_on_slabs(normal_income, slabs)
        - tax_on_slabs(normal_income - dividend, slabs)
    )


def _capped_and_uncapped(
    comp: Computation, slabs, slices: List[SpecialRateSlice]
) -> Tuple[Decimal, Decimal]:
    """Split the tax into the part the 15% cap covers and the part it does not.

    The proviso to Paragraph A of Part I of the First Schedule caps surcharge at
    15% on income by way of dividend and on income under sections 111A, 112 and
    112A. Winnings under section 115BB are not on that list, so they bear the
    full rate — including 25% or 37% — like ordinary income.
    """
    capped = sum(
        (s.tax for s in slices if s.code != "winnings_115bb"), D(0)
    )
    capped += _dividend_slab_tax(
        comp.normal_income, comp.dividend_in_normal_income, slabs
    )
    capped = min(capped, non_negative(comp.tax_after_rebate))
    return capped, non_negative(comp.tax_after_rebate - capped)


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
    capped_tax, uncapped_tax = _capped_and_uncapped(comp, slabs, slices)
    surcharge = uncapped_tax * band.rate + capped_tax * min(band.rate, cap)

    # ---- Marginal relief ---------------------------------------------------
    # Tax plus surcharge on the income above the threshold may not exceed the
    # tax plus surcharge at the threshold plus the whole of the excess income.
    excess = income - band.threshold
    tax_at_threshold, capped_at_threshold = _tax_at_income(
        band.threshold, comp, slabs, slices
    )
    previous_band = None
    for candidate in regime.surcharge_bands:
        if candidate.threshold < band.threshold:
            previous_band = candidate
    if previous_band is not None:
        # The same split applies at the threshold — applying the capped rate to
        # the whole of the tax there would understate the ceiling and hand out
        # relief nobody is entitled to.
        uncapped_at_threshold = non_negative(tax_at_threshold - capped_at_threshold)
        tax_at_threshold += (
            uncapped_at_threshold * previous_band.rate
            + capped_at_threshold * min(previous_band.rate, cap)
        )

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
    slabs,
    slices: List[SpecialRateSlice],
) -> Tuple[Decimal, Decimal]:
    """Tax on a hypothetical lower total income, and how much of it is capped.

    Used only for surcharge marginal relief. The shortfall is taken off the
    slab-rate income first and then off the special-rate slices from the
    lowest rate upward, which mirrors how the relief is worked out in practice.
    Returns the tax before surcharge and cess, and the portion of it that the
    15% cap covers.
    """
    reduction = non_negative(comp.total_income - income)
    normal = comp.normal_income
    take_from_normal = min(reduction, normal)
    normal -= take_from_normal
    reduction -= take_from_normal

    tax = tax_on_slabs(normal, slabs)
    # Dividends are the top slice of the slab-rate pool, so they are the first
    # thing a reduction in that pool eats into.
    capped = _dividend_slab_tax(
        normal, min(comp.dividend_in_normal_income, normal), slabs
    )
    for slice_ in sorted(slices, key=lambda s: s.rate):
        chargeable = slice_.chargeable
        take = min(reduction, chargeable)
        reduction -= take
        slice_tax = (chargeable - take) * slice_.rate
        tax += slice_tax
        if slice_.code != "winnings_115bb":
            capped += slice_tax
    return tax, min(capped, tax)


# --------------------------------------------------------------------------
# Income the section 234C proviso covers
# --------------------------------------------------------------------------


def build_deferrable(tr: TaxReturn, comp: Computation) -> List[DeferrableItem]:
    """Attribute tax to each piece of income the proviso to section 234C covers.

    Capital gains, dividends and winnings are excused from the instalments that
    fell due before the income arose — but only the tax *on that income*, so it
    has to be picked out of the aggregate liability.
    """
    items: List[DeferrableItem] = []
    if comp.total_income <= 0:
        return items

    # Surcharge, cess and the rebate all sit outside the slab and special-rate
    # arithmetic, so the attributed tax is grossed up in the same proportion as
    # the liability as a whole.
    base_tax = comp.tax_before_rebate
    gross_up = (
        comp.total_tax_liability / base_tax if base_tax > 0 else D(0)
    )
    average_rate = comp.total_tax_liability / comp.total_income

    # ---- Capital gains -----------------------------------------------------
    special_by_code = {s.code: s for s in comp.special_slices}
    gains_by_code: Dict[str, List] = {}
    for item in tr.capital_gains:
        if item.net_gain > 0:
            gains_by_code.setdefault(item.category, []).append(item)

    for code, gains in gains_by_code.items():
        total_gain = sum((g.net_gain for g in gains), D(0))
        if total_gain <= 0:
            continue
        slice_ = special_by_code.get(code)
        for gain in gains:
            if gain.sale_date is None:
                # Without a date the proviso cannot be applied. Leaving the
                # item out puts the tax back in the regular pool, where it is
                # required in the ordinary 15/45/75/100 fractions. Listing it
                # with no date would be worse than useless: build_plan reads
                # that as "arose after 15 March" and waives the interest
                # entirely.
                continue
            share = gain.net_gain / total_gain
            if slice_ is not None:
                tax = slice_.tax * share * gross_up
            else:
                # Taxed at slab rates alongside everything else.
                tax = gain.net_gain * average_rate
            items.append(DeferrableItem(
                kind="capital_gains",
                label=gain.description or "Capital gain",
                arising_on=gain.sale_date,
                income=gain.net_gain,
                tax=tax,
            ))

    # ---- Dividends ---------------------------------------------------------
    # Each receipt carries its own payment date, which is what the proviso
    # turns on. Indian receipts are already in rupees; foreign ones share out
    # the converted total in proportion to the amounts declared.
    # A receipt with no payment date never reached the income figures either —
    # ``compute_dividends`` skips it — so counting it here would apportion the
    # converted total against income that is not in the return.
    domestic_receipts = [
        d for d in tr.dividends
        if d.gross_amount_fx > 0 and d.is_domestic and d.pay_date is not None
    ]
    foreign_receipts = [
        d for d in tr.dividends
        if d.gross_amount_fx > 0 and not d.is_domestic and d.pay_date is not None
    ]

    foreign_total_fx = sum((d.gross_amount_fx for d in foreign_receipts), D(0))
    for dividend in foreign_receipts:
        share = dividend.gross_amount_fx / foreign_total_fx
        income = tr.other_sources.foreign_dividend_income * share
        items.append(DeferrableItem(
            kind="dividend",
            label=f"Dividend — {dividend.symbol or 'foreign holding'}",
            arising_on=dividend.pay_date,
            income=income,
            tax=income * average_rate,
        ))

    dated_domestic = D(0)
    for dividend in domestic_receipts:
        dated_domestic += dividend.gross_amount_fx
        items.append(DeferrableItem(
            kind="dividend",
            label=f"Dividend — {dividend.symbol or 'Indian holding'}",
            arising_on=dividend.pay_date,
            income=dividend.gross_amount_fx,
            tax=dividend.gross_amount_fx * average_rate,
        ))

    # Anything left in the head that no dated receipt accounts for — a figure
    # typed straight in, or lifted from the AIS — is deliberately left out.
    # The proviso turns on when the income arose, and with no date there is
    # nothing to apply it to; the tax stays in the regular pool and is required
    # in the ordinary fractions.
    undated = non_negative(tr.other_sources.dividend_income - dated_domestic)
    if undated > 0:
        comp.warnings.append(
            f"₹{undated:,.0f} of dividend income has no payment date, so the "
            "proviso to section 234C cannot be applied to it and interest is "
            "computed on the ordinary instalment fractions. Enter the payment "
            "dates to claim the relief."
        )

    # ---- Winnings ----------------------------------------------------------
    # Winnings are covered by the proviso too, but nothing records when they
    # arose, and an item with no date would be read as arising after 15 March
    # and waived in full. Left out, the tax simply follows the regular schedule.
    if special_by_code.get("winnings_115bb") is not None:
        comp.warnings.append(
            "Winnings under section 115BB are covered by the proviso to "
            "section 234C, but this return does not record the date they "
            "arose, so no relief has been claimed for them."
        )

    return [item for item in items if item.tax > 0 and item.arising_on is not None]


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

    if comp.trading is not None and comp.trading.has_anything:
        comp.warnings.extend(comp.trading.warnings)
        if comp.audit_required:
            comp.warnings.append(
                "With an audit under section 44AB the due date moves to "
                f"{ay.due_date_audit:%d %B %Y}, and Form 3CA/3CB with Form 3CD "
                "has to be filed a month before the return."
            )
        if regime.key == "old":
            # Section 115BAC(6). A salaried taxpayer chooses afresh every year
            # on the return itself; someone with business income does not.
            comp.warnings.append(
                "The old regime is cheaper here, but with business income it "
                "is not a choice you make on the return. Form 10-IEA has to be "
                f"filed before {ay.due_date_non_audit:%d %B %Y} to opt out of "
                "section 115BAC — and section 115BAC(6) lets you do that only "
                "once. Return to the new regime in a later year and you cannot "
                "leave it again while the business continues."
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
