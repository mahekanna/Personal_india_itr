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

    # Free-form notes the parsers leave for the user.
    notes: List[str] = Field(default_factory=list)
