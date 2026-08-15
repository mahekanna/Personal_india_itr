"""US stock-plan and brokerage statements.

E*TRADE, Fidelity NetBenefits, Charles Schwab and Morgan Stanley StockPlan
Connect all export the same handful of facts under different headings, and the
same account produces several different files:

* a **vesting** or "releases" report — the tranche, the shares, the fair market
  value on the vesting date, and what was sold to cover withholding
* a **1099-DIV** or dividend activity report, which is where reinvestment shows
  up as a purchase on the payment date
* a **1099-B** or gain-and-loss report listing disposals

Rather than a parser per broker, each file is sniffed for which of those three
it is, and the headings are mapped onto one canonical set. Everything is left
in the source currency: the Rule 115 conversion happens later, against the
month the transaction fell in, and doing it here would lose that.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..money import D
from .base import Extraction, parse_date

DOC_TYPE = "us_equity"

_ALIASES: Dict[str, Tuple[str, ...]] = {
    "symbol": (
        "symbol", "ticker", "security", "stock symbol", "security symbol",
        "company", "plan", "security name", "investment name", "description",
    ),
    "vest_date": (
        "vest date", "release date", "date of vest", "vest dt", "released on",
        "settlement date", "distribution date", "vesting date",
    ),
    "grant_date": ("grant date", "award date", "date granted", "grant dt"),
    "grant_id": ("grant number", "award id", "grant id", "award number", "grant"),
    "shares_vested": (
        "shares vested", "quantity vested", "vested quantity", "shares released",
        "released quantity", "qty vested", "shares", "quantity",
        "number of shares",
    ),
    "fmv": (
        "fair market value", "fmv", "market value per share", "vest price",
        "release price", "price per share", "fmv per share", "market price",
        "vest fmv",
    ),
    "shares_sold_to_cover": (
        "shares sold", "shares withheld", "sell to cover", "shares sold to cover",
        "quantity sold for taxes", "shares traded for taxes", "withheld for taxes",
        "shares withheld for taxes",
    ),
    "sale_price": (
        "sale price", "sell price", "price", "proceeds per share",
        "sale price per share", "average price",
    ),
    "pay_date": (
        "pay date", "payment date", "date paid", "dividend date",
        "transaction date", "activity date", "date",
    ),
    "dividend_amount": (
        "dividend", "dividend amount", "gross dividend", "amount",
        "ordinary dividends", "total dividend", "cash dividend", "gross amount",
    ),
    "tax_withheld": (
        "federal income tax withheld", "tax withheld", "withholding",
        "foreign tax paid", "nra withholding", "us tax withheld",
        "federal tax withheld", "backup withholding",
    ),
    "reinvest_shares": (
        "shares purchased", "reinvested shares", "shares acquired",
        "units purchased", "quantity purchased", "shares reinvested",
    ),
    "reinvest_price": (
        "reinvestment price", "purchase price", "price per share reinvested",
        "reinvest price",
    ),
    "sale_date": (
        "sale date", "date sold", "sold date", "trade date", "date of sale",
        "disposition date",
    ),
    "acquired_date": (
        "date acquired", "acquisition date", "acquired", "purchase date",
    ),
    "quantity_sold": (
        "quantity sold", "shares sold", "quantity", "shares", "units sold",
    ),
    "proceeds": (
        "proceeds", "gross proceeds", "net proceeds", "total proceeds",
        "sale amount", "amount",
    ),
    "cost_basis": (
        "cost basis", "basis", "adjusted cost basis", "total cost",
        "cost or other basis",
    ),
    "fees": ("commission", "fees", "commission and fees", "transaction fee"),
    "action": ("action", "type", "transaction type", "activity", "record type"),
    # -- ESPP ------------------------------------------------------------
    "purchase_date_espp": (
        "purchase date", "date of purchase", "espp purchase date",
        "exercise date", "date acquired",
    ),
    "offering_start": (
        "offering date", "grant date", "offering start date", "subscription date",
        "offering period begin", "grant/offering date", "period start",
    ),
    "shares_purchased_espp": (
        "shares purchased", "qty. purchased", "quantity purchased",
        "shares acquired", "no. of shares purchased", "purchased quantity",
    ),
    "purchase_price": (
        "purchase price", "price paid", "purchase price per share",
        "espp purchase price", "your price", "discounted price",
        "purchase price per share (usd)",
    ),
    "fmv_at_purchase": (
        "market value per share on purchase date", "fmv on purchase date",
        "market value at purchase", "purchase date market value",
        "fair market value at purchase", "closing price on purchase date",
        "market value per share",
    ),
    "fmv_at_offering": (
        "grant date market value", "offering date market value",
        "market value at offering", "fmv on grant date",
        "grant date fair market value", "offering price",
    ),
    "contributions": (
        "contributions", "total contributions", "payroll deductions",
        "amount contributed", "employee contribution",
    ),
    "refund": ("refund", "refunded", "cash returned", "residual cash"),
}

# Words that identify what kind of file this is.
_VEST_HINTS = ("vest", "release", "restricted stock", "rsu", "stock plan",
               "benefit history", "award")
_DIVIDEND_HINTS = ("dividend", "1099-div", "reinvest", "drip", "distribution")
_SALE_HINTS = ("1099-b", "gain", "loss", "sold", "proceeds", "disposition",
               "realized")
_ESPP_HINTS = ("espp", "stock purchase", "employee stock purchase", "purchase",
               "offering", "subscription", "3922")


def score_filename(filename: str) -> float:
    lowered = filename.lower()
    tokens = ("etrade", "e-trade", "fidelity", "schwab", "morganstanley",
              "morgan_stanley", "stockplan", "netbenefits", "rsu", "espp",
              "1099", "benefit_history", "releases", "shareworks", "equityedge")
    return 0.9 if any(token in lowered for token in tokens) else 0.0


def score(text: str) -> float:
    lowered = text.lower()
    hits = sum(
        1
        for token in ("restricted stock unit", "rsu", "vest", "fair market value",
                      "1099-div", "1099-b", "stock plan", "espp",
                      "reinvest", "grant number")
        if token in lowered
    )
    return min(hits / 3.0, 1.0)


# --------------------------------------------------------------------------


def parse_tabular(raw: bytes, filename: str) -> Extraction:
    import pandas as pd

    out = Extraction(document_type=DOC_TYPE, source_filename=filename)
    frames = _load_frames(raw, filename, out)
    if not frames:
        return out

    for sheet_name, frame in frames:
        header_row = _find_header_row(frame)
        if header_row is None:
            continue
        body = _reheader(frame, header_row)
        mapping = _map_columns(body.columns)
        kind = _classify_sheet(sheet_name, body.columns, mapping)

        if kind == "espp":
            _read_espp(body, mapping, out, filename)
        elif kind == "vest":
            _read_vests(body, mapping, out, filename)
        elif kind == "dividend":
            _read_dividends(body, mapping, out, filename)
        elif kind == "sale":
            _read_sales(body, mapping, out, filename)

    total = (
        len(out.rsu_vests) + len(out.dividends) + len(out.foreign_sales)
        + len(out.espp_purchases)
    )
    if total:
        out.confidence = 0.85
        _summarise(out)
    else:
        out.warnings.append(
            "No vesting, dividend or sale rows were recognised in this file. "
            "From E*TRADE use 'Stock Plan → My Account → Gains & Losses' and "
            "'Benefit History'; from Fidelity, the 'Realized Gain/Loss' and "
            "'Dividends & Interest' exports."
        )
    return out


def _summarise(out: Extraction) -> None:
    if out.rsu_vests:
        shares = sum((D(v.get("shares_vested", 0)) for v in out.rsu_vests), D(0))
        out.warnings.append(
            f"{len(out.rsu_vests)} vesting tranche(s) totalling {shares} shares "
            "were read. The fair market value on each vesting date is salary "
            "under section 17(2)(vi) — check whether your Form 16 already "
            "includes it before letting it be added again."
        )
    reinvested = [d for d in out.dividends if d.get("is_reinvested")]
    if reinvested:
        out.warnings.append(
            f"{len(reinvested)} of {len(out.dividends)} dividend(s) were "
            "reinvested. Each reinvestment is taxable income now and creates a "
            "new lot with its own 24-month holding clock."
        )
    if out.espp_purchases:
        discount = sum(
            (D(p.get("fmv_per_share_fx", 0)) - D(p.get("price_paid_per_share_fx", 0)))
            * D(p.get("shares_purchased", 0))
            for p in out.espp_purchases
        )
        out.warnings.append(
            f"{len(out.espp_purchases)} ESPP purchase(s) were read, with a "
            f"discount of about {out.espp_purchases[0].get('currency', 'USD')} "
            f"{discount:,.2f} in total. That discount is salary under section "
            "17(2)(vi). On sale the cost basis is the fair market value, not "
            "the price you paid — this file will show the price you paid."
        )
    if out.foreign_sales:
        out.warnings.append(
            f"{len(out.foreign_sales)} disposal(s) were read. Foreign shares "
            "turn long term only after 24 months, because no securities "
            "transaction tax is paid on a US trade."
        )


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
    """Map headings onto canonical keys, preferring an exact match.

    Order matters here. "Shares" is an alias of several keys, so an exact hit on
    a specific heading has to win over a substring hit on a generic one.
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


def _classify_sheet(sheet_name: str, columns, mapping: Dict[str, str]) -> str:
    blob = (str(sheet_name) + " " + " ".join(str(c) for c in columns)).lower()

    # ESPP is checked first, because a purchase report has both a date and a
    # quantity and would otherwise look like a vesting report. What sets it
    # apart is a price the employee paid.
    has_espp = "purchase_price" in mapping and (
        "fmv_at_purchase" in mapping or "fmv" in mapping
    )
    if has_espp and any(hint in blob for hint in _ESPP_HINTS):
        return "espp"

    has_vest_date = "vest_date" in mapping
    has_dividend = "dividend_amount" in mapping and (
        "pay_date" in mapping or "dividend" in blob
    )
    has_sale = "sale_date" in mapping and (
        "proceeds" in mapping or "quantity_sold" in mapping
    )

    if has_vest_date and any(hint in blob for hint in _VEST_HINTS):
        return "vest"
    if has_dividend and any(hint in blob for hint in _DIVIDEND_HINTS):
        return "dividend"
    if has_sale and any(hint in blob for hint in _SALE_HINTS):
        return "sale"
    # Fall back on structure when the wording gives nothing away.
    if has_espp:
        return "espp"
    if has_vest_date:
        return "vest"
    if has_sale:
        return "sale"
    if has_dividend:
        return "dividend"
    return "unknown"


# --------------------------------------------------------------------------
# Row readers
# --------------------------------------------------------------------------


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
    """Strip the dollar sign and any thousands separators."""
    return D(_cell(row, mapping, key).replace("$", "").replace("USD", ""))


def _is_noise(symbol: str) -> bool:
    return symbol.lower() in ("", "total", "totals", "grand total", "subtotal")


def _read_vests(frame, mapping, out: Extraction, filename: str) -> None:
    for _, row in frame.iterrows():
        vest_date = parse_date(_cell(row, mapping, "vest_date"))
        shares = _money(row, mapping, "shares_vested")
        fmv = _money(row, mapping, "fmv")
        symbol = _cell(row, mapping, "symbol")

        if _is_noise(symbol) and not vest_date:
            continue
        if shares <= 0 or fmv <= 0 or vest_date is None:
            continue

        out.rsu_vests.append({
            "symbol": _clean_symbol(symbol),
            "grant_id": _cell(row, mapping, "grant_id"),
            "grant_date": parse_date(_cell(row, mapping, "grant_date")),
            "vest_date": vest_date,
            "shares_vested": shares,
            "fmv_per_share_fx": fmv,
            "shares_sold_to_cover": _money(row, mapping, "shares_sold_to_cover"),
            "sale_price_per_share_fx": _money(row, mapping, "sale_price") or fmv,
            "currency": "USD",
            "country_code": "2",
            "source_document": filename,
        })


def _read_espp(frame, mapping, out: Extraction, filename: str) -> None:
    for _, row in frame.iterrows():
        purchase_date = parse_date(
            _cell(row, mapping, "purchase_date_espp")
            or _cell(row, mapping, "vest_date")
        )
        shares = (
            _money(row, mapping, "shares_purchased_espp")
            or _money(row, mapping, "shares_vested")
        )
        price_paid = _money(row, mapping, "purchase_price")
        fmv = _money(row, mapping, "fmv_at_purchase") or _money(row, mapping, "fmv")
        symbol = _cell(row, mapping, "symbol")

        if purchase_date is None or shares <= 0 or price_paid <= 0:
            continue
        if _is_noise(symbol) and not fmv:
            continue

        out.espp_purchases.append({
            "symbol": _clean_symbol(symbol),
            "offering_start_date": parse_date(_cell(row, mapping, "offering_start")),
            "purchase_date": purchase_date,
            "shares_purchased": shares,
            "fmv_per_share_fx": fmv,
            "price_paid_per_share_fx": price_paid,
            "offering_price_fx": _money(row, mapping, "fmv_at_offering"),
            "contributions_fx": _money(row, mapping, "contributions"),
            "refund_fx": _money(row, mapping, "refund"),
            "currency": "USD",
            "country_code": "2",
            "source_document": filename,
        })


def _read_dividends(frame, mapping, out: Extraction, filename: str) -> None:
    for _, row in frame.iterrows():
        pay_date = parse_date(_cell(row, mapping, "pay_date"))
        gross = _money(row, mapping, "dividend_amount")
        symbol = _cell(row, mapping, "symbol")

        if gross <= 0 or pay_date is None or _is_noise(symbol):
            continue

        action = _cell(row, mapping, "action").lower()
        reinvest_shares = _money(row, mapping, "reinvest_shares")
        reinvested = bool(reinvest_shares > 0) or "reinvest" in action

        out.dividends.append({
            "symbol": _clean_symbol(symbol),
            "pay_date": pay_date,
            "gross_amount_fx": gross,
            "foreign_tax_withheld_fx": abs(_money(row, mapping, "tax_withheld")),
            "currency": "USD",
            "country_code": "2",
            "is_reinvested": reinvested,
            "shares_acquired": reinvest_shares,
            "reinvest_price_per_share_fx": _money(row, mapping, "reinvest_price"),
            "source_document": filename,
        })


def _read_sales(frame, mapping, out: Extraction, filename: str) -> None:
    for _, row in frame.iterrows():
        sale_date = parse_date(_cell(row, mapping, "sale_date"))
        shares = _money(row, mapping, "quantity_sold")
        proceeds = _money(row, mapping, "proceeds")
        symbol = _cell(row, mapping, "symbol")

        if sale_date is None or shares <= 0 or _is_noise(symbol):
            continue

        price = _money(row, mapping, "sale_price")
        if price <= 0 and proceeds > 0:
            price = proceeds / shares

        out.foreign_sales.append({
            "symbol": _clean_symbol(symbol),
            "sale_date": sale_date,
            "shares": shares,
            "price_per_share_fx": price,
            "fees_fx": abs(_money(row, mapping, "fees")),
            "currency": "USD",
            "country_code": "2",
            "source_document": filename,
        })


def _clean_symbol(raw: str) -> str:
    """Pull the ticker out of things like "ACME INC COM (ACME)"."""
    bracketed = re.search(r"\(([A-Z.]{1,6})\)", raw)
    if bracketed:
        return bracketed.group(1)
    token = raw.strip().split()[0] if raw.strip() else ""
    return token.upper()[:12]
