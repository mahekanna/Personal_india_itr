# Personal India ITR

A local web application that prepares an Indian income tax return end to end:
it reads your Form 16, Form 26AS, AIS, broker statements and US stock-plan
exports, reconciles them against one another, computes the tax under both
regimes, picks the right ITR form and produces a portal-ready ITR JSON plus a
field-by-field filing pack.

It handles **US RSUs, ESPP, dividends and dividend reinvestment** properly —
including the three things that trip nearly everyone up: the 24-month holding
period, the calendar-year Schedule FA, and reinvested dividends. A quarterly
vesting schedule is described once and expands into its tranches, and an
**advance-tax planner** works out what to pay each quarter, applying the proviso
to section 234C that most calculators ignore.

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
| Broker tax P&L | XLSX, XLS, CSV | **F&O, intraday, currency and commodity**, sorted into the three heads of income they legally belong to, with turnover recomputed |
| US stock plan and brokerage | XLSX, XLS, CSV | Vesting tranches, **ESPP purchases**, dividends with record dates and per-share rates, reinvestment, disposals — from E*TRADE, Fidelity, Schwab and Morgan Stanley StockPlan Connect |

ICICI Direct, Zerodha Console, Groww, Upstox, Angel One, Dhan, Kuvera, CAMS and
KFintech all use different column headings for the same handful of numbers, so
headings are mapped onto a canonical set rather than maintaining a parser per
broker. A file whose headings are unrecognised produces a warning — never an
empty result, because a year that failed to parse must not look like a year with
no trading in it.

**Sorts a trading account into the right heads.** One demat account produces
three, and merging them is unlawful in either direction:

| Segment | Head | Why |
|---|---|---|
| Delivery equity | Capital gains | Sections 111A and 112A |
| Intraday equity | **Speculative** business | Section 43(5). Loss meets speculative income only, and lapses after four years |
| Equity, index, currency, commodity F&O | **Non-speculative** business | The provisos to section 43(5) take derivatives on a recognised exchange out of the definition |

Turnover is recomputed from the trade rows as the absolute value of each result,
per the ICAI Guidance Note on Tax Audit (Revised 2023). Many brokers still
report the pre-2022 figure, which adds the full sale consideration of options —
a number that can be twenty times the correct one and manufactures a section
44AB audit out of nothing.

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
- Section 234C computed with its first proviso, so capital gains and dividends
  are not charged interest for the instalments that fell due before they arose

### US RSUs, ESPP and dividends

Foreign equity is not one taxable event but three, and collapsing them is where
returns go wrong.

**Vesting is salary.** The same section 17(2)(vi) that catches the ESPP discount
taxes the fair market value on the
vesting date as a perquisite at slab rates. Section 49(2AA) then makes that same
value the cost basis on sale, which is what stops it being taxed twice. Your
employer normally runs it through payroll, so it is already inside the Form 16 —
each vest carries a tick-box for that, and the reconciler queries a vest with no
matching section 17(2) figure.

**The holding period is 24 months, not 12.** Sections 111A and 112A require
securities transaction tax, which is never paid on a NYSE or NASDAQ trade. So a
US share is not "listed" for this purpose however obviously listed it looks: it
turns long term only at 24 months, and there is no ₹1.25 lakh shelter. Selling
at 18 months means slab rates — up to 30% plus surcharge — rather than 12.5%.
On a decent tranche that is lakhs. The interface shows the exact date each lot
crosses over.

**Dividends follow the position, not the paperwork.** Entitlement is fixed on
the *record date*, and a holding that grows every quarter as tranches vest, as
ESPP shares are bought and as dividends are reinvested held a different number of
shares on each one. Enter the declared rate per share once and each payment is
sized from the lot ledger — the same ledger the capital-gains matching uses, so
the position is the real one:

| Paid | Held on record date | Rate | Gross |
|---|---|---|---|
| 20 Aug | 100 — the June tranche | $0.72 | $72.00 |
| 20 Nov | 200 — and September's | $0.75 | $150.00 |
| 20 Feb | 250 — and the December ESPP purchase | $0.70 | $175.00 |

A payment you entered by hand, or imported from a 1099-DIV, always wins; the
schedule fills gaps rather than overwriting. And where a rate per share is known,
the declared amount is checked back against the ledger: a dividend implying 500
shares when 100 were held means a vesting tranche or a purchase is missing. A
holding that paid nothing all year is queried too, because the AIS will have the
payment even when the return does not.

**Indian dividends** are the same income under different plumbing — no
conversion, no foreign credit, and TDS at 10% under section 194 once a payer
crosses ₹10,000 in the year, a threshold the Finance Act 2025 raised from
₹5,000. That TDS becomes an ordinary tax credit, and a large dividend recorded
without it is flagged, since unclaimed TDS is a refund forgone. Recording the
payment date matters for more than tidiness: it is what earns the section 234C
relief described below.

**Interest on money borrowed to buy the shares** is deductible against dividend
income under the proviso to section 57(i), but only up to **20% of that income**,
and nothing else is deductible at all — not demat charges, not advisory fees.
Enter what you paid and the cap is applied. It survives in both regimes; section
115BAC restricts only clause (iia), the family-pension deduction.

**Reinvested dividends are income now.** A dividend is taxable on the payment
date whether it reaches your bank or buys more shares, and it is taxed on the
**gross**, before the 25% the US withholds. What reinvestment does change is the
cost basis: every reinvestment is a fresh lot with its own acquisition date and
its own 24-month clock. Quarterly reinvestment over three years leaves twelve
small lots, most still short term when the position is finally sold — which is
why a position you have "held for years" throws off short-term gains nobody
expected. Each lot is tracked separately and matched first in, first out.

**Rule 115 picks the exchange rate, and you do not.** Every amount converts at
the SBI telegraphic transfer buying rate on the *last day of the month before*
the transaction. A vest on 15 September uses the 31 August rate; three tranches
in March, June and September use three different rates. A built-in table gets
you computing immediately, but every rate in it is flagged provisional and the
Foreign income page lists exactly which months you relied on so you can replace
them with the published figures.

**The foreign tax credit is capped.** Rule 128(2) allows the lower of the tax
paid abroad and the Indian tax attributable to that same income — the excess is
neither refunded nor carried forward, and the system says how much was lost and
why. Withholding above the 25% the India-US treaty permits is not creditable
here at all, which usually means a lapsed Form W-8BEN. Rule 128(9) wants
**Form 67 filed before the return**; claiming credit without it is reported as a
blocking error, because CPC denies it first and makes you appeal afterwards.

**Schedule FA is the one with a ₹10 lakh penalty.** It reports the **calendar**
year — 1 January to 31 December 2025 for AY 2026-27 — not the financial year, so
a February 2026 vest is this year's salary but next year's Schedule FA. There is
no minimum value and no income requirement: a resident and ordinarily resident
must report every foreign asset held at any point in that year. Omitting one
attracts a flat ₹10 lakh under sections 42 and 43 of the Black Money Act,
charged on the non-disclosure itself, so it applies just as fully when the tax
was paid correctly. Table A2 (the custodial account) and Table A3 (the shares)
are both generated, and a missing entity address is flagged before the portal
rejects it.

### ESPP: the discount, and the basis trap

An Employee Stock Purchase Plan takes payroll deductions over an offering period
and buys shares at a discount. Two consequences follow, and the second is where
money quietly goes missing.

**The discount is salary.** Section 17(2)(vi) taxes the gap between the fair
market value on the purchase date and what you actually paid, at slab rates,
with TDS under section 192 in the month of purchase. Your own contributions are
not deductible against it — they came out of salary that was already taxed.

**The cost basis on sale is the fair market value, not the price you paid.**
Section 49(2AA) fixes the cost at the amount already brought to tax as a
perquisite. Your broker's statement and any US 1099-B both report the discounted
purchase price, because that is correct for US tax and wrong for Indian tax.
Copying it across taxes the discount twice — once as salary in the year of
purchase, once as capital gain on sale.

The error is not small, because of the lookback. A standard plan buys at 85% of
the *lower* of the offering-start price and the purchase-date price, so when the
share price has risen the real discount is far more than the headline 15%:

| | |
|---|---|
| Offering start, 1 Jun | $150 |
| Purchase date, 1 Dec — market | $200 |
| Purchase date — what you paid | $127.50 (85% of the lower) |
| **Effective discount** | **36.25%**, not 15% |
| Perquisite taxed as salary | ₹6,38,000 |
| Cost basis on sale, s.49(2AA) | **₹17,60,000** |
| What the 1099-B would show | ₹11,22,000 |

Sell at $210 and the gain is ₹88,000. Take the basis off the 1099-B instead and
it becomes ₹7,26,000 — ₹6,38,000 too much, which is exactly the perquisite,
taxed a second time. The interface shows both figures side by side so the
difference is visible rather than implied, and ESPP lots are matched FIFO
alongside RSU tranches and reinvestment lots.

One honest caveat: fair market value for a share not listed on a *recognised*
(Indian) stock exchange is, strictly, a Category I merchant banker's valuation
under Rule 3(8), not the NYSE closing price. In practice employers use the
market price and report it in Form 16. Use whatever figure your Form 16 uses, so
the return and the TDS agree.

### Quarterly vesting, and advance tax

**A grant is described once.** A four-year grant vesting quarterly is sixteen
separate salary events, each at its own price, its own Rule 115 exchange rate
and its own 24-month clock. Give the system the grant, the frequency, the first
vest date and any cliff, and it generates the tranches — clamping to the month
end, so a grant vesting on the 31st vests on 28 February rather than rolling
into March and landing in a different quarter. Tranches add back to the grant
exactly; rounding never loses or invents a share.

Tranches with a real price go into the return. Tranches still to come are
**projections**: they are valued at your estimate, used to size the advance-tax
instalments, and never allowed anywhere near the return or the ITR JSON. A vest
you entered by hand always beats the generated one.

**The advance-tax planner is where the ₹234C proviso earns its keep.** Section
211 wants 15% by 15 June, 45% by 15 September, 75% by 15 December and the whole
of it by 15 March, and section 234C charges 1% a month on any shortfall. Applied
naively that is punitive for anyone with capital gains, because it demands tax
in June on a gain made in December.

The **first proviso to section 234C(1)** says otherwise. Where the shortfall is
down to capital gains, dividends, winnings, or business income in its first
year, no interest arises for the earlier instalments — *provided* the whole of
the tax on that income is paid in the remaining ones, or by 31 March where the
income arose after 15 March.

So the planner splits your liability in two:

| | Follows | Because |
|---|---|---|
| Salary, interest, rent | 15 / 45 / 75 / 100 | Foreseeable from April |
| **Capital gains and dividends** | Full tax, from the quarter it arose | The proviso |

The trade is worth understanding: relief on the earlier instalments is bought by
owing the *entire* tax on that income at the very next one, not a fraction of
it. Miss that instalment and the relief goes with it. The planner shows both the
interest actually chargeable and the net saving against a naive calculation.

It also shows, per quarter, which vests and which sales fell in that window,
what was required, what was paid, what is short and what that costs — with the
challan details for the payment (ITNS 280, minor head 100) and somewhere to log
the BSR code and serial number afterwards, so the next quarter accounts for it
and the figures are ready for the return.

**Picks the form and explains why.** ITR-1 versus ITR-2 versus ITR-4, with every
disqualification listed. Filing the wrong form makes a return defective under
section 139(9), so the selector escalates when in doubt.

**Produces the output.** ITR-1 and ITR-2 JSON, a computation-sheet PDF worth
keeping for the next few years, and a filing pack mapping every figure to its
portal field.

---

## Seeing it with data in it

```bash
python demo/seed_demo.py          # one fictional taxpayer, two scenarios
python run.py
```

That creates two returns for AY 2026-27 belonging to an invented Bengaluru
engineer with a US employer's equity — a four-year grant vesting quarterly, an
ESPP purchase with a lookback, dividends part-reinvested, a couple of sales, an
Indian dividend, a home loan and the usual deductions. One is the finished
return; the other is the same year still in progress, seen from December, where
salary TDS covers the salary and the gains fall to advance tax. Nothing in it is
real: no genuine PAN, TAN, IFSC or account number appears anywhere.

Two scripts read the running application rather than describing it:

- `demo/capture.py` drives it with a real browser and writes a screenshot of
  each screen to `demo/screens/` — how the images below were made, and a quick
  way to eyeball the interface after a change.
- `demo/build_walkthrough.py` bundles every screen's actual HTML, with the
  stylesheet inlined, into one browsable file. Not a mock-up: only the
  navigation between screens is added, and the forms are left in place and made
  inert.

```bash
python run.py --port 8790 --no-browser &
python demo/build_walkthrough.py --port 8790 \
    --filing <return-id> --planning <return-id>
```

![Regime comparison](demo/screens/06-compare.jpg)

![Advance tax planner](demo/screens/07-planner.jpg)

## Installing and running

Python 3.10 or newer. Nothing else — no database server, no account, no
network service.

```bash
git clone https://github.com/mahekanna/Personal_india_itr.git
cd Personal_india_itr

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run.py
```

It opens at <http://127.0.0.1:8000>, **bound to localhost only** — your Form 16
has no business being reachable from the network. Confirm it started cleanly by
creating a return: the first screen should ask what your year looked like, and
name a form and a due date.

```bash
python run.py --port 9000          # a different port
python run.py --reload             # reload on code changes
python run.py --no-browser         # do not open a browser

pytest -q                          # 428 tests, ~3 seconds
```

### Where your data lives

One SQLite file at `~/.india-itr/returns.db`, outside the repository so it
cannot be committed by accident. Override the location with `ITR_DATA_DIR`.
Delete the file to start over.

**Raw uploads are never stored.** A document is parsed in memory and only the
extracted figures are kept, so the database holds numbers rather than a copy of
your Form 16.

### Giving it to someone else

Send them this repository. Do not host it for them.

Hosting other people's tax documents makes you a Data Fiduciary under the DPDP
Act 2023 — notice, consent, breach reporting to the Data Protection Board and
to the affected users, a named grievance officer, and penalties up to ₹250 crore
for failing to take reasonable security safeguards. CERT-In's 2022 directions
add six-hour breach reporting. And preparing other people's returns, especially
for a fee, moves towards the e-Return Intermediary registration this project
exists to avoid.

Self-hosted, none of that applies to you and their data never leaves their
machine. That is not a compromise; it is the better answer.

If you do host it, note that there is **no ownership model at all** — any
return can be fetched by its ID with no check on who is asking. That is correct
for a single-user local tool and an immediate data breach on a public one. You
would need a user model, an ownership check on every route, CSRF tokens and
rate limiting before exposing it to anyone.

---

## Collecting your documents

The first screen produces this list tailored to your answers, and the upload
page keeps it visible as you go. In full, for the situations this covers:

### From the income tax portal — <https://incometax.gov.in>

| What | Where |
|---|---|
| **Form 26AS** | e-File → Income Tax Returns → View Form 26AS → continue to TRACES → View Tax Credit → your assessment year |
| **AIS and TIS** | Services → AIS → your assessment year. Password is your PAN in lower case followed by date of birth as `DDMMYYYY` |
| **Advance tax challans** | e-Pay Tax → Payment History. You need the BSR code, challan serial number and date — the credit is not matched without all three |

### From your employer

- **Form 16**, Parts A and B, from every employer you had in the year.
- **Form 12BA**, the perquisite statement. Ask for it by name if it was not
  issued. It is what values an RSU vest or an ESPP discount, and what tells you
  whether the perquisite is already inside your Form 16 gross salary — getting
  that wrong taxes it twice or not at all.

### From your Indian broker

- **Annual tax P&L for the financial year**, segment-wise. It must separate
  delivery, intraday and F&O; they are three different heads of income.
- **Annual charges and brokerage statement.** Brokerage, exchange and clearing
  charges, SEBI fees, STT or CTT, GST, stamp duty and depository charges are all
  deductible once trading is business income. Not claiming them is money left on
  the table.
- **Capital gains statement**, if your broker issues it separately.
- For mutual funds, the **consolidated capital gains statement** from CAMS or
  KFintech covers every fund house at once, free by email.
- For anything bought before 31 January 2018, the **highest quoted price on that
  date**. Section 55(2)(ac) grandfathers the gain up to it; without the figure
  you pay tax on a gain that is not taxable.

### From your foreign broker or stock plan administrator

- **Vesting or release statements** for every tranche, with the fair market
  value on each vesting date.
- **ESPP purchase confirmations** — fair market value on the purchase date, the
  price you paid, and the price at the start of the offering.
- **Form 1099-DIV**, and **Form 1099-B** or the realised gain-and-loss report.
- **Year-end statement** showing the position on 31 December and the highest
  value during the calendar year. Schedule FA runs on the *calendar* year.
- **The company's and the broker's registered address and ZIP code.** Schedule
  FA asks for both and the portal will not accept the row without them.

### Look up

- **SBI TT buying rate** for the last day of each month in which you had a vest,
  an ESPP purchase, a dividend or a sale. Rule 115 uses the rate on the last day
  of the month *before* the income arose, never the rate on the day itself.

---

## Tools

Two scripts in `tools/`, both for the things the application itself cannot
verify.

### Describing a statement without disclosing it

Teaching the parser a new broker needs the *shape* of the file — sheet names,
column headings, how many rows of preamble sit above the header, how dates are
written, whether a loss is `-1234` or `(1,234)`. None of that needs a real
figure.

```bash
python tools/describe_statement.py ~/Downloads/broker_tax_pnl.xlsx
```

Every value is replaced by its type, and anything shaped like a PAN, IFSC,
account number, client code, email or mobile number is dropped rather than
described — a description of a PAN is still most of a PAN. Column headings
survive, because they are the entire point. The output is a dozen lines and is
safe to paste into an issue. Read it before you send it.

### Checking the JSON against the department's own schema

This is the one open caveat that matters. The arithmetic inside each schedule is
tested against the statute; the *element names* were written from the published
schema by hand and have never been checked against it.

CBDT publishes the schema alongside each offline utility — e-Filing portal →
Downloads → Income Tax Returns → your assessment year, named like
`ITR-3_2026_Main_V1.1.json`.

```bash
python tools/validate_against_schema.py ITR-3_2026_Main_V1.1.json return.json
```

It reports every element name the return emits that the schema does not define
— each one a probable rejection — plus required elements that are missing, and
formal violations where the schema file is JSON Schema proper.

---

## Using it

**0 · Start.** Seventeen yes-or-no questions about what your year looked like,
answered before anything is uploaded — because nobody knows which documents to
gather until they know which form they are filing. Out of it comes the form,
a grouped list of exactly what to fetch and where from, and the dates that
matter. If your situation is one this cannot compute — crypto, a HUF return,
non-residence, a business with real books — it says so here and declines,
rather than producing a plausible wrong answer further down.

**1 · Documents.** Upload everything, with the checklist from step 0 alongside. Password-protected AIS and 26AS PDFs are
opened automatically using the department's convention — PAN in lower case
followed by date of birth as `DDMMYYYY`, so `abcde1234f01011990`. If nothing has
been read yet, fill your PAN and date of birth in on the Income page first.

**2 · Review.** Every extracted figure, with a confidence bar and the raw text
it came from. Nothing has touched the return yet. Untick whatever looks wrong.
Where Form 16 and Form 26AS disagree on TDS, the default is to trust 26AS, since
that is the statement the department matches your claim against.

**4 · Foreign.** Vesting schedules, individual vests, dividends, sales,
exchange rates and Schedule FA.

**3 · Income.** Everything read from your documents, pre-filled and editable,
plus what the documents did not cover. Enter deductions as what you actually
invested — the ceilings are applied for you.

**5 · Regime.** Both regimes side by side, line by line, with reconciliation
findings. It recommends one; you can override it.

**Advance tax** sits outside the wizard, because it looks forward at the year
in progress rather than back at the one being filed. It is reachable from the
Foreign, Regime and File screens.

**6 · File.** The chosen ITR form with reasons, the JSON, the computation PDF,
the filing pack, and an ordered checklist — including paying self-assessment tax
*before* generating the final JSON if there is a balance, and e-verifying within
30 days, without which the return is treated as never filed.

### Dates, and the one that catches people

Section 139(1) is **staggered by form** from AY 2026-27, as a statutory
amendment rather than a departmental circular:

| | Due |
|---|---|
| ITR-1 and ITR-2 | 31 July |
| **ITR-3 and ITR-4, no audit** | **31 August** |
| Tax audit under section 44AB | 31 October |
| Belated return under 139(4) | 31 December |

A trader measured against the salaried date is told they are late when they are
not — which costs a ₹5,000 fee under section 234F, interest under 234A, and
worst of all the **Form 10-IEA** window, since that closes on whichever date
actually applies. With business income the old regime cannot be chosen on the
return; Form 10-IEA has to be filed on or before the due date, and section
115BAC(6) allows the opt-out only once. Miss it and the new regime applies for
the year whatever the comparison says.

Two more that are easy to lose:

- **Form 67** must be on the portal *before* the return, not with it, or the
  foreign tax credit is liable to be denied outright.
- **Losses under sections 72, 73 and 74** carry forward only on a return filed
  by the due date. A belated return forfeits them. A house property loss under
  section 71B is the one exception and survives either way.

---

## Correctness

The tax engine has golden tests whose expected values were worked out by hand
from the statute rather than generated by running the code — a test that records
current behaviour merely freezes bugs in place. They cover every new-regime slab
boundary, 87A and its marginal relief, surcharge marginal relief at the ₹50 lakh
threshold, the capital-gains buckets, loss set-off ordering, the Chapter VI-A
ceilings and the interest sections.

```bash
pytest -q                 # 428 tests
```

A further set covers robustness rather than arithmetic: what happens when a
field holds `1e999`, when an assessment year is one the engine has never heard
of, when every date is missing, when the upload is not really a PDF. Each was
found by probing the running application, and each was a real failure before the
fix it names.

The tests are self-authored, which makes them circular in one direction: they
test the author's reading of the statute, not the statute. Each cites the
section it relies on so that the reading can be checked rather than taken on
trust. Where a defect has been fixed, the test names it and explains what the
wrong behaviour was — because the interesting thing about a tax bug is not that
it is fixed but what it used to cost.

Two invariants worth knowing about, because they are easy to get wrong:

- The **₹1.25 lakh under section 112A is a rate threshold, not a deduction**.
  The full gain stays in total income, where it counts towards the 87A ceiling
  and the surcharge thresholds. Treating it as a deduction understates total
  income and silently changes the answer.
- **Money is `Decimal` everywhere.** A 0.005 float drift is the difference
  between a return that matches the department's computation and one that
  attracts a demand notice.
- **A US share is not a listed share.** Sections 111A and 112A need securities
  transaction tax. Foreign equity therefore takes 24 months to turn long term
  and gets no ₹1.25 lakh exemption — applying the Indian equity rules to it
  understates the tax badly.
- **Schedule FA is on the calendar year.** Nothing else in the return is, so the
  figures deliberately do not tie to the rest of it.
- **Section 234C has a proviso.** Charging capital gains as though they should
  have been foreseen in April is the commonest advance-tax error, and it always
  errs against the taxpayer.
- **A projection is not a figure.** Tranches that have not vested exist to size
  an instalment. They are barred from the return and from the ITR JSON, and
  there is a test that says so.
- **An ESPP cost basis is the fair market value.** The 1099-B disagrees, and the
  1099-B is right about US tax and wrong about this one.
- **A dividend is sized by the record date.** Not the pay date, and not the
  position held today.
- **Bad input must never cost the user their data.** A single unreadable field
  used to discard the whole return on the next read, which looks exactly like
  the data having vanished. The loader now salvages every field it can and says
  which ones it could not.
- **A dated dividend earns the section 234C relief; an undated one cannot.**
  Where no date is known the earliest instalment is assumed, which errs against
  the taxpayer rather than understating the liability.

---

## Who this is for

Someone **resident and ordinarily resident**, with any combination of:

- salary, from one employer or several
- one or more house properties
- delivery equity and mutual funds — capital gains
- intraday, F&O, currency and commodity trading — business income, ITR-3
- US stock compensation: RSUs, ESPP, dividends, dividend reinvestment,
  Schedule FA, Schedule FSI, Form 67
- interest, dividends and the ordinary other-sources items

That covers ITR-1, ITR-2 and ITR-3, and it is deliberately the band of people
who find those forms hardest.

## Who it is NOT for

The first screen asks about each of these and **refuses to compute** rather than
producing a plausible wrong answer. That refusal is the feature; a tax return
that is confidently wrong is worse than none.

- **Crypto and other virtual digital assets.** Not implemented at all. Section
  115BBH is a flat 30% with no deduction beyond cost and — unusually — no
  set-off of a loss against anything, not even another crypto gain. Left
  unhandled, a gain would fall into ordinary capital gains at 12.5% and quietly
  absorb losses the section forbids. This is the one gap that would have failed
  silently, which is why it is asked about first.
- **Non-residents and RNOR.** Residence changes what is taxable at all, whether
  the basic exemption can shelter capital gains, and which treaty applies.
- **HUF returns.** Individuals only; the JSON declares the status as individual.
- **A business or profession with real books** — stock, debtors, depreciation, a
  balance sheet. It handles trading, where the broker's statement is the only
  record there is.

## What it does not do

- **Submit the return.** See above — that needs an ERI licence.
- **ITR-4 JSON** — presumptive income under 44AD, 44ADA and 44AE is computed and
  appears in the filing pack, but the JSON is not generated.
- **Relief under section 89** for salary arrears, and **section 91** relief for
  countries India has no treaty with. Section 90 relief *is* computed.
- **Clubbing of income** under section 64 — a spouse's or minor child's income.
- **Agricultural income** aggregated for rate purposes.
- **ESOPs with deferred taxation** under section 191(2) for eligible start-ups.
- **Disqualifying dispositions** and other US-side ESPP concepts. They change
  the US tax treatment; they have no bearing on the Indian computation, which
  fixes the perquisite at the purchase date regardless of when you sell.
- **Currencies other than USD** are converted through a cross-rate off USD,
  which is cruder than the direct rate. Enter the rate by hand for EUR or GBP.
- **Scanned documents.** There is no OCR. Download the digitally generated PDF
  from the source, or enter those figures by hand.
- **Replace a chartered accountant** on anything genuinely contentious.

## What has never been tested against reality

Stated plainly, because you are about to trust this with a tax return:

- **No real Form 16, Form 26AS, AIS or broker statement has been through the
  parsers.** Every fixture is synthetic and written by the same author as the
  parser, which is circular. Check every figure on the review screen against
  your own document — the screen exists for that.
- **No generated JSON has ever been imported into the department's offline
  utility.** The arithmetic inside the schedules is tested; the element names
  are written from the published schema and are not corroborated. If the import
  is refused, that is a naming problem rather than a tax one, and the filing
  pack carries the same figures to key in by hand.
- **The built-in exchange rates are provisional reference figures**, flagged as
  such wherever they are used. Look up the published SBI TT buying rate for each
  month end before filing.
- **The tests are self-authored**, and test the author's reading of the statute.
  Each cites the section it relies on so the reading can be checked.

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
    advance_tax.py    Instalments, and the proviso to section 234C
    trading.py        F&O, intraday and commodity: which segment is speculative,
                      turnover under the ICAI note, and the s.44AB test
    engine.py         The computation, and the regime comparison
  parsers/
    base.py           PDF text and table extraction, decryption, field scraping
    registry.py       Document identification and dispatch
    form16.py  form26as.py  ais.py  bank.py  broker.py  us_equity.py
    trading_pnl.py    A broker's segment-wise tax P&L: F&O, intraday,
                      currency and commodity
  planner.py          The advance-tax planner
  foreign/
    forex.py          Rule 115 conversion, and which months still need a rate
    vesting.py        Expanding a grant into its tranches
    rsu.py            Vesting, lot tracking, FIFO matching, the 24-month test
    espp.py           The discount as perquisite, and the s.49(2AA) cost basis
    dividends.py      Payments sized from the holdings ledger, domestic and
                      foreign, the treaty cap and section 57(i)
    ftc.py            Section 90 credit under Rule 128, and Form 67
    schedule_fa.py    Calendar-year foreign asset disclosure
    pipeline.py       Folds all of the above into ordinary return entries
  itr/
    selector.py       Which ITR form, and why
    json_builder.py   ITR-1, ITR-2 and ITR-3 JSON
    filing_pack.py    Every figure, mapped to its portal field
  questionnaire.py    The opening questions, the form, the document checklist,
                      and the situations this refuses to compute
  reconcile.py        Cross-document checks
  merge.py            Applying reviewed extractions, with de-duplication
  report.py           The computation-sheet PDF
  main.py             Routes
tools/
  describe_statement.py       A statement's shape, with nothing disclosed
  validate_against_schema.py  The JSON against the department's own schema
tests/                428 tests
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

Dividends follow section 194 for TDS at the ₹10,000 threshold set by the Finance
Act 2025, section 194K for mutual funds, and the proviso to section 57(i) for the
20% interest cap.

Advance tax follows sections 207, 208 and 211, with interest under section 234C
and its first proviso as substituted by the Finance Act 2021, which extended the
relief to dividend income.

Trading follows section 43(5): clause (d) of the proviso for derivatives on a
recognised stock exchange and clause (e) for commodity derivatives, both of
which are therefore *not* speculative, against the main section for intraday
equity, which is. Losses follow section 71(2A) — never against salary — section
72 for eight years, and section 73, which rings speculation off at four.
Turnover follows the ICAI Guidance Note on Tax Audit (Revised 2023), para 5.10,
whose eighth edition dropped the full sale consideration of options from the
computation. The audit threshold is section 44AB(a) with its proviso, which
raises ₹1 crore to ₹10 crore where cash is under 5% of receipts and payments
alike.

Due dates follow section 139(1) as staggered from AY 2026-27 by the Finance Act
2026 — 31 July, 31 August for a business return without an audit, 31 October
with one — and the regime election follows section 115BAC(6) with Form 10-IEA
under Rule 21AGA, which ties the opt-out to whichever of those dates applies.

Foreign equity follows sections 17(2)(vi) and 49(2AA) for RSU vesting, ESPP
discounts and cost basis, with Rule 3(8) for fair market value, section 112 for gains on shares not listed on a recognised Indian stock
exchange, Rule 115 for currency conversion, section 90 with Rule 128 and Form 67
for the foreign tax credit, Article 10 of the India-US treaty for the 25%
dividend withholding cap, and the Black Money (Undisclosed Foreign Income and
Assets) and Imposition of Tax Act 2015 for Schedule FA.

---

## A working tool, not tax advice

This prepares a return from the documents you supply. It does not know about the
circumstances you did not tell it. Check every figure against the portal's own
preview before you submit, and take professional advice on anything contentious.
The return you file is your responsibility.
