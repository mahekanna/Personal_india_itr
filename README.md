# Personal India ITR

A local web application that prepares an Indian income tax return end to end:
it reads your Form 16, Form 26AS, AIS and broker statements, reconciles them
against one another, computes the tax under both regimes, picks the right ITR
form and produces a portal-ready ITR JSON plus a field-by-field filing pack.

Everything runs on your machine. No document is uploaded anywhere.

---

## Where this stops, and why

It stops one click short of submission, and that is a hard limit rather than an
unfinished feature.

Submitting a return through the department's API requires an **e-Return
Intermediary (ERI) licence**. Getting one means being a registered company, LLP
or a certified firm of Chartered Accountants, Advocates or Company Secretaries;
a net worth above ₹1 crore if registering as a company; passing data-security
certification with a due-diligence certificate from an authorised CISA
professional; and holding Type-2 ERI status for the authentication, pre-fill and
submission endpoints. No individual is going to clear that bar for their own
return.

So this system mimics what the department's own **offline utility** does. It
emits a `.json` file in the ITR schema, which you upload at:

> incometax.gov.in → e-File → Income Tax Returns → File Income Tax Return →
> select the assessment year and form → **Offline / Import pre-filled data** →
> upload the JSON

The portal validates the file, shows you a preview, and takes the submission
from there. You get full automation of the hard part — reading documents,
reconciling them and computing the tax — without the corporate compliance hoops.

**One caveat, stated plainly.** The department revises the JSON schema every
assessment year, and the revisions are not always published before the utility
ships. The structure here follows the published schema, but the only honest test
is whether the offline utility imports the file without complaint. Run that test
once before you trust it. If it objects, the field-by-field filing pack contains
every figure mapped to its portal location, so you can type the return in and
the computation is identical either way.

---

## What it does

**Reads your documents.** Drop the whole folder in at once — nothing needs
labelling. Each file is identified, parsed and scored for confidence.

| Document | Formats | What comes out |
|---|---|---|
| Form 16 | PDF | Employer, TAN, the 17(1)/17(2)/17(3) split, section 10 exemptions, professional tax, TDS, Chapter VI-A the employer allowed |
| Form 26AS | PDF | Every TDS, TCS and challan row, by deductor |
| AIS / TIS | JSON, PDF | Salary, interest, dividend, rent, securities sales, by information category |
| Bank interest certificates | PDF | Savings and deposit interest kept separate — only savings interest qualifies for 80TTA |
| Broker capital gains | XLSX, XLS, CSV | Every transaction, bucketed by asset type and holding period |

Zerodha Console, Groww, Upstox, Kuvera, CAMS and KFintech all use different
column headings for the same six numbers, so headings are mapped onto a
canonical set rather than maintaining a parser per broker.

**Reconciles them against each other.** Three mismatches cause almost every
notice under section 143(1)(a), and all three are checked before you file:

- TDS you are claiming that Form 26AS does not show — this gets disallowed
- Income the AIS reports that your return omits
- Securities sales in the AIS with no matching capital gain

**Computes the tax properly.** Both regimes, in full:

- Slab tax by age band, section 87A **with marginal relief**, surcharge **with
  marginal relief** and the 15% cap on capital-gains surcharge, cess
- Capital gains under the post-23-July-2024 regime — 111A at 20%, 112A at 12.5%
  above ₹1.25 lakh, 112 at 12.5%, with the grandfathered
  20%-with-indexation option on pre-cutoff property where it works out cheaper
- Section 112A grandfathering using the 31 January 2018 fair market value
- Loss set-off under sections 70, 71 and 74, against the highest-taxed bucket
  first, with carry-forward
- The unused basic exemption set against capital gains for residents, as the
  provisos to sections 111A, 112 and 112A permit
- Chapter VI-A with every statutory ceiling, and the aggregate limited by
  section 80A(2) — deductions never touch special-rate income
- Interest under sections 234A, 234B and 234C, and the section 234F fee, with
  the section 207(2) exemption for senior citizens without business income

**Picks the form and explains why.** ITR-1 versus ITR-2 versus ITR-4, with every
disqualification listed. Filing the wrong form makes a return defective under
section 139(9), so the selector escalates when in doubt.

**Produces the output.** ITR-1 and ITR-2 JSON, a computation-sheet PDF worth
keeping for the next few years, and a filing pack mapping every figure to its
portal field.

---

## Installing and running

Python 3.10 or newer.

```bash
git clone <this repository>
cd Personal_india_itr

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run.py
```

It opens at <http://127.0.0.1:8000>, bound to localhost only — your Form 16 has
no business being reachable from the network.

```bash
python run.py --port 9000          # a different port
python run.py --reload             # reload on code changes
python run.py --no-browser         # do not open a browser
```

Returns live in a SQLite file at `~/.india-itr/returns.db`. Override with
`ITR_DATA_DIR`.

---

## Using it

The wizard is five steps.

**1 · Documents.** Upload everything. Password-protected AIS and 26AS PDFs are
opened automatically using the department's convention — PAN in lower case
followed by date of birth as `DDMMYYYY`, so `abcde1234f01011990`. If nothing has
been read yet, fill your PAN and date of birth in on the Income page first.

**2 · Review.** Every extracted figure, with a confidence bar and the raw text
it came from. Nothing has touched the return yet. Untick whatever looks wrong.
Where Form 16 and Form 26AS disagree on TDS, the default is to trust 26AS, since
that is the statement the department matches your claim against.

**3 · Income.** Everything read from your documents, pre-filled and editable,
plus what the documents did not cover. Enter deductions as what you actually
invested — the ceilings are applied for you.

**4 · Regime.** Both regimes side by side, line by line, with reconciliation
findings. It recommends one; you can override it.

**5 · File.** The chosen ITR form with reasons, the JSON, the computation PDF,
the filing pack, and an ordered checklist — including paying self-assessment tax
*before* generating the final JSON if there is a balance, and e-verifying within
30 days, without which the return is treated as never filed.

---

## Correctness

The tax engine has 47 golden tests whose expected values were worked out by hand
from the statute rather than generated by running the code — a test that records
current behaviour merely freezes bugs in place. They cover every new-regime slab
boundary, 87A and its marginal relief, surcharge marginal relief at the ₹50 lakh
threshold, the capital-gains buckets, loss set-off ordering, the Chapter VI-A
ceilings and the interest sections.

```bash
pytest tests/ -q          # 101 tests
```

Two invariants worth knowing about, because they are easy to get wrong:

- The **₹1.25 lakh under section 112A is a rate threshold, not a deduction**.
  The full gain stays in total income, where it counts towards the 87A ceiling
  and the surcharge thresholds. Treating it as a deduction understates total
  income and silently changes the answer.
- **Money is `Decimal` everywhere.** A 0.005 float drift is the difference
  between a return that matches the department's computation and one that
  attracts a demand notice.

---

## What it does not do

- **Submit the return.** See above.
- **ITR-3** — business income with regular books of account. The computation and
  filing pack still work; only JSON generation is absent.
- **ITR-4 JSON** — presumptive income under 44AD, 44ADA and 44AE is computed and
  appears in the filing pack, but the JSON is not generated yet.
- **Non-resident taxation**, DTAA relief under sections 90 and 91, and relief
  under section 89 for arrears.
- **F&O and speculative business income**, which are business heads, not capital
  gains.
- **Scanned documents.** There is no OCR. Download the digitally generated PDF
  from the source, or enter those figures by hand.
- **Replace a chartered accountant** on anything genuinely contentious.

---

## Layout

```
app/
  money.py            Decimal helpers and Indian digit grouping
  schemas.py          The canonical shape of a return — the contract everything shares
  tax/
    rules.py          Rate cards by assessment year. Every Finance Act change lands here
    heads.py          Salary, house property, business, capital gains, other sources
    chapter_via.py    Chapter VI-A with the statutory ceilings
    interest.py       Sections 234A, 234B, 234C and the 234F fee
    engine.py         The computation, and the regime comparison
  parsers/
    base.py           PDF text and table extraction, decryption, field scraping
    registry.py       Document identification and dispatch
    form16.py  form26as.py  ais.py  bank.py  broker.py
  itr/
    selector.py       Which ITR form, and why
    json_builder.py   ITR-1 and ITR-2 JSON
    filing_pack.py    Every figure, mapped to its portal field
  reconcile.py        Cross-document checks
  merge.py            Applying reviewed extractions, with de-duplication
  report.py           The computation-sheet PDF
  main.py             Routes
tests/                101 tests
```

Adding an assessment year is a data edit in `tax/rules.py`, not a code change —
the engine holds no year-specific constants of its own.

---

## Open-source foundations

| Library | Used for |
|---|---|
| [pdfplumber](https://github.com/jsvine/pdfplumber) | PDF text and table extraction |
| [pypdf](https://github.com/py-pdf/pypdf) | Decrypting password-protected AIS and 26AS |
| [pandas](https://pandas.pydata.org/) + openpyxl / xlrd | Broker and mutual-fund statements |
| [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) | The web application |
| [Pydantic](https://docs.pydantic.dev/) | Validating the return model |
| [SQLAlchemy](https://www.sqlalchemy.org/) | Local SQLite persistence |
| [ReportLab](https://www.reportlab.com/opensource/) | The computation-sheet PDF |

Prior art in this space is mostly CLI- or agent-shaped rather than a web
application — [itr-wala](https://github.com/karanb192/itr-wala) has a
well-tested deterministic engine, and [OpenTax](https://github.com/Nootus/OpenTax)
aims at a framework. Neither was adopted wholesale here; the tax engine is
written directly against the statute so that every rate has one home and every
figure is traceable.

---

## Statutory basis

AY 2026-27 (FY 2025-26) reflects the Finance Act 2025 — the new-regime slab
table, the ₹12,00,000 rebate ceiling with a ₹60,000 maximum rebate, and the
₹75,000 standard deduction — together with the capital-gains regime introduced
by the Finance (No. 2) Act 2024 with effect from 23 July 2024. AY 2025-26 is
retained so that a belated or updated return can still be prepared.

---

## A working tool, not tax advice

This prepares a return from the documents you supply. It does not know about the
circumstances you did not tell it. Check every figure against the portal's own
preview before you submit, and take professional advice on anything contentious.
The return you file is your responsibility.
