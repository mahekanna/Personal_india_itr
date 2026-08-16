"""Reading a broker's annual tax P&L, where one workbook holds three heads.

The fixtures here are shaped like the exports these brokers produce — a title
block, a header a few rows down, a sheet per segment, and a "Total" footer —
but they are written by hand. No real ICICI Direct file has been through this
parser, which is stated in the module docstring too. What the tests do pin is
the routing and the arithmetic, which is where the money is.
"""

from __future__ import annotations

import io

import pytest

from app.money import D
from app.parsers.registry import parse_document
from app.parsers.trading_pnl import _blob, _segment_of, parse_tabular


def workbook(sheets: dict) -> bytes:
    import pandas as pd

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(
                writer, sheet_name=name, header=False, index=False
            )
    return buffer.getvalue()


DELIVERY = [
    ["Realised Gain/Loss", None, None, None, None, None],
    [None, None, None, None, None, None],
    ["Scrip Name", "Quantity", "Buy Date", "Sell Date", "Buy Value", "Sell Value"],
    ["INFY", "100", "2023-05-10", "2025-08-12", "140000", "172000"],
]
INTRADAY = [
    ["Intraday Profit & Loss", None, None, None, None],
    ["Scrip Code", "Trade Date", "Quantity", "Profit/Loss", "Brokerage"],
    ["RELIANCE", "2025-06-11", "200", "-45000", "1200"],
    ["HDFCBANK", "2025-07-03", "150", "18000", "900"],
    ["Total", "", "", "-27000", "2100"],
]
FNO = [
    ["Futures & Options P&L", None, None, None, None, None, None],
    ["Contract", "Expiry", "Quantity", "Profit/Loss", "Brokerage", "STT", "GST"],
    ["NIFTY 24500 CE", "2025-08-28", "750", "-125000", "2400", "3100", "640"],
    ["BANKNIFTY FUT", "2025-09-25", "450", "310000", "1800", "2200", "480"],
    ["NIFTY 25000 PE", "2025-10-30", "300", "-64000", "900", "1100", "250"],
]
CURRENCY = [
    ["Currency F&O", None, None, None, None],
    ["Contract", "Trade Date", "Quantity", "Profit/Loss", "Brokerage"],
    ["USDINR FUT", "2025-07-29", "10000", "22000", "350"],
]
COMMODITY = [
    ["MCX Commodity P&L", None, None, None, None],
    ["Contract", "Trade Date", "Quantity", "Profit/Loss", "Brokerage"],
    ["GOLD DEC FUT", "2025-11-05", "2", "-38000", "500"],
]


def by_segment(extraction) -> dict:
    return {row["segment"]: row for row in extraction.trading_segments}


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_one_workbook_splits_into_three_heads_of_income():
    raw = workbook({
        "Equity Delivery": DELIVERY,
        "Equity Intraday": INTRADAY,
        "Derivatives F&O": FNO,
        "Currency Derivatives": CURRENCY,
        "Commodity": COMMODITY,
    })
    out = parse_document(raw, "ICICIdirect_TaxPnL_FY2025-26.xlsx")

    # Delivery is capital gains and must not appear as trading income.
    assert len(out.capital_gains) == 1
    assert out.capital_gains[0]["category"] == "ltcg_112a"

    segments = by_segment(out)
    assert set(segments) == {
        "equity_intraday", "equity_fo", "currency_fo", "commodity_fo",
    }


def test_the_arithmetic_is_per_segment():
    raw = workbook({"Equity Intraday": INTRADAY, "Derivatives F&O": FNO})
    segments = by_segment(parse_tabular(raw, "pnl.xlsx"))

    intraday = segments["equity_intraday"]
    assert intraday["gross_profit"] == D(-45_000) + D(18_000)
    # Turnover is the absolute value of each result: 45,000 + 18,000.
    assert intraday["turnover"] == D(63_000)
    assert intraday["brokerage"] == D(2_100)

    fno = segments["equity_fo"]
    assert fno["gross_profit"] == D(121_000)          # -1,25,000 + 3,10,000 - 64,000
    assert fno["turnover"] == D(499_000)              # 1,25,000 + 3,10,000 + 64,000
    assert fno["securities_transaction_tax"] == D(6_400)
    assert fno["gst"] == D(1_370)


def test_a_total_footer_is_not_a_trade():
    """It would double the segment and inflate turnover by half."""
    segments = by_segment(parse_tabular(workbook({"Intraday": INTRADAY}), "p.xlsx"))
    assert segments["equity_intraday"]["gross_profit"] == D(-27_000)


def test_a_delivery_sheet_alone_yields_no_trading_income():
    out = parse_tabular(workbook({"Equity Delivery": DELIVERY}), "p.xlsx")
    assert out.trading_segments == []


# --------------------------------------------------------------------------
# Segment detection
# --------------------------------------------------------------------------


def test_a_pandas_index_does_not_leak_its_repr_into_the_blob():
    """str() on a pandas Index gives "Index([...], dtype='object')", and the
    literal word "index" matched an F&O hint — which silently swept the whole
    intraday sheet into the wrong segment, and so into the wrong head."""
    import pandas as pd

    columns = pd.Index(["Scrip Code", "Trade Date", "Profit/Loss"])
    assert "dtype" not in _blob([columns])
    assert _segment_of("Equity Intraday", columns) == "equity_intraday"


@pytest.mark.parametrize(
    "sheet_name, expected",
    [
        ("Equity Intraday", "equity_intraday"),
        ("Intraday Equity", "equity_intraday"),
        ("Derivatives F&O", "equity_fo"),
        ("Futures and Options", "equity_fo"),
        ("Currency Derivatives", "currency_fo"),
        ("USDINR Futures", "currency_fo"),
        ("MCX Commodity", "commodity_fo"),
        # A derivative squared off the same day is still not speculative: the
        # proviso to s.43(5)(d) turns on the contract, not on delivery.
        ("F&O Intraday", "equity_fo"),
    ],
)
def test_sheet_names_map_to_the_right_statutory_segment(sheet_name, expected):
    assert _segment_of(sheet_name, []) == expected


def test_an_unrecognised_sheet_is_reported_not_swallowed():
    unknown = [
        ["Some Other Report", None, None, None],
        ["Contract", "Trade Date", "Quantity", "Profit/Loss"],
        ["MYSTERY", "2025-06-11", "10", "5000"],
    ]
    out = parse_tabular(workbook({"Sheet1": unknown}), "p.xlsx")
    assert out.trading_segments == []
    assert any("could not be placed in a segment" in w for w in out.warnings)


# --------------------------------------------------------------------------
# Turnover reporting
# --------------------------------------------------------------------------


def test_turnover_is_recomputed_and_the_user_is_told_why():
    out = parse_tabular(workbook({"Derivatives F&O": FNO}), "p.xlsx")
    assert any("ICAI Guidance Note" in w for w in out.warnings)
    assert any("pre-2022 method" in w for w in out.warnings)


def test_a_summary_only_segment_keeps_the_brokers_figure_and_says_so():
    """One line and no trades: there is nothing to recompute from, so the
    broker's number stands — but the audit determination now rests on it."""
    summary = [
        ["F&O Summary", None, None],
        ["Segment", "Profit/Loss", "Turnover"],
        ["Equity Futures & Options", "121000", "48000000"],
    ]
    out = parse_tabular(workbook({"Summary": summary}), "p.xlsx")
    segments = by_segment(out)
    assert segments["equity_fo"]["turnover"] == D(48_000_000)
    assert any("carried through unchecked" in w for w in out.warnings)


def test_the_speculation_ring_fence_is_pointed_out():
    out = parse_tabular(workbook({"Equity Intraday": INTRADAY}), "p.xlsx")
    assert any("Section 73" in w for w in out.warnings)


# --------------------------------------------------------------------------
# Merging into the return
# --------------------------------------------------------------------------


def test_segments_accumulate_across_two_statements():
    """A trader often has one file per exchange, or a revised one."""
    from app.merge import apply_extractions
    from app.schemas import TaxReturn

    tr = TaxReturn(assessment_year="2026-27")
    first = parse_tabular(workbook({"F&O": FNO}), "nse.xlsx")
    second = parse_tabular(workbook({"F&O": CURRENCY}), "cds.xlsx")
    apply_extractions(tr, [first, second], accepted_paths=set())

    assert len(tr.trading_segments) == 2
    assert {s.segment for s in tr.trading_segments} == {"equity_fo", "currency_fo"}


def test_the_same_file_twice_does_not_double_the_income():
    from app.merge import apply_extractions
    from app.schemas import TaxReturn

    tr = TaxReturn(assessment_year="2026-27")
    raw = workbook({"Derivatives F&O": FNO})
    twice = [parse_tabular(raw, "pnl.xlsx"), parse_tabular(raw, "pnl.xlsx")]
    apply_extractions(tr, twice, accepted_paths=set())

    assert len(tr.trading_segments) == 1
    assert tr.trading_segments[0].gross_profit == D(121_000)


# --------------------------------------------------------------------------
# All the way through to the computation
# --------------------------------------------------------------------------


def test_a_whole_workbook_reaches_the_tax_computation():
    from app.merge import apply_extractions
    from app.schemas import SalaryIncome, TaxReturn
    from app.tax.engine import compute
    from app.tax.rules import get_ay

    tr = TaxReturn(assessment_year="2026-27")
    tr.salaries = [SalaryIncome(employer_name="Acme", salary_17_1=D("3000000"))]
    raw = workbook({
        "Equity Delivery": DELIVERY,
        "Equity Intraday": INTRADAY,
        "Derivatives F&O": FNO,
        "Currency Derivatives": CURRENCY,
        "Commodity": COMMODITY,
    })
    extraction = parse_document(raw, "ICICIdirect_TaxPnL.xlsx")
    apply_extractions(tr, [extraction], accepted_paths=set())

    comp = compute(tr, "new", get_ay("2026-27"))

    # F&O: 1,21,000 less brokerage 5,100, STT 6,400 and GST 1,370 = 1,08,130
    # Currency: 22,000 less 350 = 21,650
    # Commodity: -38,000 less 500 = -38,500
    #   1,08,130 + 21,650 - 38,500 = 91,280
    # Intraday is a loss, so section 73 holds it back out of the head entirely.
    assert comp.business == D("91280")
    assert comp.carried_forward["speculative_business"] == D("29100")
    # Delivery equity stayed in capital gains at the concessional rate.
    assert [s.code for s in comp.special_slices] == ["ltcg_112a"]
    assert comp.audit_required is False


# --------------------------------------------------------------------------
# Other brokers
# --------------------------------------------------------------------------
#
# The parser maps column names, not brokers, so it should read whatever an
# Indian broker exports without a parser each. These fixtures are shaped like
# what each one is reported to produce — none is a verified sample — but they
# pin the mapping against genuinely different naming conventions.


BROKER_FORMATS = {
    "zerodha": ("F&O", [
        ["Tradewise P&L", None, None, None, None, None],
        ["Symbol", "Entry Date", "Exit Date", "Quantity", "Realized P&L",
         "Turnover"],
        ["NIFTY25AUG24500CE", "2025-08-01", "2025-08-28", "750", "-125000",
         "125000"],
        ["BANKNIFTY25SEPFUT", "2025-09-01", "2025-09-25", "450", "310000",
         "310000"],
    ], "equity_fo", D(185_000), D(435_000)),
    "groww": ("Futures and Options", [
        ["Stock Name", "Trade Date", "Qty", "Net P&L", "Charges"],
        ["NIFTY 25000 PE", "2025-10-30", "300", "-64000", "2250"],
    ], "equity_fo", D(-64_000), D(64_000)),
    "upstox": ("Derivatives", [
        ["Scrip", "Expiry", "Quantity", "Profit / Loss", "Total Brokerage",
         "STT"],
        ["NIFTY AUG FUT", "2025-08-28", "500", "95000", "1500", "2000"],
    ], "equity_fo", D(95_000), D(95_000)),
    "angelone_commodity": ("Commodity MCX", [
        ["Contract", "Trade Date", "Lots", "P&L", "Brokerage"],
        ["GOLD DEC FUT", "2025-11-05", "2", "-38000", "500"],
    ], "commodity_fo", D(-38_000), D(38_000)),
    "angelone_currency": ("Currency Segment", [
        ["Contract", "Trade Date", "Lots", "P&L", "Brokerage"],
        ["USDINR FUT", "2025-07-29", "10", "22000", "350"],
    ], "currency_fo", D(22_000), D(22_000)),
}


@pytest.mark.parametrize("broker", sorted(BROKER_FORMATS))
def test_other_brokers_parse_without_a_parser_each(broker):
    sheet, rows, segment, profit, turnover = BROKER_FORMATS[broker]
    segments = by_segment(parse_tabular(workbook({sheet: rows}), f"{broker}.xlsx"))

    assert segment in segments, f"{broker} produced nothing"
    assert segments[segment]["gross_profit"] == profit
    assert segments[segment]["turnover"] == turnover


def test_a_bare_charges_column_is_not_dropped():
    """Some brokers itemise every charge; others give one "Charges" figure.
    Unmapped it went unclaimed, and these are deductible business expenses."""
    sheet, rows, _, _, _ = BROKER_FORMATS["groww"]
    segments = by_segment(parse_tabular(workbook({sheet: rows}), "groww.xlsx"))
    assert segments["equity_fo"]["other_expenses"] == D(2_250)


def test_the_catch_all_does_not_steal_the_itemised_columns():
    """"Charges" is a substring of half the other headings. The narrower
    fields have to claim theirs first or the breakdown collapses into one
    number."""
    from app.parsers.trading_pnl import _map_columns

    mapping = _map_columns([
        "Contract", "Profit/Loss", "Brokerage", "Exchange Transaction Charges",
        "STT", "SEBI Turnover Fees", "Stamp Duty", "GST", "DP Charges",
        "Other Charges",
    ])
    assert mapping["brokerage"] == "Brokerage"
    assert mapping["exchange_charges"] == "Exchange Transaction Charges"
    assert mapping["dp_charges"] == "DP Charges"
    assert mapping["other_charges"] == "Other Charges"


def test_a_per_row_segment_column_beats_the_sheet_name():
    """Some brokers put every segment on one sheet with a segment column."""
    csv = (
        "Dhan Tax P&L Report\n"
        "segment,scrip,trade_date,quantity,profit_loss,brokerage\n"
        "FNO,NIFTY 25100 CE,2025-09-11,600,74000,1800\n"
        "INTRADAY,TATASTEEL,2025-06-02,400,-12000,600\n"
    ).encode()
    segments = by_segment(parse_tabular(csv, "dhan_pnl.csv"))

    assert segments["equity_fo"]["gross_profit"] == D(74_000)
    assert segments["equity_intraday"]["gross_profit"] == D(-12_000)


def test_an_unrecognised_format_says_so_rather_than_reading_nothing():
    """The failure has to be loud. A broker this parser has never seen must
    not look like a year with no trading in it."""
    rows = [
        ["Mystery Broker Report", None, None],
        ["Ticker Ref", "Movement", "Net Value"],
        ["ABC123", "10", "5000"],
    ]
    out = parse_tabular(workbook({"Sheet1": rows}), "unknown_broker.xlsx")
    assert out.trading_segments == []
    assert out.warnings
    assert any("not ones this parser knows" in w or "could not be placed" in w
               for w in out.warnings)
