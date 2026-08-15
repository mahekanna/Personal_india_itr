#!/usr/bin/env python3
"""Seed a realistic return, so the application can be looked at with data in it.

    python demo/seed_demo.py

Creates one return for AY 2026-27 belonging to a fictional salaried engineer
with a US employer's equity: a four-year grant vesting quarterly, an ESPP
purchase with a lookback, dividends part-reinvested, a couple of sales, an
Indian dividend, a home loan and the usual deductions.

Everything here is invented. No real PAN, TAN, IFSC or account number appears.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import ReturnRecord, get_session, init_db  # noqa: E402
from app.money import D  # noqa: E402
from app.schemas import (  # noqa: E402
    BankAccount,
    CapitalGainItem,
    DividendReceipt,
    DividendSchedule,
    ESPPPurchase,
    ForeignHolding,
    ForeignSale,
    HouseProperty,
    RSUVest,
    SalaryIncome,
    TaxPayment,
    TaxReturn,
    VestingSchedule,
)

# Rates pinned so the demo is reproducible and nothing shows as provisional.
# These are illustrative, not the published SBI figures.
DEMO_RATES = {
    "2025-03": "85.60", "2025-04": "85.10", "2025-05": "85.40",
    "2025-06": "85.75", "2025-07": "87.55", "2025-08": "88.15",
    "2025-09": "88.75", "2025-10": "88.65", "2025-11": "89.40",
    "2025-12": "89.90", "2026-01": "90.10", "2026-02": "89.75",
    "2026-03": "89.50", "2023-08": "82.70", "2023-11": "83.35",
}


def build_demo_return() -> TaxReturn:
    tr = TaxReturn(
        assessment_year="2026-27",
        regime_choice="auto",
        filing_date=date(2026, 7, 28),
        has_business_income=False,
    )

    # ---- Who is filing ----------------------------------------------------
    tr.taxpayer.name = "Asha Ramanathan"
    tr.taxpayer.pan = "AAAPZ1234C"
    tr.taxpayer.date_of_birth = date(1989, 4, 12)
    tr.taxpayer.email = "asha@example.invalid"
    tr.taxpayer.mobile = "9000000000"
    tr.taxpayer.address_line = "418, 12th Main, Indiranagar"
    tr.taxpayer.city = "Bengaluru"
    tr.taxpayer.state_code = "29"
    tr.taxpayer.pincode = "560038"
    tr.taxpayer.residential_status = "RES"
    tr.taxpayer.has_foreign_assets = True
    tr.taxpayer.bank_accounts = [BankAccount(
        ifsc="DEMO0001234", bank_name="Demo Bank",
        account_number="000011112222", is_primary_refund_account=True,
    )]

    # ---- Salary -----------------------------------------------------------
    # Gross of ₹42 lakh, of which ₹13,44,000 is the RSU and ESPP perquisite the
    # employer already ran through payroll.
    tr.salaries = [SalaryIncome(
        employer_name="Acme Software India Private Limited",
        employer_tan="BLRA12345B",
        employer_category="PE",
        salary_17_1=D(4_200_000),
        perquisites_17_2=D(1_344_000),
        exempt_allowances={"hra": D(480_000), "lta": D(60_000)},
        professional_tax=D(2_400),
        employer_nps_contribution=D(420_000),
    )]

    # ---- The grant: 400 shares over four years, quarterly, one-year cliff --
    tr.vesting_schedules = [VestingSchedule(
        grant_id="RSU-2024-0117",
        symbol="ACME",
        company_name="Acme Software Inc",
        grant_date=date(2024, 9, 15),
        total_shares=D(400),
        frequency="quarterly",
        first_vest_date=date(2025, 9, 15),
        tranches=16,
        cliff_shares=D(100),
        estimated_fmv_per_share_fx=D(178),
        sell_to_cover_fraction=D("0.31"),
        included_in_form16=True,
        actual_fmv={
            "2025-09-15": "151.40",
            "2025-12-15": "163.20",
            "2026-03-15": "171.85",
        },
    )]

    # A tranche from an older grant, already long term by the time it is sold.
    tr.rsu_vests = [RSUVest(
        symbol="ACME", company_name="Acme Software Inc",
        grant_id="RSU-2022-0308",
        vest_date=date(2023, 9, 15), shares_vested=D(60),
        fmv_per_share_fx=D(98), shares_sold_to_cover=D(19),
        sale_price_per_share_fx=D("98.20"), included_in_form16=True,
    )]

    # ---- ESPP: 15% plan with a lookback, after the price ran up -----------
    tr.espp_purchases = [ESPPPurchase(
        symbol="ACME", company_name="Acme Software Inc",
        plan_name="Acme ESPP 2025-H1",
        offering_start_date=date(2025, 6, 2),
        purchase_date=date(2025, 12, 1),
        shares_purchased=D(48),
        fmv_per_share_fx=D(162),
        price_paid_per_share_fx=D("109.65"),   # 85% of the $129 offering price
        offering_price_fx=D(129),
        contributions_fx=D("5263.20"),
        included_in_form16=True,
    )]

    # ---- Dividends: quarterly, reinvested, sized from the position --------
    tr.dividend_schedules = [DividendSchedule(
        symbol="ACME", company_name="Acme Software Inc",
        currency="USD", frequency="quarterly",
        first_pay_date=date(2025, 5, 22), payments=4,
        dividend_per_share_fx=D("0.42"),
        record_date_lead_days=15,
        withholding_rate=D("0.25"),
        reinvested=True,
        declared_rates={"2025-08-21": "0.44", "2025-11-20": "0.46"},
    )]

    # An Indian holding, with TDS under section 194.
    tr.dividends = [DividendReceipt(
        symbol="INFY", company_name="Infosys Limited", currency="INR",
        record_date=date(2025, 6, 27), pay_date=date(2025, 7, 8),
        gross_amount_fx=D(31_500), dividend_per_share_fx=D(21),
        tds_deducted=D(3_150),
    )]
    tr.other_sources.dividend_interest_expense = D(90_000)

    # ---- Sales ------------------------------------------------------------
    tr.foreign_sales = [
        # The 2023 tranche — held over 24 months, so 12.5%.
        ForeignSale(symbol="ACME", sale_date=date(2026, 1, 19),
                    shares=D(41), price_per_share_fx=D(168), fees_fx=D("4.95")),
        # Part of the September 2025 tranche — four months, so slab rates.
        ForeignSale(symbol="ACME", sale_date=date(2026, 2, 11),
                    shares=D(25), price_per_share_fx=D("174.30"), fees_fx=D("4.95")),
    ]

    tr.foreign_holdings = [ForeignHolding(
        symbol="ACME", entity_name="Acme Software Inc",
        entity_address="1 Innovation Way, San Jose, California",
        entity_zip="95110", nature_of_entity="Listed company",
        broker_name="Demo Stock Plan Services",
        broker_address="200 Market Street, New York",
        broker_zip="10005", broker_account_number="XXXX-4417",
        peak_price_fx=D("176.40"), year_end_price_fx=D("163.90"),
        opening_shares=D(41), opening_value_inr=D(332_182),
    )]

    # ---- Indian equity ----------------------------------------------------
    tr.capital_gains = [
        CapitalGainItem(
            category="ltcg_112a", description="Nifty Index Fund — 1,200 units",
            purchase_date=date(2021, 8, 3), sale_date=date(2025, 10, 14),
            sale_consideration=D(742_000), cost_of_acquisition=D(455_000),
        ),
        CapitalGainItem(
            category="stcg_111a", description="HDFC Bank — 300 shares",
            purchase_date=date(2025, 5, 20), sale_date=date(2025, 11, 3),
            sale_consideration=D(528_000), cost_of_acquisition=D(474_000),
        ),
    ]

    # ---- House property, other income, deductions -------------------------
    tr.house_properties = [HouseProperty(
        property_type="SOP", address="Flat 7B, Whitefield, Bengaluru",
        interest_24b=D(286_000), ownership_share=D(1),
    )]

    tr.other_sources.savings_bank_interest = D(18_400)
    tr.other_sources.fixed_deposit_interest = D(96_500)

    tr.deductions.s80c = D(150_000)
    tr.deductions.s80ccd1b = D(50_000)
    tr.deductions.s80d_self = D(28_000)
    tr.deductions.s80d_parents = D(46_000)
    tr.deductions.s80e = D(64_000)
    tr.deductions.s80g_50pct_no_limit = D(25_000)

    # ---- Taxes already paid -----------------------------------------------
    tr.taxes_paid.payments = [
        TaxPayment(kind="tds_salary", deductor_name="Acme Software India",
                   deductor_tan="BLRA12345B", amount=D(1_186_000)),
        TaxPayment(kind="tds_other", deductor_name="Demo Bank",
                   deductor_tan="BLRD54321E", amount=D(9_650)),
        TaxPayment(kind="advance_tax", deductor_name="Self — advance tax",
                   amount=D(60_000), payment_date=date(2025, 12, 14),
                   bsr_code="0510308", challan_serial="04417"),
    ]

    tr.foreign_settings.forex_overrides = {
        month: {"USD": rate} for month, rate in DEMO_RATES.items()
    }
    tr.foreign_settings.form67_filed = True
    tr.foreign_settings.form67_ack = "67-DEMO-2026-0041"
    return tr


def build_planning_return() -> TaxReturn:
    """The year in progress, seen from December.

    The same person, but partway through the year and with the equity sold
    rather than held. Salary TDS covers the salary and nothing else, so the
    gains and dividends fall to advance tax — which is the situation the
    planner exists for, and the one where the proviso to section 234C matters.
    """
    tr = build_demo_return()
    tr.regime_choice = "new"
    tr.filing_date = None

    # A large sale in the December quarter, on top of the existing ones.
    tr.foreign_sales.append(ForeignSale(
        symbol="ACME", sale_date=date(2025, 12, 9),
        shares=D(60), price_per_share_fx=D("181.40"), fees_fx=D("4.95"),
    ))
    tr.capital_gains.append(CapitalGainItem(
        category="stcg_111a", description="Reliance Industries — 400 shares",
        purchase_date=date(2025, 7, 8), sale_date=date(2025, 12, 4),
        sale_consideration=D(1_310_000), cost_of_acquisition=D(1_046_000),
    ))

    # Only the salary has been withheld against, and no advance tax yet.
    tr.taxes_paid.payments = [TaxPayment(
        kind="tds_salary", deductor_name="Acme Software India",
        deductor_tan="BLRA12345B", amount=D(1_186_000),
    )]
    return tr


def main() -> str:
    init_db()
    session = get_session()
    try:
        filing = ReturnRecord(
            assessment_year="2026-27",
            label="Demo — Asha Ramanathan, filing AY 2026-27",
        )
        filing.save(build_demo_return())

        planning = ReturnRecord(
            assessment_year="2026-27",
            label="Demo — advance tax, FY 2025-26 in progress",
        )
        planning.save(build_planning_return())

        session.add_all([filing, planning])
        session.commit()
        print(filing.id)
        print(planning.id, file=sys.stderr)
        return filing.id
    finally:
        session.close()


if __name__ == "__main__":
    main()
