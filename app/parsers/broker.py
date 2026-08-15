"""Broker and mutual-fund capital-gains statements.

Zerodha Console, Groww, Upstox, Kuvera, CAMS and KFintech all export a
tradewise or summary capital-gains file, and every one of them uses different
column headings for the same six numbers. Rather than maintain a parser per
broker, this module maps whatever headings it finds onto a canonical set and
classifies each row by asset type and holding period.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..money import D
from .base import Extraction, parse_date

DOC_TYPE = "broker_pnl"

# Canonical field -> the headings brokers actually use, lower-cased.
_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "symbol": (
        "symbol", "scrip name", "security name", "stock name", "instrument",
        "isin/scheme name", "scheme name", "fund name", "name of the security",
        "particulars", "script name",
    ),
    "isin": ("isin", "isin code"),
    "quantity": ("quantity", "qty", "units", "no. of units", "quantity sold"),
    "sale_date": (
        "sell date", "sale date", "date of sale", "exit date", "sell_date",
        "date of transfer", "redemption date", "date of redemption",
    ),
    "purchase_date": (
        "buy date", "purchase date", "date of purchase", "entry date",
        "buy_date", "date of acquisition", "allotment date",
    ),
    "sale_consideration": (
        "sell value", "sale value", "sell amount", "sale consideration",
        "amount received", "redemption amount", "sell_value", "sale amount",
        "total sale value", "sell price total",
    ),
    "cost_of_acquisition": (
        "buy value", "purchase value", "buy amount", "cost of acquisition",
        "amount invested", "purchase cost", "buy_value", "purchase amount",
        "total buy value", "acquisition cost",
    ),
    "fmv_31jan2018": (
        "fair market value", "fmv", "grandfathered value", "fmv as on 31/01/2018",
        "31-jan-2018", "nav as on 31/01/2018",
    ),
    "gain": (
        "realized p&l", "realised p&l", "profit", "gain", "p&l", "net gain",
        "short term gain", "long term gain", "capital gain", "realized gain",
    ),
    "term": ("term", "type", "gain type", "holding period", "category"),
    "asset_type": ("asset type", "instrument type", "segment", "scheme type"),
}


def score_filename(filename: str) -> float:
    lowered = filename.lower()
    tokens = ("pnl", "p&l", "capital", "gain", "tradewise", "console",
              "zerodha", "groww", "upstox", "cams", "kfintech", "kuvera")
    return 0.85 if any(token in lowered for token in tokens) else 0.0


def score(text: str) -> float:
    lowered = text.lower()
    hits = sum(
        1
        for token in ("capital gain", "realized p&l", "realised p&l",
                      "buy value", "sell value", "grandfathered",
                      "short term", "long term", "tradewise")
        if token in lowered
    )
    return min(hits / 4.0, 1.0)


def parse_tabular(raw: bytes, filename: str) -> Extraction:
    """Read a CSV or Excel capital-gains statement."""
    import pandas as pd

    out = Extraction(document_type=DOC_TYPE, source_filename=filename)
    frames = _load_frames(raw, filename, out)
    if not frames:
        return out

    for label, frame in frames:
        header_row = _find_header_row(frame)
        if header_row is None:
            continue
        frame = _reheader(frame, header_row)
        mapping = _map_columns(frame.columns)
        if "sale_consideration" not in mapping and "gain" not in mapping:
            continue
        _rows_to_gains(frame, mapping, out, filename, label)

    if out.capital_gains:
        out.confidence = 0.85
        total = sum((D(item["sale_consideration"]) for item in out.capital_gains), D(0))
        out.add(
            "crosscheck.broker_sale_consideration",
            "Total sale consideration in this statement",
            total, confidence=0.9,
        )
    else:
        out.warnings.append(
            "No capital-gains rows were recognised in this file. Check that it "
            "is the tradewise or capital-gains export rather than a holdings or "
            "ledger report."
        )
    return out


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _load_frames(raw: bytes, filename: str, out: Extraction) -> List[Tuple[str, Any]]:
    import pandas as pd

    lowered = filename.lower()
    try:
        if lowered.endswith(".csv") or lowered.endswith(".txt"):
            # Broker CSVs open with a title block of one-cell lines before the
            # real header. Left to itself pandas takes the column count from
            # that first line and then discards every actual data row, so the
            # width is measured up front and forced.
            import csv as csv_module

            text = raw.decode("utf-8-sig", errors="replace")
            width = max(
                (len(row) for row in csv_module.reader(io.StringIO(text))),
                default=1,
            )
            return [("csv", pd.read_csv(
                io.StringIO(text), header=None, dtype=str,
                names=list(range(width)), on_bad_lines="skip",
                skip_blank_lines=True, engine="python",
            ))]
        engine = "xlrd" if lowered.endswith(".xls") else "openpyxl"
        book = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None,
                             dtype=str, engine=engine)
        return list(book.items())
    except Exception as exc:  # noqa: BLE001 - report, do not crash the upload
        out.warnings.append(f"This spreadsheet could not be opened: {exc}")
        return []


def _find_header_row(frame) -> Optional[int]:
    """Broker exports bury the header several rows down under a title block."""
    limit = min(len(frame), 30)
    best, best_hits = None, 0
    for index in range(limit):
        cells = [str(cell).strip().lower() for cell in frame.iloc[index].tolist()]
        hits = sum(
            1
            for cell in cells
            for aliases in _COLUMN_ALIASES.values()
            if cell in aliases
        )
        if hits > best_hits:
            best, best_hits = index, hits
    return best if best_hits >= 2 else None


def _reheader(frame, header_row: int):
    header = [str(cell).strip() for cell in frame.iloc[header_row].tolist()]
    body = frame.iloc[header_row + 1:].copy()
    body.columns = header
    return body


def _map_columns(columns) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for column in columns:
        key = str(column).strip().lower()
        for canonical, aliases in _COLUMN_ALIASES.items():
            if canonical in mapping:
                continue
            if key in aliases or any(alias in key for alias in aliases):
                mapping[canonical] = column
                break
    return mapping


# --------------------------------------------------------------------------
# Row classification
# --------------------------------------------------------------------------

_EQUITY_HINTS = ("equity", "share", "stock", "eq", "listed")
_DEBT_HINTS = ("debt", "liquid", "gilt", "bond", "money market", "arbitrage")
_MF_HINTS = ("mutual fund", "mf", "scheme", "fund", "nav")
_CUTOFF_23_JUL_2024 = date(2024, 7, 23)


def _rows_to_gains(frame, mapping, out: Extraction, filename: str, sheet: str) -> None:
    for _, row in frame.iterrows():
        def cell(key: str) -> str:
            column = mapping.get(key)
            if column is None:
                return ""
            value = row.get(column)
            return "" if value is None else str(value).strip()

        sale_value = D(cell("sale_consideration"))
        cost = D(cell("cost_of_acquisition"))
        gain_column = D(cell("gain"))

        if sale_value == 0 and cost == 0 and gain_column == 0:
            continue

        sale_date = parse_date(cell("sale_date"))
        purchase_date = parse_date(cell("purchase_date"))
        symbol = cell("symbol") or cell("isin") or "Security"
        if symbol.lower() in ("nan", "total", "grand total", ""):
            continue

        if sale_value == 0 and gain_column and cost:
            sale_value = cost + gain_column
        if cost == 0 and gain_column and sale_value:
            cost = sale_value - gain_column

        term_hint = (cell("term") + " " + cell("asset_type") + " " + sheet).lower()
        category = _classify(
            symbol, term_hint, purchase_date, sale_date, cell("asset_type")
        )

        fmv = D(cell("fmv_31jan2018")) or None
        quantity = cell("quantity")
        description = f"{symbol}" + (f" ({quantity} units)" if quantity else "")

        out.capital_gains.append({
            "category": category,
            "description": description[:90],
            "sale_date": sale_date,
            "purchase_date": purchase_date,
            "sale_consideration": sale_value,
            "cost_of_acquisition": cost,
            "fmv_31jan2018": fmv,
            "source_document": filename,
        })


def _classify(
    symbol: str,
    term_hint: str,
    purchase_date: Optional[date],
    sale_date: Optional[date],
    asset_type: str,
) -> str:
    """Decide which capital-gains bucket a row belongs to.

    Holding period governs the term when both dates are present; otherwise we
    trust whatever the broker labelled the row. Equity and equity mutual funds
    turn long term at 12 months, everything else at 24.
    """
    blob = f"{symbol} {asset_type} {term_hint}".lower()
    is_debt_fund = any(hint in blob for hint in _DEBT_HINTS)
    is_equity = any(hint in blob for hint in _EQUITY_HINTS) or not any(
        hint in blob for hint in _MF_HINTS + _DEBT_HINTS
    )

    # Debt funds bought on or after 1 April 2023 are always short term and
    # always taxed at slab rates — section 50AA.
    if is_debt_fund:
        return "stcg_debt_mf"

    long_term = None
    if purchase_date and sale_date:
        days = (sale_date - purchase_date).days
        long_term = days > (365 if is_equity else 730)
    elif "long" in term_hint:
        long_term = True
    elif "short" in term_hint:
        long_term = False

    if long_term is None:
        long_term = False

    if is_equity:
        return "ltcg_112a" if long_term else "stcg_111a"
    return "ltcg_112_other" if long_term else "stcg_slab"
