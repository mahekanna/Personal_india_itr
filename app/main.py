"""The web application.

A six-step wizard — upload documents, review what was extracted, fill the gaps,
work through the foreign income, compare the two regimes, generate the filing
pack — plus the advance-tax planner, which sits outside the wizard because it
looks forward at the year in progress rather than back at the one being filed.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .db import DocumentRecord, ReturnRecord, get_or_create_return, get_session, init_db, list_returns
from .itr import filing_pack as pack_builder
from .itr import json_builder
from .itr.selector import select_form
from .merge import apply_extractions, replace_tds_from_26as
from .money import D, inr
from .parsers.base import Extraction, Fact
from .parsers.registry import DOCUMENT_LABELS, parse_document
from .reconcile import reconcile
from .planner import build_planner, record_payment_hint
from .schemas import (
    BankAccount,
    CapitalGainItem,
    DividendReceipt,
    ForeignHolding,
    ForeignSale,
    HouseProperty,
    RSUVest,
    SalaryIncome,
    VestingSchedule,
    TaxPayment,
    TaxReturn,
)
from .tax.engine import compare_regimes, compute
from .tax.rules import ASSESSMENT_YEARS, CURRENT_AY, get_ay

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Personal India ITR",
    description="Pre-preparation and filing-pack generation for Indian income "
                "tax returns.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["inr"] = inr
# Jinja has no built-in ``startswith`` test, and the review screen needs one to
# separate cross-check facts from the ones that get written into the return.
templates.env.tests["startswith"] = lambda value, prefix: str(value).startswith(prefix)


def render(name: str, context: Dict[str, Any]):
    request = context.pop("request")
    return templates.TemplateResponse(request, name, context)


# Created at import time rather than on a startup event, so that the tables
# exist however the app is launched — uvicorn, a test client, or an embedding.
init_db()


def db_session() -> Session:
    session = get_session()
    try:
        yield session
    finally:
        session.close()


def _load(session: Session, return_id: str) -> ReturnRecord:
    record = session.get(ReturnRecord, return_id)
    if record is None:
        raise HTTPException(404, "That return no longer exists.")
    return record


def _ctx(request: Request, record: ReturnRecord, **extra: Any) -> Dict[str, Any]:
    context = {
        "request": request,
        "record": record,
        "tr": record.load(),
        "assessment_years": sorted(ASSESSMENT_YEARS, reverse=True),
        "today": date.today(),
    }
    context.update(extra)
    return context


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(db_session)):
    return render("home.html",
        {
            "request": request,
            "returns": list_returns(session),
            "assessment_years": sorted(ASSESSMENT_YEARS, reverse=True),
            "current_ay": CURRENT_AY,
            "today": date.today(),
            "due_date": get_ay(CURRENT_AY).due_date_non_audit,
        },
    )


@app.post("/returns/new")
def new_return(
    assessment_year: str = Form(CURRENT_AY),
    label: str = Form(""),
    session: Session = Depends(db_session),
):
    record = get_or_create_return(session, None, assessment_year)
    record.label = label or f"Return for AY {assessment_year}"
    session.commit()
    return RedirectResponse(f"/returns/{record.id}/documents", status_code=303)


@app.post("/returns/{return_id}/delete")
def delete_return(return_id: str, session: Session = Depends(db_session)):
    record = _load(session, return_id)
    session.delete(record)
    session.commit()
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Step 1 — documents
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/documents", response_class=HTMLResponse)
def documents_page(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    return render("documents.html",
        _ctx(request, record, step=1, labels=DOCUMENT_LABELS,
             documents=record.documents),
    )


@app.post("/returns/{return_id}/documents")
async def upload_documents(
    return_id: str,
    files: List[UploadFile] = File(...),
    forced_type: str = Form(""),
    session: Session = Depends(db_session),
):
    record = _load(session, return_id)
    tr = record.load()

    for upload in files:
        raw = await upload.read()
        if not raw:
            continue
        try:
            extraction = parse_document(
                raw,
                upload.filename or "document",
                pan=tr.taxpayer.pan,
                date_of_birth=tr.taxpayer.date_of_birth,
                forced_type=forced_type,
            )
        except Exception as exc:  # noqa: BLE001 - surface, never crash
            extraction = Extraction(
                document_type="unknown",
                source_filename=upload.filename or "document",
            )
            extraction.warnings.append(f"Could not read this file: {exc}")

        session.add(
            DocumentRecord(
                return_id=record.id,
                filename=extraction.source_filename,
                document_type=extraction.document_type,
                confidence=f"{extraction.confidence:.2f}",
                extraction=_encode_extraction(extraction),
            )
        )
    session.commit()
    return RedirectResponse(f"/returns/{return_id}/review", status_code=303)


@app.post("/returns/{return_id}/documents/{doc_id}/delete")
def delete_document(
    return_id: str, doc_id: int, session: Session = Depends(db_session)
):
    document = session.get(DocumentRecord, doc_id)
    if document:
        session.delete(document)
        session.commit()
    return RedirectResponse(f"/returns/{return_id}/documents", status_code=303)


# --------------------------------------------------------------------------
# Step 2 — review extractions
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    documents = [
        {
            "record": document,
            "extraction": _decode_extraction(document.load_extraction()),
        }
        for document in record.documents
    ]
    return render("review.html",
        _ctx(request, record, step=2, documents=documents, labels=DOCUMENT_LABELS),
    )


@app.post("/returns/{return_id}/review")
async def apply_review(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    tr = record.load()
    form = await request.form()
    accepted = set(form.getlist("accept"))
    prefer_26as = form.get("prefer_26as") == "on"

    extractions = [
        _decode_extraction(document.load_extraction())
        for document in record.documents
        if not document.applied
    ]
    apply_extractions(tr, extractions, accepted_paths=accepted)
    if prefer_26as:
        replace_tds_from_26as(tr, extractions)

    for document in record.documents:
        document.applied = 1

    record.save(tr)
    session.commit()
    return RedirectResponse(f"/returns/{return_id}/income", status_code=303)


# --------------------------------------------------------------------------
# Step 3 — income and deductions
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/income", response_class=HTMLResponse)
def income_page(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    return render("income.html", _ctx(request, record, step=3)
    )


@app.post("/returns/{return_id}/income")
async def save_income(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    tr = record.load()
    form = dict(await request.form())
    raw = await request.form()

    tr.assessment_year = form.get("assessment_year", tr.assessment_year)
    tr.regime_choice = form.get("regime_choice", "auto")
    tr.has_business_income = form.get("has_business_income") == "on"
    filing = form.get("filing_date", "")
    tr.filing_date = _parse_iso_date(filing)

    # ---- Taxpayer ---------------------------------------------------------
    taxpayer = tr.taxpayer
    taxpayer.name = form.get("name", taxpayer.name)
    taxpayer.pan = form.get("pan", taxpayer.pan).upper()
    taxpayer.date_of_birth = _parse_iso_date(form.get("date_of_birth", "")) or taxpayer.date_of_birth
    taxpayer.email = form.get("email", taxpayer.email)
    taxpayer.mobile = form.get("mobile", taxpayer.mobile)
    taxpayer.address_line = form.get("address_line", taxpayer.address_line)
    taxpayer.city = form.get("city", taxpayer.city)
    taxpayer.state_code = form.get("state_code", taxpayer.state_code)
    taxpayer.pincode = form.get("pincode", taxpayer.pincode)
    taxpayer.residential_status = form.get("residential_status", "RES")
    taxpayer.has_foreign_assets = form.get("has_foreign_assets") == "on"
    taxpayer.is_company_director = form.get("is_company_director") == "on"
    taxpayer.holds_unlisted_equity = form.get("holds_unlisted_equity") == "on"

    # ---- Bank account -----------------------------------------------------
    ifsc = form.get("bank_ifsc", "").strip().upper()
    account_no = form.get("bank_account", "").strip()
    if ifsc and account_no:
        taxpayer.bank_accounts = [
            BankAccount(
                ifsc=ifsc,
                bank_name=form.get("bank_name", ""),
                account_number=account_no,
                is_primary_refund_account=True,
            )
        ]

    # ---- Repeating rows ---------------------------------------------------
    tr.salaries = _collect_salaries(raw)
    tr.house_properties = _collect_house_properties(raw)
    tr.capital_gains = _collect_capital_gains(raw)
    tr.taxes_paid.payments = _collect_payments(raw)

    # ---- Other sources ----------------------------------------------------
    source = tr.other_sources
    for field_name in (
        "savings_bank_interest", "fixed_deposit_interest", "other_interest",
        "income_tax_refund_interest", "dividend_income", "family_pension",
        "winnings_115bb", "gifts_taxable", "other_income",
    ):
        setattr(source, field_name, D(form.get(f"os_{field_name}", 0)))

    # ---- Deductions -------------------------------------------------------
    deductions = tr.deductions
    for field_name in deductions.model_fields:
        if field_name.startswith("s80") or field_name == "rent_paid_annual":
            value = form.get(f"ded_{field_name}")
            if value is None:
                continue
            if isinstance(getattr(deductions, field_name), bool):
                setattr(deductions, field_name, value == "on")
            else:
                setattr(deductions, field_name, D(value))
    deductions.s80dd_severe = form.get("ded_s80dd_severe") == "on"
    deductions.s80u_severe = form.get("ded_s80u_severe") == "on"

    # ---- Business ---------------------------------------------------------
    tr.business.scheme = form.get("business_scheme", "none")
    tr.business.gross_turnover_digital = D(form.get("turnover_digital", 0))
    tr.business.gross_turnover_cash = D(form.get("turnover_cash", 0))
    tr.business.gross_receipts_44ada = D(form.get("receipts_44ada", 0))

    record.save(tr)
    session.commit()
    return RedirectResponse(f"/returns/{return_id}/foreign", status_code=303)


# --------------------------------------------------------------------------
# Step 4 — foreign income
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/foreign", response_class=HTMLResponse)
def foreign_page(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    from .foreign.forex import ForexTable, preceding_month
    from .foreign.pipeline import apply_foreign

    record = _load(session, return_id)
    tr = record.load()
    _, foreign = apply_foreign(tr)

    # Which months the user should look a rate up for, and what is in use now.
    forex = ForexTable(tr.foreign_settings.forex_overrides)
    dates = (
        [v.vest_date for v in tr.rsu_vests]
        + [d.pay_date for d in tr.dividends]
        + [s.sale_date for s in tr.foreign_sales]
    )
    months = sorted({preceding_month(d) for d in dates if d})
    rate_rows = []
    for month in months:
        try:
            rate = forex.rate_for_month(month, "USD")
            rate_rows.append({
                "month": month, "value": rate.value,
                "provisional": rate.provisional, "source": rate.source,
            })
        except Exception:  # noqa: BLE001 - a missing rate is a row to fill in
            rate_rows.append({
                "month": month, "value": "", "provisional": True,
                "source": "Not on file — enter it",
            })

    return render("foreign.html",
        _ctx(request, record, step=4, foreign=foreign, rate_rows=rate_rows),
    )


@app.post("/returns/{return_id}/foreign")
async def save_foreign(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    tr = record.load()
    raw = await request.form()
    form = dict(raw)

    tr.vesting_schedules = _collect_schedules(raw)
    tr.rsu_vests = _collect_vests(raw)
    tr.dividends = _collect_dividends(raw)
    tr.foreign_sales = _collect_foreign_sales(raw)
    tr.foreign_holdings = _collect_holdings(raw)

    settings = tr.foreign_settings
    settings.form67_filed = form.get("form67_filed") == "on"
    settings.form67_ack = form.get("form67_ack", "")
    settings.lot_matching = form.get("lot_matching", "fifo")

    overrides: Dict[str, Dict[str, str]] = dict(settings.forex_overrides)
    for row in _indexed(raw, "fx"):
        month = row.get("month", "")
        value = row.get("usd", "").strip()
        if month and value:
            overrides.setdefault(month, {})["USD"] = value
        elif month and month in overrides:
            overrides[month].pop("USD", None)
    settings.forex_overrides = {k: v for k, v in overrides.items() if v}

    record.save(tr)
    session.commit()
    return RedirectResponse(f"/returns/{return_id}/compare", status_code=303)


def _collect_vests(form) -> List[RSUVest]:
    out: List[RSUVest] = []
    for row in _indexed(form, "vest"):
        shares = D(row.get("shares_vested", 0))
        if shares <= 0:
            continue
        out.append(RSUVest(
            symbol=row.get("symbol", "").upper(),
            company_name=row.get("company_name", ""),
            grant_id=row.get("grant_id", ""),
            vest_date=_parse_iso_date(row.get("vest_date", "")),
            shares_vested=shares,
            fmv_per_share_fx=D(row.get("fmv_per_share_fx", 0)),
            shares_sold_to_cover=D(row.get("shares_sold_to_cover", 0)),
            sale_price_per_share_fx=D(row.get("sale_price_per_share_fx", 0)),
            currency=row.get("currency", "USD") or "USD",
            included_in_form16=row.get("included_in_form16") == "on",
            forex_rate_override=D(row["forex_rate_override"])
            if row.get("forex_rate_override") else None,
        ))
    return out


def _collect_schedules(form) -> List[VestingSchedule]:
    out: List[VestingSchedule] = []
    for row in _indexed(form, "sched"):
        total = D(row.get("total_shares", 0))
        if total <= 0:
            continue
        # Actual prices arrive as "2025-09-15=150.00" lines, one per tranche.
        actual: Dict[str, str] = {}
        for line in (row.get("actual_fmv", "") or "").splitlines():
            if "=" not in line:
                continue
            when, _, price = line.partition("=")
            when, price = when.strip(), price.strip()
            if when and price:
                actual[when] = price
        out.append(VestingSchedule(
            grant_id=row.get("grant_id", ""),
            symbol=row.get("symbol", "").upper(),
            company_name=row.get("company_name", ""),
            grant_date=_parse_iso_date(row.get("grant_date", "")),
            total_shares=total,
            frequency=row.get("frequency", "quarterly") or "quarterly",
            first_vest_date=_parse_iso_date(row.get("first_vest_date", "")),
            tranches=int(D(row.get("tranches", 0)) or 16),
            cliff_shares=D(row.get("cliff_shares", 0)),
            estimated_fmv_per_share_fx=D(row.get("estimated_fmv_per_share_fx", 0)),
            sell_to_cover_fraction=D(row.get("sell_to_cover_fraction", "0.31")),
            included_in_form16=row.get("included_in_form16") == "on",
            actual_fmv=actual,
        ))
    return out


def _collect_dividends(form) -> List[DividendReceipt]:
    out: List[DividendReceipt] = []
    for row in _indexed(form, "div"):
        gross = D(row.get("gross_amount_fx", 0))
        if gross <= 0:
            continue
        out.append(DividendReceipt(
            symbol=row.get("symbol", "").upper(),
            pay_date=_parse_iso_date(row.get("pay_date", "")),
            gross_amount_fx=gross,
            foreign_tax_withheld_fx=D(row.get("foreign_tax_withheld_fx", 0)),
            currency=row.get("currency", "USD") or "USD",
            is_reinvested=row.get("is_reinvested") == "on",
            shares_acquired=D(row.get("shares_acquired", 0)),
            reinvest_price_per_share_fx=D(row.get("reinvest_price_per_share_fx", 0)),
        ))
    return out


def _collect_foreign_sales(form) -> List[ForeignSale]:
    out: List[ForeignSale] = []
    for row in _indexed(form, "fsale"):
        shares = D(row.get("shares", 0))
        if shares <= 0:
            continue
        out.append(ForeignSale(
            symbol=row.get("symbol", "").upper(),
            sale_date=_parse_iso_date(row.get("sale_date", "")),
            shares=shares,
            price_per_share_fx=D(row.get("price_per_share_fx", 0)),
            fees_fx=D(row.get("fees_fx", 0)),
            currency=row.get("currency", "USD") or "USD",
        ))
    return out


def _collect_holdings(form) -> List[ForeignHolding]:
    out: List[ForeignHolding] = []
    for row in _indexed(form, "hold"):
        symbol = row.get("symbol", "").upper()
        if not symbol:
            continue
        out.append(ForeignHolding(
            symbol=symbol,
            entity_name=row.get("entity_name", ""),
            entity_address=row.get("entity_address", ""),
            entity_zip=row.get("entity_zip", ""),
            broker_name=row.get("broker_name", ""),
            broker_address=row.get("broker_address", ""),
            broker_account_number=row.get("broker_account_number", ""),
            peak_price_fx=D(row.get("peak_price_fx", 0)),
            year_end_price_fx=D(row.get("year_end_price_fx", 0)),
            opening_shares=D(row.get("opening_shares", 0)),
            opening_value_inr=D(row.get("opening_value_inr", 0)),
        ))
    return out


# --------------------------------------------------------------------------
# Step 5 — regime comparison
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/compare", response_class=HTMLResponse)
def compare_page(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    tr = record.load()
    comparison = compare_regimes(tr)
    tr = comparison.prepared or tr
    extractions = [
        _decode_extraction(document.load_extraction())
        for document in record.documents
    ]
    report = reconcile(tr, extractions)
    return render("compare.html",
        _ctx(request, record, step=5, comparison=comparison, report=report,
             chosen=comparison.chosen),
    )


@app.post("/returns/{return_id}/compare")
def choose_regime(
    return_id: str,
    regime_choice: str = Form("auto"),
    session: Session = Depends(db_session),
):
    record = _load(session, return_id)
    tr = record.load()
    tr.regime_choice = regime_choice
    record.save(tr)
    session.commit()
    return RedirectResponse(f"/returns/{return_id}/file", status_code=303)


# --------------------------------------------------------------------------
# The advance-tax planner — a forward-looking tool, not part of filing
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/planner", response_class=HTMLResponse)
def planner_page(
    request: Request,
    return_id: str,
    as_of: str = "",
    session: Session = Depends(db_session),
):
    record = _load(session, return_id)
    tr = record.load()
    when = _parse_iso_date(as_of) or date.today()
    result = build_planner(tr, as_of=when)
    return render("planner.html",
        _ctx(request, record, planner=result, as_of=when,
             challan=record_payment_hint(result.plan),
             schedules=tr.vesting_schedules),
    )


@app.post("/returns/{return_id}/planner/payment")
async def record_advance_payment(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    """Log an advance-tax challan, so the next quarter's plan reflects it."""
    record = _load(session, return_id)
    tr = record.load()
    form = dict(await request.form())

    amount = D(form.get("amount", 0))
    if amount > 0:
        tr.taxes_paid.payments.append(TaxPayment(
            kind="advance_tax",
            amount=amount,
            payment_date=_parse_iso_date(form.get("payment_date", "")) or date.today(),
            bsr_code=form.get("bsr_code", ""),
            challan_serial=form.get("challan_serial", ""),
            deductor_name="Self — advance tax",
        ))
        record.save(tr)
        session.commit()
    return RedirectResponse(f"/returns/{return_id}/planner", status_code=303)


# --------------------------------------------------------------------------
# Step 6 — filing pack
# --------------------------------------------------------------------------


@app.get("/returns/{return_id}/file", response_class=HTMLResponse)
def file_page(
    request: Request, return_id: str, session: Session = Depends(db_session)
):
    record = _load(session, return_id)
    tr = record.load()
    comparison = compare_regimes(tr)
    comp = comparison.chosen
    # Everything downstream works off the prepared return — the one with RSU
    # perquisite, foreign capital gains and Schedule FA rows already folded in.
    # Using the raw return here would silently drop all of it from the JSON.
    tr = comparison.prepared or tr
    decision = select_form(tr)
    pack = pack_builder.build_filing_pack(tr, comp, decision.form)
    extractions = [
        _decode_extraction(document.load_extraction())
        for document in record.documents
    ]
    report = reconcile(tr, extractions)

    json_error = ""
    if decision.supported:
        try:
            json_builder.build(tr, comp, decision.form)
        except Exception as exc:  # noqa: BLE001
            json_error = str(exc)

    return render("file.html",
        _ctx(request, record, step=6, comp=comp, decision=decision, pack=pack,
             report=report, json_error=json_error, comparison=comparison),
    )


@app.get("/returns/{return_id}/itr.json")
def download_itr_json(return_id: str, session: Session = Depends(db_session)):
    record = _load(session, return_id)
    tr = record.load()
    comparison = compare_regimes(tr)
    comp = comparison.chosen
    tr = comparison.prepared or tr
    decision = select_form(tr)
    try:
        payload = json_builder.build(tr, comp, decision.form)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    filename = (
        f"{tr.taxpayer.pan or 'ITR'}_{decision.form.replace('-', '')}_"
        f"AY{tr.assessment_year.replace('-', '')}.json"
    )
    return Response(
        content=json_builder.to_json_bytes(payload),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/returns/{return_id}/computation.pdf")
def download_computation(return_id: str, session: Session = Depends(db_session)):
    from .report import build_computation_pdf

    record = _load(session, return_id)
    tr = record.load()
    comparison = compare_regimes(tr)
    tr = comparison.prepared or tr
    decision = select_form(tr)
    pdf = build_computation_pdf(tr, comparison, decision)
    filename = f"Computation_{tr.taxpayer.pan or 'ITR'}_AY{tr.assessment_year}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/returns/{return_id}/data.json")
def export_data(return_id: str, session: Session = Depends(db_session)):
    record = _load(session, return_id)
    return JSONResponse(json.loads(record.payload or "{}"))


# --------------------------------------------------------------------------
# API for the calculator widget
# --------------------------------------------------------------------------


@app.post("/api/quick-compare")
async def quick_compare(request: Request):
    """A standalone what-if calculator, used by the home page widget."""
    body = await request.json()
    tr = TaxReturn(assessment_year=body.get("assessment_year", CURRENT_AY))
    tr.filing_date = _parse_iso_date(body.get("filing_date", "")) or date.today()
    salary = D(body.get("salary", 0))
    if salary:
        tr.salaries = [SalaryIncome(employer_name="Employer", salary_17_1=salary)]
    tr.other_sources.savings_bank_interest = D(body.get("savings_interest", 0))
    tr.other_sources.fixed_deposit_interest = D(body.get("fd_interest", 0))
    tr.deductions.s80c = D(body.get("s80c", 0))
    tr.deductions.s80ccd1b = D(body.get("s80ccd1b", 0))
    tr.deductions.s80d_self = D(body.get("s80d", 0))
    ltcg = D(body.get("ltcg_112a", 0))
    stcg = D(body.get("stcg_111a", 0))
    if ltcg:
        tr.capital_gains.append(
            CapitalGainItem(category="ltcg_112a", description="Equity LTCG",
                            sale_consideration=ltcg)
        )
    if stcg:
        tr.capital_gains.append(
            CapitalGainItem(category="stcg_111a", description="Equity STCG",
                            sale_consideration=stcg)
        )

    comparison = compare_regimes(tr)

    def summarise(comp) -> Dict[str, Any]:
        return {
            "regime": comp.regime,
            "regime_name": comp.regime_name,
            "gross_total_income": str(comp.gross_total_income),
            "deductions": str(comp.deductions_total),
            "total_income": str(comp.total_income_rounded),
            "tax": str(comp.tax_before_rebate),
            "rebate": str(comp.rebate_87a),
            "surcharge": str(comp.surcharge),
            "cess": str(comp.cess),
            "total_tax": str(comp.total_tax_liability),
            "effective_rate": str(comp.effective_rate),
        }

    return {
        "new": summarise(comparison.new),
        "old": summarise(comparison.old),
        "recommended": comparison.recommended,
        "saving": str(comparison.saving),
    }


# --------------------------------------------------------------------------
# Form parsing helpers
# --------------------------------------------------------------------------


def _parse_iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _indexed(form, prefix: str) -> List[Dict[str, str]]:
    """Turn ``salary_0_name``, ``salary_1_name`` into a list of dicts."""
    rows: Dict[int, Dict[str, str]] = {}
    for key in form.keys():
        if not key.startswith(f"{prefix}_"):
            continue
        remainder = key[len(prefix) + 1:]
        index_text, _, field_name = remainder.partition("_")
        if not index_text.isdigit() or not field_name:
            continue
        rows.setdefault(int(index_text), {})[field_name] = form.get(key, "")
    return [rows[index] for index in sorted(rows)]


def _collect_salaries(form) -> List[SalaryIncome]:
    out: List[SalaryIncome] = []
    for row in _indexed(form, "salary"):
        if not any(row.values()):
            continue
        gross = D(row.get("salary_17_1", 0))
        if gross <= 0 and not row.get("employer_name"):
            continue
        exemptions = {}
        if D(row.get("hra", 0)):
            exemptions["hra"] = D(row["hra"])
        if D(row.get("lta", 0)):
            exemptions["lta"] = D(row["lta"])
        if D(row.get("other_exempt", 0)):
            exemptions["other_section_10"] = D(row["other_exempt"])
        out.append(
            SalaryIncome(
                employer_name=row.get("employer_name", ""),
                employer_tan=row.get("employer_tan", "").upper(),
                employer_category=row.get("employer_category", "OTH") or "OTH",
                salary_17_1=gross,
                perquisites_17_2=D(row.get("perquisites_17_2", 0)),
                profits_in_lieu_17_3=D(row.get("profits_in_lieu_17_3", 0)),
                exempt_allowances=exemptions,
                professional_tax=D(row.get("professional_tax", 0)),
                employer_nps_contribution=D(row.get("employer_nps", 0)),
            )
        )
    return out


def _collect_house_properties(form) -> List[HouseProperty]:
    out: List[HouseProperty] = []
    for row in _indexed(form, "hp"):
        if not any(row.values()):
            continue
        rent = D(row.get("annual_rent_received", 0))
        interest = D(row.get("interest_24b", 0))
        if rent <= 0 and interest <= 0:
            continue
        out.append(
            HouseProperty(
                property_type=row.get("property_type", "SOP") or "SOP",
                address=row.get("address", ""),
                annual_rent_received=rent,
                municipal_taxes_paid=D(row.get("municipal_taxes_paid", 0)),
                interest_24b=interest,
                ownership_share=D(row.get("ownership_share", 1)) or D(1),
            )
        )
    return out


def _collect_capital_gains(form) -> List[CapitalGainItem]:
    out: List[CapitalGainItem] = []
    for row in _indexed(form, "cg"):
        sale = D(row.get("sale_consideration", 0))
        if sale <= 0:
            continue
        out.append(
            CapitalGainItem(
                category=row.get("category", "ltcg_112a") or "ltcg_112a",
                description=row.get("description", ""),
                sale_date=_parse_iso_date(row.get("sale_date", "")),
                purchase_date=_parse_iso_date(row.get("purchase_date", "")),
                sale_consideration=sale,
                cost_of_acquisition=D(row.get("cost_of_acquisition", 0)),
                transfer_expenses=D(row.get("transfer_expenses", 0)),
                fmv_31jan2018=D(row["fmv_31jan2018"])
                if row.get("fmv_31jan2018") else None,
                indexed_cost_of_acquisition=D(row["indexed_cost"])
                if row.get("indexed_cost") else None,
                exemption_amount=D(row.get("exemption_amount", 0)),
                exemption_section=row.get("exemption_section", ""),
            )
        )
    return out


def _collect_payments(form) -> List[TaxPayment]:
    out: List[TaxPayment] = []
    for row in _indexed(form, "tax"):
        amount = D(row.get("amount", 0))
        if amount <= 0:
            continue
        out.append(
            TaxPayment(
                kind=row.get("kind", "tds_other") or "tds_other",
                deductor_name=row.get("deductor_name", ""),
                deductor_tan=row.get("deductor_tan", "").upper(),
                amount=amount,
                payment_date=_parse_iso_date(row.get("payment_date", "")),
                bsr_code=row.get("bsr_code", ""),
                challan_serial=row.get("challan_serial", ""),
            )
        )
    return out


# --------------------------------------------------------------------------
# Extraction serialisation
# --------------------------------------------------------------------------


def _encode_extraction(extraction: Extraction) -> str:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return {"__decimal__": str(value)}
        if isinstance(value, date):
            return {"__date__": value.isoformat()}
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return json.dumps({
        "document_type": extraction.document_type,
        "source_filename": extraction.source_filename,
        "confidence": extraction.confidence,
        "facts": [
            {
                "path": fact.path, "label": fact.label,
                "value": convert(fact.value), "confidence": fact.confidence,
                "evidence": fact.evidence, "source": fact.source,
            }
            for fact in extraction.facts
        ],
        "salaries": convert(extraction.salaries),
        "payments": convert(extraction.payments),
        "capital_gains": convert(extraction.capital_gains),
        "house_properties": convert(extraction.house_properties),
        "rsu_vests": convert(extraction.rsu_vests),
        "dividends": convert(extraction.dividends),
        "foreign_sales": convert(extraction.foreign_sales),
        "warnings": extraction.warnings,
        "raw_text_excerpt": extraction.raw_text_excerpt,
    })


def _decode_extraction(payload: Dict[str, Any]) -> Extraction:
    def convert(value: Any) -> Any:
        if isinstance(value, dict):
            if "__decimal__" in value:
                return D(value["__decimal__"])
            if "__date__" in value:
                return datetime.strptime(value["__date__"], "%Y-%m-%d").date()
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    extraction = Extraction(
        document_type=payload.get("document_type", "unknown"),
        source_filename=payload.get("source_filename", ""),
        confidence=float(payload.get("confidence", 0) or 0),
    )
    for fact in payload.get("facts", []):
        extraction.facts.append(
            Fact(
                path=fact["path"], label=fact["label"],
                value=convert(fact["value"]),
                confidence=float(fact.get("confidence", 0.8)),
                evidence=fact.get("evidence", ""),
                source=fact.get("source", ""),
            )
        )
    extraction.salaries = convert(payload.get("salaries", []))
    extraction.payments = convert(payload.get("payments", []))
    extraction.capital_gains = convert(payload.get("capital_gains", []))
    extraction.house_properties = convert(payload.get("house_properties", []))
    extraction.rsu_vests = convert(payload.get("rsu_vests", []))
    extraction.dividends = convert(payload.get("dividends", []))
    extraction.foreign_sales = convert(payload.get("foreign_sales", []))
    extraction.warnings = payload.get("warnings", [])
    extraction.raw_text_excerpt = payload.get("raw_text_excerpt", "")
    return extraction
