"""Statutory rate cards, keyed by assessment year.

Everything that changes with a Finance Act lives here and nowhere else. The
engine in ``engine.py`` reads these tables and has no year-specific constants of
its own, so supporting a new assessment year is a data edit, not a code change.

Sources for AY 2026-27 (FY 2025-26): Finance Act 2025 (slab and 87A changes
under section 115BAC), and Finance (No. 2) Act 2024 for the capital-gains
regime that took effect on 23 July 2024.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..money import D

# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SlabBand:
    """One row of a slab table. ``upto=None`` means "and above"."""

    upto: Optional[Decimal]
    rate: Decimal

    def label(self) -> str:
        return "above" if self.upto is None else str(self.upto)


@dataclass(frozen=True)
class Rebate87A:
    """Section 87A rebate parameters."""

    income_ceiling: Decimal
    max_rebate: Decimal
    # Marginal relief caps the tax at (total income - ceiling) just above the
    # ceiling. Introduced for the new regime; never applied under the old one.
    marginal_relief: bool
    # Section 115BAC(1A) as amended by the Finance Act 2025 allows the rebate
    # only against tax on income charged at slab rates, so under the new regime
    # every special-rate bucket is carved out. Under the old regime only the
    # proviso to section 87A read with section 112A applies.
    exclude_all_special: bool = False
    excluded_sections: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SurchargeBand:
    threshold: Decimal
    rate: Decimal


@dataclass(frozen=True)
class RegimeRules:
    """A complete rate card for one regime in one assessment year."""

    key: str
    name: str
    slabs_by_age: Dict[str, List[SlabBand]]
    rebate: Rebate87A
    surcharge_bands: List[SurchargeBand]
    # Surcharge on capital gains (111A/112A/112) and dividend is capped at this
    # rate by the proviso to Part I of the First Schedule.
    surcharge_cap_on_special_income: Decimal
    standard_deduction_salary: Decimal
    family_pension_deduction_cap: Decimal
    family_pension_deduction_fraction: Decimal
    # Chapter VI-A sections that survive in this regime.
    allowed_chapter_via: Tuple[str, ...]
    # Employer NPS contribution ceiling as a fraction of salary — 80CCD(2).
    nps_employer_fraction: Decimal
    # Section 24(b) interest on a self-occupied property.
    self_occupied_interest_cap: Decimal
    # Whether a house-property loss may be set off against other heads.
    allow_house_property_setoff: bool


@dataclass(frozen=True)
class CapitalGainRule:
    """How one flavour of capital gain is taxed."""

    code: str
    label: str
    rate: Optional[Decimal]  # None => taxed at slab rates
    exemption: Decimal = D(0)
    # Resident individuals may compute LTCG on land/building acquired before
    # 23-Jul-2024 at 20% with indexation if that is lower — Finance (No.2)
    # Act 2024, fifth proviso to section 112(1).
    grandfathered_indexation_option: bool = False
    indexed_rate: Optional[Decimal] = None


@dataclass(frozen=True)
class AssessmentYear:
    """Everything the engine needs for one assessment year."""

    ay: str
    fy: str
    fy_start: date
    fy_end: date
    due_date_non_audit: date
    due_date_audit: date
    belated_return_deadline: date
    cess_rate: Decimal
    regimes: Dict[str, RegimeRules]
    capital_gains: Dict[str, CapitalGainRule]
    default_regime: str
    # Section 234F late-filing fee: (income threshold, fee below, fee above).
    late_fee_income_threshold: Decimal
    late_fee_small: Decimal
    late_fee_large: Decimal
    # Advance tax is not payable at all below this liability — section 208.
    advance_tax_floor: Decimal
    # Section 234C instalment schedule: (due date, cumulative %, months charged).
    advance_tax_schedule: List[Tuple[date, Decimal, int]]
    interest_rate_per_month: Decimal
    cost_inflation_index: Dict[str, int] = field(default_factory=dict)
    # Chapter VI-A monetary ceilings for this year.
    deduction_limits: Dict[str, Decimal] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------

_NEW_REGIME_ALLOWED_VIA = ("80CCD2", "80CCH", "80JJAA")

_OLD_REGIME_ALLOWED_VIA = (
    "80C", "80CCC", "80CCD1", "80CCD1B", "80CCD2", "80D", "80DD", "80DDB",
    "80E", "80EE", "80EEA", "80EEB", "80G", "80GG", "80GGA", "80GGC",
    "80TTA", "80TTB", "80U", "80JJAA", "80CCH",
)

_OLD_SURCHARGE = [
    SurchargeBand(D("5000000"), D("0.10")),
    SurchargeBand(D("10000000"), D("0.15")),
    SurchargeBand(D("20000000"), D("0.25")),
    SurchargeBand(D("50000000"), D("0.37")),
]

# The new regime caps surcharge at 25% — the 37% band was never extended to it.
_NEW_SURCHARGE = [
    SurchargeBand(D("5000000"), D("0.10")),
    SurchargeBand(D("10000000"), D("0.15")),
    SurchargeBand(D("20000000"), D("0.25")),
]

_OLD_SLABS = {
    "below_60": [
        SlabBand(D("250000"), D("0")),
        SlabBand(D("500000"), D("0.05")),
        SlabBand(D("1000000"), D("0.20")),
        SlabBand(None, D("0.30")),
    ],
    "senior": [  # 60 to 79
        SlabBand(D("300000"), D("0")),
        SlabBand(D("500000"), D("0.05")),
        SlabBand(D("1000000"), D("0.20")),
        SlabBand(None, D("0.30")),
    ],
    "super_senior": [  # 80 and above
        SlabBand(D("500000"), D("0")),
        SlabBand(D("1000000"), D("0.20")),
        SlabBand(None, D("0.30")),
    ],
}

_DEDUCTION_LIMITS = {
    "80C": D("150000"),          # 80C + 80CCC + 80CCD(1) share a single ceiling
    "80CCD1B": D("50000"),
    "80D_self": D("25000"),
    "80D_self_senior": D("50000"),
    "80D_parents": D("25000"),
    "80D_parents_senior": D("50000"),
    "80D_preventive": D("5000"),
    "80DD_normal": D("75000"),
    "80DD_severe": D("125000"),
    "80DDB_normal": D("40000"),
    "80DDB_senior": D("100000"),
    "80EE": D("50000"),
    "80EEA": D("150000"),
    "80EEB": D("150000"),
    "80GG": D("60000"),
    "80TTA": D("10000"),
    "80TTB": D("50000"),
    "80U_normal": D("75000"),
    "80U_severe": D("125000"),
}

# Cost Inflation Index, notified under section 48. Needed only for the
# grandfathered 20%-with-indexation option on pre-23-Jul-2024 land/building.
_CII = {
    "2001-02": 100, "2002-03": 105, "2003-04": 109, "2004-05": 113,
    "2005-06": 117, "2006-07": 122, "2007-08": 129, "2008-09": 137,
    "2009-10": 148, "2010-11": 167, "2011-12": 184, "2012-13": 200,
    "2013-14": 220, "2014-15": 240, "2015-16": 254, "2016-17": 264,
    "2017-18": 272, "2018-19": 280, "2019-20": 289, "2020-21": 301,
    "2021-22": 317, "2022-23": 331, "2023-24": 348, "2024-25": 363,
}


def _capital_gain_rules() -> Dict[str, CapitalGainRule]:
    """Post-23-July-2024 capital gains regime (applies wholly to FY 2025-26)."""
    return {
        "stcg_111a": CapitalGainRule(
            "stcg_111a", "STCG on listed equity / equity MF (STT paid) — s.111A",
            D("0.20"),
        ),
        "stcg_slab": CapitalGainRule(
            "stcg_slab", "STCG on other assets (taxed at slab rates)", None,
        ),
        "stcg_debt_mf": CapitalGainRule(
            "stcg_debt_mf", "Specified mutual funds — s.50AA (always short term)",
            None,
        ),
        "ltcg_112a": CapitalGainRule(
            "ltcg_112a", "LTCG on listed equity / equity MF (STT paid) — s.112A",
            D("0.125"), exemption=D("125000"),
        ),
        "ltcg_112_property": CapitalGainRule(
            "ltcg_112_property", "LTCG on land or building — s.112 @12.5%",
            D("0.125"),
            grandfathered_indexation_option=True,
            indexed_rate=D("0.20"),
        ),
        # Where the asset was acquired before 23 July 2024 by a resident
        # individual or HUF, the fifth proviso to section 112(1) lets them pay
        # 20% on the indexed gain instead if that works out cheaper. Items that
        # take that option land in this bucket.
        "ltcg_112_property_indexed": CapitalGainRule(
            "ltcg_112_property_indexed",
            "LTCG on land or building — s.112 @20% with indexation",
            D("0.20"),
        ),
        "ltcg_112_other": CapitalGainRule(
            "ltcg_112_other", "LTCG on other assets (gold, unlisted, debt) — s.112",
            D("0.125"),
        ),
    }


# --------------------------------------------------------------------------
# Assessment years
# --------------------------------------------------------------------------

AY_2026_27 = AssessmentYear(
    ay="2026-27",
    fy="2025-26",
    fy_start=date(2025, 4, 1),
    fy_end=date(2026, 3, 31),
    due_date_non_audit=date(2026, 7, 31),
    due_date_audit=date(2026, 10, 31),
    belated_return_deadline=date(2026, 12, 31),
    cess_rate=D("0.04"),
    default_regime="new",
    late_fee_income_threshold=D("500000"),
    late_fee_small=D("1000"),
    late_fee_large=D("5000"),
    advance_tax_floor=D("10000"),
    interest_rate_per_month=D("0.01"),
    advance_tax_schedule=[
        (date(2025, 6, 15), D("0.15"), 3),
        (date(2025, 9, 15), D("0.45"), 3),
        (date(2025, 12, 15), D("0.75"), 3),
        (date(2026, 3, 15), D("1.00"), 1),
    ],
    cost_inflation_index=_CII,
    deduction_limits=_DEDUCTION_LIMITS,
    capital_gains=_capital_gain_rules(),
    regimes={
        "new": RegimeRules(
            key="new",
            name="New regime (section 115BAC)",
            # Age makes no difference under the new regime.
            slabs_by_age={
                age: [
                    SlabBand(D("400000"), D("0")),
                    SlabBand(D("800000"), D("0.05")),
                    SlabBand(D("1200000"), D("0.10")),
                    SlabBand(D("1600000"), D("0.15")),
                    SlabBand(D("2000000"), D("0.20")),
                    SlabBand(D("2400000"), D("0.25")),
                    SlabBand(None, D("0.30")),
                ]
                for age in ("below_60", "senior", "super_senior")
            },
            rebate=Rebate87A(
                income_ceiling=D("1200000"),
                max_rebate=D("60000"),
                marginal_relief=True,
                exclude_all_special=True,
            ),
            surcharge_bands=_NEW_SURCHARGE,
            surcharge_cap_on_special_income=D("0.15"),
            standard_deduction_salary=D("75000"),
            family_pension_deduction_cap=D("25000"),
            family_pension_deduction_fraction=D("0.3333333333"),
            allowed_chapter_via=_NEW_REGIME_ALLOWED_VIA,
            nps_employer_fraction=D("0.14"),
            self_occupied_interest_cap=D("0"),
            allow_house_property_setoff=False,
        ),
        "old": RegimeRules(
            key="old",
            name="Old regime",
            slabs_by_age=_OLD_SLABS,
            rebate=Rebate87A(
                income_ceiling=D("500000"),
                max_rebate=D("12500"),
                marginal_relief=False,
                excluded_sections=("ltcg_112a",),
            ),
            surcharge_bands=_OLD_SURCHARGE,
            surcharge_cap_on_special_income=D("0.15"),
            standard_deduction_salary=D("50000"),
            family_pension_deduction_cap=D("15000"),
            family_pension_deduction_fraction=D("0.3333333333"),
            allowed_chapter_via=_OLD_REGIME_ALLOWED_VIA,
            nps_employer_fraction=D("0.10"),
            self_occupied_interest_cap=D("200000"),
            allow_house_property_setoff=True,
        ),
    },
)


# AY 2025-26 (FY 2024-25) — kept so users can prepare a belated or updated
# return. The only structural differences are the new-regime slab table and the
# 87A ceiling; the capital-gains regime is the same post-23-Jul-2024 one.
AY_2025_26 = AssessmentYear(
    ay="2025-26",
    fy="2024-25",
    fy_start=date(2024, 4, 1),
    fy_end=date(2025, 3, 31),
    due_date_non_audit=date(2025, 9, 15),
    due_date_audit=date(2025, 10, 31),
    belated_return_deadline=date(2025, 12, 31),
    cess_rate=D("0.04"),
    default_regime="new",
    late_fee_income_threshold=D("500000"),
    late_fee_small=D("1000"),
    late_fee_large=D("5000"),
    advance_tax_floor=D("10000"),
    interest_rate_per_month=D("0.01"),
    advance_tax_schedule=[
        (date(2024, 6, 15), D("0.15"), 3),
        (date(2024, 9, 15), D("0.45"), 3),
        (date(2024, 12, 15), D("0.75"), 3),
        (date(2025, 3, 15), D("1.00"), 1),
    ],
    cost_inflation_index=_CII,
    deduction_limits=_DEDUCTION_LIMITS,
    capital_gains=_capital_gain_rules(),
    regimes={
        "new": RegimeRules(
            key="new",
            name="New regime (section 115BAC)",
            slabs_by_age={
                age: [
                    SlabBand(D("300000"), D("0")),
                    SlabBand(D("700000"), D("0.05")),
                    SlabBand(D("1000000"), D("0.10")),
                    SlabBand(D("1200000"), D("0.15")),
                    SlabBand(D("1500000"), D("0.20")),
                    SlabBand(None, D("0.30")),
                ]
                for age in ("below_60", "senior", "super_senior")
            },
            rebate=Rebate87A(
                income_ceiling=D("700000"),
                max_rebate=D("25000"),
                marginal_relief=True,
                exclude_all_special=True,
            ),
            surcharge_bands=_NEW_SURCHARGE,
            surcharge_cap_on_special_income=D("0.15"),
            standard_deduction_salary=D("75000"),
            family_pension_deduction_cap=D("25000"),
            family_pension_deduction_fraction=D("0.3333333333"),
            allowed_chapter_via=_NEW_REGIME_ALLOWED_VIA,
            nps_employer_fraction=D("0.14"),
            self_occupied_interest_cap=D("0"),
            allow_house_property_setoff=False,
        ),
        "old": RegimeRules(
            key="old",
            name="Old regime",
            slabs_by_age=_OLD_SLABS,
            rebate=Rebate87A(
                income_ceiling=D("500000"),
                max_rebate=D("12500"),
                marginal_relief=False,
                excluded_sections=("ltcg_112a",),
            ),
            surcharge_bands=_OLD_SURCHARGE,
            surcharge_cap_on_special_income=D("0.15"),
            standard_deduction_salary=D("50000"),
            family_pension_deduction_cap=D("15000"),
            family_pension_deduction_fraction=D("0.3333333333"),
            allowed_chapter_via=_OLD_REGIME_ALLOWED_VIA,
            nps_employer_fraction=D("0.10"),
            self_occupied_interest_cap=D("200000"),
            allow_house_property_setoff=True,
        ),
    },
)


ASSESSMENT_YEARS: Dict[str, AssessmentYear] = {
    "2026-27": AY_2026_27,
    "2025-26": AY_2025_26,
}

CURRENT_AY = "2026-27"


def get_ay(ay: str | None = None) -> AssessmentYear:
    """Look up an assessment year, defaulting to the current filing season."""
    key = ay or CURRENT_AY
    if key not in ASSESSMENT_YEARS:
        raise ValueError(
            f"Assessment year {key!r} is not supported. "
            f"Available: {', '.join(sorted(ASSESSMENT_YEARS))}"
        )
    return ASSESSMENT_YEARS[key]


def age_band(date_of_birth: date | None, ay: AssessmentYear) -> str:
    """Resident age band, determined on the last day of the previous year.

    Section 80U/slab benefits turn on age *at any time during* the previous
    year, so someone turning 60 on 31 March 2026 is a senior for FY 2025-26.
    """
    if date_of_birth is None:
        return "below_60"
    years = ay.fy_end.year - date_of_birth.year
    if (ay.fy_end.month, ay.fy_end.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    if years >= 80:
        return "super_senior"
    if years >= 60:
        return "senior"
    return "below_60"
