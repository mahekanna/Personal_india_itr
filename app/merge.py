"""Merge reviewed extractions into the return.

Applied only after the user has seen the figures. Repeating rows (employers,
TDS entries, capital-gain transactions) are de-duplicated so re-uploading the
same Form 16 twice does not double the salary.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Set

from .money import D
from .parsers.base import Extraction
from .schemas import (
    CapitalGainItem,
    HouseProperty,
    SalaryIncome,
    TaxPayment,
    TaxReturn,
)


def apply_extractions(
    tr: TaxReturn,
    extractions: Iterable[Extraction],
    accepted_paths: Set[str] | None = None,
) -> List[str]:
    """Write accepted facts into the return. Returns a log of what changed."""
    log: List[str] = []

    for extraction in extractions:
        for fact in extraction.facts:
            if fact.path.startswith("crosscheck."):
                continue
            key = f"{extraction.source_filename}:{fact.path}"
            if accepted_paths is not None and key not in accepted_paths:
                continue
            if _set_path(tr, fact.path, fact.value):
                log.append(f"{fact.label} set to {fact.as_display()} "
                           f"(from {extraction.source_filename})")

        for row in extraction.salaries:
            if _merge_salary(tr, row):
                log.append(
                    f"Added salary from {row.get('employer_name', 'an employer')}"
                )

        for row in extraction.payments:
            if _merge_payment(tr, row):
                log.append(
                    f"Added {row.get('kind')} of ₹{D(row.get('amount', 0)):,.0f}"
                )

        added_gains = 0
        for row in extraction.capital_gains:
            if _merge_capital_gain(tr, row):
                added_gains += 1
        if added_gains:
            log.append(
                f"Added {added_gains} capital-gain transaction(s) from "
                f"{extraction.source_filename}"
            )

        for row in extraction.house_properties:
            tr.house_properties.append(HouseProperty(**row))
            log.append("Added a house property")

        for warning in extraction.warnings:
            if warning not in tr.notes:
                tr.notes.append(warning)

    return log


# --------------------------------------------------------------------------


def _set_path(tr: TaxReturn, path: str, value: Any) -> bool:
    """Set a dotted attribute path, e.g. ``other_sources.dividend_income``."""
    parts = path.split(".")
    target: Any = tr
    for part in parts[:-1]:
        if not hasattr(target, part):
            return False
        target = getattr(target, part)
    leaf = parts[-1]
    if not hasattr(target, leaf):
        return False

    current = getattr(target, leaf)
    # Money accumulates — two bank certificates both add to deposit interest.
    # Everything else is replaced only when it is currently empty, so a value
    # the user typed is never silently overwritten.
    if isinstance(current, Decimal):
        setattr(target, leaf, current + D(value))
        return True
    if current in (None, "", 0):
        setattr(target, leaf, value)
        return True
    return False


def _merge_salary(tr: TaxReturn, row: Dict[str, Any]) -> bool:
    tan = (row.get("employer_tan") or "").strip().upper()
    name = (row.get("employer_name") or "").strip().lower()
    for existing in tr.salaries:
        if tan and existing.employer_tan.upper() == tan:
            return False
        if not tan and name and existing.employer_name.strip().lower() == name:
            return False
    tr.salaries.append(SalaryIncome(**row))
    return True


def _merge_payment(tr: TaxReturn, row: Dict[str, Any]) -> bool:
    amount = D(row.get("amount", 0))
    if amount <= 0:
        return False
    tan = (row.get("deductor_tan") or "").strip().upper()
    kind = row.get("kind")
    for existing in tr.taxes_paid.payments:
        same_deductor = (
            tan and existing.deductor_tan.upper() == tan
        ) or (
            not tan
            and existing.deductor_name.strip().lower()
            == (row.get("deductor_name") or "").strip().lower()
        )
        if existing.kind == kind and same_deductor and existing.amount == amount:
            return False
    tr.taxes_paid.payments.append(TaxPayment(**row))
    return True


def _merge_capital_gain(tr: TaxReturn, row: Dict[str, Any]) -> bool:
    for existing in tr.capital_gains:
        if (
            existing.description == row.get("description")
            and existing.sale_date == row.get("sale_date")
            and existing.sale_consideration == D(row.get("sale_consideration", 0))
        ):
            return False
    tr.capital_gains.append(CapitalGainItem(**row))
    return True


def replace_tds_from_26as(tr: TaxReturn, extractions: Iterable[Extraction]) -> None:
    """Trust Form 26AS over Form 16 for TDS, since 26AS is what gets matched."""
    rows = [
        row
        for extraction in extractions
        if extraction.document_type == "form26as"
        for row in extraction.payments
    ]
    if not rows:
        return
    tr.taxes_paid.payments = [
        payment for payment in tr.taxes_paid.payments
        if payment.kind not in ("tds_salary", "tds_other", "tcs")
    ]
    for row in rows:
        tr.taxes_paid.payments.append(TaxPayment(**row))
    tr.notes.append(
        "TDS entries were replaced with those in Form 26AS, because that is "
        "the statement the department matches your claim against."
    )
