#!/usr/bin/env python3
"""Drive the running application with a real browser and capture each screen.

    python demo/seed_demo.py
    python run.py --port 8777 --no-browser &
    python demo/capture.py --port 8777 --out demo/screens

Used to produce the screenshots in the README, and to eyeball the interface
after a change without clicking through it by hand.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHROMIUM = "/opt/pw-browsers/chromium"


def synthetic_form16() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    lines = [
        "FORM NO. 16",
        "Certificate under section 203 of the Income-tax Act, 1961",
        "Name and address of the Employer",
        "ACME SOFTWARE INDIA PRIVATE LIMITED, Bengaluru",
        "TAN of the Deductor  BLRA12345B",
        "Name and address of the Employee",
        "ASHA RAMANATHAN",
        "PAN of the Employee  AAAPZ1234C",
        "Assessment Year  2026-27",
        "PART B (Annexure)",
        "Details of Salary Paid and any other income and tax deducted",
        "(a) Salary as per provisions contained in section 17(1)   4200000.00",
        "(b) Value of perquisites under section 17(2)   1344000.00",
        "(c) Profits in lieu of salary under section 17(3)   0.00",
        "(d) Total   5544000.00",
        "Less: Allowances to the extent exempt under section 10",
        "House Rent Allowance   480000.00",
        "Leave Travel Allowance   60000.00",
        "Tax on employment under section 16(iii)   2400.00",
        "Deductions under Chapter VI-A",
        "(a) Deduction in respect of life insurance premia 80C   150000.00",
        "(b) 80CCD(1B)   50000.00",
        "(c) 80D health insurance premium   28000.00",
        "Total amount of tax deducted at source   1186000.00",
    ]
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in lines:
        page.setFont("Helvetica", 9)
        page.drawString(40, y, line)
        y -= 14
    page.save()
    return buffer.getvalue()


ESPP_CSV = (
    "Demo Stock Plan Services - Employee Stock Purchase Plan - Purchase Confirmation\n\n"
    "Symbol,Offering Date,Purchase Date,Qty. Purchased,Purchase Price,"
    "Market Value Per Share On Purchase Date,Grant Date Market Value,Contributions\n"
    "ACME,2025-06-02,2025-12-01,48,109.65,162.00,129.00,5263.20\n"
)

DIV_CSV = (
    "Demo Stock Plan Services - Dividends and Interest (1099-DIV)\n"
    "Symbol,Record Date,Pay Date,Shares Held,Rate Per Share,Dividend Amount,"
    "Federal Income Tax Withheld,Shares Purchased,Reinvestment Price,Action\n"
    "ACME,2025-11-05,2025-11-20,141,0.46,64.86,16.22,0.39,166.30,Reinvest\n"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--out", default="demo/screens")
    parser.add_argument("--return-id", default="")
    parser.add_argument("--planner-return-id", default="")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{args.port}"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1340, "height": 960},
                                device_scale_factor=2)

        def shot(name: str, full: bool = True) -> None:
            page.wait_for_timeout(280)
            page.screenshot(path=str(out / f"{name}.png"), full_page=full)
            print(f"  {name}.png")

        # ---- 1. Home ------------------------------------------------------
        page.goto(base, wait_until="networkidle")
        page.fill("#q_salary", "4200000")
        page.fill("#q_80c", "150000")
        page.click("#q_go")
        page.wait_for_timeout(900)
        shot("01-home")

        # ---- the seeded return -------------------------------------------
        return_id = args.return_id
        if not return_id:
            page.click("table a")
            page.wait_for_load_state("networkidle")
            return_id = page.url.split("/returns/")[1].split("/")[0]
        print(f"  (return {return_id})")

        # ---- 2. Documents, with real files uploaded ------------------------
        page.goto(f"{base}/returns/{return_id}/documents", wait_until="networkidle")
        page.set_input_files("input[type=file]", [
            {"name": "Form16_AcmeIndia.pdf", "mimeType": "application/pdf",
             "buffer": synthetic_form16()},
            {"name": "stockplan_espp_purchase.csv", "mimeType": "text/csv",
             "buffer": ESPP_CSV.encode()},
            {"name": "stockplan_1099div.csv", "mimeType": "text/csv",
             "buffer": DIV_CSV.encode()},
        ])
        shot("02-documents")

        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        shot("03-review")

        # ---- 4. Income ----------------------------------------------------
        page.goto(f"{base}/returns/{return_id}/income", wait_until="networkidle")
        shot("04-income")

        # ---- 5. Foreign ---------------------------------------------------
        page.goto(f"{base}/returns/{return_id}/foreign", wait_until="networkidle")
        shot("05-foreign")

        # ---- 6. Regime comparison ------------------------------------------
        page.goto(f"{base}/returns/{return_id}/compare", wait_until="networkidle")
        shot("06-compare")

        # ---- 7. Advance tax -------------------------------------------------
        # Shown on the second seeded return: the year still in progress, where
        # salary TDS covers the salary and the gains fall to advance tax.
        planner_id = args.planner_return_id or return_id
        page.goto(f"{base}/returns/{planner_id}/planner?as_of=2025-12-01",
                  wait_until="networkidle")
        shot("07-planner")

        # ---- 8. Filing pack --------------------------------------------------
        page.goto(f"{base}/returns/{return_id}/file", wait_until="networkidle")
        shot("08-file")

        browser.close()
    print(f"\nWrote screenshots to {out}/")


if __name__ == "__main__":
    main()
