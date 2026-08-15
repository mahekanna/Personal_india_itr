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
    _check_foreign(tr, report)
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


def _check_foreign(tr: TaxReturn, report: ReconciliationReport) -> None:
    """Foreign holdings carry their own, much larger, failure modes."""
    has_foreign = bool(
        tr.rsu_vests or tr.dividends or tr.foreign_sales
        or tr.foreign_holdings or tr.foreign_assets or tr.espp_purchases
    )
    if not has_foreign:
        return

    resident = tr.taxpayer.residential_status == "RES"

    # -- Schedule FA ------------------------------------------------------
    if resident and not tr.foreign_assets:
        report.add(
            "error",
            "Foreign holdings are present but Schedule FA is empty",
            "A resident and ordinarily resident must report every foreign asset "
            "held at any point in the calendar year, at any value, income or "
            "no income.",
            "Fill in the entity name, address and year-end price for each "
            "holding on the Foreign income page. Omitting an asset attracts a "
            "flat ₹10 lakh penalty under the Black Money Act — far more than "
            "any tax at stake.",
        )
    elif resident and not tr.taxpayer.has_foreign_assets:
        report.add(
            "error",
            "The foreign-assets declaration is unticked",
            "Schedule FA rows exist but the return does not declare that "
            "foreign assets are held.",
            "Tick 'I hold foreign assets' on the Income page.",
        )

    incomplete = [
        row.entity_name for row in tr.foreign_assets
        if row.table == "A3" and (not row.entity_address or not row.entity_zip)
    ]
    if incomplete:
        report.add(
            "warning",
            "Schedule FA rows are missing an address",
            "The portal will not accept a Table A3 row without the entity's "
            "address and ZIP code: " + ", ".join(incomplete[:4]) + ".",
            "The registered address is on the company's investor-relations "
            "page or the top of any 10-K.",
        )

    # -- Form 67 ----------------------------------------------------------
    withheld = sum((d.foreign_tax_withheld_fx for d in tr.dividends), D(0))
    if withheld > 0 and not tr.foreign_settings.form67_filed:
        report.add(
            "error",
            "Foreign tax credit claimed without Form 67",
            "Tax was withheld abroad and is being credited, but Form 67 is not "
            "marked as filed.",
            "Rule 128(9) wants Form 67 on the portal before the return. CPC "
            "denies the credit when it is missing, and getting it restored "
            "means an appeal.",
        )

    # -- The RSU perquisite against Form 16 --------------------------------
    vest_value = sum(
        (v.shares_vested * v.fmv_per_share_fx for v in tr.rsu_vests), D(0)
    )
    perquisite_in_form16 = sum((s.perquisites_17_2 for s in tr.salaries), D(0))
    if vest_value > 0 and perquisite_in_form16 <= 0:
        report.add(
            "warning",
            "RSUs vested but Form 16 shows no perquisite",
            "Vesting is taxable as salary under section 17(2)(vi), so it "
            "normally appears in the Form 16 perquisite figure.",
            "Check the Form 16. If the vests really are missing from it, untick "
            "'already included in my Form 16' against each one so the "
            "perquisite is added — and expect the tax to rise accordingly.",
        )

    # -- ESPP ---------------------------------------------------------------
    espp_discount = sum(
        (p.total_discount_fx for p in tr.espp_purchases), D(0)
    )
    if espp_discount > 0 and perquisite_in_form16 <= 0:
        report.add(
            "warning",
            "ESPP shares were bought at a discount but Form 16 shows no perquisite",
            "The discount is salary under section 17(2)(vi) and is normally run "
            "through payroll in the month of purchase.",
            "Check the Form 16. If the discount really is missing from it, "
            "untick 'already included in my Form 16' against each purchase so "
            "it is added to salary here.",
        )

    missing_fmv = [
        p for p in tr.espp_purchases
        if p.shares_purchased > 0 and p.fmv_per_share_fx <= 0
    ]
    if missing_fmv:
        report.add(
            "error",
            "ESPP purchase with no fair market value",
            f"{len(missing_fmv)} purchase(s) have no fair market value on the "
            "purchase date, so neither the perquisite nor the cost basis can be "
            "computed.",
            "The figure is on the purchase confirmation from your broker. "
            "Without it the cost basis is nil and the whole sale value would be "
            "taxed as gain.",
        )

    # The basis trap, checked against what actually reached Schedule CG.
    espp_lots_sold = [
        item for item in tr.capital_gains
        if item.lot_origin == "espp" and item.sale_consideration > 0
    ]
    if espp_lots_sold:
        report.add(
            "info",
            "ESPP shares sold — cost basis taken at fair market value",
            f"{len(espp_lots_sold)} disposal(s) of ESPP shares use the fair "
            "market value at purchase as the cost, per section 49(2AA), not the "
            "discounted price you paid.",
            "If you cross-check against a US 1099-B it will disagree, and it is "
            "the 1099-B that is wrong for Indian purposes — it reports the US "
            "basis.",
        )

    # -- Holding periods people misjudge -----------------------------------
    near_miss = [
        item for item in tr.capital_gains
        if item.category == "stcg_slab_foreign"
        and item.purchase_date and item.sale_date
        and 18 <= _months_between(item.purchase_date, item.sale_date) < 24
    ]
    if near_miss:
        report.add(
            "info",
            "Foreign shares sold shortly before the 24-month mark",
            f"{len(near_miss)} lot(s) were sold between 18 and 24 months after "
            "acquisition, so they are taxed at slab rates rather than 12.5%.",
            "Nothing to fix on this return — but foreign shares need 24 months, "
            "not the 12 that Indian listed shares need. Worth knowing before "
            "the next sale.",
        )

    # -- Unmatched disposals ------------------------------------------------
    unmatched = [
        item for item in tr.capital_gains
        if item.is_foreign and item.cost_of_acquisition == 0
        and item.sale_consideration > 0
    ]
    if unmatched:
        report.add(
            "error",
            "Foreign shares sold with no acquisition to match",
            f"{len(unmatched)} disposal(s) could not be matched to a vesting "
            "tranche or purchase, so the entire sale value is being treated as "
            "gain.",
            "Add the missing vests on the Foreign income page, or the tax will "
            "be computed on the gross proceeds.",
        )


def _months_between(start, end) -> int:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months


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
