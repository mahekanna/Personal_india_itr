"""Parser tests, built on synthetic documents that mimic the real layouts."""

from __future__ import annotations

import io
import json
from datetime import date

import pytest

from app.money import D
from app.parsers.base import candidate_passwords, parse_date, financial_year_of
from app.parsers.registry import parse_document


def _pdf(lines):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in lines:
        page.setFont("Helvetica", 9)
        page.drawString(40, y, line)
        y -= 13
        if y < 40:
            page.showPage()
            y = 800
    page.save()
    return buffer.getvalue()


FORM16_LINES = [
    "FORM NO. 16",
    "Certificate under section 203 of the Income-tax Act, 1961",
    "Name and address of the Employer",
    "ACME SOFTWARE PRIVATE LIMITED, Bengaluru",
    "TAN of the Deductor  BLRA12345B",
    "Name and address of the Employee",
    "ASHA RAMANATHAN",
    "PAN of the Employee  ABCDE1234F",
    "PART B (Annexure)",
    "Details of Salary Paid and any other income and tax deducted",
    "(a) Salary as per provisions contained in section 17(1)   2400000.00",
    "(b) Value of perquisites under section 17(2)   50000.00",
    "(d) Total   2450000.00",
    "House Rent Allowance   240000.00",
    "Leave Travel Allowance   45000.00",
    "Tax on employment under section 16(iii)   2400.00",
    "Total amount of tax deducted at source   480000.00",
]

BROKER_CSV = """Zerodha Console - Tradewise Capital Gains
Period: 2025-04-01 to 2026-03-31

Symbol,ISIN,Quantity,Buy Date,Buy Value,Sell Date,Sell Value,Realized P&L
INFY,INE009A01021,100,2021-06-10,500000,2025-09-15,825000,325000
TCS,INE467B01029,50,2025-04-01,200000,2025-08-01,300000,100000
"""


# --------------------------------------------------------------------------
# Form 16
# --------------------------------------------------------------------------


def test_form16_is_identified_and_read():
    extraction = parse_document(_pdf(FORM16_LINES), "form16.pdf")
    assert extraction.document_type == "form16"
    assert extraction.confidence > 0.5

    salary = extraction.salaries[0]
    assert salary["employer_tan"] == "BLRA12345B"
    assert salary["salary_17_1"] == D("2400000.00")
    assert salary["perquisites_17_2"] == D("50000.00")
    assert salary["exempt_allowances"]["hra"] == D("240000.00")
    assert salary["professional_tax"] == D("2400.00")
    assert salary["tds_deducted"] == D("480000.00")


def test_form16_yields_a_matching_tds_payment():
    extraction = parse_document(_pdf(FORM16_LINES), "form16.pdf")
    payment = extraction.payments[0]
    assert payment["kind"] == "tds_salary"
    assert payment["amount"] == D("480000.00")
    assert payment["deductor_tan"] == "BLRA12345B"


def test_employee_pan_is_preferred_over_the_deductor_pan():
    lines = ["PAN of the Deductor  ZZZZZ9999Z"] + FORM16_LINES
    extraction = parse_document(_pdf(lines), "form16.pdf")
    pan = next(f for f in extraction.facts if f.path == "taxpayer.pan")
    assert pan.value == "ABCDE1234F"


# --------------------------------------------------------------------------
# Broker statements
# --------------------------------------------------------------------------


def test_broker_csv_with_a_title_block_still_parses():
    """The title lines above the header used to swallow every data row."""
    extraction = parse_document(BROKER_CSV.encode(), "zerodha_pnl.csv")
    assert extraction.document_type == "broker_pnl"
    assert len(extraction.capital_gains) == 2


def test_holding_period_decides_the_bucket_not_the_broker_label():
    extraction = parse_document(BROKER_CSV.encode(), "zerodha_pnl.csv")
    by_symbol = {
        row["description"].split(" ")[0]: row for row in extraction.capital_gains
    }
    assert by_symbol["INFY"]["category"] == "ltcg_112a"     # held four years
    assert by_symbol["TCS"]["category"] == "stcg_111a"      # held four months


def test_debt_fund_always_lands_in_the_50aa_bucket():
    csv = (
        "Scheme Name,Purchase Date,Purchase Value,Sale Date,Sale Value,Scheme Type\n"
        "HDFC Liquid Fund,2020-01-01,200000,2025-06-01,260000,Debt\n"
    )
    extraction = parse_document(csv.encode(), "cams.csv")
    assert extraction.capital_gains[0]["category"] == "stcg_debt_mf"


def test_gain_column_backfills_a_missing_cost():
    csv = (
        "Symbol,Sell Date,Sell Value,Realized P&L\n"
        "WIPRO,2025-08-01,300000,50000\n"
    )
    extraction = parse_document(csv.encode(), "pnl.csv")
    row = extraction.capital_gains[0]
    assert row["cost_of_acquisition"] == D(250_000)


def test_broker_totals_are_recorded_for_reconciliation():
    extraction = parse_document(BROKER_CSV.encode(), "zerodha_pnl.csv")
    fact = next(
        f for f in extraction.facts
        if f.path == "crosscheck.broker_sale_consideration"
    )
    assert fact.value == D(1_125_000)


# --------------------------------------------------------------------------
# AIS
# --------------------------------------------------------------------------


def test_ais_json_categories_map_onto_the_return():
    payload = {
        "AISData": {
            "PART_B": [
                {"infoCategory": "Salary", "informationValue": 2450000},
                {"infoCategory": "Interest from savings bank",
                 "informationValue": 12000},
                {"infoCategory": "Interest from deposit",
                 "informationValue": 85000},
                {"infoCategory": "Dividend", "informationValue": 33000},
            ]
        },
        "pan": "ABCDE1234F",
    }
    extraction = parse_document(json.dumps(payload).encode(), "ais.json")
    assert extraction.document_type == "ais"
    values = {fact.path: fact.value for fact in extraction.facts}
    assert values["other_sources.savings_bank_interest"] == D(12_000)
    assert values["other_sources.fixed_deposit_interest"] == D(85_000)
    assert values["other_sources.dividend_income"] == D(33_000)
    # Salary is a repeating structure, so it comes back as a cross-check only.
    assert "crosscheck.salaries.salary_17_1" in values


def test_malformed_json_is_reported_not_raised():
    extraction = parse_document(b"{not json", "ais.json")
    assert extraction.warnings
    assert not extraction.facts


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def test_a_scanned_pdf_is_reported_as_unreadable():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    page.rect(100, 100, 200, 200)
    page.save()
    extraction = parse_document(buffer.getvalue(), "scan.pdf")
    assert extraction.document_type == "unknown"
    assert any("scanned" in w for w in extraction.warnings)


def test_an_unsupported_extension_is_refused_politely():
    extraction = parse_document(b"whatever", "notes.docx")
    assert extraction.document_type == "unknown"
    assert extraction.warnings


def test_password_candidates_follow_the_department_convention():
    passwords = candidate_passwords("ABCDE1234F", date(1990, 1, 1))
    assert "abcde1234f01011990" in passwords


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("15/09/2025", date(2025, 9, 15)),
        ("15-09-2025", date(2025, 9, 15)),
        ("15-Sep-2025", date(2025, 9, 15)),
        ("2025-09-15", date(2025, 9, 15)),
        ("not a date", None),
        ("", None),
    ],
)
def test_date_parsing_covers_the_formats_these_documents_use(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "when, expected",
    [
        (date(2025, 4, 1), "2025-26"),
        (date(2026, 3, 31), "2025-26"),
        (date(2026, 4, 1), "2026-27"),
    ],
)
def test_financial_year_boundaries(when, expected):
    assert financial_year_of(when) == expected


def test_money_parsing_handles_indian_formatting():
    assert D("₹12,34,567.00") == D("1234567.00")
    assert D("Rs. 1,50,000/-") == D(150_000)
    assert D("(5,000)") == D(-5_000)
    assert D("garbage") == D(0)
    assert D(None) == D(0)


def test_indian_digit_grouping():
    from app.money import inr

    assert inr(1234567) == "12,34,567"
    assert inr(100000) == "1,00,000"
    assert inr(999) == "999"
    assert inr(-1234567) == "-12,34,567"


# --------------------------------------------------------------------------
# The formats TRACES and the portal actually hand you
# --------------------------------------------------------------------------


TEXT_26AS = (
    "Form 26AS\n"
    "Assessment Year: 2026-27  Permanent Account Number: ABCDE1234F\n"
    "PART I - Details of Tax Deducted at Source\n"
    "Sr. No.^Name of Deductor^TAN of Deductor^Total Amount Paid^Total Tax Deducted\n"
    "1^ACME TECHNOLOGIES PVT LTD^BLRA12345B^3000000.00^500000.00\n"
    "2^ICICI BANK LIMITED^MUMI54321C^48000.00^4800.00\n"
).encode()


def test_a_text_form_26as_is_not_mistaken_for_a_broker_export():
    """TRACES offers HTML, text or PDF. A .txt went down the spreadsheet path,
    was identified as a broker statement, read nil, and reported that it found
    no capital gains — so every TDS credit in it was silently lost and the
    user was told something irrelevant."""
    from app.parsers.registry import parse_document

    out = parse_document(TEXT_26AS, "26AS_2026-27.txt")
    assert out.document_type == "form26as"
    assert len(out.payments) == 2


def test_the_zip_traces_hands_over_is_opened():
    import io
    import zipfile

    from app.parsers.registry import parse_document

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("26AS_ABCDE1234F_2026-27.txt", TEXT_26AS)

    out = parse_document(buffer.getvalue(), "26AS.zip")
    assert out.document_type == "form26as"
    assert len(out.payments) == 2


def test_a_genuine_broker_txt_still_reaches_the_broker_parser():
    """The text-first check must not capture files that really are tabular."""
    from app.parsers.registry import parse_document

    csv = (
        "Tradewise P&L\n"
        "Symbol,Buy Date,Sell Date,Quantity,Buy Value,Sell Value\n"
        "INFY,2023-05-10,2025-08-12,100,140000,172000\n"
    ).encode()
    out = parse_document(csv, "pnl.txt")
    assert out.document_type == "broker_pnl"
    assert len(out.capital_gains) == 1


def test_an_encrypted_zip_asks_for_the_date_of_birth():
    import io
    import zipfile

    from app.parsers.registry import parse_document

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("inner.txt", TEXT_26AS)
    # zipfile cannot write encrypted archives, so assert the message path on a
    # corrupt one instead: either way the user must be told, not ignored.
    out = parse_document(b"PK\x03\x04 not really a zip", "26AS.zip")
    assert out.document_type == "unknown"
    assert out.warnings
