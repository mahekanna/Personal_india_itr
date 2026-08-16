#!/usr/bin/env python3
"""Describe a broker statement's structure without disclosing anything in it.

To teach the parser a new broker, all that is needed is the *shape* of the
file: what the sheets are called, what the column headings are, how many rows
of preamble sit above the header, how dates are written and how a loss is
signed. None of that requires a single real figure.

So this reads a statement and prints only that. Every value is replaced by a
description of its type — ``<number>``, ``<date:DD/MM/YYYY>``, ``<text>`` — and
anything that looks like a PAN, a client code or an account number is dropped
before it can reach the output. The result is safe to paste into an issue or a
chat.

    python tools/describe_statement.py ICICIdirect_TaxPnL_FY2025-26.xlsx

Read the output before you send it. It is short, and it is the only way to be
sure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Anything shaped like an identifier is removed rather than described, because
# a description of a PAN is still most of a PAN.
_SENSITIVE = re.compile(
    r"""(
        \b[A-Z]{5}\d{4}[A-Z]\b            # PAN
      | \b[A-Z]{4}0[A-Z0-9]{6}\b          # IFSC
      | \b\d{9,18}\b                      # account, demat or client codes
      | \b[\w.+-]+@[\w-]+\.[\w.]+\b       # email
      | \b(?:\+?91[-\s]?)?[6-9]\d{9}\b    # mobile
    )""",
    re.VERBOSE,
)

_DATE_SHAPES = (
    (re.compile(r"^\d{4}-\d{2}-\d{2}"), "date:YYYY-MM-DD"),
    (re.compile(r"^\d{1,2}/\d{1,2}/\d{4}"), "date:DD/MM/YYYY"),
    (re.compile(r"^\d{1,2}-\d{1,2}-\d{4}"), "date:DD-MM-YYYY"),
    (re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2,4}"), "date:DD-Mon-YYYY"),
    (re.compile(r"^\d{1,2}\s[A-Za-z]{3}\s\d{2,4}"), "date:DD Mon YYYY"),
)


def describe(value) -> str:
    """One cell, reduced to its type."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return ""
    if _SENSITIVE.search(text):
        return "<redacted>"

    for pattern, label in _DATE_SHAPES:
        if pattern.match(text):
            return f"<{label}>"

    bare = text.replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    negative = bare.startswith("(") and bare.endswith(")")
    if negative:
        bare = bare[1:-1]
    try:
        float(bare)
    except ValueError:
        # Short labels are structural — "Total", "NIFTY", "FNO" — and worth
        # keeping. Anything long enough to be personal is not.
        return text if len(text) <= 24 else "<text>"
    if negative:
        return "<number: negative, written as (1,234)>"
    if bare.startswith("-"):
        return "<number: negative, written as -1234>"
    return "<number>"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    path = Path(sys.argv[1])
    import pandas as pd

    if path.suffix.lower() in (".csv", ".txt"):
        frames = {path.name: pd.read_csv(path, header=None, dtype=str,
                                         on_bad_lines="skip", engine="python")}
    else:
        engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
        frames = pd.read_excel(path, sheet_name=None, header=None, dtype=str,
                               engine=engine)

    print(f"FILE: {path.suffix.lower()}   sheets: {len(frames)}\n")
    for name, frame in frames.items():
        print(f"=== SHEET: {name!r}   {len(frame)} rows x {len(frame.columns)} cols")
        for index in range(min(len(frame), 12)):
            cells = [describe(cell) for cell in frame.iloc[index].tolist()]
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                print(f"  row {index:>2}: " + " | ".join(cells))
        if len(frame) > 12:
            print(f"  ... {len(frame) - 12} more rows of the same shape")
        print()


if __name__ == "__main__":
    main()
