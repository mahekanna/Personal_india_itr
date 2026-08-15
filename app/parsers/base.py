"""Shared plumbing for every parser.

A parser never mutates the return directly. It produces an ``Extraction``: a
bag of typed facts, each carrying where it came from and how confident we are.
The user reviews those facts, and only then do they get merged. Tax software
that silently overwrites a figure is worse than no tax software.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..money import D

# --------------------------------------------------------------------------
# Extraction model
# --------------------------------------------------------------------------


@dataclass
class Fact:
    """One extracted value."""

    path: str                 # dotted path into TaxReturn, e.g. "other_sources.dividend_income"
    label: str                # human wording for the review screen
    value: Any
    confidence: float = 0.8   # 0..1
    evidence: str = ""        # the line of text it came from
    source: str = ""          # file name
    editable: bool = True

    def as_display(self) -> str:
        if isinstance(self.value, Decimal):
            return f"{self.value:,.2f}"
        return str(self.value)


@dataclass
class Extraction:
    document_type: str
    source_filename: str
    confidence: float = 0.0
    facts: List[Fact] = field(default_factory=list)
    # Repeating rows the caller appends rather than sets: salaries, TDS
    # entries, capital-gain transactions.
    salaries: List[Dict[str, Any]] = field(default_factory=list)
    payments: List[Dict[str, Any]] = field(default_factory=list)
    capital_gains: List[Dict[str, Any]] = field(default_factory=list)
    house_properties: List[Dict[str, Any]] = field(default_factory=list)
    # Foreign equity, kept apart from ``capital_gains`` because a vest or a
    # dividend is not yet a gain — it has to go through the Rule 115 conversion
    # and the lot matching first.
    rsu_vests: List[Dict[str, Any]] = field(default_factory=list)
    espp_purchases: List[Dict[str, Any]] = field(default_factory=list)
    dividends: List[Dict[str, Any]] = field(default_factory=list)
    foreign_sales: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_text_excerpt: str = ""
    # Set when the file is an encrypted PDF none of the candidate passwords
    # opened. The batch parser retries these once the PAN has been found in
    # some other document, which is the usual way it becomes known.
    needs_password: bool = False

    def add(self, path: str, label: str, value: Any, *,
            confidence: float = 0.8, evidence: str = "") -> None:
        if value is None:
            return
        if isinstance(value, Decimal) and value == 0:
            return
        self.facts.append(
            Fact(path=path, label=label, value=value, confidence=confidence,
                 evidence=evidence, source=self.source_filename)
        )


class ParserError(Exception):
    pass


class PasswordRequired(ParserError):
    """The PDF is encrypted and we have no working password."""


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------


def candidate_passwords(pan: str = "", dob: Optional[date] = None) -> List[str]:
    """The password formats the department and the banks actually use.

    AIS and 26AS use the PAN in lower case followed by the date of birth as
    DDMMYYYY. Form 16 from most payroll systems uses the same, or the PAN in
    upper case. Bank interest certificates vary wildly.
    """
    pan = (pan or "").strip()
    out: List[str] = []
    if pan and dob:
        for pan_form in (pan.lower(), pan.upper()):
            out.append(f"{pan_form}{dob:%d%m%Y}")
            out.append(f"{pan_form}{dob:%d%m%y}")
            out.append(f"{pan_form}_{dob:%d%m%Y}")
    if pan:
        out.extend([pan.upper(), pan.lower()])
    if dob:
        out.append(f"{dob:%d%m%Y}")
    return out


def extract_pdf_text(
    data: bytes, passwords: Sequence[str] = (), max_pages: int = 60
) -> str:
    """Return the full text of a PDF, decrypting it first if we must."""
    import pdfplumber
    from pypdf import PdfReader, PdfWriter

    payload = data
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            opened = False
            for password in list(passwords) + [""]:
                try:
                    if reader.decrypt(password) != 0:
                        opened = True
                        break
                except Exception:  # noqa: BLE001 - pypdf raises a variety of errors
                    continue
            if not opened:
                raise PasswordRequired(
                    "This PDF is password protected. AIS and Form 26AS use your "
                    "PAN in lower case followed by your date of birth as "
                    "DDMMYYYY — fill those in on the profile page and re-upload."
                )
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            buffer = io.BytesIO()
            writer.write(buffer)
            payload = buffer.getvalue()
    except PasswordRequired:
        raise
    except Exception:  # noqa: BLE001 - fall through to pdfplumber on odd files
        payload = data

    chunks: List[str] = []
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        for page in pdf.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def extract_pdf_tables(
    data: bytes, passwords: Sequence[str] = (), max_pages: int = 40
) -> List[List[List[str]]]:
    """Every table on every page, as lists of rows of cells."""
    import pdfplumber
    from pypdf import PdfReader, PdfWriter

    payload = data
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            for password in list(passwords) + [""]:
                try:
                    if reader.decrypt(password) != 0:
                        break
                except Exception:  # noqa: BLE001
                    continue
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            buffer = io.BytesIO()
            writer.write(buffer)
            payload = buffer.getvalue()
    except Exception:  # noqa: BLE001
        payload = data

    tables: List[List[List[str]]] = []
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        for page in pdf.pages[:max_pages]:
            for table in page.extract_tables() or []:
                tables.append(
                    [[(cell or "").strip() for cell in row] for row in table]
                )
    return tables


# --------------------------------------------------------------------------
# Field scraping helpers
# --------------------------------------------------------------------------

_AMOUNT = r"(-?[\d,]+(?:\.\d{1,2})?)"

PAN_RE = re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")
TAN_RE = re.compile(r"\b([A-Z]{4}\d{5}[A-Z])\b")
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
DATE_RE = re.compile(
    r"\b(\d{1,2})[-/\s]([A-Za-z]{3,9}|\d{1,2})[-/\s](\d{2,4})\b"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def find_amount_after(text: str, *patterns: str, window: int = 220) -> Optional[Decimal]:
    """First rupee figure appearing after any of the given label patterns."""
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            tail = text[match.end(): match.end() + window]
            found = re.search(_AMOUNT, tail)
            if found:
                value = D(found.group(1))
                if value != 0:
                    return value
    return None


def find_all_amounts(line: str) -> List[Decimal]:
    return [D(m) for m in re.findall(_AMOUNT, line)]


def find_line(text: str, *patterns: str) -> str:
    for pattern in patterns:
        for line in text.splitlines():
            if re.search(pattern, line, re.IGNORECASE):
                return line.strip()
    return ""


def parse_date(value: str) -> Optional[date]:
    """Parse the date formats these documents use, without dateutil."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d",
                "%d-%B-%Y", "%d %B %Y", "%d/%m/%y", "%d-%b-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    match = DATE_RE.search(value)
    if match:
        day, month_token, year = match.groups()
        month = (
            int(month_token)
            if month_token.isdigit()
            else _MONTHS.get(month_token[:3].lower(), 0)
        )
        if month:
            year_int = int(year)
            if year_int < 100:
                year_int += 2000
            try:
                return date(year_int, month, int(day))
            except ValueError:
                return None
    return None


def financial_year_of(when: date) -> str:
    """The Indian financial year a date falls in, as "2025-26"."""
    start = when.year if when.month >= 4 else when.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def normalise(text: str) -> str:
    """Collapse whitespace so multi-line labels still match."""
    return re.sub(r"[ \t]+", " ", text)


def looks_like(text: str, *needles: str) -> int:
    """How many of the needles appear — used for document-type scoring."""
    lowered = text.lower()
    return sum(1 for needle in needles if needle.lower() in lowered)
