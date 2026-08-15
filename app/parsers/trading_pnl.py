"""Trading profit-and-loss statements: F&O, intraday, currency and commodity.

A broker's annual tax P&L is one workbook with a sheet per segment, and the
segments are three different heads of income. ``broker.py`` already reads the
capital-gains sheet — delivery equity. This module reads the rest, the part that
is *business* income:

* intraday equity, which is speculative under section 43(5)
* equity and index F&O, currency F&O, commodity F&O, which are not

The single most useful thing it does is recompute **turnover**. Brokers report
it inconsistently, and many still use the pre-2022 method that adds the full
sale consideration of options — a figure that can be twenty times the correct
one and manufactures a tax audit out of nothing. Where the file gives trade-wise
rows, turnover is recomputed here from the absolute value of each row's result,
which is what the ICAI Guidance Note (Revised 2023) asks for. Where the file
only gives a segment summary, the broker's own figure is carried through and
flagged, because there is nothing to recompute from.

A caveat stated plainly: this parser has been written against the column names
these exports are documented and reported to use, not against a verified sample
of every broker's file. Anything it cannot map is reported rather than dropped,
and the review screen shows every figure before it reaches the return.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..money import D
from .base import Extraction, parse_date

DOC_TYPE = "trading_pnl"

# Canonical field -> headings these exports use, lower-cased.
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "symbol": (
        "symbol", "scrip", "scrip name", "scrip code", "stock code", "contract",
        "instrument", "security", "underlying", "particulars", "script name",
        "stock name", "series",
    ),
    "expiry": ("expiry", "expiry date", "contract expiry"),
    "buy_date": ("buy date", "purchase date", "entry date", "date of buy"),
    "sell_date": ("sell date", "sale date", "exit date", "date of sell"),
    "trade_date": ("trade date", "date", "transaction date", "settlement date"),
    "quantity": ("quantity", "qty", "lots", "net qty", "traded quantity"),
    "buy_value": (
        "buy value", "buy amount", "purchase value", "total buy value",
        "buy consideration", "gross buy value",
    ),
    "sell_value": (
        "sell value", "sell amount", "sale value", "total sell value",
        "sale consideration", "gross sell value",
    ),
    "profit": (
        "profit/loss", "profit / loss", "p&l", "pnl", "net profit", "realised p&l",
        "realized p&l", "net p&l", "profit or loss", "gain/loss", "net profit/loss",
        "profit", "loss",
    ),
    "turnover": ("turnover", "total turnover", "gross turnover"),
    # -- charges ---------------------------------------------------------
    "brokerage": ("brokerage", "brokerage amount", "total brokerage"),
    "stt": ("stt", "securities transaction tax", "ctt",
            "commodity transaction tax", "stt/ctt"),
    "exchange_charges": (
        "exchange transaction charges", "transaction charges", "exchange charges",
        "turnover charges", "exchange turnover charges",
    ),
    "sebi_fees": ("sebi turnover fees", "sebi charges", "sebi fees"),
    "stamp_duty": ("stamp duty", "stamp charges", "stamp"),
    "gst": ("gst", "service tax", "goods and services tax", "igst", "cgst"),
    "dp_charges": ("dp charges", "depository charges", "demat charges"),
    "other_charges": ("other charges", "misc charges", "clearing charges"),
    "segment": ("segment", "product", "product type", "exchange", "category",
                "type"),
}

# Order matters twice over.
#
# "Currency futures" contains "futures", so the narrower segments are tested
# before the general derivative one.
#
# And F&O is tested before intraday deliberately. A derivative squared off the
# same day is still **not** speculative: clause (d) of the proviso to section
# 43(5) turns on the contract being an eligible derivative transaction on a
# recognised exchange, not on whether delivery was taken. Only *equity*
# intraday is speculative, so a sheet named "F&O Intraday" must land in
# equity_fo, and this ordering is what puts it there.
#
# Every hint here has to be a word that cannot appear by accident. Bare "fut",
# "opt" and "index" were all tried and all misfired — "index" matched the repr
# of a pandas Index and swallowed the whole intraday sheet.
_SEGMENT_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("commodity_fo", ("commodity", "mcx", "ncdex", "bullion", "comdex", "ctt")),
    ("currency_fo", ("currency", "cds", "usdinr", "eurinr", "gbpinr", "jpyinr",
                     "forex")),
    ("equity_fo", ("f&o", "fno", "f & o", "futures", "options", "derivative",
                   "nfo", "bfo", "index option", "index future")),
    ("equity_intraday", ("intraday", "intra day", "intra-day", "day trading",
                         "speculative", "speculation", "btst", "square off",
                         "squareoff")),
)

_DELIVERY_HINTS = ("delivery", "cash", "capital gain", "short term", "long term",
                   "equity delivery", "cnc", "holding")


def score_filename(filename: str) -> float:
    lowered = filename.lower()
    tokens = ("pnl", "p&l", "profit", "tax", "fno", "f&o", "derivative",
              "intraday", "trading", "icici", "idirect", "trade")
    return 0.85 if any(token in lowered for token in tokens) else 0.0


def score(text: str) -> float:
    lowered = text.lower()
    hits = sum(
        1
        for token in ("f&o", "futures", "options", "intraday", "turnover",
                      "brokerage", "securities transaction tax", "profit/loss",
                      "derivative", "speculative")
        if token in lowered
    )
    return min(hits / 4.0, 1.0)


# --------------------------------------------------------------------------


@dataclass
class _Accumulator:
    segment: str
    profits: List[Decimal]
    charges: Dict[str, Decimal]
    reported_turnover: Decimal = D(0)
    rows: int = 0
    summary_only: bool = False


def parse_tabular(raw: bytes, filename: str) -> Extraction:
    out = Extraction(document_type=DOC_TYPE, source_filename=filename)
    frames = _load_frames(raw, filename, out)
    if not frames:
        return out

    accumulators: Dict[str, _Accumulator] = {}
    skipped: List[str] = []

    for sheet_name, frame in frames:
        header_row = _find_header_row(frame)
        if header_row is None:
            continue
        body = _reheader(frame, header_row)
        mapping = _map_columns(body.columns)

        sheet_segment = _segment_of(str(sheet_name), body.columns)
        if sheet_segment is None and _looks_like_delivery(str(sheet_name), body.columns):
            # broker.py owns this sheet — delivery equity is capital gains.
            continue

        _read_rows(body, mapping, sheet_segment, str(sheet_name),
                   accumulators, skipped)

    for accumulator in accumulators.values():
        out.trading_segments.append(_to_segment(accumulator, filename))

    if out.trading_segments:
        out.confidence = 0.8
        _summarise(out, accumulators)
    else:
        out.warnings.append(
            "No F&O, intraday, currency or commodity rows were recognised in "
            "this file. If it is the capital-gains statement, that is expected "
            "— delivery equity is read separately. If it is the derivatives "
            "P&L, the column headings were not ones this parser knows; send "
            "the file and it can be taught them."
        )
    if skipped:
        out.warnings.append(
            f"{len(skipped)} row(s) carried figures but could not be placed in "
            "a segment: " + "; ".join(skipped[:3])
            + (" ..." if len(skipped) > 3 else "")
            + ". They have been left out rather than guessed at."
        )
    return out


# --------------------------------------------------------------------------
# Loading and shape detection
# --------------------------------------------------------------------------


def _load_frames(raw: bytes, filename: str, out: Extraction) -> List[Tuple[str, Any]]:
    import pandas as pd

    lowered = filename.lower()
    try:
        if lowered.endswith((".csv", ".txt")):
            import csv as csv_module

            text = raw.decode("utf-8-sig", errors="replace")
            width = max(
                (len(row) for row in csv_module.reader(io.StringIO(text))),
                default=1,
            )
            return [(filename, pd.read_csv(
                io.StringIO(text), header=None, dtype=str,
                names=list(range(width)), on_bad_lines="skip",
                skip_blank_lines=True, engine="python",
            ))]
        engine = "xlrd" if lowered.endswith(".xls") else "openpyxl"
        book = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None,
                             dtype=str, engine=engine)
        return list(book.items())
    except Exception as exc:  # noqa: BLE001
        out.warnings.append(f"This file could not be opened: {exc}")
        return []


def _find_header_row(frame) -> Optional[int]:
    limit = min(len(frame), 30)
    best, best_hits = None, 0
    for index in range(limit):
        cells = [str(cell).strip().lower() for cell in frame.iloc[index].tolist()]
        hits = sum(
            1 for cell in cells
            for aliases in _ALIASES.values() if cell in aliases
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
    """Exact heading match first, then substring — order matters.

    "Profit" is a substring of "Profit/Loss", and "STT" of "STT/CTT". Letting a
    loose match win would put the wrong column against the wrong field.
    """
    mapping: Dict[str, str] = {}
    normalised = [(column, str(column).strip().lower()) for column in columns]

    for canonical, aliases in _ALIASES.items():
        for column, key in normalised:
            if key in aliases and canonical not in mapping:
                mapping[canonical] = column
                break
    for canonical, aliases in _ALIASES.items():
        if canonical in mapping:
            continue
        for column, key in normalised:
            if any(alias in key for alias in aliases):
                mapping[canonical] = column
                break
    return mapping


def _blob(parts) -> str:
    """Flatten sheet names and column collections into one searchable string.

    A pandas ``Index`` is neither a list nor a tuple, so testing for those two
    let ``str()`` fall through to the repr — ``Index([...], dtype='object')`` —
    which injected the literal word "index" into every blob and mis-filed a
    whole sheet. Anything iterable that is not a string is expanded.
    """
    pieces: List[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            pieces.append(part)
        elif hasattr(part, "__iter__"):
            pieces.extend(str(item) for item in part)
        else:
            pieces.append(str(part))
    return " ".join(pieces).lower()


def _segment_of(*blobs) -> Optional[str]:
    """Which statutory segment a sheet, a row or a column set belongs to."""
    text = _blob(blobs)
    for segment, hints in _SEGMENT_HINTS:
        if any(hint in text for hint in hints):
            return segment
    return None


def _looks_like_delivery(*blobs) -> bool:
    return any(hint in _blob(blobs) for hint in _DELIVERY_HINTS)


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

_CHARGE_FIELDS = (
    "brokerage", "stt", "exchange_charges", "sebi_fees", "stamp_duty",
    "gst", "dp_charges", "other_charges",
)


def _cell(row, mapping: Dict[str, str], key: str) -> str:
    column = mapping.get(key)
    if column is None:
        return ""
    value = row.get(column)
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "-", "--") else text


def _money(row, mapping: Dict[str, str], key: str) -> Decimal:
    text = _cell(row, mapping, key)
    # Brokers write a loss as (1,234) as often as -1,234; D() handles both.
    return D(text.replace("₹", "").replace("Rs.", "").replace("Rs", ""))


def _read_rows(
    frame, mapping, sheet_segment: Optional[str], sheet_name: str,
    accumulators: Dict[str, _Accumulator], skipped: List[str],
) -> None:
    for _, row in frame.iterrows():
        symbol = _cell(row, mapping, "symbol")
        if symbol.lower() in ("total", "totals", "grand total", "subtotal"):
            continue

        profit = _money(row, mapping, "profit")
        buy_value = _money(row, mapping, "buy_value")
        sell_value = _money(row, mapping, "sell_value")
        charges = {
            field: _money(row, mapping, field) for field in _CHARGE_FIELDS
        }
        if not profit and (buy_value or sell_value):
            profit = sell_value - buy_value

        has_anything = bool(
            profit or buy_value or sell_value or any(charges.values())
        )
        if not has_anything:
            continue

        # A per-row segment column beats the sheet name — one sheet often
        # carries several segments in an "Exchange" or "Product" column.
        segment = (
            _segment_of(_cell(row, mapping, "segment"), symbol)
            or sheet_segment
        )
        if segment is None:
            if _looks_like_delivery(_cell(row, mapping, "segment"), sheet_name):
                continue
            skipped.append(f"{symbol or 'a row'} in sheet {sheet_name!r}")
            continue

        accumulator = accumulators.get(segment)
        if accumulator is None:
            accumulator = _Accumulator(
                segment=segment, profits=[],
                charges={field: D(0) for field in _CHARGE_FIELDS},
            )
            accumulators[segment] = accumulator

        accumulator.profits.append(profit)
        accumulator.rows += 1
        for field, amount in charges.items():
            accumulator.charges[field] += abs(amount)
        accumulator.reported_turnover += _money(row, mapping, "turnover")


def _to_segment(accumulator: _Accumulator, filename: str) -> Dict[str, Any]:
    from ..tax.trading import turnover_from_trades

    gross = sum(accumulator.profits, D(0))
    computed = turnover_from_trades(accumulator.profits)

    # Where the file is trade-wise, the recomputed figure is the right one.
    # Where it is a single summary line there is nothing to recompute from, so
    # the broker's own number stands — flagged in the warnings.
    summary_only = accumulator.rows <= 1 and accumulator.reported_turnover > 0
    turnover = accumulator.reported_turnover if summary_only else computed
    accumulator.summary_only = summary_only

    charges = accumulator.charges
    return {
        "segment": accumulator.segment,
        "gross_profit": gross,
        "turnover": turnover,
        "brokerage": charges["brokerage"],
        "exchange_transaction_charges": charges["exchange_charges"],
        "securities_transaction_tax": charges["stt"],
        "sebi_turnover_fees": charges["sebi_fees"],
        "stamp_duty": charges["stamp_duty"],
        "gst": charges["gst"],
        "depository_charges": charges["dp_charges"],
        "other_expenses": charges["other_charges"],
        "source_document": filename,
    }


def _summarise(out: Extraction, accumulators: Dict[str, _Accumulator]) -> None:
    from ..tax.trading import SEGMENT_LABELS

    for entry in out.trading_segments:
        label = SEGMENT_LABELS.get(entry["segment"], entry["segment"])
        out.warnings.append(
            f"{label}: profit {entry['gross_profit']:,.2f}, turnover "
            f"{entry['turnover']:,.2f}, charges "
            f"{sum(D(v) for k, v in entry.items() if k not in ('segment', 'gross_profit', 'turnover', 'source_document')):,.2f}."
        )

    recomputed = [a for a in accumulators.values() if not a.summary_only]
    if recomputed:
        out.warnings.append(
            "Turnover has been recomputed from the trade rows as the absolute "
            "value of each result, added up — the method in the ICAI Guidance "
            "Note (Revised 2023). If your broker's own summary shows a much "
            "larger figure it is using the pre-2022 method, which adds the full "
            "sale consideration of options. That inflated number is what pushes "
            "people into a section 44AB audit they do not need."
        )
    summarised = [a for a in accumulators.values() if a.summary_only]
    if summarised:
        from ..tax.trading import SEGMENT_LABELS as labels

        out.warnings.append(
            "These segments gave a summary line rather than trade rows, so the "
            "broker's own turnover figure has been carried through unchecked: "
            + ", ".join(labels.get(a.segment, a.segment) for a in summarised)
            + ". Confirm which method it used before relying on the audit "
            "determination."
        )

    speculative = [e for e in out.trading_segments
                   if e["segment"] == "equity_intraday"]
    if speculative:
        out.warnings.append(
            "Intraday equity has been kept in its own segment. Section 73 "
            "ring-fences a speculation loss — it meets speculative income and "
            "nothing else, and it lapses after four years rather than eight. "
            "Broker summaries routinely add it to the F&O line, which either "
            "wastes the loss or claims a set-off the statute refuses."
        )
