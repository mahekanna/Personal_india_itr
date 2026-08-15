"""Expanding a vesting schedule into individual vests.

A four-year grant vesting quarterly is sixteen separate salary events. Each has
its own fair market value, its own Rule 115 exchange rate, and its own 24-month
capital-gains clock. They are not interchangeable and they cannot be averaged.

Tranches that have already vested need the actual fair market value on the day,
which comes from the broker's release confirmation. Tranches still in the future
are **projections**: they are used to work out what advance tax to pay and
nothing else, and they never reach the return or the ITR JSON.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from ..money import D, non_negative
from ..schemas import RSUVest, TaxReturn, VestingSchedule

_MONTHS_BETWEEN = {
    "monthly": 1,
    "quarterly": 3,
    "semiannual": 6,
    "annual": 12,
}


@dataclass
class ExpansionResult:
    vests: List[RSUVest] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def actual(self) -> List[RSUVest]:
        return [v for v in self.vests if not v.is_projected]

    @property
    def projected(self) -> List[RSUVest]:
        return [v for v in self.vests if v.is_projected]


def add_months(start: date, months: int) -> date:
    """Add months, clamping the day to the end of the target month.

    A grant vesting on the 31st vests on the 28th or 29th in February. Rolling
    into March instead would push the tranche into a different quarter and a
    different exchange rate.
    """
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def expand_schedule(
    schedule: VestingSchedule,
    as_of: Optional[date] = None,
    window: Optional[tuple] = None,
) -> ExpansionResult:
    """Turn one grant into its individual vesting events.

    ``window`` is the financial year being worked on. Tranches outside it are
    still generated — the caller may want the whole grant — but they raise no
    warnings, because a missing price for a tranche two years out is not a
    problem with this year's return.
    """
    result = ExpansionResult()
    as_of = as_of or date.today()

    if not schedule.first_vest_date or schedule.tranches <= 0:
        result.warnings.append(
            f"Grant {schedule.grant_id or schedule.symbol}: a first vest date "
            "and a number of tranches are needed before it can be expanded."
        )
        return result

    step = _MONTHS_BETWEEN.get(schedule.frequency, 3)

    cliff = non_negative(schedule.cliff_shares)
    remaining_shares = non_negative(schedule.total_shares - cliff)
    remaining_tranches = schedule.tranches - (1 if cliff > 0 else 0)
    if remaining_tranches <= 0:
        per_tranche = D(0)
    else:
        per_tranche = remaining_shares / remaining_tranches

    allocated = D(0)
    for index in range(schedule.tranches):
        vest_date = add_months(schedule.first_vest_date, index * step)

        if index == 0 and cliff > 0:
            shares = cliff
        elif index == schedule.tranches - 1:
            # The last tranche mops up the rounding, so the tranches always
            # add back to the grant exactly.
            shares = non_negative(schedule.total_shares - allocated)
        else:
            shares = per_tranche
        allocated += shares
        if shares <= 0:
            continue

        actual_fmv = schedule.actual_fmv.get(vest_date.isoformat())
        is_projected = actual_fmv is None
        fmv = D(actual_fmv) if actual_fmv is not None else schedule.estimated_fmv_per_share_fx

        # A tranche in the past with no price on file is a gap, not a
        # projection — the real number exists and has to be fetched.
        in_window = window is None or window[0] <= vest_date <= window[1]
        if is_projected and vest_date <= as_of and in_window:
            result.warnings.append(
                f"{schedule.symbol} vested {shares:g} share(s) on "
                f"{vest_date:%d %b %Y} but no fair market value is on file. "
                "Until the actual price is entered this tranche is treated as "
                "an estimate and left out of the return."
            )

        sold_to_cover = shares * non_negative(schedule.sell_to_cover_fraction)

        result.vests.append(RSUVest(
            symbol=schedule.symbol,
            company_name=schedule.company_name,
            grant_id=schedule.grant_id,
            grant_date=schedule.grant_date,
            vest_date=vest_date,
            shares_vested=shares,
            fmv_per_share_fx=fmv,
            shares_sold_to_cover=sold_to_cover,
            sale_price_per_share_fx=fmv,
            currency=schedule.currency,
            country_code=schedule.country_code,
            included_in_form16=schedule.included_in_form16,
            is_projected=is_projected,
            grant_reference=schedule.grant_id or schedule.symbol,
            source_document=f"Vesting schedule {schedule.grant_id}".strip(),
        ))

    if not schedule.estimated_fmv_per_share_fx and result.projected:
        result.warnings.append(
            f"Grant {schedule.grant_id or schedule.symbol}: no estimated price "
            "is set, so the tranches still to vest are valued at nil. Put in a "
            "figure — the advance-tax plan is only as good as that estimate."
        )
    return result


def expand_all(
    tr: TaxReturn,
    as_of: Optional[date] = None,
    window: Optional[tuple] = None,
) -> ExpansionResult:
    """Expand every schedule on the return, skipping vests already entered.

    A tranche entered by hand, or imported from a broker release report, always
    wins over the generated one — the schedule is a convenience, not a source
    of truth.
    """
    combined = ExpansionResult()
    existing = {
        (v.symbol.upper(), v.vest_date)
        for v in tr.rsu_vests
        if v.vest_date and not v.is_projected
    }

    for schedule in tr.vesting_schedules:
        result = expand_schedule(schedule, as_of, window)
        combined.warnings.extend(result.warnings)
        for vest in result.vests:
            if (vest.symbol.upper(), vest.vest_date) in existing:
                continue
            combined.vests.append(vest)

    return combined


def vests_in_year(
    vests: List[RSUVest], fy_start: date, fy_end: date
) -> List[RSUVest]:
    return [
        vest for vest in vests
        if vest.vest_date and fy_start <= vest.vest_date <= fy_end
    ]


def summarise_by_quarter(
    vests: List[RSUVest], fy_start: date, fy_end: Optional[date] = None
) -> Dict[int, List[RSUVest]]:
    """Group vests by the advance-tax instalment window they fall in.

    Bounded by the financial year at both ends. A grant running four years
    generates tranches well past this year, and without the upper bound every
    one of them would pile into the fourth instalment.
    """
    buckets: Dict[int, List[RSUVest]] = {1: [], 2: [], 3: [], 4: []}
    fy_end = fy_end or date(fy_start.year + 1, 3, 31)
    boundaries = [
        add_months(fy_start, 2).replace(day=15),      # 15 June
        add_months(fy_start, 5).replace(day=15),      # 15 September
        add_months(fy_start, 8).replace(day=15),      # 15 December
    ]
    for vest in vests:
        if not vest.vest_date:
            continue
        if not (fy_start <= vest.vest_date <= fy_end):
            continue
        if vest.vest_date <= boundaries[0]:
            buckets[1].append(vest)
        elif vest.vest_date <= boundaries[1]:
            buckets[2].append(vest)
        elif vest.vest_date <= boundaries[2]:
            buckets[3].append(vest)
        else:
            buckets[4].append(vest)
    return buckets
