# Current state and correction scope

## Added: guided seasonal table creation

The app now has a **New table** workflow at `/seasons/new`. It validates a new season before saving, can copy matching Brand + Season rules from an earlier season, accepts customer and brand definitions, and supports tab-separated product rows pasted directly from Excel. The preview derives order totals and initial requirements without writing to the database. Final creation is all-or-nothing and records an audit event.

Each created season receives its own generated master table at `/seasons/{season}` and each brand order has a season-specific detail route. The AI intake screen at `/ai-intake` extracts source facts from typed messages, browser speech-to-text, photos, PDFs, Excel/CSV, and text files. It separates mixed evidence into one draft per customer/product/color/size allocation and matches existing catalog products. Required business fields prevent incomplete drafts from saving without adding a separate consent checkbox. It never calculates money or infers product size from body measurements or photos. The machine-readable contract remains available at `/api/season-intake/schema`.

Multimodal uploads are submitted directly from the intake composer without a consent gate. Documents and images are treated as untrusted evidence, embedded instructions are ignored, conflicting facts become questions, and unsupported or oversized files fail clearly. Excel cell values are extracted locally before the structured request. Source filenames and the extraction summary are retained with a saved row for audit. Uploaded product photos are validated and temporarily staged; AI associates relevant filenames with individual drafts, and only the selected photo for a saved row becomes a managed permanent product image. Other uploaded binaries are not duplicated into accounting storage. Speech-to-text remains local in the browser until the user submits it.

Product images can now be uploaded in bulk during seasonal-table creation without renaming files. Real JPG/PNG/WEBP files are validated, exact article/SKU filenames attach automatically, and other filenames receive searchable deterministic suggestions based on article, product name, brand, and color. A user must explicitly choose every non-exact match; the system never guesses. Preview images become permanent only after atomic season creation succeeds. The searchable image library finds saved images by product, SKU, brand, color, customer, season, or original filename, and every brand product row also supports an auditable manual add/replace action.

AI intake product-image discovery can use a private search service. Deterministic title relevance removes obvious noise, then AI examines the candidate thumbnails with the extracted brand/product/article/color facts. A high-confidence candidate is marked and preselected as `AI recommended`, while the source page remains available to open.

## Baseline inspected on 2026-08-23

The existing project is a FastAPI application backed by SQLite and SQLAlchemy. It already imports the supplied `Отчет_SS26 AA.xlsx` workbook, normalizes product rows, derives money with Python `Decimal`, exposes brand and customer tables, tracks Ready batches and Sped shipments, generates customer-filtered PDFs, records audit events, and runs a visible SYSTEM CHECK.

The supplied workbook and all five SS26 PDFs were inspected as business references. The workbook is the arithmetic source; the PDFs confirm the table-oriented visual language, item images, brand pages, customer codes, and later dynamic shipment columns. The source workbook contains known damaged media/package metadata and broken cached formulas, so the importer preserves those discrepancies for review rather than copying them into accounting totals.

## What is already correct

- Product rows are the financial source of truth; dashboard, customer tables, Ready/Sped totals, and PDFs are calculated from them.
- Money uses centralized `Decimal` functions with explicit `ROUND_HALF_UP` cent and whole-euro display rounding.
- Brand rules are keyed by Brand + Season, while historical applied item markup is retained.
- The remaining factory percentage is derived only from items assigned to a Ready batch.
- Factory payments and customer payments are separate records.
- Shipment assignment has one authoritative join record, preventing an item from belonging to two shipments.
- The importer understands the heterogeneous SS26 brand sheets, retains source file/sheet/row/raw values, and routes uncertain allocations to review.
- Customer reports filter by customer before calculating or rendering, so another customer's products cannot leak into a statement.
- Switzerland can be exempted from percentage transport through Brand + Season rules.
- The existing 20-test suite passes, the prototype verifier reconciles the reference workbook totals, and all primary application routes return HTTP 200.

## Conflicts and gaps found

- Brand tables can edit an imported product but cannot create a new owner-entered product row.
- Ready-batch payment completion is checked only at aggregate batch level; one customer's overpayment could incorrectly mask another customer's missing Ready payment.
- The dashboard does not yet expose factory money paid and remaining factory liability alongside customer money received.
- Factory payments cannot be optionally tied to a specific factory/brand order.
- Shipment costs combine suitcase and bag costs; bag cost needs its own source field, and there is no UI for exceptional manual expenses even though the model supports them.
- The customer PDF does not show expected receipt in its summary and does not show each shipment's allocated costs/status.
- SYSTEM CHECK needs more explicit customer-level payment allocation checks, shipment/date checks, and the requested clean-state wording (`Difference: EUR 0.00` / `All calculations reconcile`).

## Changes in this milestone

- Add owner-created product rows to existing brand orders, defaulting markup from the applicable Brand + Season rule and auditing the creation.
- Make Ready collection completion customer-by-customer and use that result for the two-week expected receipt date.
- Surface factory required/paid/balance values and permit factory payments to reference an order.
- Scope customer payments, shipments, and expenses to a season so FW26 facts cannot leak into SS26 customer accounting.
- Separate bag, suitcase, fixed logistics, and exceptional expenses in the data-entry workflow.
- Extend customer statements/PDFs with expected receipt and shipment cost/status detail.
- Strengthen SYSTEM CHECK and add deterministic workflow tests covering partial Ready liability, customer allocation, payment completion, shipment transport including Switzerland, factory liability, and PDF generation.

All changes are additive. Existing routes, imported SS26 records, workbook reconciliation behavior, and historical source provenance are preserved.

## Verification on 2026-08-24

- The full suite at that checkpoint passed: **39 tests**; the later usability pass below expands it to 41.
- The restarted app is live on `http://127.0.0.1:8001`.
- The AI intake page was visually checked with the working-season explanation, optional typed input, multi-file picker, speech-to-text controls, and a responsive two-source layout.
- The optional local image-search service runs on `127.0.0.1:8888`.

## Usability and performance pass on 2026-08-24

- The expanded suite passes: **41 tests**, and all 18 primary UI/API routes return HTTP 200.
- A synthetic image-order test through the configured AI service correctly extracted customer, brand, product, explicit size, factory price meaning, and source filename without saving a database row.
- Brand-table routing is now confidence-gated; an ambiguous brand never silently falls back to the first table.
- Uploaded product photos skip redundant web search, and independent image-enrichment jobs run concurrently for the first five drafts.
- File count and size limits are checked in the browser before upload.
- Users can save one draft without leaving the page or create every complete unsaved draft with one button, visible progress, and per-row error stopping.
- The current imported SS26 data remains intentionally unreconciled: SYSTEM CHECK shows four unallocated products plus customer initial/Ready allocation differences. These source-data issues were not silently changed.
