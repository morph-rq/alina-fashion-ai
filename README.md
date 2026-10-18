# app

A clean FastAPI/SQLite workspace for fashion orders, customer records, collections, payments, shipping, product images, Excel exports, and customer PDFs.

## Start here (testing copy, 2026-10-18)

- Everyday work: **Intake** (`/intake`) — upload or describe, answer the questions, correct, save once,
  download Excel. Guide: [docs/operator-guide.md](docs/operator-guide.md).
- Google Drive and Gmail: [docs/drive-gmail-setup.md](docs/drive-gmail-setup.md) (account setup pending).
- Backups and restore: [docs/backup-restore.md](docs/backup-restore.md).
- What changed, how it was checked, what remains: [docs/IMPLEMENTATION_PROGRESS.md](docs/IMPLEMENTATION_PROGRESS.md).
- Run: `start-local.ps1` (127.0.0.1:8001). Tests: `..lina-fashion\.venv\Scripts\python.exe -m pytest -q`.

## What is included

- a deliberately empty SQLite database: upload a master workbook to start any season from a fresh base;
- a preview-first **Create new seasonal table** wizard that atomically creates the season, customer definitions, Brand + Season rules, brand orders, and optional Excel-pasted product rows;
- managed bulk product-image upload with arbitrary filenames, exact SKU auto-matching, searchable deterministic product suggestions, explicit user assignment, preview thumbnails, permanent storage only after creation, an image library, and auditable per-product replacement;
- fast AI intake that accepts typed text, browser speech-to-text, photos, PDFs, Excel/CSV, and text files, separates mixed evidence into editable customer/product drafts, creates customer codes from first/last-name initials, confidently routes the Brand + Season table, matches the existing catalog, and refuses to infer size from measurements or photos;
- optional Google Programmable Search image suggestions with source links and explicit approval before a selected image is downloaded;
- optional image search with deterministic relevance filtering and AI review of the strongest product-image candidates;
- incremental master-workbook imports that add safe changes, skip unchanged rows, and send ambiguous or operationally protected changes to review;
- a dynamic SS26 master dashboard;
- brand product editing plus owner-entered product creation, with read-only calculated totals and Brand + Season markup defaults;
- Ready batches with customer remaining-payment liability and factory-payment tracking;
- rules keyed by **Brand + Season** for markup, initial/Ready percentages, transport, exemptions, receipt days, and rounding notes;
- staged customer payments, customer aliases, source allocation provenance, Allocation Review, validation, and audit history;
- customer-by-customer Ready collection checks so one client's overpayment cannot hide another client's missing payment;
- separate customer-money and factory-liability ledgers, with optional factory-order payment allocation;
- a supplier Ready-list processor that matches products, creates the Ready batch, updates liabilities, and generates PDFs only for affected customers;
- an operations Inbox that shows exceptions and the next actions instead of requiring every page to be checked;
- privacy-filtered customer PDFs containing customer-facing products, final prices, payments, delivery charges/status, and balance only, plus all-customer, Ready-batch-only, and outstanding-only ZIPs;
- calculation, real-workbook import, workflow, and PDF-isolation tests.

## Start the application

PowerShell:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\scripts\run.ps1
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

For private GitHub upload and phone-accessible hosting, follow [DEPLOYMENT.md](DEPLOYMENT.md). The hosted Blueprint requires a website password and keeps business data on a persistent disk.

The database starts empty. Use **Import** in the app, or initialize it from a master workbook on the command line:

```powershell
.venv\Scripts\python.exe scripts\bootstrap_ss26.py "C:\Users\user\Downloads\Отчет_SS26 AA.xlsx"
```

Uploading the identical file is idempotent. A revised master workbook is imported incrementally: unchanged source rows are skipped, safe new/changed rows are applied, and changes to Ready, shipped, or otherwise advanced items are preserved for human review instead of being overwritten.

After the master import, the normal recurring flow is **Ready batches → Process a supplier Ready list**. The file needs an `Article`/`SKU` column; `Brand`, `Customer`/`Allocation`, `Size`, `Quantity`, and `Ready` are supported when present. Conflicts and ambiguous matches go to Import Review. Successfully matched products update the dashboard and produce a ZIP containing a fresh statement for each affected customer.

## Test

```powershell
.venv\Scripts\python.exe -m pytest
```

The test suite distinguishes mathematical exact values, two-decimal accounting values, and whole-euro display values. It also imports the real workbook, tests Ready-to-shipment recalculation, verifies customer-by-customer Ready completion and season isolation, exercises owner-entered rows, atomic season creation, arbitrary-filename image assignment and search, guarded multimodal AI intake, local spreadsheet extraction, separate multi-order drafts, confident/ambiguous brand routing, transient AI fallbacks, image filtering, reconciliation, and customer PDF isolation.

### Multimodal AI intake

Open `/ai-intake` and use any combination of a typed note, speech-to-text, product photos, PDF, Excel/CSV, or text files. A submission can create several independent drafts when the sources describe several customers or products. AI associates relevant uploaded product photos with the corresponding drafts; the selected temporary preview becomes that row's managed product image when the row is saved. Required business fields still prevent incomplete rows from being created.

For faster entry, uploaded photos suppress unnecessary web-image search for the matching draft and remaining image searches run concurrently. Brand tables are preselected only from a confident catalog or brand-name match; ambiguous brands stay unselected. A row can be saved without leaving the page, and **Create all drafts** saves every complete unsaved draft sequentially with visible progress. Failed or incomplete drafts stop safely without duplicating already saved rows in the active page.

The season selector is the accounting destination: it determines which Brand + Season rules, totals, customer statements, and reports receive the saved row. The latest/current season is selected automatically. Customer and brand can be supplied directly in the AI note; a missing Brand + Season table is created when a complete extracted draft is saved. Attachments are treated as untrusted evidence, so instructions inside uploaded documents are not executed. Uploaded documents are not retained as duplicate order data; the saved row keeps the source description for audit. A selected uploaded product photo is retained in the managed image library.

### Local SearXNG image search

The private SearXNG service is defined in `infra/searxng` and binds only to `127.0.0.1:8888`. Start it with `docker compose up -d` from that folder, inspect it with `docker compose ps`, and stop it with `docker compose down`. The AI intake uses SearXNG first and Google Custom Search only as an optional fallback.

## Navigation

- **Dashboard** - season/brand totals, dynamic shipments, saldo, customer paid/charged/payable, and factory required/paid/balance.
- **Inbox** - exceptions-only work queue for import review, allocations, collections, factory payments, shipping, and outstanding customers.
- **Orders** - normalized order list and derived amounts.
- **New table** - guided season/customer/brand setup, copied rules, bulk Excel paste, deterministic preview, and all-or-nothing creation.
- **Customers** - isolated account tables with initial payment, Ready balance, transport, status, expected receipt, and delivery state.
- **Ready batches** - Ready 1/2/3-style liabilities, customer collections, and factory payments.
- **Shipments** - rule-driven transport with destination exemptions and separate fixed logistics, suitcase, bag, and exceptional costs.
- **Payments** - auditable customer payments/refunds allocated to initial or Ready stages.
- **Validation** - SYSTEM CHECK with blocking errors and warnings.
- **Reports** - one PDF per customer or an all-customer ZIP.
- **Import** - initial or incremental `.xlsx` import, before/after change history, and raw Import Review queue.
- **Rules** - editable Brand + Season financial configuration.
- **Audit log** - linked from the footer.

Read-only JSON endpoints are also available at `/api/dashboard` and `/api/validation`; OpenAPI is at `/docs`.

The machine-readable source-fact contract for a future intake assistant is at `/api/season-intake/schema`. AI may collect or normalize explicit facts, but monetary calculations remain in the deterministic Decimal engine.

## Architecture

```text
Excel workbook
      |
      v
tailored importer --> normalized SQLAlchemy models --> SQLite
      |                         |
      v                         v
Import Review          Decimal calculation engine
                                |
            +-------------------+-------------------+
            v                   v                   v
        dashboard          validation          customer PDFs
                                                    |
                                             customer filter first
```

Important modules:

- `app/models.py` - normalized persistent business facts;
- `app/calculations.py` - centralized Decimal arithmetic and rounding;
- `app/importer.py` - per-sheet mappings plus safe incremental reconciliation;
- `app/ready_importer.py` - generic supplier Ready-list matching and quantity splitting;
- `app/operations.py` - derived exceptions-only operational work queue;
- `app/services.py` - order, season, shipment, and customer aggregation;
- `app/validation.py` - reconciliation and integrity checks;
- `app/pdf_report.py` - premium customer statements;
- `app/main.py` - FastAPI routes and web workflow;
- `app/schemas.py` - Pydantic API/input contracts.

Calculated totals are not persisted. `ShipmentItem` is the authoritative shipment assignment; `OrderItem.shipment_id` is a computed property so assignment is never duplicated.

## Workbook rules implemented

```text
base_total = wholesale_unit_price × quantity
markup_amount = base_total × markup_percentage
final_total = base_total + markup_amount

required_prepayment = final_order_total × 50%
saldo = final_order_total - ready_value - shipment_values

initial_due = final_order_total × BrandSeasonRule.initial_prepayment_percentage
ready_balance_due = Ready-batch goods × BrandSeasonRule.ready_balance_percentage
current_merchandise_charge = initial_due + ready_balance_due
total_charged = current_merchandise_charge + shipment fees + explicit expenses
```

The operational workflow now models the two stages explicitly. An initial percentage is allocated when the combined factory order is placed; the remaining percentage is due only for the customers whose goods enter a Ready batch. Historical fulfilled SS26 items are attached to `Ready 1 (Imported)` because the workbook does not retain original Ready-batch identity.

Historical applied markup remains on each item. Defaults and future behavior come from the Brand + Season rule, so changing FW26 cannot rewrite SS26. The normal transport rule is 5%; exempt destinations such as Switzerland (`CH`) resolve to 0%. Fixed and suitcase costs remain separate.

## Reference reconciliation snapshot

Importing the supplied reference workbook should produce:

- final order value: `114,599.25`;
- required per-order prepayments, rounded to cents then summed: `57,299.65`;
- Ready: `26,396.00`;
- saldo: `17,851.05`;
- paid: `106,175.00`;
- current staged merchandise liability: `105,673.84`;
- imported explicit expenses: `5,057.54`;
- charged after two-decimal accounting quantization: `110,731.38`;
- payable across all internal records: `4,556.38`.

The corrected importer identifies four Dior rows whose raw descriptions name two customers although the source quantity is one. Those products are intentionally unallocated and appear as blocking validation issues; customer-facing totals therefore exclude them until review. The whole-season factory liability still includes them.

## Assumptions and unresolved questions

- Supplier/detail tables are the primary incoming source. Customer descriptions are parsed into per-unit allocations, with aliases applied before splitting.
- Blank customer fields default to `AA` only because this particular import is explicitly configured as the AA workbook context. Explicit `BB` and `Sp` values are separate customers.
- Four actual Dior descriptions (`IdS/SV` plus `AA` with quantity 1) cannot be split safely. They retain file/sheet/row/raw text in Allocation Review and are not assigned by guesswork.
- Payment rows do not contain a customer field; they are assigned to AA based on the workbook context. Confirm this before production use.
- Imported payments have no initial/Ready reference, so they remain stage-unallocated and validation flags them. New payments can be allocated explicitly.
- Quantity/price rows whose workbook total is zero, blank, or broken are imported as `Cancelled` and flagged, preserving the source order total without silently charging them.
- Readiness ranges omit years and include an invalid `31/04` range; they remain source notes rather than guessed dates.
- Historical transport lines sometimes group several shipments. They are imported as explicit customer expenses. New shipments use deterministic configured fee rules.
- The workbook has one corrupted embedded image and broken `#REF!`/cached `#VALUE!` content. These remain visible in Import Review.
- Later PDFs show Sped 17, 18, and 21, while the primary workbook stops at Sped 16. The UI supports dynamic creation, but later PDF facts are not silently merged into the Excel import.

See `docs/workbook-analysis.md` for the cell/formula analysis and `docs/business-rules.md` for the authoritative calculation policy.

## Clean-base policy

No source workbook, extracted image, generated statement, or seeded business record is committed as live application data. Imported source copies are retained under timestamped folders in `data/imports/` for auditability until the operator intentionally archives them. The original files selected by the user are never modified.

Each tracked import on the Import page has a **Delete stored file** action. It removes only the timestamped project copy; normalized accounting records, review decisions, and the audit trail remain intact. This is intentional—a source-file cleanup must never silently alter financial totals.

The Import page also has a typed-confirmation **Delete all imported data** action for starting over. It clears every business table plus managed source workbooks, extracted product images, and generated PDFs/ZIPs. Application code and original files outside the project are never removed.
