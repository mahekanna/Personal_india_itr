"""Form 16 — the TDS certificate an employer issues under section 203.

Part A carries the TAN and the quarterly TDS. Part B carries the salary
break-up, the section 10 exemptions, the Chapter VI-A deductions the employer
allowed, and the tax computed. Layouts differ between TRACES-generated PDFs and
the annexures payroll vendors bolt on, so every field is matched against
several phrasings and anything not found is simply left for the user.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from ..money import D
from .base import (
    Extraction,
    PAN_RE,
    TAN_RE,
    find_amount_after,
    find_line,
    looks_like,
    normalise,
    parse_date,
)

DOC_TYPE = "form16"


def score(text: str) -> float:
    """How strongly this text looks like a Form 16."""
    hits = looks_like(
        text,
        "FORM NO. 16", "FORM NO.16", "Certificate under section 203",
        "PART B", "Details of Salary Paid", "Gross Salary",
    )
    if "form no. 16" in text.lower() or "form no.16" in text.lower():
        hits += 3
    if "16a" in text.lower()[:2000] and "salary" not in text.lower()[:2000]:
        hits -= 2
    return min(hits / 5.0, 1.0)


_EXEMPTION_PATTERNS = {
    "hra": (r"House\s*Rent\s*Allowance", r"\bHRA\b", r"10\(13A\)"),
    "lta": (r"Leave\s*Travel", r"\bLTA\b", r"10\(5\)"),
    "gratuity": (r"Gratuity", r"10\(10\)"),
    "leave_encashment": (r"Leave\s*encashment", r"10\(10AA\)"),
    "children_education_allowance": (r"Children\s*Education",),
    "hostel_allowance": (r"Hostel",),
    "conveyance_allowance": (r"Conveyance",),
}

_DEDUCTION_PATTERNS = {
    "deductions.s80c": (
        r"80C\b", r"Deduction in respect of life insurance",
    ),
    "deductions.s80ccd1b": (r"80CCD\s*\(?1B\)?", r"80CCD\(1B\)"),
    "deductions.s80ccd2": (r"80CCD\s*\(?2\)?", r"contribution by the employer"),
    "deductions.s80d": (r"80D\b", r"health insurance premi"),
    "deductions.s80e": (r"80E\b", r"interest on loan taken for higher education"),
    "deductions.s80g": (r"80G\b", r"donations to certain funds"),
    "deductions.s80tta": (r"80TTA\b",),
}


def parse(text: str, filename: str, tables=None) -> Extraction:
    text = normalise(text)
    out = Extraction(document_type=DOC_TYPE, source_filename=filename)
    out.confidence = score(text)
    out.raw_text_excerpt = text[:2000]

    salary: dict = {"source_document": filename}

    # ---- Identity -----------------------------------------------------------
    employer_line = find_line(text, r"Name and address of the Employer",
                              r"Name of the Employer")
    tan_match = TAN_RE.search(text)
    if tan_match:
        salary["employer_tan"] = tan_match.group(1)

    employer_name = _employer_name(text)
    if employer_name:
        salary["employer_name"] = employer_name

    pans = PAN_RE.findall(text)
    if pans:
        # The employee's PAN is usually the one labelled as such; fall back to
        # the last PAN on the page, since the deductor's own PAN comes first.
        labelled = re.search(
            r"PAN of the (?:Employee|Deductee)[^A-Z]{0,40}([A-Z]{5}\d{4}[A-Z])",
            text, re.IGNORECASE,
        )
        employee_pan = labelled.group(1) if labelled else pans[-1]
        out.add("taxpayer.pan", "PAN", employee_pan, confidence=0.9,
                evidence=f"Found in {filename}")

    employee_name = _employee_name(text)
    if employee_name:
        out.add("taxpayer.name", "Name of the taxpayer", employee_name,
                confidence=0.6)

    # ---- Salary break-up ----------------------------------------------------
    gross = find_amount_after(
        text,
        r"Gross Salary\s*\(?1?\)?\s*\(?d\)?", r"Total\s+of\s+Gross\s+Salary",
        r"\(d\)\s*Total", r"Gross Salary",
    )
    s17_1 = find_amount_after(
        text, r"Salary as per provisions contained in section\s*17\s*\(1\)",
        r"section\s*17\s*\(1\)",
    )
    s17_2 = find_amount_after(
        text, r"Value of perquisites under section\s*17\s*\(2\)",
        r"section\s*17\s*\(2\)",
    )
    s17_3 = find_amount_after(
        text, r"Profits in lieu of salary under section\s*17\s*\(3\)",
        r"section\s*17\s*\(3\)",
    )

    if s17_1:
        salary["salary_17_1"] = s17_1
    elif gross:
        salary["salary_17_1"] = gross
        out.warnings.append(
            "The 17(1)/17(2)/17(3) split was not readable, so the whole gross "
            "salary has been placed against section 17(1). Check the perquisite "
            "figure if you had one."
        )
    if s17_2:
        salary["perquisites_17_2"] = s17_2
    if s17_3:
        salary["profits_in_lieu_17_3"] = s17_3

    # ---- Section 10 exemptions ---------------------------------------------
    exemptions: dict = {}
    for key, patterns in _EXEMPTION_PATTERNS.items():
        amount = find_amount_after(text, *patterns)
        if amount and amount > 0:
            exemptions[key] = amount
    if exemptions:
        salary["exempt_allowances"] = exemptions

    # ---- Section 16 ---------------------------------------------------------
    professional_tax = find_amount_after(
        text, r"Tax on employment under section\s*16\s*\(iii\)",
        r"Professional Tax", r"section\s*16\s*\(iii\)",
    )
    if professional_tax:
        salary["professional_tax"] = professional_tax

    # ---- TDS ----------------------------------------------------------------
    tds = find_amount_after(
        text,
        r"Total amount of tax deducted at source",
        r"Amount of tax deducted",
        r"Total\s*\(?Rs\.?\)?\s*deducted",
        r"Tax deducted at source",
    )
    if tds:
        salary["tds_deducted"] = tds
        out.payments.append({
            "kind": "tds_salary",
            "deductor_name": salary.get("employer_name", ""),
            "deductor_tan": salary.get("employer_tan", ""),
            "amount": tds,
            "source_document": filename,
        })

    # ---- Chapter VI-A the employer allowed ---------------------------------
    for path, patterns in _DEDUCTION_PATTERNS.items():
        amount = find_amount_after(text, *patterns)
        if amount and amount > 0:
            label = path.split(".")[-1].upper().replace("S", "Section ", 1)
            out.add(path, f"{label} (as allowed by the employer)", amount,
                    confidence=0.6)

    # ---- Regime the employer applied ---------------------------------------
    lowered = text.lower()
    if "115bac" in lowered:
        opted_out = re.search(
            r"opt(?:ing|ed)?\s+out\s+of\s+.{0,40}115BAC", text, re.IGNORECASE
        )
        out.add(
            "regime_choice",
            "Regime your employer used for TDS",
            "old" if opted_out else "new",
            confidence=0.5,
        )

    if salary.get("salary_17_1") or salary.get("tds_deducted"):
        out.salaries.append(salary)
    else:
        out.warnings.append(
            "No salary figure could be read from this Form 16. It may be a "
            "scanned image rather than a text PDF — enter the figures by hand "
            "on the Income page."
        )
    return out


def _employer_name(text: str) -> str:
    match = re.search(
        r"Name and address of the Employer[^\n]*\n\s*([^\n]{3,80})",
        text, re.IGNORECASE,
    )
    if match:
        return _clean_name(match.group(1))
    match = re.search(r"Name of the Employer\s*[:\-]?\s*([^\n]{3,80})",
                      text, re.IGNORECASE)
    return _clean_name(match.group(1)) if match else ""


def _employee_name(text: str) -> str:
    match = re.search(
        r"Name and address of the (?:Employee|Deductee)[^\n]*\n\s*([^\n]{3,80})",
        text, re.IGNORECASE,
    )
    if match:
        return _clean_name(match.group(1))
    match = re.search(r"Name of the (?:Employee|Deductee)\s*[:\-]?\s*([^\n]{3,80})",
                      text, re.IGNORECASE)
    return _clean_name(match.group(1)) if match else ""


def _clean_name(raw: str) -> str:
    cleaned = re.sub(r"\s{2,}", " ", raw).strip(" :-\t")
    # Strip a PAN or TAN that got swept into the same line.
    cleaned = PAN_RE.sub("", cleaned)
    cleaned = TAN_RE.sub("", cleaned)
    return cleaned.strip(" ,-")[:80]
