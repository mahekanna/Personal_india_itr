"""The advance-tax planner.

Two questions, asked four times a year:

    What do I owe by the next instalment date, and what happens if I miss it?

Answering them needs the whole return computed as it stands *plus* whatever is
still to come — the tranches left to vest, the sales you intend to make. So the
planner runs the ordinary engine over a projected version of the return, and
then splits the liability into the part that follows the plain 15/45/75/100
schedule and the part the proviso to section 234C lets you defer until the
quarter it actually arises in.

The projected figures never touch the return. They exist to decide a payment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from .foreign.pipeline import apply_foreign
from .foreign.vesting import expand_all, summarise_by_quarter
from .money import D, non_negative, rupees
from .schemas import TaxReturn
from .tax.advance_tax import AdvanceTaxPlan, DeferrableItem, build_plan
from .tax.engine import Computation, build_deferrable, compute
from .tax.rules import AssessmentYear, age_band, get_ay

# The challan a personal advance-tax payment goes on.
CHALLAN = {
    "form": "ITNS 280",
    "major_head": "0021 — Income Tax (Other than Companies)",
    "minor_head_advance": "100 — Advance Tax",
    "minor_head_self_assessment": "300 — Self Assessment Tax",
    "where": "incometax.gov.in → e-Pay Tax",
}


@dataclass
class QuarterView:
    """One instalment, dressed for display."""

    number: int
    label: str
    window: str
    due_date: date
    cumulative_pct: str
    required: Decimal
    paid: Decimal
    shortfall: Decimal
    interest: Decimal
    status: str
    events: List[str] = field(default_factory=list)


@dataclass
class PlannerResult:
    plan: AdvanceTaxPlan
    computation: Computation
    quarters: List[QuarterView] = field(default_factory=list)
    projected_vest_value: Decimal = D(0)
    projected_tax_effect: Decimal = D(0)
    has_projections: bool = False
    challan: Dict[str, str] = field(default_factory=lambda: dict(CHALLAN))
    warnings: List[str] = field(default_factory=list)

    @property
    def next_due(self) -> Optional[date]:
        nxt = self.plan.next_instalment
        return nxt.due_date if nxt else None

    @property
    def pay_now(self) -> Decimal:
        return rupees(self.plan.pay_now)


def build_planner(
    tr: TaxReturn,
    as_of: Optional[date] = None,
    include_projections: bool = True,
) -> PlannerResult:
    ay = get_ay(tr.assessment_year)
    as_of = as_of or date.today()

    # ---- Compute the year as it will end up --------------------------------
    prepared, foreign = apply_foreign(tr, include_projected=include_projections)
    if tr.regime_choice in ("new", "old"):
        comp = compute(prepared, tr.regime_choice, ay, foreign=foreign)
    else:
        # Compare on the *projected* year, not the banked one. Picking the
        # regime from what has happened so far and then planning instalments
        # against a year that includes four more vests can choose the wrong
        # regime, and the instalments follow the choice.
        new = compute(prepared, "new", ay, foreign=foreign)
        old = compute(prepared, "old", ay, foreign=foreign)
        new_cost = new.total_tax_liability + new.interest.total_interest
        old_cost = old.total_tax_liability + old.interest.total_interest
        comp = new if new_cost <= old_cost else old

    # ---- What is projected rather than banked ------------------------------
    projected = [v for v in prepared.rsu_vests if v.is_projected]
    # Take the rupee value from the vest computation rather than recomputing
    # it — that is where the Rule 115 rate was actually resolved.
    projected_value = sum(
        (outcome.perquisite_inr for outcome in foreign.vests.vests
         if outcome.vest.is_projected),
        D(0),
    )

    # ---- The instalment plan ------------------------------------------------
    payments = [
        (payment.payment_date or ay.fy_end, payment.amount)
        for payment in tr.taxes_paid.payments
        if payment.kind == "advance_tax"
    ]
    band = age_band(tr.taxpayer.date_of_birth, ay)

    plan = build_plan(
        ay,
        total_tax_liability=comp.total_tax_liability,
        tds_and_tcs=comp.tds + comp.tcs,
        deferrable=build_deferrable(prepared, comp),
        payments=payments,
        as_of=as_of,
        senior_without_business=(
            band in ("senior", "super_senior") and not tr.has_business_income
        ),
    )

    result = PlannerResult(
        plan=plan, computation=comp,
        has_projections=bool(projected),
        projected_vest_value=projected_value,
    )
    result.warnings.extend(plan.notes)
    result.warnings.extend(foreign.warnings)
    result.quarters = _to_views(plan, prepared, ay, as_of)
    _add_planner_notes(result, tr, prepared, comp, ay, projected)
    return result


# --------------------------------------------------------------------------


def _to_views(
    plan: AdvanceTaxPlan, tr: TaxReturn, ay: AssessmentYear, as_of: date
) -> List[QuarterView]:
    vest_buckets = summarise_by_quarter(tr.rsu_vests, ay.fy_start, ay.fy_end)
    views: List[QuarterView] = []

    for instalment in plan.instalments:
        events: List[str] = []
        for vest in vest_buckets.get(instalment.number, []):
            marker = " (projected)" if vest.is_projected else ""
            events.append(
                f"{vest.symbol} vested {vest.shares_vested:g} share(s) on "
                f"{vest.vest_date:%d %b}{marker}"
            )
        for item in instalment.deferrable_items:
            events.append(
                f"{item.label} — tax ₹{item.tax:,.0f}"
            )

        views.append(QuarterView(
            number=instalment.number,
            label=instalment.label,
            window=f"{instalment.window_start:%d %b} to {instalment.window_end:%d %b %Y}",
            due_date=instalment.due_date,
            cumulative_pct=f"{instalment.cumulative_fraction * 100:.0f}%",
            required=rupees(instalment.total_required),
            paid=rupees(instalment.paid),
            shortfall=rupees(instalment.shortfall),
            interest=rupees(instalment.interest),
            status=instalment.status(as_of),
            events=events,
        ))
    return views


def _add_planner_notes(
    result: PlannerResult,
    original: TaxReturn,
    prepared: TaxReturn,
    comp: Computation,
    ay: AssessmentYear,
    projected: List,
) -> None:
    plan = result.plan

    if not plan.liable:
        result.warnings.insert(0, plan.exemption_reason)
        return

    if projected:
        result.warnings.append(
            f"{len(projected)} tranche(s) have not vested yet and are valued at "
            "your estimated price. They are in this plan so the instalments are "
            "not understated, and they are kept out of the return entirely. "
            "Revise the estimate when the real price is known."
        )

    if plan.deferrable_tax > 0:
        result.warnings.append(
            f"₹{plan.deferrable_tax:,.0f} of the liability is on capital gains "
            "and dividends. The proviso to section 234C excuses the instalments "
            "that fell due before that income arose — but the whole of the tax "
            "on it is then due at the very next instalment, not a fraction of "
            "it. Missing that one costs the relief."
        )

    # Section 234B bites at 90%, and it is charged on the whole shortfall from
    # 1 April of the assessment year, so it is the more expensive of the two.
    ninety = plan.assessed_tax * D("0.90")
    if plan.paid_to_date < ninety:
        result.warnings.append(
            f"Advance tax paid so far is ₹{plan.paid_to_date:,.0f}. Reaching "
            f"₹{rupees(ninety):,.0f} — 90% of the assessed tax — by 31 March "
            "avoids section 234B altogether, which runs at 1% a month on the "
            f"whole shortfall from 1 April {ay.fy_end.year} until the return "
            "is filed."
        )

    last_instalment = plan.instalments[-1]
    if plan.as_of > last_instalment.due_date:
        result.warnings.append(
            "The 15 March instalment has passed. Anything paid now is still "
            "worth paying — it is treated as advance tax if paid by 31 March, "
            "and stops section 234B running — but the section 234C interest "
            "already charged cannot be undone."
        )

    salary_tds = comp.tds
    if salary_tds > 0 and prepared.rsu_vests:
        result.warnings.append(
            f"₹{salary_tds:,.0f} of TDS is already accounted for. Your employer "
            "withholds on the vesting perquisite under section 192, so that "
            "part usually needs no advance tax — the gap is normally the gains "
            "on sale and the dividends, which no one withholds against."
        )


# --------------------------------------------------------------------------
# A payment, once made
# --------------------------------------------------------------------------


def record_payment_hint(plan: AdvanceTaxPlan) -> Dict[str, str]:
    """What to put on the challan, and what to keep afterwards."""
    nxt = plan.next_instalment
    return {
        **CHALLAN,
        "assessment_year": plan.assessment_year,
        "amount": f"{rupees(plan.pay_now):,.0f}",
        "by": f"{nxt.due_date:%d %B %Y}" if nxt else "31 March",
        "after": (
            "Keep the BSR code, challan serial number and date — all three go "
            "on the return, and the credit will not be matched without them. "
            "They also appear in Form 26AS within a few days."
        ),
    }
