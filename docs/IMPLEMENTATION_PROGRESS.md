# Implementation progress (testing copy)

Working copy: `alina-fashion-local` (port 8001). The operational `alina-fashion`
copy, its database, launchers, port 8000 and tunnel are read-only references and
were not changed. `alina-fashion-recovery` was not touched.

Resume from this file. Each stage lists what was implemented, how it was
checked, and what remains. "Verified" means an automated test or a recorded
check below; mocked provider tests are not live-account verification.

## Baseline (2026-10-18)

- Snapshot before any change: `backups/claude-baseline/testing-copy-20261018.tar`
  (+ `.sha256`). A local git repository was initialised in this folder for
  reviewable history; business data stays git-ignored.
- Python: the shared interpreter `..\alina-fashion\.venv\Scripts\python.exe`
  (3.12). No packages were installed or upgraded. `cryptography`, `httpx` and
  `openpyxl` already present there cover all new work.
- Test baseline: 108 passed, 1 failed (`test_library_supports_collections_and_standalone_brands`,
  an outdated assertion about library product images, which this copy removed on purpose).
  Tests also read the workspace `.env`, so with credentials configured most
  route tests returned HTTP 401. `tests/conftest.py` now neutralises
  credentials, the database URL and live AI/search before the app is imported.

Run the suite:

```powershell
& ..\alina-fashion\.venv\Scripts\python.exe -m pytest -q
```

## Capability / migration map (main vs testing)

| Area | Main copy | Testing copy | Decision for the release baseline |
| --- | --- | --- | --- |
| Factory liability basis | Wholesale base (`factory_initial_due`, `factory_ready_due`) | Customer final price | **Main's rule ported**, made purchase-group aware |
| Customer initial shares | Cent-exact `allocate_money` | Per-item rounding (can differ by cents) | **Ported**, now per row with complementary Ready share |
| Delivery estimate | Requires every affected customer's full season charges paid | Ready collection only | **Ported** (test from main) |
| Groups | `GoodsGroup` fee snapshots as expenses | `PurchaseGroup` + distributor + seller credit | Keep testing's purchase groups; do not add a second group system (IMP-13) |
| Excel round trip | `excel_edit` on business export, protects Ready/shipped/grouped rows | `excel_exchange` dedicated workbook, new rows, replay-proof | Keep `excel_exchange`; add main's operational-row protection (stage 3) |
| Private client Excel | `excel_views` allowlisted workbook | Business workbook filtered | To port in stage 3 |
| AI intake save | No server idempotency | Confirmation token + `BEGIN IMMEDIATE` | Keep; replace with persistent drafts (stage 3) |
| Source archive | — | `intake_archive` | Keep; becomes the content-addressed source store (stage 4) |
| Backups | DPAPI-encrypted archives from launcher | Unencrypted verified zips + Drive `drive.file` upload | Replace with portable encrypted recovery archives (stage 6) |
| Threema sending | Implemented | — | Not ported (not part of this brief); remains main-only |
| Login launcher | `serve_local.py` enforces credentials, security headers | Basic auth middleware only when configured | Consider porting when this copy is served beyond loopback |

## Stage 1 — correctness (done)

Implemented (commits in local git):

- **FIX-02 refunds**: one sign helper (`calculations.signed_payment_amount`);
  PDF payment rows say "Refund" with a negative amount and end with "Net payments
  received" equal to the summary; Excel Payments sheet adds a signed "Net effect"
  column; customer and payment pages show signed amounts.
- **Repeated partial Ready rows**: a split remainder stays available to later rows of
  the same upload (previously the second row went to review).
- **Split provenance**: `ready_importer.split_item` copies every descriptive,
  price, allocation and provenance column, copies collection membership, records
  `split_from_item_id` and an audit entry. System Check no longer mistakes a split
  for a duplicate allocation.
- **Final-price reverse rounding**: `app/pricing.wholesale_from_final`; when no cent
  wholesale reproduces a stated final, the final is kept as the selling price and
  the derivation is recorded in the audit text.
- **Versioned migrations**: `schema_migrations` table; data migrations run once.
  The legacy allocation reparse no longer removes a customer assignment on
  restart and skips rows with a reviewed allocation note. Manual/AI rows are not
  relabelled with the reference workbook filename.
- **One liability path** in `services.py`: `item_rates`, `item_initial_shares`,
  `client_initial_charges`, `customer_ready_due`, `factory_initial_due`,
  `factory_ready_due`, used by statements, dashboard, System Check, To-do, Ready page.
  A fully Ready row is charged exactly its value (initial + Ready shares are complementary).
- `OrderItem.revision` (SQLAlchemy optimistic version counter) for stale-edit detection.

Checks:

- `tests/test_stage1_regressions.py` (reproduced each defect first, then fixed).
- `tests/test_ledger_consistency.py` (3 tests ported from main + group/To-do/exact-charge cases).
- `scripts/migration_check.py data/fashion_orders.db` on a temporary copy:
  three migrations applied once, none re-applied on restart, **no allocation
  changes**, customer BB charged now equals order value (4,095.95; was 4,095.96).
  Remaining System Check errors on that copy are business-data issues
  (4 unallocated products → initial/Ready allocation differences), not code defects.

## Stages 2–6

See the sections below as they are completed.
