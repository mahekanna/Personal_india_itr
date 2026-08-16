"""The describer has one job: reveal the shape and none of the contents."""

from __future__ import annotations

import io

import pytest

from tools.describe_statement import describe


@pytest.mark.parametrize("secret", [
    "ABCDE1234F",                       # PAN
    "ICIC0000001",                      # IFSC
    "084213977612",                     # client or account code
    "someone@example.com",
    "9876543210",
    "+91 9876543210",
])
def test_identifiers_are_removed_not_described(secret):
    """A description of a PAN is still most of a PAN."""
    assert describe(secret) == "<redacted>"
    assert describe(f"Client Code: {secret}") == "<redacted>"


@pytest.mark.parametrize("value", ["45000", "1,45,000.00", "₹45,000"])
def test_amounts_never_survive(value):
    assert describe(value) == "<number>"


def test_the_sign_convention_is_reported_because_it_matters():
    """Brokers write a loss as -1234 or as (1,234), and the parser has to
    read both."""
    assert "(1,234)" in describe("(45,000.00)")
    assert "-1234" in describe("-45000")


@pytest.mark.parametrize("value, shape", [
    ("2025-06-11", "date:YYYY-MM-DD"),
    ("11/06/2025", "date:DD/MM/YYYY"),
    ("11-06-2025", "date:DD-MM-YYYY"),
    ("11-Jun-2025", "date:DD-Mon-YYYY"),
])
def test_date_formats_are_reported_not_the_dates(value, shape):
    assert describe(value) == f"<{shape}>"


def test_column_headings_and_short_labels_survive():
    """These are the whole point — they are what teaches the parser."""
    for heading in ("Scrip Code", "Profit/Loss", "Total", "NIFTY 24500 CE"):
        assert describe(heading) == heading


def test_long_free_text_does_not():
    long_text = "Statement of account for the period April 2025 to March 2026"
    assert describe(long_text) == "<text>"


def test_a_whole_workbook_discloses_nothing(capsys, tmp_path):
    import sys

    import pandas as pd

    from tools import describe_statement

    path = tmp_path / "statement.xlsx"
    pd.DataFrame([
        ["Client Code: 084213977612   PAN: ABCDE1234F", None, None],
        ["Scrip Code", "Trade Date", "Profit/Loss"],
        ["RELIANCE", "11/06/2025", "(45,000.00)"],
    ]).to_excel(path, sheet_name="Intraday", header=False, index=False)

    argv = sys.argv
    sys.argv = ["describe_statement.py", str(path)]
    try:
        describe_statement.main()
    finally:
        sys.argv = argv

    output = capsys.readouterr().out
    assert "Scrip Code" in output and "Intraday" in output
    for secret in ("ABCDE1234F", "084213977612", "45,000", "45000"):
        assert secret not in output
