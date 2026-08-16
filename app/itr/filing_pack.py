"""The filing pack: every number, mapped to the portal field it belongs in.

The JSON upload is the fast path. This is the fallback that always works — if
the schema has drifted, or the form is ITR-3 or ITR-4, the user can still type
the return in online and know exactly what goes where.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List

from ..money import D, inr
from ..schemas import TaxReturn
from ..tax.engine import Computation
from ..tax.rules import due_date_for, get_ay


@dataclass
class PackRow:
    field_label: str
    value: str
    note: str = ""


@dataclass
class PackSection:
    portal_location: str
    rows: List[PackRow] = field(default_factory=list)


@dataclass
class FilingPack:
    form: str
    assessment_year: str
    regime: str
    sections: List[PackSection] = field(default_factory=list)
    checklist: List[str] = field(default_factory=list)


def build_filing_pack(tr: TaxReturn, comp: Computation, form: str) -> FilingPack:
    ay = get_ay(tr.assessment_year)
    pack = FilingPack(
        form=form,
        assessment_year=tr.assessment_year,
        regime=comp.regime_name,
    )

    personal = PackSection("Part A — General Information")
    personal.rows += [
        PackRow("PAN", tr.taxpayer.pan),
        PackRow("Name", tr.taxpayer.name),
        PackRow("Date of birth",
                tr.taxpayer.date_of_birth.strftime("%d/%m/%Y")
                if tr.taxpayer.date_of_birth else ""),
        PackRow("Residential status", tr.taxpayer.residential_status),
        PackRow("Filed under section",
                _filing_section_label(tr, ay)),
        PackRow("Opting for the new regime u/s 115BAC",
                "Yes" if comp.regime == "new" else "No",
                "This is the single most consequential toggle on the form."),
    ]
    pack.sections.append(personal)

    if tr.salaries:
        salary = PackSection("Schedule S — Salary")
        for employer in tr.salaries:
            prefix = employer.employer_name or "Employer"
            salary.rows += [
                PackRow(f"{prefix} — TAN", employer.employer_tan),
                PackRow(f"{prefix} — gross salary u/s 17(1)",
                        inr(employer.salary_17_1)),
                PackRow(f"{prefix} — perquisites u/s 17(2)",
                        inr(employer.perquisites_17_2)),
                PackRow(f"{prefix} — exempt allowances u/s 10",
                        inr(employer.total_exempt)),
            ]
        salary.rows.append(
            PackRow("Income chargeable under Salaries", inr(comp.salary),
                    "After the standard deduction")
        )
        pack.sections.append(salary)

    if tr.house_properties:
        house = PackSection("Schedule HP — House Property")
        for prop in tr.house_properties:
            house.rows += [
                PackRow(f"{prop.address or prop.property_type} — type",
                        prop.property_type),
                PackRow("Gross rent received", inr(prop.annual_rent_received)),
                PackRow("Municipal taxes paid", inr(prop.municipal_taxes_paid)),
                PackRow("Interest on borrowed capital u/s 24(b)",
                        inr(prop.interest_24b)),
            ]
        house.rows.append(
            PackRow("Income from house property", inr(comp.house_property))
        )
        pack.sections.append(house)

    if comp.special_slices or tr.capital_gains:
        gains = PackSection("Schedule CG and Schedule SI — Capital Gains")
        for slice_ in comp.special_slices:
            gains.rows.append(
                PackRow(slice_.label, inr(slice_.income),
                        f"Taxed at {slice_.rate * 100:.1f}% on "
                        f"₹{inr(slice_.chargeable)} after exemptions")
            )
        gains.rows.append(
            PackRow("Total capital gains", inr(comp.capital_gains))
        )
        pack.sections.append(gains)

    other = PackSection("Schedule OS — Other Sources")
    source = tr.other_sources
    for label, amount in [
        ("Interest from savings bank accounts", source.savings_bank_interest),
        ("Interest from deposits", source.fixed_deposit_interest),
        ("Other interest", source.other_interest),
        ("Dividend income", source.dividend_income),
        ("Family pension", source.family_pension),
        ("Any other income", source.other_income),
    ]:
        if amount:
            other.rows.append(PackRow(label, inr(amount)))
    if other.rows:
        other.rows.append(PackRow("Income from other sources",
                                  inr(comp.other_sources)))
        pack.sections.append(other)

    if comp.deduction_detail and comp.deduction_detail.lines:
        via = PackSection("Schedule VI-A — Deductions")
        for line in comp.deduction_detail.lines:
            if line.allowed > 0:
                via.rows.append(
                    PackRow(f"Section {line.section}", inr(line.allowed),
                            line.note)
                )
        via.rows.append(PackRow("Total Chapter VI-A deductions",
                                inr(comp.deductions_total)))
        pack.sections.append(via)

    if tr.foreign_assets:
        fa = PackSection("Schedule FA — Foreign Assets (CALENDAR year)")
        fa.rows.append(PackRow(
            "Reporting period",
            f"1 January to 31 December {tr.foreign_assets[0].calendar_year}",
            "Not the financial year. This is the single commonest error in "
            "this schedule.",
        ))
        for row in tr.foreign_assets:
            prefix = f"Table {row.table} · {row.entity_name}"
            fa.rows += [
                PackRow(f"{prefix} — country", row.country_name),
                PackRow(f"{prefix} — address",
                        f"{row.entity_address} {row.entity_zip}".strip()),
                PackRow(f"{prefix} — date acquired",
                        row.date_acquired.strftime("%d/%m/%Y")
                        if row.date_acquired else ""),
                PackRow(f"{prefix} — initial investment",
                        inr(row.initial_investment)),
                PackRow(f"{prefix} — peak value", inr(row.peak_value)),
                PackRow(f"{prefix} — closing balance", inr(row.closing_value)),
                PackRow(f"{prefix} — gross income and proceeds",
                        inr(row.gross_income_accrued)),
            ]
        pack.sections.append(fa)

    ftc = getattr(comp, "ftc", None)
    if ftc and getattr(ftc, "lines", None):
        fsi = PackSection("Schedule FSI and Schedule TR — Foreign Tax Credit")
        for line in ftc.lines:
            fsi.rows += [
                PackRow(f"{line.nature_of_income} — income from outside India",
                        inr(line.income_inr)),
                PackRow(f"{line.nature_of_income} — tax paid outside India",
                        inr(line.foreign_tax_inr)),
                PackRow(f"{line.nature_of_income} — tax payable in India",
                        inr(line.indian_tax_on_income)),
                PackRow(f"{line.nature_of_income} — relief claimed u/s 90",
                        inr(line.credit_allowed),
                        "Rule 128 caps the credit at the Indian tax on the "
                        "same income"),
            ]
        fsi.rows.append(PackRow("Total relief u/s 90", inr(ftc.total_credit)))
        if ftc.total_forfeited > 0:
            fsi.rows.append(PackRow(
                "Foreign tax that cannot be credited",
                inr(ftc.total_forfeited),
                "Not refundable and does not carry forward",
            ))
        pack.sections.append(fsi)

    computation = PackSection("Part B-TI and Part B-TTI — Computation")
    computation.rows += [
        PackRow("Gross total income", inr(comp.gross_total_income)),
        PackRow("Total income", inr(comp.total_income_rounded)),
        PackRow("Tax at normal rates", inr(comp.tax_on_normal_income)),
        PackRow("Tax at special rates", inr(comp.tax_on_special_income)),
        PackRow("Rebate u/s 87A", inr(comp.rebate_87a)),
        PackRow("Surcharge", inr(comp.surcharge),
                f"After marginal relief of ₹{inr(comp.surcharge_marginal_relief)}"
                if comp.surcharge_marginal_relief else ""),
        PackRow("Health and education cess", inr(comp.cess)),
        PackRow("Net tax liability", inr(comp.total_tax_liability)),
        PackRow("Interest u/s 234A", inr(comp.interest.section_234a)),
        PackRow("Interest u/s 234B", inr(comp.interest.section_234b)),
        PackRow("Interest u/s 234C", inr(comp.interest.section_234c)),
        PackRow("Fee u/s 234F", inr(comp.interest.section_234f)),
    ]
    pack.sections.append(computation)

    taxes = PackSection("Schedule TDS, TCS and IT — Taxes Paid")
    taxes.rows += [
        PackRow("Total TDS", inr(comp.tds)),
        PackRow("Total TCS", inr(comp.tcs)),
        PackRow("Advance tax", inr(comp.advance_tax)),
        PackRow("Self-assessment tax", inr(comp.self_assessment_tax)),
        PackRow("Total taxes paid", inr(comp.total_taxes_paid)),
    ]
    if comp.net_payable > 0:
        taxes.rows.append(
            PackRow("Balance tax payable", inr(comp.net_payable),
                    "Pay this as self-assessment tax under minor head 300 "
                    "before submitting the return, then enter the challan.")
        )
    else:
        taxes.rows.append(PackRow("Refund due", inr(comp.refund_due)))
    pack.sections.append(taxes)

    pack.checklist = _checklist(tr, comp, form)
    return pack


def _filing_section_label(tr: TaxReturn, ay) -> str:
    from datetime import date

    filing_date = tr.filing_date or date.today()
    if tr.is_revised:
        return "139(5) — revised return"
    if filing_date > due_date_for(ay, has_business=bool(tr.trading_segments)):
        return "139(4) — belated return"
    return "139(1) — on or before the due date"


def _checklist(tr: TaxReturn, comp: Computation, form: str) -> List[str]:
    steps = []
    if comp.net_payable > 0:
        steps.append(
            f"Pay ₹{inr(comp.net_payable)} of self-assessment tax at "
            "e-Pay Tax on the portal (minor head 300), and note the BSR code, "
            "challan serial number and date."
        )
        steps.append(
            "Add that challan to the return before generating the JSON again, "
            "otherwise the portal will show the tax as still outstanding."
        )
    ftc = getattr(comp, "ftc", None)
    if ftc and getattr(ftc, "total_credit", 0) > 0:
        steps.insert(0, (
            "File Form 67 first, before the return — e-File → Income Tax Forms "
            "→ File Income Tax Forms → Form 67. Rule 128(9) requires it, and "
            "CPC denies the foreign tax credit without it."
        ))
    if tr.foreign_assets:
        steps.append(
            "Check Schedule FA line by line. It reports the CALENDAR year, not "
            "the financial year, and omitting a single foreign asset carries a "
            "flat ₹10 lakh penalty under the Black Money Act."
        )

    steps += [
        f"Log in at incometax.gov.in and go to e-File → Income Tax Returns → "
        f"File Income Tax Return, choose AY {tr.assessment_year} and {form}.",
        "Choose the offline or utility route and upload the generated JSON.",
        "Compare the portal's preview against the computation sheet from this "
        "system, line by line, before you submit.",
        "Submit, then e-verify within 30 days — Aadhaar OTP is the quickest. "
        "An unverified return is treated as never filed.",
    ]
    if comp.refund_due > 0:
        steps.append(
            "Confirm the refund bank account is pre-validated on the portal, "
            "or the refund will fail."
        )
    if comp.carried_forward:
        steps.append(
            "This return carries losses forward, which only survives if it is "
            "filed by the due date under section 139(1)."
        )
    return steps
