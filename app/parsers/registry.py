"""Work out what a file is, then hand it to the right parser.

The user should be able to drop the whole folder in at once — Form 16 from two
employers, the AIS PDF, a 26AS, three bank certificates and a broker export —
without labelling anything.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import ais, bank, broker, form16, form26as, trading_pnl, us_equity
from .base import (  # noqa: F401 - PasswordRequired is re-raised below
    Extraction,
    PasswordRequired,
    candidate_passwords,
    extract_pdf_tables,
    extract_pdf_text,
)

# Ordered by specificity: the first parser whose score clears the bar wins,
# but we always run every scorer so an ambiguous file can be reported as such.
_PDF_PARSERS: List[Tuple[str, Callable[[str], float], Callable]] = [
    (form16.DOC_TYPE, form16.score, form16.parse),
    (form26as.DOC_TYPE, form26as.score, form26as.parse),
    (ais.DOC_TYPE, ais.score, ais.parse),
    (bank.DOC_TYPE, bank.score, bank.parse),
]

DOCUMENT_LABELS = {
    "form16": "Form 16 (salary TDS certificate)",
    "form26as": "Form 26AS (tax credit statement)",
    "ais": "Annual Information Statement",
    "bank_interest": "Bank interest certificate",
    "broker_pnl": "Broker capital-gains statement",
    "trading_pnl": "Trading P&L — F&O, intraday, currency, commodity",
    "us_equity": "US stock-plan or brokerage statement (RSU, dividends, sales)",
    "unknown": "Unrecognised document",
}

_TABULAR_SUFFIXES = (".csv", ".xlsx", ".xls", ".txt")
_ARCHIVE_SUFFIXES = (".zip",)


class ParserRegistry:
    """Kept as a class so callers can register their own parsers later."""

    def __init__(self) -> None:
        self.pdf_parsers = list(_PDF_PARSERS)


def parse_document(
    raw: bytes,
    filename: str,
    *,
    pan: str = "",
    date_of_birth: Optional[date] = None,
    forced_type: str = "",
) -> Extraction:
    """Identify and parse one uploaded file."""
    lowered = filename.lower()

    # ---- A zip, which is how TRACES hands over the text Form 26AS ---------
    if lowered.endswith(".zip"):
        return _parse_zip(raw, filename, pan=pan, date_of_birth=date_of_birth,
                          forced_type=forced_type)

    # ---- Spreadsheets and CSVs are broker exports of one flavour or another
    if lowered.endswith(_TABULAR_SUFFIXES):
        # ...but a .txt is just as likely to be the text-format Form 26AS or
        # AIS, and those are read by the same parsers that read the PDF. Sent
        # down the spreadsheet path it was identified as a broker statement,
        # read nil, and reported that it found no capital gains — so a
        # taxpayer uploading a TDS certificate lost every credit in it and was
        # told something irrelevant about capital gains.
        if lowered.endswith(".txt") and not forced_type:
            as_text = _parse_as_text(raw, filename)
            if as_text is not None:
                return as_text
        return _parse_tabular(raw, filename, forced_type)

    # ---- AIS JSON ---------------------------------------------------------
    if lowered.endswith(".json"):
        return ais.parse("", filename, raw=raw)

    if not lowered.endswith(".pdf"):
        out = Extraction(document_type="unknown", source_filename=filename)
        out.warnings.append(
            f"{filename} is not a file type this system reads. Upload a PDF, "
            "CSV, XLSX or the AIS JSON."
        )
        return out

    # ---- PDFs -------------------------------------------------------------
    passwords = candidate_passwords(pan, date_of_birth)
    try:
        text = extract_pdf_text(raw, passwords)
    except PasswordRequired as exc:
        out = Extraction(document_type="unknown", source_filename=filename)
        out.warnings.append(str(exc))
        out.needs_password = True
        return out

    if len(text.strip()) < 40:
        out = Extraction(document_type="unknown", source_filename=filename)
        out.warnings.append(
            f"{filename} appears to be a scanned image — no text could be "
            "extracted. Re-download the digitally generated PDF, or enter the "
            "figures by hand."
        )
        return out

    scores = {name: scorer(text) for name, scorer, _ in _PDF_PARSERS}
    # A broker statement occasionally arrives as a PDF.
    scores[broker.DOC_TYPE] = max(
        broker.score(text), broker.score_filename(filename)
    )

    chosen = forced_type or max(scores, key=lambda key: scores[key])
    if not forced_type and scores.get(chosen, 0) < 0.25:
        out = Extraction(document_type="unknown", source_filename=filename)
        out.raw_text_excerpt = text[:1500]
        out.warnings.append(
            f"Could not tell what {filename} is. Pick the document type by "
            "hand and upload it again."
        )
        return out

    tables = []
    if chosen in ("form26as", "ais", "form16"):
        try:
            tables = extract_pdf_tables(raw, passwords)
        except Exception:  # noqa: BLE001 - tables are a bonus, not a requirement
            tables = []

    if chosen == broker.DOC_TYPE:
        out = Extraction(document_type=broker.DOC_TYPE, source_filename=filename)
        out.warnings.append(
            "This looks like a broker capital-gains statement in PDF form. "
            "The Excel or CSV export from your broker parses far more "
            "reliably — please upload that instead."
        )
        out.raw_text_excerpt = text[:1500]
        return out

    parser = dict((name, fn) for name, _, fn in _PDF_PARSERS)[chosen]
    extraction = parser(text, filename, tables)

    # Record the runner-up so an ambiguous document can be flagged.
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.15 and ranked[1][1] > 0.3:
        extraction.warnings.append(
            f"{filename} could also be a "
            f"{DOCUMENT_LABELS.get(ranked[1][0], ranked[1][0])}. "
            "Check the extracted figures carefully."
        )
    return extraction


def _parse_as_text(raw: bytes, filename: str) -> Optional[Extraction]:
    """Try the text-format statements before assuming a plain file is tabular.

    Returns ``None`` when nothing recognises it, so the caller can fall back to
    the spreadsheet path.
    """
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    scores = {name: scorer(text) for name, scorer, _ in _PDF_PARSERS}
    best = max(scores, key=lambda key: scores[key])
    if scores[best] < 0.3:
        return None

    parser = dict((name, fn) for name, _, fn in _PDF_PARSERS)[best]
    extraction = parser(text, filename, None)
    extraction.warnings.append(
        f"{filename} was read as a {DOCUMENT_LABELS.get(best, best)} rather "
        "than a spreadsheet. The PDF version of the same statement usually "
        "extracts more reliably — if figures are missing, download that "
        "instead."
    )
    return extraction


def _parse_zip(
    raw: bytes,
    filename: str,
    *,
    pan: str = "",
    date_of_birth: Optional[date] = None,
    forced_type: str = "",
) -> Extraction:
    """Open a zip and parse what is inside it.

    TRACES delivers the text Form 26AS this way, protected with the date of
    birth as DDMMYYYY. Unhandled, the upload was simply rejected as a file type
    this system does not read.
    """
    import io
    import zipfile

    out = Extraction(document_type="unknown", source_filename=filename)
    passwords = [p.encode() for p in candidate_passwords(pan, date_of_birth)]

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        out.warnings.append(f"{filename} could not be opened as a zip: {exc}")
        return out

    members = [m for m in archive.namelist() if not m.endswith("/")]
    if not members:
        out.warnings.append(f"{filename} is an empty archive.")
        return out

    for member in members:
        content = None
        for password in [None] + passwords:
            try:
                content = archive.read(member, pwd=password)
                break
            except Exception:  # noqa: BLE001 - wrong password, or not encrypted
                continue
        if content is None:
            out.warnings.append(
                f"{member} inside {filename} is password protected and none of "
                "the usual passwords opened it. TRACES uses your date of birth "
                "as DDMMYYYY — fill it in on the Income page and re-upload, or "
                "extract the file yourself and upload what is inside."
            )
            out.needs_password = True
            continue

        inner = parse_document(
            content, member, pan=pan, date_of_birth=date_of_birth,
            forced_type=forced_type,
        )
        if inner.document_type != "unknown":
            return inner
        out.warnings.extend(inner.warnings)

    return out


def _parse_tabular(raw: bytes, filename: str, forced_type: str = "") -> Extraction:
    """Decide between an Indian broker export and a US stock-plan file.

    The filename is a strong hint but not a reliable one — people rename these.
    So whichever parser actually finds rows wins, and the US one is tried first
    only when the name suggests it.
    """
    if forced_type == "us_equity":
        return us_equity.parse_tabular(raw, filename)
    if forced_type == "broker_pnl":
        return broker.parse_tabular(raw, filename)
    if forced_type == "trading_pnl":
        return trading_pnl.parse_tabular(raw, filename)

    prefer_us = us_equity.score_filename(filename) > 0

    def found_anything(extraction) -> bool:
        return bool(
            extraction.rsu_vests or extraction.espp_purchases
            or extraction.dividends or extraction.foreign_sales
        )

    if prefer_us:
        result = us_equity.parse_tabular(raw, filename)
        if found_anything(result):
            return result

    # An Indian broker's annual tax P&L is one workbook holding both: a
    # capital-gains sheet for delivery, and F&O, intraday, currency and
    # commodity sheets that are business income. Both parsers run, and the
    # results are combined rather than one winning — reading only the sheet
    # that happened to be recognised first is how a whole segment goes missing.
    indian = broker.parse_tabular(raw, filename)
    trading = trading_pnl.parse_tabular(raw, filename)

    if trading.trading_segments:
        indian.trading_segments = trading.trading_segments
        indian.warnings.extend(trading.warnings)
        if not indian.capital_gains:
            indian.document_type = trading_pnl.DOC_TYPE
        indian.confidence = max(indian.confidence, trading.confidence)

    if indian.capital_gains or indian.trading_segments:
        return indian

    if not prefer_us:
        result = us_equity.parse_tabular(raw, filename)
        if found_anything(result):
            return result

    return indian


def parse_many(
    files: Sequence[Tuple[str, bytes]],
    *,
    pan: str = "",
    date_of_birth: Optional[date] = None,
) -> List[Extraction]:
    """Parse a whole batch, tolerating failures on individual files."""
    known_pan = pan

    def attempt(name: str, raw: bytes) -> Extraction:
        try:
            return parse_document(
                raw, name, pan=known_pan, date_of_birth=date_of_birth
            )
        except PasswordRequired as exc:
            # ``parse_document`` normally catches this itself, but it can also
            # come from a table pass or a parser further in. Either way the
            # file is worth retrying once the PAN is known, so the marker has
            # to survive.
            locked = Extraction(document_type="unknown", source_filename=name)
            locked.warnings.append(str(exc))
            locked.needs_password = True
            return locked
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            failed = Extraction(document_type="unknown", source_filename=name)
            failed.warnings.append(f"{name} could not be read: {exc}")
            return failed

    results: List[Extraction] = []
    for name, raw in files:
        extraction = attempt(name, raw)
        results.append(extraction)
        if not known_pan:
            for fact in extraction.facts:
                if fact.path == "taxpayer.pan":
                    known_pan = str(fact.value)
                    break

    # Second pass. The AIS and Form 26AS are encrypted with the PAN, and the
    # PAN is usually only discovered part way through the batch — from a Form
    # 16 further down the list. Anything that failed for want of a password
    # before that discovery gets one more go with it. Without this the order
    # the files happened to be uploaded in decided whether they parsed.
    if known_pan and known_pan != pan:
        for index, (name, raw) in enumerate(files):
            if not results[index].needs_password:
                continue
            retried = attempt(name, raw)
            if not retried.needs_password:
                results[index] = retried

    return results
