"""Cross-document reconciliation.

A notice under section 143(1)(a) almost always comes from one of three
mismatches: TDS claimed that 26AS does not show, income the AIS reports that
the return omits, or a sale of securities with no matching capital gain. This
module looks for exactly those, before filing rather than after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from .money import D, inr, non_negative
from .parsers.base import Extraction
from .schemas import TaxReturn

# Rounding noise below this is not worth a warning.
TOLERANCE = D("100")


@dataclass
class Finding:
    severity: str          # "error" | "warning" | "info"
    title: str
    detail: str
    suggestion: str = ""
    amount: Optional[Decimal] = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error"


@dataclass
class ReconciliationReport:
    findings: List[Finding] = field(default_factory=list)

    def add(self, severity: str, title: str, detail: str,
            suggestion: str = "", amount: Optional[Decimal] = None) -> None:
        self.findings.append(Finding(severity, title, detail, suggestion, amount))

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def clean(self) -> bool:
        return not self.errors and not self.warnings


def reconcile(tr: TaxReturn, extractions: List[Extraction]) -> ReconciliationReport:
    report = ReconciliationReport()
    by_type: Dict[str, List[Extraction]] = {}
    for extraction in extractions:
        by_type.setdefault(extraction.document_type, []).append(extraction)

    _check_tds_against_26as(tr, by_type, report)
    _check_ais_income(tr, extractions, report)
    _check_securities_coverage(tr, extractions, report)
    _check_missing_documents(tr, by_type, report)
    _check_internal_consistency(tr, report)
    return report


# --------------------------------------------------------------------------


def _check_tds_against_26as(
    tr: TaxReturn, by_type: Dict[str, List[Extraction]], report: ReconciliationReport
) -> None:
    if "form26as" not in by_type:
        return

    claimed = tr.taxes_paid.total("tds_salary", "tds_other", "tcs")
    in_26as = D(0)
    for extraction in by_type["form26as"]:
        for payment in extraction.payments:
            if payment.get("kind") in ("tds_salary", "tds_other", "tcs"):
                in_26as += D(payment.get("amount", 0))

    difference = claimed - in_26as
    if abs(difference) <= TOLERANCE:
        report.add(
            "info", "TDS agrees with Form 26AS",
            f"₹{inr(claimed)} of TDS is claimed and Form 26AS shows ₹{inr(in_26as)}.",
        )
        return

    if difference > 0:
        report.add(
            "error",
            "You are claiming more TDS than Form 26AS shows",
            f"The return claims ₹{inr(claimed)} but Form 26AS accounts for only "
            f"₹{inr(in_26as)} — a gap of ₹{inr(difference)}.",
            "Credit that 26AS does not show will be disallowed under section "
            "143(1). Ask the deductor to file or correct their TDS return "
            "before you file.",
            amount=difference,
        )
    else:
        report.add(
            "warning",
            "Form 26AS shows more TDS than you are claiming",
            f"Form 26AS shows ₹{inr(in_26as)} but the return claims only "
            f"₹{inr(claimed)} — ₹{inr(-difference)} is unclaimed.",
            "You are leaving a refund on the table. Check whether a deductor "
            "is missing from the return.",
            amount=-difference,
        )


def _check_ais_income(
    tr: TaxReturn, extractions: List[Extraction], report: ReconciliationReport
) -> None:
    ais_values: Dict[str, Decimal] = {}
    for extraction in extractions:
        if extraction.document_type != "ais":
            continue
        for fact in extraction.facts:
            path = fact.path.replace("crosscheck.", "")
            if isinstance(fact.value, Decimal):
                ais_values[path] = ais_values.get(path, D(0)) + fact.value

    if not ais_values:
        return

    comparisons = [
        ("other_sources.savings_bank_interest", "Savings bank interest",
         tr.other_sources.savings_bank_interest),
        ("other_sources.fixed_deposit_interest", "Deposit interest",
         tr.other_sources.fixed_deposit_interest),
        ("other_sources.dividend_income", "Dividend income",
         tr.other_sources.dividend_income),
        ("salaries.salary_17_1", "Salary",
         sum((s.salary_17_1 for s in tr.salaries), D(0))),
    ]

    for path, label, declared in comparisons:
        reported = ais_values.get(path)
        if reported is None:
            continue
        shortfall = reported - declared
        if shortfall > TOLERANCE:
            report.add(
                "error",
                f"{label} in the AIS exceeds what the return declares",
                f"The AIS reports ₹{inr(reported)}; the return shows "
                f"₹{inr(declared)}.",
                "Either add the missing income or submit feedback on the "
                "compliance portal if the AIS entry is wrong. Filing without "
                "doing one of those invites a notice.",
                amount=shortfall,
            )
        elif shortfall < -TOLERANCE:
            report.add(
                "info",
                f"{label} declared is higher than the AIS shows",
                f"The return declares ₹{inr(declared)} against ₹{inr(reported)} "
                "in the AIS. Declaring more than the department can see is "
                "safe.",
            )


def _check_securities_coverage(
    tr: TaxReturn, extractions: List[Extraction], report: ReconciliationReport
) -> None:
    ais_sales = D(0)
    broker_sales = D(0)
    for extraction in extractions:
        for fact in extraction.facts:
            if fact.path == "crosscheck.securities_sale_consideration":
                ais_sales += D(fact.value)
            elif fact.path == "crosscheck.broker_sale_consideration":
                broker_sales += D(fact.value)

    if ais_sales <= 0:
        return

    declared = sum((item.sale_consideration for item in tr.capital_gains), D(0))
    covered = max(broker_sales, declared)
    gap = ais_sales - covered
    # AIS securities values are notoriously approximate, so only flag a
    # material gap.
    if gap > max(TOLERANCE * 100, ais_sales * D("0.10")):
        report.add(
            "warning",
            "Securities sales in the AIS are not fully covered",
            f"The AIS reports sales of ₹{inr(ais_sales)}; the capital-gains "
            f"schedule covers ₹{inr(covered)}.",
            "Upload the capital-gains statement from every broker and mutual "
            "fund you sold through. AIS values are approximate, so a small "
            "difference is normal.",
            amount=gap,
        )


def _check_missing_documents(
    tr: TaxReturn, by_type: Dict[str, List[Extraction]], report: ReconciliationReport
) -> None:
    if "form26as" not in by_type:
        report.add(
            "warning", "Form 26AS has not been uploaded",
            "Without it there is no way to verify that the TDS you are "
            "claiming actually reached the department.",
            "Download it from the e-filing portal under e-File → Income Tax "
            "Returns → View Form 26AS.",
        )
    if "ais" not in by_type:
        report.add(
            "warning", "The Annual Information Statement has not been uploaded",
            "The AIS is what the department already believes about your year. "
            "Filing without checking it is the commonest cause of a notice.",
            "Download it from the compliance portal — the JSON version parses "
            "best.",
        )
    if tr.salaries and "form16" not in by_type:
        report.add(
            "info", "No Form 16 was uploaded",
            "Salary figures have been entered without a Form 16 to check them "
            "against.",
        )


def _check_internal_consistency(tr: TaxReturn, report: ReconciliationReport) -> None:
    if not tr.taxpayer.pan or len(tr.taxpayer.pan) != 10:
        report.add(
            "error", "PAN is missing or malformed",
            "A ten-character PAN is required before the return can be built.",
        )
    if not any(account.is_primary_refund_account for account in tr.taxpayer.bank_accounts):
        if tr.taxpayer.bank_accounts:
            report.add(
                "warning", "No bank account is marked for the refund",
                "Mark one account as the refund account.",
            )
        else:
            report.add(
                "warning", "No bank account has been entered",
                "At least one pre-validated bank account is needed for a "
                "refund to be paid.",
            )

    for salary in tr.salaries:
        if salary.tds_deducted > 0 and not salary.employer_tan:
            report.add(
                "error",
                f"TDS claimed without a TAN for {salary.employer_name or 'an employer'}",
                "The department cannot match a TDS claim without the "
                "deductor's TAN.",
                "The TAN is on the first page of the Form 16.",
            )

    for item in tr.capital_gains:
        if item.sale_consideration > 0 and item.cost_of_acquisition == 0:
            report.add(
                "warning",
                f"No cost of acquisition for {item.description or 'a security'}",
                "The whole sale value would be taxed as a gain.",
                "Fill in what you paid for it.",
                amount=item.sale_consideration,
            )
            break
