"""The computation sheet, as a PDF.

This is the document to keep. If a notice arrives three years from now, it is
the only record of how each figure was arrived at.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import List

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .money import D, inr
from .schemas import TaxReturn
from .tax.engine import Computation, RegimeComparison

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d4d4d4")
ACCENT = colors.HexColor("#0b5c3f")
BAND = colors.HexColor("#f4f4f2")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=16, leading=20,
            textColor=INK, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=9.5, leading=13,
            textColor=MUTED, spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=11, leading=14,
            textColor=ACCENT, spaceBefore=12, spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9, leading=12.5,
            textColor=INK,
        ),
        "note": ParagraphStyle(
            "note", parent=base["Normal"], fontSize=8, leading=11,
            textColor=MUTED,
        ),
    }


def build_computation_pdf(
    tr: TaxReturn, comparison: RegimeComparison, decision
) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Computation of income — {tr.taxpayer.name or tr.taxpayer.pan}",
    )
    styles = _styles()
    story: List = []
    comp = comparison.chosen

    story.append(Paragraph("Computation of Total Income and Tax", styles["title"]))
    story.append(Paragraph(
        f"{tr.taxpayer.name or '—'} &nbsp;·&nbsp; PAN {tr.taxpayer.pan or '—'} "
        f"&nbsp;·&nbsp; Assessment Year {tr.assessment_year} "
        f"&nbsp;·&nbsp; {comp.regime_name} &nbsp;·&nbsp; {decision.form}",
        styles["subtitle"],
    ))

    # ---- Summary ----------------------------------------------------------
    story.append(Paragraph("Summary", styles["h2"]))
    summary_rows = [
        ["Gross total income", inr(comp.gross_total_income)],
        ["Less: deductions under Chapter VI-A", inr(comp.deductions_total)],
        ["Total income", inr(comp.total_income_rounded)],
        ["Tax on total income", inr(comp.tax_before_rebate)],
        ["Less: rebate u/s 87A", inr(comp.rebate_87a)],
        ["Add: surcharge", inr(comp.surcharge)],
        ["Add: health and education cess", inr(comp.cess)],
        ["Net tax liability", inr(comp.total_tax_liability)],
        ["Add: interest and fee u/s 234A/B/C/F", inr(comp.interest.total)],
        ["Less: taxes already paid", inr(comp.total_taxes_paid)],
    ]
    final_label = "Balance tax payable" if comp.net_payable > 0 else "Refund due"
    final_value = comp.net_payable if comp.net_payable > 0 else comp.refund_due
    summary_rows.append([final_label, inr(final_value)])
    story.append(_money_table(summary_rows, highlight_last=True))

    # ---- Head-wise --------------------------------------------------------
    story.append(Paragraph("Head-wise computation", styles["h2"]))
    head_rows = []
    for line in comp.lines:
        if line.amount == 0 and not line.is_subtotal:
            continue
        head_rows.append([line.label, inr(line.amount), line.is_subtotal])
    story.append(_line_table(head_rows))

    # ---- Special rates ----------------------------------------------------
    if comp.special_slices:
        story.append(Paragraph("Income taxed at special rates", styles["h2"]))
        rows = [["Description", "Income", "Exempt", "Chargeable", "Rate", "Tax"]]
        for slice_ in comp.special_slices:
            exempt = slice_.statutory_exemption + slice_.basic_exemption_used
            rows.append([
                Paragraph(slice_.label, _styles()["note"]),
                inr(slice_.income), inr(exempt), inr(slice_.chargeable),
                f"{slice_.rate * 100:.1f}%", inr(slice_.tax),
            ])
        table = Table(rows, colWidths=[62 * mm, 24 * mm, 21 * mm, 24 * mm,
                                       14 * mm, 24 * mm])
        table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)

    # ---- Deductions -------------------------------------------------------
    if comp.deduction_detail and comp.deduction_detail.lines:
        story.append(Paragraph("Deductions under Chapter VI-A", styles["h2"]))
        rows = [["Section", "Claimed", "Allowed"]]
        for line in comp.deduction_detail.lines:
            rows.append([
                Paragraph(f"<b>{line.section}</b> — {line.label}", styles["note"]),
                inr(line.claimed), inr(line.allowed),
            ])
        table = Table(rows, colWidths=[115 * mm, 27 * mm, 27 * mm])
        table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)

    # ---- Regime comparison ------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Comparison of the two regimes", styles["h2"]))
    rows = [
        ["", "New regime", "Old regime"],
        ["Total income", inr(comparison.new.total_income_rounded),
         inr(comparison.old.total_income_rounded)],
        ["Deductions allowed", inr(comparison.new.deductions_total),
         inr(comparison.old.deductions_total)],
        ["Tax before rebate", inr(comparison.new.tax_before_rebate),
         inr(comparison.old.tax_before_rebate)],
        ["Rebate u/s 87A", inr(comparison.new.rebate_87a),
         inr(comparison.old.rebate_87a)],
        ["Surcharge", inr(comparison.new.surcharge), inr(comparison.old.surcharge)],
        ["Cess", inr(comparison.new.cess), inr(comparison.old.cess)],
        ["Net tax liability", inr(comparison.new.total_tax_liability),
         inr(comparison.old.total_tax_liability)],
    ]
    table = Table(rows, colWidths=[85 * mm, 42 * mm, 42 * mm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, RULE),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"<b>{comparison.recommended.title()} regime</b> is cheaper by "
        f"₹{inr(comparison.saving)} on these figures.",
        styles["body"],
    ))

    # ---- Warnings ---------------------------------------------------------
    notes = list(comp.warnings) + list(comp.interest.notes)
    if notes:
        story.append(Paragraph("Notes", styles["h2"]))
        for note in notes:
            story.append(Paragraph(f"• {note}", styles["note"]))
            story.append(Spacer(1, 2))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"Prepared on {date.today():%d %B %Y} by Personal India ITR. This is a "
        "working computation prepared from the documents supplied — not "
        "professional tax advice. Verify every figure against the portal's "
        "preview before submitting.",
        styles["note"],
    ))

    document.build(story)
    return buffer.getvalue()


def _money_table(rows: List[List[str]], highlight_last: bool = False) -> Table:
    table = Table([[label, value] for label, value in rows],
                  colWidths=[125 * mm, 44 * mm])
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
    ]
    if highlight_last:
        style += [
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), BAND),
            ("TEXTCOLOR", (0, -1), (-1, -1), ACCENT),
        ]
    table.setStyle(TableStyle(style))
    return table


def _line_table(rows: List[List]) -> Table:
    data = [[label, value] for label, value, _ in rows]
    table = Table(data, colWidths=[125 * mm, 44 * mm])
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index, (_, _, is_subtotal) in enumerate(rows):
        if is_subtotal:
            style.append(("FONTNAME", (0, index), (-1, index), "Helvetica-Bold"))
            style.append(("LINEABOVE", (0, index), (-1, index), 0.4, RULE))
    table.setStyle(TableStyle(style))
    return table
