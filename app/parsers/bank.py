"""Bank and post-office interest certificates.

These arrive as a one-page PDF listing interest credited on savings accounts
and on term deposits, and often the TDS deducted. The split matters: savings
interest qualifies for section 80TTA while deposit interest does not.
"""

from __future__ import annotations

import re
from decimal import Decimal

from ..money import D
from .base import (
    Extraction,
    IFSC_RE,
    TAN_RE,
    find_amount_after,
    looks_like,
    normalise,
)

DOC_TYPE = "bank_interest"


def score(text: str) -> float:
    hits = looks_like(
        text,
        "Interest Certificate", "Interest Paid", "Interest Credited",
        "Savings Account", "Term Deposit", "Fixed Deposit",
        "TDS Certificate", "Provisional Interest",
    )
    strong = looks_like(text, "interest certificate", "interest paid/credited")
    return min((hits + strong * 2) / 5.0, 1.0)


def parse(text: str, filename: str, tables=None) -> Extraction:
    text = normalise(text)
    out = Extraction(document_type=DOC_TYPE, source_filename=filename)
    out.confidence = score(text)
    out.raw_text_excerpt = text[:1500]

    bank = _bank_name(text, filename)

    savings = find_amount_after(
        text,
        r"Interest (?:paid|credited)? ?on savings",
        r"Savings (?:Bank )?(?:Account )?Interest",
        r"SB (?:A/c )?Interest",
    )
    deposit = find_amount_after(
        text,
        r"Interest (?:paid|credited)? ?on (?:term|fixed|time) deposit",
        r"(?:Term|Fixed) Deposit Interest",
        r"\bFD Interest\b",
        r"Interest on deposits",
    )
    total = find_amount_after(
        text, r"Total interest", r"Aggregate interest", r"Total Interest Paid"
    )

    if savings:
        out.add("other_sources.savings_bank_interest",
                f"Savings account interest — {bank}", savings, confidence=0.8)
    if deposit:
        out.add("other_sources.fixed_deposit_interest",
                f"Deposit interest — {bank}", deposit, confidence=0.8)
    if total and not savings and not deposit:
        out.add("other_sources.fixed_deposit_interest",
                f"Interest — {bank} (savings/deposit split not stated)",
                total, confidence=0.5)
        out.warnings.append(
            f"{bank}: the certificate did not separate savings interest from "
            "deposit interest, so the whole amount has been treated as deposit "
            "interest. Split it yourself if part was savings interest — only "
            "that part qualifies for section 80TTA."
        )

    tds = find_amount_after(
        text, r"TDS (?:deducted|amount)", r"Tax Deducted at Source",
        r"Total TDS",
    )
    if tds and tds > 0:
        tan = TAN_RE.search(text)
        out.payments.append({
            "kind": "tds_other",
            "deductor_name": bank,
            "deductor_tan": tan.group(1) if tan else "",
            "amount": tds,
            "source_document": filename,
        })

    if not out.facts:
        out.warnings.append(
            f"No interest figure could be read from {filename}. Enter it by "
            "hand on the Income page."
        )
    return out


def _bank_name(text: str, filename: str) -> str:
    known = (
        "State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank",
        "Kotak Mahindra", "Punjab National Bank", "Bank of Baroda",
        "Canara Bank", "Union Bank", "IDFC FIRST", "Yes Bank", "IndusInd",
        "Post Office", "Bandhan Bank", "Federal Bank", "RBL Bank",
    )
    for name in known:
        if name.lower() in text.lower():
            return name
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if 4 <= len(first) <= 60:
        return first
    return filename.rsplit(".", 1)[0][:40]
