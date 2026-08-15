"""Advance tax: the four instalments, and the proviso that changes everything.

Section 211 wants advance tax in four instalments — 15% by 15 June, 45% by
15 September, 75% by 15 December and the whole of it by 15 March — and section
234C charges 1% a month on whatever was short at each date.

Applied naively that is punitive for anyone with capital gains, because it asks
you to have paid tax in June on a gain you made in December. So the **first
proviso to section 234C(1)** carves out income you could not reasonably have
estimated:

    (a) capital gains
    (b) winnings from lotteries, crossword puzzles and the like
    (c) business income, in the year it arises for the first time
    (d) dividend income, other than deemed dividend under section 2(22)(e)

For those, no interest is charged for the earlier instalments **provided the
whole of the tax on that income is paid in the remaining instalments** — or,
where the income arises after 15 March and no instalment is left, by 31 March.

The practical consequence for someone selling RSUs: a sale in the December
quarter costs nothing in 234C interest for the June and September instalments,
but the entire tax on it must be paid by 15 December. Miss that and interest
runs on the whole amount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from ..money import D, non_negative, rupees
from .rules import AssessmentYear

# Kinds of income the proviso covers. Anything else is "regular" and follows
# the plain 15/45/75/100 schedule.
DEFERRABLE_KINDS = ("capital_gains", "dividend", "winnings", "first_year_business")


@dataclass
class DeferrableItem:
    """Tax on one piece of income the proviso covers, and when it arose."""

    kind: str
    label: str
    arising_on: Optional[date]
    income: Decimal
    tax: Decimal


@dataclass
class Instalment:
    """One of the four dates, and how it stands."""

    number: int
    label: str
    window_start: date
    window_end: date
    due_date: date
    cumulative_fraction: Decimal
    months_charged: int

    # What section 211 read with the proviso actually requires by this date.
    regular_required: Decimal = D(0)
    deferrable_required: Decimal = D(0)
    paid: Decimal = D(0)
    # Income that arose inside this window, for the explanation column.
    deferrable_items: List[DeferrableItem] = field(default_factory=list)
    interest: Decimal = D(0)
    interest_waived: Decimal = D(0)

    @property
    def total_required(self) -> Decimal:
        return self.regular_required + self.deferrable_required

    @property
    def shortfall(self) -> Decimal:
        return non_negative(self.total_required - self.paid)

    def status(self, today: date) -> str:
        if self.due_date < today:
            return "met" if self.shortfall <= 0 else "missed"
        return "due" if (self.due_date - today).days <= 45 else "upcoming"


@dataclass
class AdvanceTaxPlan:
    assessment_year: str
    financial_year: str
    as_of: date
    instalments: List[Instalment] = field(default_factory=list)

    total_tax_liability: Decimal = D(0)
    tds_and_tcs: Decimal = D(0)
    assessed_tax: Decimal = D(0)          # the base advance tax is worked on
    regular_tax: Decimal = D(0)
    deferrable_tax: Decimal = D(0)
    paid_to_date: Decimal = D(0)

    liable: bool = True
    exemption_reason: str = ""
    interest_234c: Decimal = D(0)
    interest_waived_by_proviso: Decimal = D(0)
    notes: List[str] = field(default_factory=list)

    @property
    def next_instalment(self) -> Optional[Instalment]:
        for instalment in self.instalments:
            if instalment.due_date >= self.as_of:
                return instalment
        return None

    @property
    def pay_now(self) -> Decimal:
        """What to pay today to be square with the next due date."""
        nxt = self.next_instalment
        return nxt.shortfall if nxt else self.outstanding

    @property
    def outstanding(self) -> Decimal:
        return non_negative(self.assessed_tax - self.paid_to_date)


# --------------------------------------------------------------------------
# The instalment calendar
# --------------------------------------------------------------------------


def build_instalments(ay: AssessmentYear) -> List[Instalment]:
    labels = ["First", "Second", "Third", "Fourth"]
    out: List[Instalment] = []
    window_start = ay.fy_start
    for index, (due, fraction, months) in enumerate(ay.advance_tax_schedule):
        out.append(Instalment(
            number=index + 1,
            label=f"{labels[index]} instalment",
            window_start=window_start,
            window_end=due,
            due_date=due,
            cumulative_fraction=fraction,
            months_charged=months,
        ))
        window_start = due + timedelta(days=1)
    return out


def instalment_for(when: Optional[date], instalments: Sequence[Instalment]) -> Optional[Instalment]:
    """Which instalment window a date falls in."""
    if when is None:
        return None
    for instalment in instalments:
        if when <= instalment.due_date:
            return instalment
    return None


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def build_plan(
    ay: AssessmentYear,
    *,
    total_tax_liability: Decimal,
    tds_and_tcs: Decimal,
    deferrable: Sequence[DeferrableItem],
    payments: Sequence[Tuple[date, Decimal]],
    as_of: Optional[date] = None,
    senior_without_business: bool = False,
) -> AdvanceTaxPlan:
    as_of = as_of or date.today()
    plan = AdvanceTaxPlan(
        assessment_year=ay.ay, financial_year=ay.fy, as_of=as_of,
        total_tax_liability=total_tax_liability, tds_and_tcs=tds_and_tcs,
    )
    plan.instalments = build_instalments(ay)
    plan.assessed_tax = non_negative(total_tax_liability - tds_and_tcs)
    plan.paid_to_date = sum((amount for _, amount in payments), D(0))

    # ---- Is advance tax payable at all? -----------------------------------
    if senior_without_business:
        plan.liable = False
        plan.exemption_reason = (
            "Section 207(2) exempts a resident senior citizen with no business "
            "income from advance tax altogether. Pay the balance as "
            "self-assessment tax before filing instead."
        )
        return plan
    if plan.assessed_tax < ay.advance_tax_floor:
        plan.liable = False
        plan.exemption_reason = (
            f"Advance tax is not payable — after TDS the liability is "
            f"₹{plan.assessed_tax:,.0f}, below the ₹{ay.advance_tax_floor:,.0f} "
            "threshold in section 208."
        )
        return plan

    # ---- Split the liability -----------------------------------------------
    # The proviso turns on *when* the income arose. An item with no date cannot
    # be placed on the calendar, and treating it as though it arose after
    # 15 March would waive the interest on it entirely — the opposite of the
    # conservative answer. It stays in the regular pool instead.
    dated = [item for item in deferrable if item.arising_on is not None]
    undated = [item for item in deferrable if item.arising_on is None]
    if undated:
        plan.notes.append(
            "No date is recorded for "
            + ", ".join(sorted({item.label for item in undated})[:4])
            + ". The proviso to section 234C cannot be applied without one, so "
            "the tax on it follows the ordinary 15/45/75/100 schedule."
        )

    # Only tax on income the proviso covers is deferrable. It is capped at the
    # assessed tax, because TDS may already have absorbed part of it.
    claimed = sum((item.tax for item in dated), D(0))
    deferrable_tax = min(claimed, plan.assessed_tax)
    plan.deferrable_tax = deferrable_tax
    plan.regular_tax = non_negative(plan.assessed_tax - deferrable_tax)

    # ---- What each date requires -------------------------------------------
    for instalment in plan.instalments:
        instalment.regular_required = plan.regular_tax * instalment.cumulative_fraction

    for item in dated:
        target = instalment_for(item.arising_on, plan.instalments)
        if target is None:
            # Arose after 15 March: the proviso allows payment up to 31 March,
            # so no instalment can require it.
            plan.notes.append(
                f"{item.label} arose after 15 March. The proviso to section "
                "234C lets the tax on it be paid by 31 March without interest — "
                "but it must actually be paid by then."
            )
            continue
        target.deferrable_items.append(item)

    # The whole of the tax on a deferrable item is required from the instalment
    # in whose window it arose, and stays required at every later date. This is
    # the conservative reading of "as part of the remaining instalments" — the
    # one that guarantees no interest.
    running = D(0)
    scale = deferrable_tax / claimed if claimed > 0 else D(1)
    for instalment in plan.instalments:
        running += sum((item.tax for item in instalment.deferrable_items), D(0)) * scale
        instalment.deferrable_required = running

    # ---- Match payments to dates -------------------------------------------
    for instalment in plan.instalments:
        instalment.paid = sum(
            (amount for paid_on, amount in payments if paid_on <= instalment.due_date),
            D(0),
        )

    # ---- Interest ----------------------------------------------------------
    _apply_234c(plan, ay)
    _add_notes(plan)
    return plan


def _apply_234c(plan: AdvanceTaxPlan, ay: AssessmentYear) -> None:
    """Interest for each instalment, with the statutory relaxations."""
    # Section 234C's own relaxation: the first two instalments are forgiven if
    # at least 12% and 36% respectively were paid.
    relaxed = {D("0.15"): D("0.12"), D("0.45"): D("0.36")}
    naive_total = D(0)

    for instalment in plan.instalments:
        threshold = relaxed.get(
            instalment.cumulative_fraction, instalment.cumulative_fraction
        )
        relaxed_regular = plan.regular_tax * threshold
        required_with_relaxation = relaxed_regular + instalment.deferrable_required

        # What the naive calculation — the one that ignores the proviso — would
        # have demanded, so the saving can be shown.
        naive_required = plan.assessed_tax * threshold

        if instalment.paid >= required_with_relaxation:
            shortfall = D(0)
        else:
            shortfall = non_negative(
                instalment.total_required - instalment.paid
            )

        instalment.interest = rupees(
            _round_down_to_hundred(shortfall)
            * ay.interest_rate_per_month
            * instalment.months_charged
        )
        naive_shortfall = non_negative(naive_required - instalment.paid)
        naive_interest = rupees(
            _round_down_to_hundred(naive_shortfall)
            * ay.interest_rate_per_month
            * instalment.months_charged
        )
        # Signed, not clamped. The proviso gives relief on the instalments
        # before the income arose, but then demands the *whole* of the tax at
        # the next one rather than the usual fraction — so on that instalment
        # it costs more, not less. Summing only the favourable differences
        # would overstate the relief.
        instalment.interest_waived = naive_interest - instalment.interest
        naive_total += naive_interest

        plan.interest_234c += instalment.interest

    plan.interest_waived_by_proviso = non_negative(
        naive_total - plan.interest_234c
    )


def _round_down_to_hundred(amount: Decimal) -> Decimal:
    """Rule 119A rounds the amount interest is computed on down to ₹100."""
    if amount <= 0:
        return D(0)
    return (amount // D("100")) * D("100")


def _add_notes(plan: AdvanceTaxPlan) -> None:
    if plan.interest_waived_by_proviso > 0:
        plan.notes.append(
            f"₹{plan.interest_waived_by_proviso:,.0f} less section 234C interest "
            "than a naive calculation would charge, because the proviso excuses "
            "the instalments that fell due before the capital gain or dividend "
            "arose. The trade is that the whole of the tax on it is then due at "
            "the very next instalment, not the usual fraction — so the relief "
            "is lost entirely if that one is missed."
        )

    missed = [i for i in plan.instalments
              if i.due_date < plan.as_of and i.shortfall > 0]
    if missed:
        names = ", ".join(f"{i.due_date:%d %b}" for i in missed)
        plan.notes.append(
            f"Instalments already short: {names}. Interest under section 234C "
            "on those is fixed and paying now will not undo it — but paying "
            "now does stop section 234B, which runs at 1% a month from 1 April "
            "of the assessment year until it is paid."
        )

    nxt = plan.next_instalment
    if nxt and nxt.shortfall > 0:
        plan.notes.append(
            f"Pay ₹{nxt.shortfall:,.0f} by {nxt.due_date:%d %B %Y} to stay clear "
            "of interest on the next instalment."
        )


# --------------------------------------------------------------------------
# Used by the interest module when computing the actual return
# --------------------------------------------------------------------------


def section_234c(
    ay: AssessmentYear,
    assessed_tax: Decimal,
    instalments_paid: Sequence[Tuple[date, Decimal]],
    deferrable: Sequence[DeferrableItem],
) -> Tuple[Decimal, List[str]]:
    """Section 234C interest, honouring the proviso.

    Returns the interest and an explanation of each charge.
    """
    plan = build_plan(
        ay,
        total_tax_liability=assessed_tax,
        tds_and_tcs=D(0),
        deferrable=deferrable,
        payments=instalments_paid,
        as_of=ay.fy_end,
    )
    notes: List[str] = []
    for instalment in plan.instalments:
        if instalment.interest > 0:
            notes.append(
                f"Section 234C: ₹{instalment.shortfall:,.0f} short by "
                f"{instalment.due_date:%d %b %Y} "
                f"({instalment.cumulative_fraction * 100:.0f}% instalment) — "
                f"{instalment.months_charged} month(s)."
            )
    if plan.interest_waived_by_proviso > 0:
        notes.append(
            f"Section 234C: ₹{plan.interest_waived_by_proviso:,.0f} not charged, "
            "as the proviso excuses the earlier instalments for capital gains "
            "and dividend income."
        )
    return plan.interest_234c, notes
