"""The canonical shape of a tax return.

This is the single contract between the three halves of the system: parsers
write into it, the tax engine reads from it, and the ITR JSON builder maps it
onto the department's schema. Nothing else is allowed to invent a field name.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .money import D

Money = Decimal


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_money(cls, value, info):
        """Accept "1,50,000" / "₹1.5" / None wherever a Decimal is expected."""
        annotation = cls.model_fields[info.field_name].annotation
        if annotation in (Decimal, Optional[Decimal]) and not isinstance(value, Decimal):
            if value is None:
                return None if annotation == Optional[Decimal] else D(0)
            return D(value)
        return value


# --------------------------------------------------------------------------
# Who is filing
# --------------------------------------------------------------------------


class BankAccount(_Base):
    ifsc: str = ""
    bank_name: str = ""
    account_number: str = ""
    account_type: Literal["SB", "CA", "OD", "CC", "NRO", "OTH"] = "SB"
    is_primary_refund_account: bool = False


class Taxpayer(_Base):
    name: str = ""
    pan: str = ""
    date_of_birth: Optional[date] = None
    aadhaar_last_four: str = ""
    email: str = ""
    mobile: str = ""
    address_line: str = ""
    city: str = ""
    state_code: str = ""
    pincode: str = ""
    residential_status: Literal["RES", "NRI", "RNOR"] = "RES"
    # Section 139(1) filing trigger flags — drives the "why must I file" panel.
    has_foreign_assets: bool = False
    is_company_director: bool = False
    holds_unlisted_equity: bool = False
    bank_accounts: List[BankAccount] = Field(default_factory=list)

    @field_validator("pan")
    @classmethod
    def _upper_pan(cls, value: str) -> str:
        return (value or "").strip().upper()


# --------------------------------------------------------------------------
# Heads of income
# --------------------------------------------------------------------------


class SalaryIncome(_Base):
    """One employer. A mid-year job change means two of these."""

    employer_name: str = ""
    employer_tan: str = ""
    employer_category: Literal["GOV", "PSU", "PE", "OTH"] = "OTH"
    salary_17_1: Money = D(0)          # Salary as per section 17(1)
    perquisites_17_2: Money = D(0)     # Value of perquisites
    profits_in_lieu_17_3: Money = D(0)
    # Section 10 exemptions (HRA, LTA, gratuity, leave encashment, and so on).
    # Only claimable in full under the old regime; the engine filters.
    exempt_allowances: Dict[str, Money] = Field(default_factory=dict)
    professional_tax: Money = D(0)      # Section 16(iii)
    entertainment_allowance: Money = D(0)  # Section 16(ii), government only
    tds_deducted: Money = D(0)
    # Employer's NPS contribution — 80CCD(2), allowed in both regimes.
    employer_nps_contribution: Money = D(0)

    @property
    def gross_salary(self) -> Money:
        return self.salary_17_1 + self.perquisites_17_2 + self.profits_in_lieu_17_3

    @property
    def total_exempt(self) -> Money:
        return sum(self.exempt_allowances.values(), D(0))


class HouseProperty(_Base):
    property_type: Literal["SOP", "LOP", "DLOP"] = "SOP"
    address: str = ""
    ownership_share: Money = D(1)       # 1 = sole owner, 0.5 = half share
    annual_rent_received: Money = D(0)
    unrealised_rent: Money = D(0)
    municipal_taxes_paid: Money = D(0)
    interest_24b: Money = D(0)          # Interest on borrowed capital
    pre_construction_interest: Money = D(0)  # 1/5th instalment for the year
    tenant_name: str = ""
    tenant_pan: str = ""


class CapitalGainItem(_Base):
    """One scrip, one property, one lot — whatever the source document reports."""

    category: str = "ltcg_112a"          # key into rules.capital_gains
    description: str = ""
    sale_date: Optional[date] = None
    purchase_date: Optional[date] = None
    sale_consideration: Money = D(0)
    cost_of_acquisition: Money = D(0)
    cost_of_improvement: Money = D(0)
    transfer_expenses: Money = D(0)
    # Section 112A grandfathering: highest quoted price on 31 Jan 2018.
    fmv_31jan2018: Optional[Money] = None
    # Only used for the 20%-with-indexation option on pre-23-Jul-2024 property.
    indexed_cost_of_acquisition: Optional[Money] = None
    purchase_fy: str = ""
    # Section 54/54F/54EC exemption claimed against this item.
    exemption_section: str = ""
    exemption_amount: Money = D(0)
    source_document: str = ""

    # -- Foreign holdings ------------------------------------------------
    # Amounts above are always INR. These record what the trade actually
    # looked like in the foreign currency, so the computation sheet can show
    # the Rule 115 working rather than an unexplained rupee figure.
    is_foreign: bool = False
    currency: str = "INR"
    sale_consideration_fx: Money = D(0)
    cost_of_acquisition_fx: Money = D(0)
    forex_rate_sale: Optional[Money] = None
    forex_rate_cost: Optional[Money] = None
    # Tax withheld abroad on this transfer, in the foreign currency.
    foreign_tax_paid_fx: Money = D(0)
    foreign_tax_paid: Money = D(0)
    country_code: str = ""
    # Set when the lot came from a vesting or a dividend reinvestment, so the
    # cost basis can be traced back to what was already taxed.
    lot_origin: Literal["purchase", "rsu_vest", "espp", "dividend_reinvest"] = "purchase"

    @property
    def net_gain(self) -> Money:
        """Gain before any section 54 exemption, using grandfathered cost."""
        cost = self.cost_of_acquisition
        if self.category == "ltcg_112a" and self.fmv_31jan2018 is not None:
            # Cost = higher of actual cost and lower of (FMV, sale value).
            grandfathered = min(self.fmv_31jan2018, self.sale_consideration)
            cost = max(cost, grandfathered)
        return (
            self.sale_consideration
            - cost
            - self.cost_of_improvement
            - self.transfer_expenses
        )


class BusinessIncome(_Base):
    """Presumptive taxation only — full books of account are out of scope."""

    scheme: Literal["none", "44AD", "44ADA", "44AE"] = "none"
    nature_of_business_code: str = ""
    trade_name: str = ""
    gross_turnover_digital: Money = D(0)   # 6% presumption under 44AD
    gross_turnover_cash: Money = D(0)      # 8% presumption under 44AD
    gross_receipts_44ada: Money = D(0)     # 50% presumption
    declared_income_44ae: Money = D(0)
    # A taxpayer may declare higher than the presumption.
    higher_declared_income: Optional[Money] = None


class OtherSourcesIncome(_Base):
    savings_bank_interest: Money = D(0)
    fixed_deposit_interest: Money = D(0)
    other_interest: Money = D(0)           # bonds, P2P, EPF taxable interest
    income_tax_refund_interest: Money = D(0)
    dividend_income: Money = D(0)
    # Foreign dividends are taxed at slab rates like any other dividend, but
    # they are tracked apart because they drive Schedule FSI, the foreign tax
    # credit and Form 67. Both lines land in the same head.
    foreign_dividend_income: Money = D(0)
    family_pension: Money = D(0)
    winnings_115bb: Money = D(0)           # flat 30%, no deduction, no rebate
    gifts_taxable: Money = D(0)
    other_income: Money = D(0)
    # Section 57 expenses against other-sources income (old regime only,
    # except the family-pension deduction which the engine handles separately).
    section_57_deductions: Money = D(0)


class ExemptIncome(_Base):
    ppf_interest: Money = D(0)
    epf_withdrawal_exempt: Money = D(0)
    agricultural_income: Money = D(0)
    dividend_exempt: Money = D(0)
    other_exempt: Money = D(0)

    @property
    def total(self) -> Money:
        return (
            self.ppf_interest + self.epf_withdrawal_exempt
            + self.agricultural_income + self.dividend_exempt + self.other_exempt
        )


# --------------------------------------------------------------------------
# Foreign equity: RSUs, ESPP and dividends
# --------------------------------------------------------------------------


class RSUVest(_Base):
    """One vesting tranche.

    Vesting is a salary event, not a capital-gains one. Section 17(2)(vi) makes
    the fair market value on the vesting date a perquisite, taxed at slab rates
    and subject to TDS under section 192. Section 49(2AA) then makes that same
    value the cost of acquisition when the shares are eventually sold, which is
    what stops it being taxed twice.
    """

    symbol: str = ""
    company_name: str = ""
    grant_id: str = ""
    grant_date: Optional[date] = None
    vest_date: Optional[date] = None
    shares_vested: Money = D(0)
    # Fair market value per share on the vesting date, in the foreign currency.
    fmv_per_share_fx: Money = D(0)
    currency: str = "USD"
    country_code: str = "2"          # United States, in the ITR country list
    # Shares the broker sold on the vest date to fund the withholding.
    shares_sold_to_cover: Money = D(0)
    sale_price_per_share_fx: Money = D(0)
    # Tax withheld abroad at vesting. Usually nil for an Indian resident, whose
    # employer withholds Indian TDS instead — but it happens on cross-border
    # assignments, and if it does it is creditable.
    foreign_tax_withheld_fx: Money = D(0)
    # Overrides the Rule 115 lookup when the user knows the exact rate.
    forex_rate_override: Optional[Money] = None
    # Set once the employer's Form 16 is known to already include this vest, so
    # the perquisite is not added to salary a second time.
    included_in_form16: bool = True
    source_document: str = ""
    # A vest that has not happened yet, generated from a vesting schedule. It
    # feeds the advance-tax planner and never the return itself.
    is_projected: bool = False
    grant_reference: str = ""

    @property
    def gross_value_fx(self) -> Money:
        return self.shares_vested * self.fmv_per_share_fx

    @property
    def shares_retained(self) -> Money:
        return self.shares_vested - self.shares_sold_to_cover


class DividendReceipt(_Base):
    """One dividend payment, reinvested or taken in cash.

    Reinvestment changes nothing about the tax: the dividend is income in the
    year it was declared, whether or not a rupee reached the bank. What it does
    change is the cost basis — the reinvested amount buys a new lot, with its
    own acquisition date and its own holding period. Treating a reinvested
    dividend as a non-event is the commonest error in this whole area, and it
    understates income now and overstates the gain later.
    """

    symbol: str = ""
    company_name: str = ""
    pay_date: Optional[date] = None
    gross_amount_fx: Money = D(0)
    foreign_tax_withheld_fx: Money = D(0)
    currency: str = "USD"
    country_code: str = "2"
    # Dividend reinvestment: the payment bought more shares instead of cash.
    is_reinvested: bool = False
    shares_acquired: Money = D(0)
    reinvest_price_per_share_fx: Money = D(0)
    forex_rate_override: Optional[Money] = None
    source_document: str = ""

    @property
    def net_amount_fx(self) -> Money:
        return self.gross_amount_fx - self.foreign_tax_withheld_fx


class ForeignAsset(_Base):
    """One Schedule FA row.

    Schedule FA runs on the *calendar* year of the foreign jurisdiction, not
    the Indian financial year. For AY 2026-27 that means 1 January to
    31 December 2025. Everyone gets this wrong at least once.

    It is also not optional and not de-minimis: a resident and ordinarily
    resident who held any foreign asset at any point in that calendar year must
    report it, whatever it was worth and whether or not it produced income.
    Omitting one attracts a flat ₹10 lakh penalty under section 42 or 43 of the
    Black Money Act, which is a far larger number than any tax at stake.
    """

    table: Literal["A1", "A2", "A3", "A4", "D", "E", "F"] = "A3"
    country_code: str = "2"
    country_name: str = "United States of America"
    entity_name: str = ""
    entity_address: str = ""
    entity_zip: str = ""
    nature_of_entity: str = ""
    nature_of_interest: str = "Direct"
    date_acquired: Optional[date] = None
    # All four in INR, per the Schedule FA columns.
    initial_investment: Money = D(0)
    peak_value: Money = D(0)
    closing_value: Money = D(0)
    gross_income_accrued: Money = D(0)
    nature_of_income: str = ""
    income_offered_amount: Money = D(0)
    income_offered_schedule: str = ""
    calendar_year: str = ""
    source_document: str = ""


class ForeignTaxPayment(_Base):
    """Tax paid abroad, for section 90 relief and Form 67."""

    country_code: str = "2"
    country_name: str = "United States of America"
    # Which head the doubly-taxed income sits under.
    income_head: Literal["salary", "other_sources", "capital_gains"] = "other_sources"
    nature_of_income: str = "Dividend"
    income_fx: Money = D(0)
    income_inr: Money = D(0)
    tax_paid_fx: Money = D(0)
    tax_paid_inr: Money = D(0)
    currency: str = "USD"
    # Article of the treaty the relief is claimed under — Article 10 for
    # dividends under the India-US treaty, which caps withholding at 25%.
    treaty_article: str = "10"
    relief_section: Literal["90", "91"] = "90"
    source_document: str = ""


class ForeignSale(_Base):
    """A disposal of foreign shares, before it has been matched to a lot."""

    symbol: str = ""
    sale_date: Optional[date] = None
    shares: Money = D(0)
    price_per_share_fx: Money = D(0)
    fees_fx: Money = D(0)
    currency: str = "USD"
    country_code: str = "2"
    source_document: str = ""
    # A sale you intend to make. Used for advance-tax planning only.
    is_projected: bool = False


class VestingSchedule(_Base):
    """A grant that vests in tranches, expanded into individual vests.

    A four-year grant vesting quarterly is sixteen separate salary events, each
    at its own fair market value and its own Rule 115 exchange rate, and each
    starting its own 24-month capital-gains clock. Entering sixteen rows by hand
    every year is how mistakes get in.
    """

    grant_id: str = ""
    symbol: str = ""
    company_name: str = ""
    grant_date: Optional[date] = None
    total_shares: Money = D(0)
    frequency: Literal["monthly", "quarterly", "semiannual", "annual"] = "quarterly"
    first_vest_date: Optional[date] = None
    tranches: int = 16
    # A cliff tranche vests a larger slice on the first date — typically 25% of
    # a four-year grant after twelve months.
    cliff_shares: Money = D(0)
    currency: str = "USD"
    country_code: str = "2"
    # Used for tranches with no actual price on file. Projections only.
    estimated_fmv_per_share_fx: Money = D(0)
    # Actual fair market value per vest date, keyed "YYYY-MM-DD". Anything here
    # makes that tranche real rather than projected.
    actual_fmv: Dict[str, Money] = Field(default_factory=dict)
    # Fraction of each tranche the broker sells to fund withholding.
    sell_to_cover_fraction: Money = D("0.31")
    included_in_form16: bool = True


class ForeignHolding(_Base):
    """Per-symbol facts Schedule FA needs and a trade file cannot supply.

    The entity's registered address and the year's peak value are not in any
    broker export, so they have to be stated once per holding and reused.
    """

    symbol: str = ""
    entity_name: str = ""
    entity_address: str = ""
    entity_zip: str = ""
    nature_of_entity: str = "Listed company"
    country_code: str = "2"
    country_name: str = "United States of America"
    # The custodial account the shares sit in — Schedule FA Table A2.
    broker_name: str = ""
    broker_address: str = ""
    broker_zip: str = ""
    broker_account_number: str = ""
    # Prices needed for the calendar-year valuation, in the foreign currency.
    peak_price_fx: Money = D(0)          # highest during the calendar year
    year_end_price_fx: Money = D(0)      # closing, 31 December
    # Position carried in from before the calendar year began.
    opening_shares: Money = D(0)
    opening_value_inr: Money = D(0)


class ForeignSettings(_Base):
    """How the foreign schedules should be computed."""

    # SBI TT buying rates the user has verified, keyed by the month the rate is
    # quoted for: {"2025-08": {"USD": "88.4213"}}.
    forex_overrides: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    # Which lot leaves first when shares are sold. FIFO is the default and the
    # only method the department has ever accepted without argument.
    lot_matching: Literal["fifo", "specific"] = "fifo"
    # Form 67 has to be on the portal before the return is filed, or the credit
    # is liable to be denied by CPC.
    form67_filed: bool = False
    form67_ack: str = ""


class BroughtForwardLoss(_Base):
    assessment_year: str = ""
    house_property_loss: Money = D(0)
    business_loss: Money = D(0)
    stcl: Money = D(0)
    ltcl: Money = D(0)


# --------------------------------------------------------------------------
# Deductions and taxes paid
# --------------------------------------------------------------------------


class Deductions(_Base):
    """Chapter VI-A claims, before any statutory ceiling is applied.

    The engine caps these; the user enters what they actually invested or paid.
    """

    s80c: Money = D(0)
    s80ccc: Money = D(0)
    s80ccd1: Money = D(0)          # Employee's own NPS, within the 80C ceiling
    s80ccd1b: Money = D(0)         # Additional NPS, ₹50,000
    s80ccd2: Money = D(0)          # Employer NPS — allowed in the new regime too
    s80d_self: Money = D(0)
    s80d_parents: Money = D(0)
    s80d_preventive: Money = D(0)
    s80dd: Money = D(0)
    s80dd_severe: bool = False
    s80ddb: Money = D(0)
    s80e: Money = D(0)             # Education loan interest, no ceiling
    s80ee: Money = D(0)
    s80eea: Money = D(0)
    s80eeb: Money = D(0)
    s80g_100pct_no_limit: Money = D(0)
    s80g_50pct_no_limit: Money = D(0)
    s80g_100pct_with_limit: Money = D(0)
    s80g_50pct_with_limit: Money = D(0)
    s80gg: Money = D(0)            # Rent paid, when no HRA is received
    s80gga: Money = D(0)
    s80ggc: Money = D(0)
    s80tta: Money = D(0)
    s80ttb: Money = D(0)
    s80u: Money = D(0)
    s80u_severe: bool = False
    s80jjaa: Money = D(0)
    s80cch: Money = D(0)           # Agnipath Scheme
    # Inputs needed to compute the 80GG ceiling.
    rent_paid_annual: Money = D(0)


class TaxPayment(_Base):
    """One challan or one TDS entry."""

    kind: Literal["tds_salary", "tds_other", "tcs", "advance_tax", "self_assessment"]
    deductor_name: str = ""
    deductor_tan: str = ""
    amount: Money = D(0)
    payment_date: Optional[date] = None
    bsr_code: str = ""
    challan_serial: str = ""
    source_document: str = ""


class TaxesPaid(_Base):
    payments: List[TaxPayment] = Field(default_factory=list)

    def total(self, *kinds: str) -> Money:
        wanted = set(kinds) if kinds else None
        return sum(
            (p.amount for p in self.payments if wanted is None or p.kind in wanted),
            D(0),
        )


# --------------------------------------------------------------------------
# The whole return
# --------------------------------------------------------------------------


class TaxReturn(_Base):
    assessment_year: str = "2026-27"
    regime_choice: Literal["auto", "new", "old"] = "auto"
    filing_date: Optional[date] = None
    is_revised: bool = False
    original_ack_number: str = ""
    has_business_income: bool = False

    taxpayer: Taxpayer = Field(default_factory=Taxpayer)
    salaries: List[SalaryIncome] = Field(default_factory=list)
    house_properties: List[HouseProperty] = Field(default_factory=list)
    capital_gains: List[CapitalGainItem] = Field(default_factory=list)
    business: BusinessIncome = Field(default_factory=BusinessIncome)
    other_sources: OtherSourcesIncome = Field(default_factory=OtherSourcesIncome)
    exempt_income: ExemptIncome = Field(default_factory=ExemptIncome)
    brought_forward_losses: List[BroughtForwardLoss] = Field(default_factory=list)
    deductions: Deductions = Field(default_factory=Deductions)
    taxes_paid: TaxesPaid = Field(default_factory=TaxesPaid)

    # -- Foreign equity ----------------------------------------------------
    rsu_vests: List[RSUVest] = Field(default_factory=list)
    vesting_schedules: List[VestingSchedule] = Field(default_factory=list)
    dividends: List[DividendReceipt] = Field(default_factory=list)
    foreign_assets: List[ForeignAsset] = Field(default_factory=list)
    foreign_taxes: List[ForeignTaxPayment] = Field(default_factory=list)
    foreign_holdings: List[ForeignHolding] = Field(default_factory=list)
    foreign_sales: List["ForeignSale"] = Field(default_factory=list)
    foreign_settings: ForeignSettings = Field(default_factory=ForeignSettings)

    # Free-form notes the parsers leave for the user.
    notes: List[str] = Field(default_factory=list)
