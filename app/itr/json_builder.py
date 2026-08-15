"""Build the ITR JSON that the e-filing portal's offline path accepts.

Direct submission needs an e-Return Intermediary licence — a registered entity,
a ₹1 crore net worth and a CISA due-diligence certificate — which no individual
is going to hold. So this module stops exactly where the offline utility stops:
a ``.json`` file that gets uploaded at e-File → Income Tax Returns → File
Income Tax Return → Offline / Import pre-filled data. The portal validates it,
shows a preview and takes the submission from there.

A caveat worth stating plainly: the department revises the JSON schema every
assessment year, and the revisions are not always documented before the utility
ships. The structure below follows the published schema, but the honest test is
whether the offline utility imports the file without complaint. That test is
one click, and ``README.md`` explains how to run it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..money import D, rupees
from ..schemas import TaxReturn
from ..tax.engine import Computation
from ..tax.rules import get_ay

# Bump these when the department publishes a new schema for the year.
SCHEMA_VERSIONS = {
    "2026-27": {"SchemaVer": "Ver1.0", "FormVer": "Ver1.0", "AssessmentYear": "2026"},
    "2025-26": {"SchemaVer": "Ver1.0", "FormVer": "Ver1.0", "AssessmentYear": "2025"},
}

SOFTWARE_ID = "PIITR0001"


def _i(value: Any) -> int:
    """The schema wants whole rupees as integers, not strings or decimals."""
    return int(rupees(D(value)))


def _split_name(full_name: str) -> Dict[str, str]:
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return {"SurNameOrOrgName": "", "FirstName": "", "MiddleName": ""}
    if len(parts) == 1:
        return {"SurNameOrOrgName": parts[0], "FirstName": "", "MiddleName": ""}
    return {
        "FirstName": parts[0],
        "MiddleName": " ".join(parts[1:-1]),
        "SurNameOrOrgName": parts[-1],
    }


def _creation_info(today: date) -> Dict[str, Any]:
    return {
        "SWVersionNo": "1.0",
        "SWCreatedBy": SOFTWARE_ID,
        "JSONCreatedBy": SOFTWARE_ID,
        "JSONCreationDate": today.isoformat(),
        "IntermediaryCity": "NA",
        "Digest": "-",
    }


def _filing_section(tr: TaxReturn, ay) -> int:
    """Section 139 sub-clause the return is filed under."""
    filing_date = tr.filing_date or date.today()
    if tr.is_revised:
        return 17          # 139(5) revised
    if filing_date > ay.due_date_non_audit:
        return 12          # 139(4) belated
    return 11              # 139(1) on or before the due date


def _personal_info(tr: TaxReturn) -> Dict[str, Any]:
    taxpayer = tr.taxpayer
    return {
        "AssesseeName": _split_name(taxpayer.name),
        "PAN": taxpayer.pan,
        "Address": {
            "ResidenceNo": "",
            "RoadOrStreet": taxpayer.address_line,
            "LocalityOrArea": taxpayer.city,
            "CityOrTownOrDistrict": taxpayer.city,
            "StateCode": taxpayer.state_code,
            "CountryCode": "91",
            "PinCode": int(taxpayer.pincode) if taxpayer.pincode.isdigit() else 0,
            "CountryCodeMobile": 91,
            "MobileNo": taxpayer.mobile,
            "EmailAddress": taxpayer.email,
        },
        "DOB": taxpayer.date_of_birth.isoformat() if taxpayer.date_of_birth else "",
        "EmployerCategory": (
            tr.salaries[0].employer_category if tr.salaries else "OTH"
        ),
        "AadhaarCardNo": "",
        "Status": "I",
    }


def _bank_details(tr: TaxReturn) -> Dict[str, Any]:
    accounts = tr.taxpayer.bank_accounts
    rows = []
    for account in accounts:
        rows.append({
            "IFSCCode": account.ifsc,
            "BankName": account.bank_name,
            "BankAccountNo": account.account_number,
            "UseForRefund": "true" if account.is_primary_refund_account else "false",
        })
    if rows and not any(r["UseForRefund"] == "true" for r in rows):
        rows[0]["UseForRefund"] = "true"
    return {"AddtnlBankDetails": rows} if rows else {}


def _verification(tr: TaxReturn, today: date) -> Dict[str, Any]:
    return {
        "Declaration": {
            "AssesseeVerName": tr.taxpayer.name,
            "FatherName": "",
            "AssesseeVerPAN": tr.taxpayer.pan,
        },
        "Capacity": "S",
        "Place": tr.taxpayer.city,
        "Date": today.isoformat(),
    }


def _tax_payments(tr: TaxReturn) -> Dict[str, Any]:
    """Challan-level detail for advance tax and self-assessment tax."""
    rows = []
    for payment in tr.taxes_paid.payments:
        if payment.kind not in ("advance_tax", "self_assessment"):
            continue
        rows.append({
            # Numbered over the challans themselves. Counting across every
            # payment left gaps in the sequence wherever a TDS row sat between
            # two challans, and the schema wants it unbroken.
            "SrNo": len(rows) + 1,
            "BSRCode": payment.bsr_code,
            "DateDep": (payment.payment_date or date.today()).isoformat(),
            "SrlNoOfChaln": int(payment.challan_serial)
            if payment.challan_serial.isdigit() else 0,
            "Amt": _i(payment.amount),
        })
    return {"TaxPayment": rows} if rows else {}


def _tds_schedules(tr: TaxReturn) -> Dict[str, Any]:
    """Schedule TDS1 (salary) and TDS2 (other than salary)."""
    salary_rows = []
    other_rows = []
    for index, payment in enumerate(tr.taxes_paid.payments, start=1):
        if payment.kind == "tds_salary":
            salary_rows.append({
                "EmployerOrDeductorOrCollectDetl": {
                    "TAN": payment.deductor_tan,
                    "EmployerOrDeductorOrCollecterName": payment.deductor_name,
                },
                "IncChrgSal": 0,
                "TotalTDSSal": _i(payment.amount),
            })
        elif payment.kind in ("tds_other", "tcs"):
            other_rows.append({
                "EmployerOrDeductorOrCollectDetl": {
                    "TAN": payment.deductor_tan,
                    "EmployerOrDeductorOrCollecterName": payment.deductor_name,
                },
                "TaxDeductCreditDtls": {
                    "TaxDeductedOwnHands": _i(payment.amount),
                    "TaxClaimedOwnHands": _i(payment.amount),
                },
            })

    out: Dict[str, Any] = {}
    if salary_rows:
        out["TDSonSalaries"] = {
            "TDSonSalary": salary_rows,
            "TotalTDSonSalaries": sum(r["TotalTDSSal"] for r in salary_rows),
        }
    if other_rows:
        out["TDSonOthThanSals"] = {
            "TDSonOthThanSal": other_rows,
            "TotalTDSonOthThanSals": sum(
                r["TaxDeductCreditDtls"]["TaxClaimedOwnHands"] for r in other_rows
            ),
        }
    return out


# --------------------------------------------------------------------------
# ITR-1
# --------------------------------------------------------------------------


def build_itr1(tr: TaxReturn, comp: Computation) -> Dict[str, Any]:
    ay = get_ay(tr.assessment_year)
    versions = SCHEMA_VERSIONS[tr.assessment_year]
    today = date.today()

    gross_salary = sum((s.gross_salary for s in tr.salaries), D(0))
    # What the engine allowed, not what was claimed. The new regime withdraws
    # some section 10 exemptions and the whole of section 16(iii), and
    # recomputing them here produced rows that did not add up to the head total
    # sitting a few keys below them.
    exempt_allowances = comp.salary_exempt_allowed
    professional_tax = comp.salary_section_16_other
    standard_deduction = comp.salary_standard_deduction

    house = tr.house_properties[0] if tr.house_properties else None
    other = tr.other_sources

    income_deductions: Dict[str, Any] = {
        "GrossSalary": _i(gross_salary),
        "Salary": _i(sum((s.salary_17_1 for s in tr.salaries), D(0))),
        "PerquisitesValue": _i(sum((s.perquisites_17_2 for s in tr.salaries), D(0))),
        "ProfitsInSalary": _i(sum((s.profits_in_lieu_17_3 for s in tr.salaries), D(0))),
        "AllwncExemptUs10": _i(exempt_allowances),
        "NetSalary": _i(gross_salary - exempt_allowances),
        "DeductionUs16": _i(standard_deduction + professional_tax),
        "DeductionUs16ia": _i(standard_deduction),
        "DeductionUs16iii": _i(professional_tax),
        "IncomeFromSal": _i(comp.salary),
        "TypeOfHP": (
            "S" if house and house.property_type == "SOP"
            else "L" if house else ""
        ),
        "IncomeFromHP": _i(comp.house_property),
        "IncomeOthSrc": _i(comp.other_sources),
        "GrossTotIncome": _i(comp.gross_total_income),
        "TotalIncome": _i(comp.total_income_rounded),
    }

    if house:
        gross_rent = house.annual_rent_received - house.unrealised_rent
        income_deductions.update({
            "GrossRentReceived": _i(gross_rent),
            "TaxPaidlocalAuth": _i(house.municipal_taxes_paid),
            "AnnualValue": _i(max(D(0), gross_rent - house.municipal_taxes_paid)),
            "StandardDeduction": _i(
                max(D(0), gross_rent - house.municipal_taxes_paid) * D("0.30")
            ),
            "InterestPayable": _i(house.interest_24b),
        })

    others: Dict[str, Any] = {}
    if other.savings_bank_interest:
        others["OthSrcSavingsBank"] = _i(other.savings_bank_interest)
    if other.fixed_deposit_interest:
        others["OthSrcDeposits"] = _i(other.fixed_deposit_interest)
    if other.dividend_income:
        others["OthSrcDividend"] = _i(other.dividend_income)
    if other.family_pension:
        others["FamilyPension"] = _i(other.family_pension)
    if others:
        income_deductions["OthersInc"] = others

    income_deductions["DeductUndChapVIA"] = _chapter_via_block(comp)
    income_deductions["UsrDeductUndChapVIA"] = _chapter_via_block(comp)

    tax_computation = {
        "TotalTaxPayable": _i(comp.tax_before_rebate),
        "Rebate87A": _i(comp.rebate_87a),
        "TaxPayableOnRebate": _i(comp.tax_after_rebate),
        "Surcharge": _i(comp.surcharge),
        "EducationCess": _i(comp.cess),
        "GrossTaxLiability": _i(
            comp.tax_after_rebate + comp.surcharge + comp.cess
        ),
        "Section89": _i(comp.relief_89),
        "NetTaxLiability": _i(comp.total_tax_liability),
        "TotalIntrstPay": _i(comp.interest.total),
        "IntrstPay": {
            "IntrstPayUs234A": _i(comp.interest.section_234a),
            "IntrstPayUs234B": _i(comp.interest.section_234b),
            "IntrstPayUs234C": _i(comp.interest.section_234c),
            "LateFilingFee234F": _i(comp.interest.section_234f),
        },
        "TotTaxPlusIntrstPay": _i(comp.total_tax_liability + comp.interest.total),
    }

    payload: Dict[str, Any] = {
        "ITR": {
            "ITR1": {
                "CreationInfo": _creation_info(today),
                "Form_ITR1": {
                    "FormName": "ITR-1",
                    "Description": "For individuals being a resident (other "
                                   "than not ordinarily resident) having total "
                                   "income upto Rs.50 lakh",
                    "AssessmentYear": versions["AssessmentYear"],
                    "SchemaVer": versions["SchemaVer"],
                    "FormVer": versions["FormVer"],
                },
                "PersonalInfo": _personal_info(tr),
                "FilingStatus": {
                    "ReturnFileSec": _filing_section(tr, ay),
                    "NewTaxRegime": "Y" if comp.regime == "new" else "N",
                    "SeventhProvisio139": "N",
                },
                "ITR1_IncomeDeductions": income_deductions,
                "ITR1_TaxComputation": tax_computation,
                "TaxPaid": {
                    "TaxesPaid": {
                        "TDS": _i(comp.tds),
                        "TCS": _i(comp.tcs),
                        "AdvanceTax": _i(comp.advance_tax),
                        "SelfAssessmentTax": _i(comp.self_assessment_tax),
                        "TotalTaxesPaid": _i(comp.total_taxes_paid),
                    },
                    "BalTaxPayable": _i(comp.net_payable),
                },
                "Refund": {
                    "RefundDue": _i(comp.refund_due),
                    **_bank_details(tr),
                },
                "Verification": _verification(tr, today),
                **_tds_schedules(tr),
                **_tax_payments(tr),
            }
        }
    }
    return payload


# --------------------------------------------------------------------------
# ITR-2
# --------------------------------------------------------------------------


def build_itr2(tr: TaxReturn, comp: Computation) -> Dict[str, Any]:
    ay = get_ay(tr.assessment_year)
    versions = SCHEMA_VERSIONS[tr.assessment_year]
    today = date.today()

    gross_salary = sum((s.gross_salary for s in tr.salaries), D(0))
    # What the engine allowed, not what was claimed. The new regime withdraws
    # some section 10 exemptions and the whole of section 16(iii), and
    # recomputing them here produced rows that did not add up to the head total
    # sitting a few keys below them.
    exempt_allowances = comp.salary_exempt_allowed
    professional_tax = comp.salary_section_16_other
    standard_deduction = comp.salary_standard_deduction

    schedule_s = {
        "TotalGrossSalary": _i(gross_salary),
        "AllwncExemptUs10": _i(exempt_allowances),
        "NetSalary": _i(gross_salary - exempt_allowances),
        "DeductionUnderSection16": _i(standard_deduction + professional_tax),
        "DeductionUs16ia": _i(standard_deduction),
        "TotIncUnderHeadSalaries": _i(comp.salary),
        "Salaries": [
            {
                "NameOfEmployer": s.employer_name,
                "TANofEmployer": s.employer_tan,
                "NatureOfEmployment": s.employer_category,
                "Salarys": {
                    "GrossSalary": _i(s.gross_salary),
                    "Salary": _i(s.salary_17_1),
                    "PerquisitesValue": _i(s.perquisites_17_2),
                    "ProfitsInLieuOfSalary": _i(s.profits_in_lieu_17_3),
                },
            }
            for s in tr.salaries
        ],
    }

    schedule_cg = _schedule_cg(tr, comp)
    schedule_si = _schedule_si(comp)

    part_b_ti = {
        "Salaries": _i(comp.salary),
        "IncomeFromHP": _i(comp.house_property),
        "CapGain": {
            "TotalCapGains": _i(comp.capital_gains),
            "ShortTerm": {
                "ShortTerm15Per": _i(_slice_income(comp, "stcg_111a")),
                # Foreign shares pay no securities transaction tax, so a
                # short-term gain on them is taxed at slab rates, not 20%.
                "ShortTermAppRate": _i(_slab_rate_gains(tr, comp)),
                "TotalShortTerm": _i(
                    _slice_income(comp, "stcg_111a") + _slab_rate_gains(tr, comp)
                ),
            },
            "LongTerm": {
                "LongTerm10Per": _i(_slice_income(comp, "ltcg_112a")),
                "LongTerm20Per": _i(
                    _slice_income(comp, "ltcg_112_property")
                    + _slice_income(comp, "ltcg_112_property_indexed")
                    + _slice_income(comp, "ltcg_112_other")
                    + _slice_income(comp, "ltcg_112_foreign")
                ),
                "TotalLongTerm": _i(
                    _slice_income(comp, "ltcg_112a")
                    + _slice_income(comp, "ltcg_112_property")
                    + _slice_income(comp, "ltcg_112_property_indexed")
                    + _slice_income(comp, "ltcg_112_other")
                    + _slice_income(comp, "ltcg_112_foreign")
                ),
            },
        },
        "IncFromOS": {"TotIncFromOS": _i(comp.other_sources)},
        "GrossTotalIncome": _i(comp.gross_total_income),
        "DeductionsUnderScheduleVIA": _i(comp.deductions_total),
        "TotalIncome": _i(comp.total_income_rounded),
        "AggregateIncome": _i(comp.total_income_rounded),
    }

    part_b_tti = {
        "ComputationOfTaxLiability": {
            "TaxPayableOnTI": {
                "TaxAtNormalRatesOnAggrInc": _i(comp.tax_on_normal_income),
                "TaxAtSpecialRates": _i(comp.tax_on_special_income),
                "RebateOnAgriInc": 0,
                "TaxPayableOnTotInc": _i(comp.tax_before_rebate),
            },
            "Rebate87A": _i(comp.rebate_87a),
            "TaxPayableOnRebate": _i(comp.tax_after_rebate),
            "Surcharge25ofSI": 0,
            "SurchargeOnAboveCrore": _i(comp.surcharge),
            "MarginalReliefOnSur": _i(comp.surcharge_marginal_relief),
            "EducationCess": _i(comp.cess),
            "GrossTaxLiability": _i(
                comp.tax_after_rebate + comp.surcharge + comp.cess
            ),
            "TaxRelief": {
                "Section89": _i(comp.relief_89),
                "Section90": _i(comp.relief_90_91),
                "Section91": 0,
                "TotTaxRelief": _i(comp.relief_89 + comp.relief_90_91),
            },
            "NetTaxLiability": _i(comp.total_tax_liability),
            "IntrstPay": {
                "IntrstPayUs234A": _i(comp.interest.section_234a),
                "IntrstPayUs234B": _i(comp.interest.section_234b),
                "IntrstPayUs234C": _i(comp.interest.section_234c),
                "LateFilingFee234F": _i(comp.interest.section_234f),
                "TotalIntrstPay": _i(comp.interest.total),
            },
            "AggregateTaxInterestLiability": _i(
                comp.total_tax_liability + comp.interest.total
            ),
        },
        "TaxPaid": {
            "TaxesPaid": {
                "TDS": _i(comp.tds),
                "TCS": _i(comp.tcs),
                "AdvanceTax": _i(comp.advance_tax),
                "SelfAssessmentTax": _i(comp.self_assessment_tax),
                "TotalTaxesPaid": _i(comp.total_taxes_paid),
            },
            "BalTaxPayable": _i(comp.net_payable),
        },
        "Refund": {
            "RefundDue": _i(comp.refund_due),
            **_bank_details(tr),
        },
    }

    payload = {
        "ITR": {
            "ITR2": {
                "CreationInfo": _creation_info(today),
                "Form_ITR2": {
                    "FormName": "ITR-2",
                    "Description": "For Individuals and HUFs not having income "
                                   "from profits and gains of business or "
                                   "profession",
                    "AssessmentYear": versions["AssessmentYear"],
                    "SchemaVer": versions["SchemaVer"],
                    "FormVer": versions["FormVer"],
                },
                "PartA_GEN1": {
                    "PersonalInfo": _personal_info(tr),
                    "FilingStatus": {
                        "ReturnFileSec": _filing_section(tr, ay),
                        "NewTaxRegime": "Y" if comp.regime == "new" else "N",
                        "SeventhProvisio139": "N",
                    },
                },
                "ScheduleS": schedule_s,
                "ScheduleHP": _schedule_hp(tr, comp),
                "ScheduleCGFor23": schedule_cg,
                "ScheduleOS": _schedule_os(tr, comp),
                "ScheduleVIA": _chapter_via_block(comp),
                "ScheduleSI": schedule_si,
                **_foreign_schedules(tr, comp),
                "PartB-TI": part_b_ti,
                "PartB_TTI": part_b_tti,
                "Verification": _verification(tr, today),
                **_tds_schedules(tr),
                **_tax_payments(tr),
            }
        }
    }
    return payload


def _slab_rate_gains(tr: TaxReturn, comp: Computation) -> Decimal:
    """Capital gains taxed at slab rates rather than at a special rate.

    Short-term gains on foreign shares and on non-STT assets sit inside normal
    income, so they never appear as a Schedule SI slice and have to be totalled
    from the source rows instead.
    """
    slab_codes = {"stcg_slab", "stcg_slab_foreign", "stcg_debt_mf"}
    return sum(
        (item.net_gain for item in tr.capital_gains
         if item.category in slab_codes and item.net_gain > 0),
        D(0),
    )


def _foreign_schedules(tr: TaxReturn, comp: Computation) -> Dict[str, Any]:
    """Schedule FA, Schedule FSI and Schedule TR.

    Schedule FA reports on the calendar year, so its figures deliberately do not
    tie to anything else in the return.
    """
    out: Dict[str, Any] = {}

    custodial = [row for row in tr.foreign_assets if row.table == "A2"]
    equity = [row for row in tr.foreign_assets if row.table == "A3"]

    if custodial or equity:
        schedule_fa: Dict[str, Any] = {}
        if custodial:
            schedule_fa["DetailsForeignCustodialAcc"] = [
                {
                    "CountryCodeExcludingIndia": row.country_code,
                    "CountryName": row.country_name,
                    "NameOfInstitution": row.entity_name,
                    "AddressOfInstitution": row.entity_address,
                    "ZipCode": row.entity_zip,
                    "AccountNumber": row.nature_of_interest,
                    "StatusOfAccount": "Owner",
                    "AccountOpeningDate": row.date_acquired.isoformat()
                    if row.date_acquired else "",
                    "PeakBalanceDuringPeriod": _i(row.peak_value),
                    "ClosingBalance": _i(row.closing_value),
                    "GrossInterestPaidCredited": _i(row.gross_income_accrued),
                    "NatureOfAmount": row.nature_of_income,
                    "AmountOfIncomeTaxableAndOfferedInThisReturn":
                        _i(row.income_offered_amount),
                    "ScheduleWhereOffered": row.income_offered_schedule,
                }
                for row in custodial
            ]
        if equity:
            schedule_fa["DetailsForeignEquityDebtInterest"] = [
                {
                    "CountryCodeExcludingIndia": row.country_code,
                    "CountryName": row.country_name,
                    "NameOfEntity": row.entity_name,
                    "AddressOfEntity": row.entity_address,
                    "ZipCode": row.entity_zip,
                    "NatureOfEntity": row.nature_of_entity,
                    "InterestAcquiringDate": row.date_acquired.isoformat()
                    if row.date_acquired else "",
                    "InitialValOfInvstmnt": _i(row.initial_investment),
                    "PeakBalanceDuringPeriod": _i(row.peak_value),
                    "ClosingBalance": _i(row.closing_value),
                    "TotGrossAmtPaidCredited": _i(row.gross_income_accrued),
                    "NatureOfAmount": row.nature_of_income,
                    "IncTaxableAndOfferedInThisReturn":
                        _i(row.income_offered_amount),
                    "ScheduleWhereOffered": row.income_offered_schedule,
                }
                for row in equity
            ]
        out["ScheduleFA"] = schedule_fa

    ftc = getattr(comp, "ftc", None)
    if ftc and getattr(ftc, "lines", None):
        by_country: Dict[str, Dict[str, Any]] = {}
        for line in ftc.lines:
            entry = by_country.setdefault(line.country_code, {
                "CountryCodeExcludingIndia": line.country_code,
                "TaxpayerIdentificationNo": "",
                "IncFromOS": {
                    "IncFrmOutsideInd": 0, "TaxPaidOutsideInd": 0,
                    "TaxPayableinInd": 0, "TaxReliefinInd": 0,
                    "TaxReliefOutsideIndiaSec": "90",
                },
            })
            head = entry["IncFromOS"]
            head["IncFrmOutsideInd"] += _i(line.income_inr)
            head["TaxPaidOutsideInd"] += _i(line.foreign_tax_inr)
            head["TaxPayableinInd"] += _i(line.indian_tax_on_income)
            head["TaxReliefinInd"] += _i(line.credit_allowed)

        out["ScheduleFSI"] = {"ScheduleFSIDtls": list(by_country.values())}
        out["ScheduleTR1"] = {
            "ScheduleTR": [
                {
                    "CountryCodeExcludingIndia": code,
                    "TaxPaidOutsideIndia": entry["IncFromOS"]["TaxPaidOutsideInd"],
                    "TaxReliefOutsideIndia": entry["IncFromOS"]["TaxReliefinInd"],
                    "TaxReliefOutsideIndiaSec": "90",
                }
                for code, entry in by_country.items()
            ],
            "TotalTaxPaidOutsideIndia": _i(
                sum(line.foreign_tax_inr for line in ftc.lines)
            ),
            "TotalTaxReliefOutsideIndia": _i(ftc.total_credit),
            "TaxReliefOutsideIndiaDTAA": _i(ftc.total_credit),
            "TaxReliefOutsideIndiaNotDTAA": 0,
        }
    return out


def _slice_income(comp: Computation, code: str) -> Decimal:
    for slice_ in comp.special_slices:
        if slice_.code == code:
            return slice_.income
    return D(0)


def _schedule_cg(tr: TaxReturn, comp: Computation) -> Dict[str, Any]:
    """Schedule CG, itemised where the department wants scrip-level detail."""
    equity_ltcg = [i for i in tr.capital_gains if i.category == "ltcg_112a"]
    rows = []
    for item in equity_ltcg:
        rows.append({
            "ShareOnWhichSTTPaid": {
                "FullConsideration": _i(item.sale_consideration),
                "CostOfAcquisition": _i(item.cost_of_acquisition),
                "FairMarketValue": _i(item.fmv_31jan2018 or 0),
                "ExpenditureWholly": _i(item.transfer_expenses),
                "Balance": _i(item.net_gain),
            }
        })
    return {
        "ShortTermCapGainFor23": {
            "EquityMFonSTT": [{
                "MFSectionCode": "1A",
                "FullConsideration": _i(sum(
                    (i.sale_consideration for i in tr.capital_gains
                     if i.category == "stcg_111a"), D(0)
                )),
                "CapgainonAssets": _i(_slice_income(comp, "stcg_111a")),
            }],
            # Slab-rate short-term gains never become a Schedule SI slice, so
            # asking _slice_income for them always returned nil and the total
            # silently dropped every foreign and non-STT short-term gain.
            "TotalSTCG": _i(
                _slice_income(comp, "stcg_111a") + _slab_rate_gains(tr, comp)
            ),
        },
        "LongTermCapGain23": {
            "SaleOfEquityShareUs112A": {
                "LTCGWithoutBenefit112A": _i(_slice_income(comp, "ltcg_112a")),
                "DeductionUs112A": _i(
                    next((s.statutory_exemption for s in comp.special_slices
                          if s.code == "ltcg_112a"), D(0))
                ),
                "ScripWiseDetail": rows,
            },
            # ltcg_112_foreign belongs here too — Part B-TI already counts it,
            # and leaving it out made Schedule CG disagree with the total it
            # feeds.
            "TotalLTCG": _i(
                _slice_income(comp, "ltcg_112a")
                + _slice_income(comp, "ltcg_112_property")
                + _slice_income(comp, "ltcg_112_property_indexed")
                + _slice_income(comp, "ltcg_112_other")
                + _slice_income(comp, "ltcg_112_foreign")
            ),
        },
        "SumOfCGIncm": _i(comp.capital_gains),
    }


def _schedule_hp(tr: TaxReturn, comp: Computation) -> Dict[str, Any]:
    rows = []
    for index, prop in enumerate(tr.house_properties, start=1):
        gross_rent = prop.annual_rent_received - prop.unrealised_rent
        annual_value = max(D(0), gross_rent - prop.municipal_taxes_paid)
        rows.append({
            "AddressDetailWithZipCode": {"AddrDetail": prop.address},
            "PropertyOwner": "SF",
            "PropertyType": prop.property_type,
            "Rent": _i(gross_rent),
            "LocalTaxes": _i(prop.municipal_taxes_paid),
            "AnnualValue": _i(annual_value),
            "ThirtyPercentOfAnnual": _i(annual_value * D("0.30")),
            "InterestPayable": _i(prop.interest_24b + prop.pre_construction_interest),
            "TotalIncomeOfHP": _i(
                annual_value - annual_value * D("0.30")
                - prop.interest_24b - prop.pre_construction_interest
            ),
        })
    return {"PropertyDetails": rows, "TotalIncomeChargeableUnHP": _i(comp.house_property)}


def _schedule_os(tr: TaxReturn, comp: Computation) -> Dict[str, Any]:
    other = tr.other_sources
    return {
        "IncOthThanOwnRaceHorse": {
            "InterestGross": _i(
                other.savings_bank_interest + other.fixed_deposit_interest
                + other.other_interest + other.income_tax_refund_interest
            ),
            "IntrstFrmSavingBank": _i(other.savings_bank_interest),
            "IntrstFrmTermDeposit": _i(other.fixed_deposit_interest),
            "IntrstFrmIncmTaxRefund": _i(other.income_tax_refund_interest),
            # Both lines are dividend income under the same head. Reporting
            # only the Indian one leaves the itemised rows short of
            # GrossIncChargeable by the whole foreign amount, which is exactly
            # the mismatch the portal's own validation looks for.
            "DividendGross": _i(
                other.dividend_income + other.foreign_dividend_income
            ),
            "FamilyPension": _i(other.family_pension),
            "AnyOtherIncome": _i(other.other_income + other.gifts_taxable),
            "GrossIncChargeable": _i(comp.other_sources),
        },
        "IncChargeable": _i(comp.other_sources),
        "IncFromLottery": _i(other.winnings_115bb),
    }


def _schedule_si(comp: Computation) -> Dict[str, Any]:
    """Schedule SI — income chargeable at special rates."""
    code_map = {
        "stcg_111a": "1A",
        "ltcg_112a": "22",
        "ltcg_112_property": "21",
        "ltcg_112_property_indexed": "21ci",
        "ltcg_112_other": "21",
        "ltcg_112_foreign": "21",
        "winnings_115bb": "5BB",
    }
    rows = []
    for slice_ in comp.special_slices:
        if slice_.chargeable <= 0:
            continue
        rows.append({
            "SecCode": code_map.get(slice_.code, "OTH"),
            "SplRatePercent": float(slice_.rate * 100),
            "SplRateInc": _i(slice_.chargeable),
            "SplRateIncTax": _i(slice_.tax),
        })
    return {
        "SplCodeRateTax": rows,
        "TotSplRateInc": _i(sum((s.chargeable for s in comp.special_slices), D(0))),
        "TotSplRateIncTax": _i(comp.tax_on_special_income),
    }


def _chapter_via_block(comp: Computation) -> Dict[str, Any]:
    """Map the allowed deductions onto the schema's section keys."""
    key_map = {
        "80C": "Section80C",
        "80CCD1B": "Section80CCDEmployeeOrSE",
        "80CCD2": "Section80CCDEmployer",
        "80D": "Section80D",
        "80DD": "Section80DD",
        "80DDB": "Section80DDB",
        "80E": "Section80E",
        "80EE": "Section80EE",
        "80EEA": "Section80EEA",
        "80EEB": "Section80EEB",
        "80G": "Section80G",
        "80GG": "Section80GG",
        "80GGA": "Section80GGA",
        "80GGC": "Section80GGC",
        "80TTA": "Section80TTA",
        "80TTB": "Section80TTB",
        "80U": "Section80U",
        "80JJAA": "Section80JJAA",
        "80CCH": "Section80CCH",
    }
    block: Dict[str, Any] = {}
    if comp.deduction_detail:
        for line in comp.deduction_detail.lines:
            key = key_map.get(line.section)
            if key and line.allowed > 0:
                block[key] = _i(line.allowed)
    block["TotalChapVIADeductions"] = _i(comp.deductions_total)
    return block


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build(tr: TaxReturn, comp: Computation, form: str) -> Dict[str, Any]:
    if form == "ITR-1":
        return build_itr1(tr, comp)
    if form == "ITR-2":
        return build_itr2(tr, comp)
    raise ValueError(
        f"{form} JSON generation is not implemented. Use the computation sheet "
        "and the filing pack with the department's offline utility."
    )


def to_json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
