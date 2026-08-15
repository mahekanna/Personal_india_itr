"""Chapter VI-A deductions, with every statutory ceiling applied.

The user enters what they actually paid or invested; this module decides what
survives. Section 80A(2) caps the aggregate at gross total income, and under
the new regime section 115BAC(2) leaves only a short whitelist standing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Tuple

from ..money import D, non_negative
from ..schemas import Deductions, TaxReturn
from .rules import AssessmentYear, RegimeRules


@dataclass
class DeductionLine:
    section: str
    label: str
    claimed: Decimal
    allowed: Decimal
    note: str = ""


@dataclass
class DeductionResult:
    total: Decimal
    lines: List[DeductionLine] = field(default_factory=list)
    disallowed: List[DeductionLine] = field(default_factory=list)


def compute_deductions(
    tr: TaxReturn,
    regime: RegimeRules,
    ay: AssessmentYear,
    gross_total_income: Decimal,
    salary_for_nps: Decimal,
    age_band: str,
    parents_are_senior: bool = False,
) -> DeductionResult:
    ded: Deductions = tr.deductions
    limits = ay.deduction_limits
    allowed_here = set(regime.allowed_chapter_via)
    lines: List[DeductionLine] = []
    disallowed: List[DeductionLine] = []

    def add(section: str, label: str, claimed: Decimal, allowed: Decimal,
            note: str = "") -> None:
        if claimed <= 0 and allowed <= 0:
            return
        line = DeductionLine(section, label, claimed, allowed, note)
        if section not in allowed_here:
            line.allowed = D(0)
            line.note = note or "Not available under the new regime"
            disallowed.append(line)
        else:
            lines.append(line)

    # -- 80C / 80CCC / 80CCD(1): one shared ceiling of ₹1,50,000 (s.80CCE) ----
    combined_claim = ded.s80c + ded.s80ccc + ded.s80ccd1
    combined_allowed = min(combined_claim, limits["80C"])
    add("80C", "Life insurance, PF, ELSS, tuition fees and the like (80C/80CCC/80CCD(1))",
        combined_claim, combined_allowed,
        "Aggregate ceiling of ₹1,50,000 under section 80CCE")

    # -- 80CCD(1B): additional NPS -------------------------------------------
    add("80CCD1B", "Additional contribution to the National Pension System",
        ded.s80ccd1b, min(ded.s80ccd1b, limits["80CCD1B"]))

    # -- 80CCD(2): employer's NPS — survives in both regimes ------------------
    employer_nps_claim = ded.s80ccd2 or sum(
        (s.employer_nps_contribution for s in tr.salaries), D(0)
    )
    nps_cap = salary_for_nps * regime.nps_employer_fraction
    add("80CCD2", "Employer's contribution to the National Pension System",
        employer_nps_claim, min(employer_nps_claim, nps_cap),
        f"Capped at {regime.nps_employer_fraction * 100:.0f}% of salary")

    # -- 80D: health insurance ------------------------------------------------
    self_cap = limits["80D_self_senior" if age_band in ("senior", "super_senior")
                      else "80D_self"]
    parents_cap = limits["80D_parents_senior" if parents_are_senior
                         else "80D_parents"]
    preventive = min(ded.s80d_preventive, limits["80D_preventive"])
    # Preventive health check-up sits inside the overall 80D ceiling.
    d_claim = ded.s80d_self + ded.s80d_parents + preventive
    d_allowed = min(ded.s80d_self + preventive, self_cap) + min(
        ded.s80d_parents, parents_cap
    )
    add("80D", "Health insurance premium and preventive check-up",
        d_claim, min(d_allowed, d_claim),
        f"Self ₹{self_cap:,.0f} + parents ₹{parents_cap:,.0f}; "
        "preventive check-up up to ₹5,000 is inside these limits")

    # -- 80DD / 80DDB / 80U: disability and treatment -------------------------
    dd_cap = limits["80DD_severe" if ded.s80dd_severe else "80DD_normal"]
    add("80DD", "Maintenance of a dependant with a disability",
        ded.s80dd, min(ded.s80dd, dd_cap) if ded.s80dd else D(0),
        "A flat deduction — the actual expenditure does not change it")

    ddb_cap = limits["80DDB_senior" if age_band in ("senior", "super_senior")
                     else "80DDB_normal"]
    add("80DDB", "Treatment of a specified disease",
        ded.s80ddb, min(ded.s80ddb, ddb_cap))

    u_cap = limits["80U_severe" if ded.s80u_severe else "80U_normal"]
    add("80U", "Deduction for a person with a disability",
        ded.s80u, min(ded.s80u, u_cap) if ded.s80u else D(0))

    # -- Interest deductions --------------------------------------------------
    add("80E", "Interest on an education loan", ded.s80e, ded.s80e,
        "No monetary ceiling; available for eight assessment years")
    add("80EE", "Interest on a first home loan", ded.s80ee,
        min(ded.s80ee, limits["80EE"]))
    add("80EEA", "Interest on an affordable home loan", ded.s80eea,
        min(ded.s80eea, limits["80EEA"]))
    add("80EEB", "Interest on an electric-vehicle loan", ded.s80eeb,
        min(ded.s80eeb, limits["80EEB"]))

    # -- 80G: donations -------------------------------------------------------
    g_result, g_note = _section_80g(ded, gross_total_income, lines)
    add("80G", "Donations to approved funds and institutions",
        ded.s80g_100pct_no_limit + ded.s80g_50pct_no_limit
        + ded.s80g_100pct_with_limit + ded.s80g_50pct_with_limit,
        g_result, g_note)

    # -- 80GG: rent paid when no HRA is received ------------------------------
    if ded.s80gg or ded.rent_paid_annual:
        gg = _section_80gg(ded, gross_total_income)
        add("80GG", "Rent paid where no house-rent allowance is received",
            ded.rent_paid_annual or ded.s80gg, gg,
            "Least of ₹5,000 a month, 25% of adjusted total income, "
            "and rent paid less 10% of adjusted total income")

    add("80GGA", "Donations for scientific research or rural development",
        ded.s80gga, ded.s80gga)
    add("80GGC", "Contribution to a political party", ded.s80ggc, ded.s80ggc)

    # -- 80TTA / 80TTB: interest ---------------------------------------------
    if age_band in ("senior", "super_senior"):
        claim = ded.s80ttb or (
            tr.other_sources.savings_bank_interest
            + tr.other_sources.fixed_deposit_interest
        )
        add("80TTB", "Interest on deposits — senior citizen",
            claim, min(claim, limits["80TTB"]))
    else:
        claim = ded.s80tta or tr.other_sources.savings_bank_interest
        add("80TTA", "Interest on savings bank accounts",
            claim, min(claim, limits["80TTA"]))

    add("80JJAA", "Employment of new workmen", ded.s80jjaa, ded.s80jjaa)
    add("80CCH", "Contribution to the Agnipath Scheme", ded.s80cch, ded.s80cch)

    total = sum((line.allowed for line in lines), D(0))

    # Section 80A(2): the aggregate cannot exceed gross total income. Nor may
    # Chapter VI-A be set against special-rate income, so the practical cap is
    # gross total income reduced by that income — the engine passes it in.
    if total > gross_total_income:
        lines.append(
            DeductionLine("80A(2)", "Restricted to gross total income",
                          total, gross_total_income - total,
                          "Chapter VI-A cannot create or increase a loss")
        )
        total = non_negative(gross_total_income)

    return DeductionResult(total=total, lines=lines, disallowed=disallowed)


def _section_80g(
    ded: Deductions, gross_total_income: Decimal, _lines: List[DeductionLine]
) -> Tuple[Decimal, str]:
    """Donations, honouring the 10%-of-adjusted-GTI qualifying limit."""
    unrestricted = ded.s80g_100pct_no_limit + (ded.s80g_50pct_no_limit * D("0.5"))
    restricted_claim = ded.s80g_100pct_with_limit + ded.s80g_50pct_with_limit
    if restricted_claim <= 0:
        return unrestricted, "100% or 50% of the donation, as notified"

    qualifying_limit = non_negative(gross_total_income) * D("0.10")
    eligible = min(restricted_claim, qualifying_limit)
    # Give the 100% category the benefit of the limit first.
    hundred = min(ded.s80g_100pct_with_limit, eligible)
    fifty = min(ded.s80g_50pct_with_limit, eligible - hundred) * D("0.5")
    return (
        unrestricted + hundred + fifty,
        "Donations in the 'with qualifying limit' category are restricted to "
        "10% of adjusted gross total income",
    )


def _section_80gg(ded: Deductions, gross_total_income: Decimal) -> Decimal:
    rent = ded.rent_paid_annual or ded.s80gg
    if rent <= 0:
        return D(0)
    adjusted = non_negative(gross_total_income)
    return non_negative(
        min(
            D("60000"),                       # ₹5,000 a month
            adjusted * D("0.25"),
            rent - adjusted * D("0.10"),
        )
    )
