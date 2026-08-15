"""Annual Information Statement and Taxpayer Information Summary.

The AIS is the single most useful document a taxpayer has, because it is what
the department already believes about their year. Anything in it that is
missing from the return is what triggers a notice.

Three shapes are handled: the JSON download, the PDF, and the per-category
CSV/Excel exports.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..money import D
from .base import (
    Extraction,
    PAN_RE,
    find_amount_after,
    looks_like,
    normalise,
    parse_date,
)

DOC_TYPE = "ais"


def score(text: str) -> float:
    hits = looks_like(
        text,
        "Annual Information Statement", "Taxpayer Information Summary",
        "AIS", "Information Category", "Part-B", "SFT-",
        "Interest from savings bank",
    )
    return min(hits / 4.0, 1.0)


# AIS information-category headings mapped onto the return. The department's
# wording changes slightly year to year, so each entry lists several phrasings.
_CATEGORY_MAP: List[tuple] = [
    (("salary",),
     "salaries.salary_17_1", "Salary reported in AIS"),
    (("interest from savings bank", "savings bank interest"),
     "other_sources.savings_bank_interest", "Savings bank interest"),
    (("interest from deposit", "interest from term deposit",
      "interest from fixed deposit"),
     "other_sources.fixed_deposit_interest", "Fixed and recurring deposit interest"),
    (("interest from others", "interest from income tax refund"),
     "other_sources.other_interest", "Other interest"),
    (("dividend",),
     "other_sources.dividend_income", "Dividend income"),
    (("rent received", "receipt of rent"),
     "house_properties.annual_rent_received", "Rent received"),
    (("business receipts", "receipts from business"),
     "business.gross_turnover_digital", "Business receipts"),
    (("income from lottery", "winnings from"),
     "other_sources.winnings_115bb", "Winnings taxable u/s 115BB"),
]


def parse(text: str, filename: str, tables=None, raw: bytes | None = None) -> Extraction:
    out = Extraction(document_type=DOC_TYPE, source_filename=filename)

    if raw and filename.lower().endswith(".json"):
        return _parse_json(raw, filename, out)

    text = normalise(text)
    out.confidence = score(text)
    out.raw_text_excerpt = text[:2000]

    pan = PAN_RE.search(text)
    if pan:
        out.add("taxpayer.pan", "PAN", pan.group(1), confidence=0.9)

    lowered = text.lower()
    for keys, path, label in _CATEGORY_MAP:
        amount = None
        for key in keys:
            if key in lowered:
                amount = find_amount_after(text, re.escape(key))
                if amount:
                    break
        if amount and amount > 0:
            # Salary and rent belong to repeating structures; surface them as
            # cross-checks rather than as direct writes.
            if path.startswith(("salaries.", "house_properties.")):
                out.add(f"crosscheck.{path}", f"{label} (for reconciliation)",
                        amount, confidence=0.55)
            else:
                out.add(path, label, amount, confidence=0.7,
                        evidence="Annual Information Statement")

    _securities_from_text(text, out, filename)

    if not out.facts and not out.capital_gains:
        out.warnings.append(
            "Nothing could be read from this AIS. The JSON download from the "
            "compliance portal parses far more reliably than the PDF."
        )
    return out


# --------------------------------------------------------------------------
# JSON download
# --------------------------------------------------------------------------


def _parse_json(raw: bytes, filename: str, out: Extraction) -> Extraction:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        out.warnings.append(f"This file is not valid JSON: {exc}")
        return out

    out.confidence = 0.95
    totals: Dict[str, Decimal] = {}

    def walk(node: Any, heading: str = "") -> None:
        if isinstance(node, dict):
            label = ""
            for key in ("infoCategory", "informationCategory", "category",
                        "infoDesc", "informationDescription", "description"):
                if isinstance(node.get(key), str):
                    label = node[key]
                    break
            amount = None
            for key in ("amount", "infoValue", "value", "totalAmount",
                        "amtValue", "informationValue"):
                if key in node and node[key] not in (None, ""):
                    amount = D(node[key])
                    break
            current = label or heading
            if current and amount is not None and amount != 0:
                totals[current.lower()] = totals.get(current.lower(), D(0)) + amount
            for value in node.values():
                walk(value, current or heading)
        elif isinstance(node, list):
            for item in node:
                walk(item, heading)

    walk(payload)

    if isinstance(payload, dict):
        pan = json.dumps(payload)[:4000]
        found = PAN_RE.search(pan)
        if found:
            out.add("taxpayer.pan", "PAN", found.group(1), confidence=0.9)

    for heading, amount in totals.items():
        for keys, path, label in _CATEGORY_MAP:
            if any(key in heading for key in keys):
                if path.startswith(("salaries.", "house_properties.")):
                    out.add(f"crosscheck.{path}",
                            f"{label} (for reconciliation)", amount,
                            confidence=0.8)
                else:
                    out.add(path, label, amount, confidence=0.85,
                            evidence=f"AIS category: {heading}")
                break

    if not totals:
        out.warnings.append(
            "The JSON was read but no information categories were recognised. "
            "It may be the TIS rather than the AIS."
        )
    return out


# --------------------------------------------------------------------------
# Securities transactions
# --------------------------------------------------------------------------

_SECURITIES_LINE = re.compile(
    r"(sale of securities|sale of mutual fund|off market|redemption)",
    re.IGNORECASE,
)


def _securities_from_text(text: str, out: Extraction, filename: str) -> None:
    """AIS reports sale consideration but not cost, so we cannot compute a gain.

    We record the aggregate so the reconciliation screen can tell the user
    whether their broker statement covers everything the department can see.
    """
    total = D(0)
    for line in text.splitlines():
        if _SECURITIES_LINE.search(line):
            amounts = [D(m) for m in re.findall(r"[\d,]+\.\d{2}", line)]
            if amounts:
                total += max(amounts)
    if total > 0:
        out.add(
            "crosscheck.securities_sale_consideration",
            "Sale consideration of securities reported in AIS",
            total,
            confidence=0.6,
            evidence="AIS reports the sale value only — the cost of "
                     "acquisition has to come from your broker statement.",
        )
        out.warnings.append(
            f"AIS shows securities sales of ₹{total:,.0f}. Upload the broker "
            "capital-gains statement so the cost of acquisition is captured, "
            "otherwise the whole sale value looks like a gain to the department."
        )
