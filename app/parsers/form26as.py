"""Form 26AS — the consolidated tax statement from TRACES.

Part I lists TDS on salary, Part II TDS on other income, Part III TDS on sale
of immovable property, Part IV TCS, and Part V the advance tax and
self-assessment challans. The PDF is laid out as tables, so we prefer
``extract_tables`` and fall back to line scraping when the table grid is lost.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, List, Optional

from ..money import D
from .base import (
    Extraction,
    PAN_RE,
    TAN_RE,
    looks_like,
    normalise,
    parse_date,
)

DOC_TYPE = "form26as"


def score(text: str) -> float:
    hits = looks_like(
        text,
        "Annual Tax Statement", "Form 26AS", "26AS",
        "Details of Tax Deducted at Source",
        "TRACES", "Deductor", "Section 203AA",
    )
    return min(hits / 4.0, 1.0)


_SECTION_HEADINGS = {
    "salary": re.compile(r"PART\s*I\b.*Tax Deducted at Source", re.IGNORECASE),
    "other": re.compile(r"PART\s*II\b", re.IGNORECASE),
    "property": re.compile(r"PART\s*III\b", re.IGNORECASE),
    "tcs": re.compile(r"PART\s*I?V\b.*(?:Collected|TCS)", re.IGNORECASE),
    "challan": re.compile(
        r"PART\s*V\b|Details of Tax Paid.*other than TDS|Advance tax",
        re.IGNORECASE,
    ),
}


def parse(text: str, filename: str, tables=None) -> Extraction:
    text = normalise(text)
    out = Extraction(document_type=DOC_TYPE, source_filename=filename)
    out.confidence = score(text)
    out.raw_text_excerpt = text[:2000]

    pan = PAN_RE.search(text)
    if pan:
        out.add("taxpayer.pan", "PAN", pan.group(1), confidence=0.9)

    name = re.search(r"Name of (?:the )?Assessee\s*[:\-]?\s*([^\n]{3,70})",
                     text, re.IGNORECASE)
    if name:
        out.add("taxpayer.name", "Name of the taxpayer",
                name.group(1).strip(), confidence=0.7)

    rows_found = 0
    for table in tables or []:
        rows_found += _rows_from_table(table, out, filename)

    if rows_found == 0:
        _rows_from_text(text, out, filename)

    if not out.payments:
        out.warnings.append(
            "No TDS or challan rows could be read from this 26AS. If it is a "
            "scanned copy, download the text PDF from TRACES and try again."
        )
    return out


def _classify_section(header_text: str) -> str:
    for kind, pattern in _SECTION_HEADINGS.items():
        if pattern.search(header_text):
            return kind
    return ""


def _rows_from_table(table: List[List[str]], out: Extraction, filename: str) -> int:
    """Pull deductor rows out of one table."""
    if not table:
        return 0
    flat_header = " ".join(cell for cell in (table[0] or []) if cell).lower()
    joined = " ".join(" ".join(row) for row in table[:3]).lower()

    is_challan = any(
        token in joined
        for token in ("bsr code", "challan", "date of deposit", "major head")
    )
    is_tds = any(
        token in joined
        for token in ("tan of deductor", "name of deductor", "tax deducted",
                      "amount of tax deducted")
    )
    if not (is_challan or is_tds):
        return 0

    count = 0
    for row in table[1:]:
        cells = [cell for cell in row if cell]
        if len(cells) < 2:
            continue
        blob = " ".join(cells)
        amounts = [D(m) for m in re.findall(r"(-?[\d,]+\.\d{2}|\b\d{1,3}(?:,\d{2,3})+\b)", blob)]
        amounts = [a for a in amounts if a > 0]
        if not amounts:
            continue

        if is_challan:
            bsr = re.search(r"\b(\d{7})\b", blob)
            when = None
            for cell in cells:
                when = parse_date(cell)
                if when:
                    break
            kind = (
                "advance_tax"
                if re.search(r"advance", blob, re.IGNORECASE)
                else "self_assessment"
            )
            out.payments.append({
                "kind": kind,
                "amount": max(amounts),
                "payment_date": when,
                "bsr_code": bsr.group(1) if bsr else "",
                "source_document": filename,
            })
            count += 1
            continue

        tan = TAN_RE.search(blob)
        name = _deductor_name(cells)
        # The "tax deducted" column is normally the last-but-one money column;
        # taking the smallest positive amount avoids grabbing the gross credit.
        deducted = min(amounts) if len(amounts) > 1 else amounts[0]
        is_salary = bool(re.search(r"\b192\b", blob))
        out.payments.append({
            "kind": "tds_salary" if is_salary else "tds_other",
            "deductor_name": name,
            "deductor_tan": tan.group(1) if tan else "",
            "amount": deducted,
            "source_document": filename,
        })
        count += 1
    return count


def _rows_from_text(text: str, out: Extraction, filename: str) -> None:
    """Fallback when the PDF table grid did not survive extraction."""
    for line in text.splitlines():
        tan = TAN_RE.search(line)
        if not tan:
            continue
        amounts = [D(m) for m in re.findall(r"[\d,]+\.\d{2}", line)]
        amounts = [a for a in amounts if a > 0]
        if not amounts:
            continue
        out.payments.append({
            "kind": "tds_salary" if "192" in line else "tds_other",
            "deductor_name": _deductor_name([line]),
            "deductor_tan": tan.group(1),
            "amount": min(amounts),
            "source_document": filename,
        })


def _deductor_name(cells: List[str]) -> str:
    for cell in cells:
        stripped = TAN_RE.sub("", cell).strip()
        # A name has letters and is not mostly digits.
        letters = sum(ch.isalpha() for ch in stripped)
        if letters >= 4 and letters > sum(ch.isdigit() for ch in stripped):
            return re.sub(r"\s{2,}", " ", stripped)[:70]
    return ""
