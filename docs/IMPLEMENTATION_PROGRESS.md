# Implementation progress (testing copy)

Working copy: `alina-fashion-local` (127.0.0.1:8001). The operational `alina-fashion` copy, its database,
launchers, port 8000 and tunnel were only read for comparison and not changed. `alina-fashion-recovery` was
not touched. No packages were installed: the shared interpreter `..\alina-fashion\.venv\Scripts\python.exe`
already provides `cryptography`, `httpx` and `openpyxl`.

Resume from this file. "Tested" means automated tests or the recorded check below. Google is simulated in
tests; **no real Google account has been connected**.

## Baseline (2026-10-18)

* Snapshot before changes: `backups/claude-baseline/testing-copy-20261018.tar` (+ `.sha256`). A local git
  repository now records every stage (`git log`); business data is git-ignored.
* Test baseline: 108 passed, 1 failed (outdated library assertion). With the workspace `.env` credentials
  most route tests returned 401; `tests/conftest.py` now isolates settings, database, secrets and folders.
* Run tests: `& ..\alina-fashion\.venv\Scripts\python.exe -m pytest -q` (now **263 passed**).
* Visual preview without login or live services: `python scripts\dev_preview.py` (127.0.0.1:8001 only), or
  `python scripts\ui_preview.py` for a throw-away copy in `tmp/ui-preview` with synthetic records.

## Capability / migration map (main vs testing)

| Area | Main copy | Testing copy (before) | Release baseline decision |
| --- | --- | --- | --- |
| Factory liability basis | Wholesale base | Customer final price | Main's rule ported, purchase-group aware |
| Customer initial shares | Cent-exact `allocate_money` | Per-item rounding | Ported; now per row with complementary Ready share |
| Delivery estimate | Needs full season payment | Ready collection only | Ported (main's test) |
| Groups | `GoodsGroup` fee snapshots | `PurchaseGroup` + seller credit | Keep purchase groups; no second group system |
| Excel round trip | `excel_edit` with operational protection | `excel_exchange` workbook | Keep exchange workbook + operational protection |
| Private client Excel | Allowlisted `excel_views` | Internal workbook filtered | Allowlisted workbook ported and fixed |
| Brand views / master summary | Present | Missing | Ported (linked formulas) |
| AI intake save | No server idempotency | Token idempotency | Replaced by persistent drafts + receipts |
| Backups | DPAPI archives via launcher | Unencrypted zips, unencrypted Drive upload | Replaced: verified snapshots, portable encryption, verified upload |
| Threema | Implemented | — | Not ported (outside this brief) |
| Login launcher (`serve_local.py`) | Present | Basic auth if configured | Not ported; port before serving beyond loopback |

## Stage 1 — correctness (implemented, tested)

* FIX-02 refunds labelled and signed in PDF, Excel and pages; net row equals the summary.
* Repeated partial Ready rows use the split remainder; splits keep full provenance, collections and
  `split_from_item_id`; System Check does not mistake splits for duplicates.
* Reverse final pricing policy (`app/pricing.py`); amount reading without guessing decimal locale.
* Versioned, run-once data migrations (`schema_migrations`); restarts no longer reallocate reviewed rows.
* One liability path in `services.py`; fully Ready rows are charged exactly their value
  (real data: customer BB charged 4,095.95 = order value; was 4,095.96).
* Checked with `scripts/migration_check.py` on copies of the untouched baseline database and of the current
  testing database: migrations applied once, none on restart, **no allocation changes**.

## Stage 2 — shared services, resolution flows, reset scope (implemented, tested)

* `app/item_service.py`: one validation/mutation path for the product form, worksheet editor, Excel apply,
  intake and allocation resolution. Revision checks (409 on stale), Ready/shipped financial edits need a
  recorded reason, Excel/AI cannot rewrite them, per-field audit.
* FIX-01/05: allocation notes resolve from operator evidence by splitting units across customers; the reason
  is stored and System Check before/after is shown. Import notes store their resolution text and time.
* FIX-06: `/workspace/reset` shows exact scope (including manual records and audit history), writes a
  verified snapshot first, and requires `RESET WORKSPACE`; Ready imports can be reversed while nothing
  depends on them; master imports are undone by restoring a snapshot.

## Stage 3 — persistent conversational intake and Excel (implemented, tested)

* `/intake`: sessions, messages, sources, draft rows with field states/evidence/history, derived questions,
  operator edits and answers, corrections of saved goods, payment drafts, idempotent save receipts, separate
  downloads. AI optional; manual completion works without it.
* Excel: formulas use Decimal rounding and ignore cancelled rows; App final total, Checks sheet with export
  metadata, master summary, brand views; text cells stored as text. Verified by rule check and by real
  LibreOffice recalculation (`app/recalculation.py`): **143 real product rows, 0 mismatches, 19/19 checks OK**.
* Customer workbook is allowlisted (no wholesale/markup/notes/other customers); internal version separate.

## Stage 4 — unified inbox and durable jobs (implemented, tested)

* `source_store`: bytes stored, fsynced and hash-checked before any row says "captured"; content-addressed
  blobs; identity (channel, account, remote id, version) deduplicates replays.
* `/inbox` with states, reasons, ignore with reason, open in intake, dead-job retry.
* `jobs`: SQLite queue with `BEGIN IMMEDIATE` claims, leases (resume after crash/restart), single-owner
  worker lease, backoff, dedupe keys. Worker starts with the app (`FASHION_BACKGROUND_WORKER`).

## Stage 5 — Google Drive and Gmail (implemented, tested with a simulated Google; account setup pending)

* OAuth web flow with PKCE, state cookie + server-side hash, granted-scope check, account-ownership check on
  reconnect, offline refresh token kept, revocation on disconnect, loopback/auth-guarded routes.
* Drive: picked files (`drive.file` + Picker), folder watch (`drive.readonly`) with listing then change
  tracking, versions as new evidence, removal noted without deleting, native export format recorded,
  checksum-verified downloads, failed items queued for retry before the checkpoint moves.
* Gmail (provider chosen by the user): read-only query + start date, full sync then history, 404 → bounded
  full resync with dedupe, raw message stored, MIME parsing, attachments as child items, HTML to text without
  remote loads, never modifies mail.
* Outbox: versioned season workbooks and recovery archives, queued offline, idempotent by content hash,
  counted only on MD5 match.
* **Pending (account owner):** Google Cloud project, consent screen/test user, Web client with the redirect
  URI, optional Picker key + project number, then connecting and choosing sources. See
  `docs/drive-gmail-setup.md`.

## Stage 6 — recovery (implemented, tested)

* `app/recovery.py` snapshots (online backup, integrity check, manifest) and restore into a new folder;
  `app/recovery_crypto.py` portable AES-256-GCM archives keyed by a recovery code; upload only after the code
  is confirmed stored; `/recovery` status, restore exercise; `scripts/restore_recovery.py` for a replacement
  computer.
* Real check on the testing copy: snapshot 341 files / 400 rows / 77 MB, restore exercise passed. Encrypted
  archive, upload and code-only restore are covered by tests (simulated Drive). A real recovery code was not
  created (the operator should create and store their own).

## Independent code review (2026-10-18)

A read-only review of the new modules found 15 issues; all were fixed with a regression test each
(`tests/test_review_fixes.py`): Gmail mail labelled after arrival was missed; one oversized e-mail blocked a
Gmail source; outbox failures were rolled back (duplicate folders/uploads, no reconnect state); syncs held
the SQLite write lock during downloads and job leases were not renewed (now per-item commits, WAL with a
busy timeout, lease heartbeat); duplicate new customers in one save; the connection's full-scope token was
sent to the Picker page and there was no Host check (now a separate drive.file-only token and a host
allowlist); a wrong-account reconnect could revoke another connection; non-Latin evidence names failed to
download; empty attachments made Gmail retries fail; reset removed connection settings and could stop the
worker; a rejected access token was not refreshed; malformed separators were read as large amounts; zero
final prices were accepted; inbox items started in the newest season silently; the backup fingerprint missed
in-place edits.

## Minimal interface (2026-10-20)

* Four areas (Overview, Workspace, Inbox, Customers) replace 22 navigation links; Search, AI Help, Add and
  Settings sit in the top bar. Workspace has one view switch (Products, Brands, Ready & shipping, Money).
  New routes: `/logistics`, `/money`, `/brands`, `/settings` (`app/ui_routes.py`); navigation in
  `app/navigation.py`; one style sheet `app/static/ui.css` and one script `app/static/app.js`.
* Every old page URL redirects with its query; POST contracts are unchanged apart from an optional, same-site
  `next` return target (`safe_next`). Feature map and checks: `docs/ui-feature-map.md`; operator version:
  `docs/navigation-guide.md`.
* Fixed on the way: stale intake receipt balances, Enter in intake questions, empty factory-order list,
  shipment rule checkbox that could not be switched off, server error on a damaged image upload.
* Payments, shipments and expenses still go to the newest season by design.

## Upload check, colours and cost corrections (2026-10-24)

* **Upload check** `/imports/{id}/check` (`app/upload_check.py`, `upload_check.html`): after a master workbook
  upload the app opens a page listing every problem of that file: open customer splits, source notes, protected
  changes and the product checks System Check runs (no customer, missing price/size, markup exception, total
  mismatch, Ready/shipment gaps). Each has a fix link; product edits return to the check. Products from the file
  are listed with their colour, filters (all / with problems / each colour) and bulk *Mark as*. **Export check
  to Excel** writes a coloured Problems + Products workbook; **Save check to Drive** queues it through the
  verified outbox. Also reachable from Excel & statements › *Check the last upload* and each import history row.
* Colours: *Not completed* is now **red** (was white) in the worksheet, legends and the editable Excel export;
  In process stays yellow, Done green. The label text is unchanged so Excel round trips keep working.
* A master workbook that cannot be read now shows the reason on the import page and keeps nothing (was a server
  error). Re-uploading the same file opens the earlier check instead of importing twice.
* Shipment costs (date, status, fixed/suitcase/bag, rule or own transport %) and exceptional expenses can be
  corrected after saving (Ready & shipping › *Edit costs* / *Edit*), each field audited, negatives refused.
* Fixed: System Check crashed for an order without a Brand + Season rule; it now reports `MISSING_BRAND_RULE`
  (warning, links to Rules) and uses the default rule. Product checks are shared via `validation.item_issues`.
* Checked: 263 tests; in the isolated preview (synthetic upload) the check page, bulk marking (6 products →
  In process, returned with "Updated 6"), Excel export (200, xlsx) and the cost drawer on desktop; the check page
  on a 375 px phone without sideways scrolling. Saving the check to a real Drive was not tested (no account).

## Tables by description, Excel-style editing, Claude settings (2026-10-24)

* **Workspace › Tables** (`/tables`, `app/tables_routes.py`, `app/table_planner.py`): describe a table; AI (or a
  built-in parser when AI is off) drafts either a season with brand tables (Season, Brand, Brand + Season rule,
  Order; existing tables and rules are never overwritten) or a free-form custom table (named, typed columns,
  optional example rows). Nothing is created before **Create**. Custom tables (`custom_tables`,
  `custom_table_rows`) have a spreadsheet grid with column letters and row numbers, click-to-edit cells (Enter /
  Tab save, Esc cancels, typed columns validated, row revision prevents overwriting newer values), add rows,
  add rows by describing or pasting them, add columns, delete rows/tables (audited) and `.xlsx` export. They never
  feed accounting.
* **Edit in cells:** worksheet (colour, size, quantity) and upload check (customer code, colour, size, quantity,
  wholesale, markup) save one cell at a time through `item_service.change_item` with the item revision; Ready or
  shipped money fields are refused there and stay in the product editor, which records a correction reason.
* **Spreadsheet look** for every table: grid lines, grey header, frozen header, Excel-green focus; on phones the
  Workspace switch is one swipeable row.
* **Settings › AI assistant** (`/settings/ai`, `app/ai_settings.py`): paste a Claude key (stored with
  `secret_store`, never shown back, excluded from backups), choose Claude Sonnet 5.5 or Claude Haiku 5.5, test it.
  A key saved here overrides `.env`. Claude defaults changed from Opus 5.5 to **Sonnet 5.5 with Haiku 5.5 as
  fallback** (user request); `FASHION_AI_MODEL` still overrides when the key comes from `.env`.
* Setup: `requirements.txt` now lists `cryptography` (it was only available through the shared interpreter);
  `setup.ps1` creates `.venv`; `scripts/run.ps1` prefers the project's own `.venv`. README has download steps.
* Checked: 268 tests. In the isolated preview (narrow pane ≈ phone width): described a supplier table with an example
  row → draft → created → edited the Phone cell (saved, survived reload); edited a size in the upload check; AI page
  and Tables view rendered. Not checked: a real Claude key (tested with a simulated API only).

## Guided tour and localhost check (2026-10-24)

* **Guide** button in the top bar, **Take the 2-minute tour** on Overview, or `?tour=1`: `app/static/tour.js`
  highlights one part at a time (Overview, Workspace views, colours, cell editing, bulk marking, Excel, Tables,
  Inbox, Customers, Settings, Claude) and moves between pages itself; arrow keys, Esc to end; docks above the tab bar
  on phones. Read-only: it never changes data. `tests/test_tour.py` checks every step page loads.
* `scripts/ui_preview.py --port N` runs the synthetic demo next to a real app that already uses 8001.
* Localhost check on the synthetic demo (127.0.0.1:8011): all 54 GET pages requested; 52 returned 200 and the two
  JSON helpers returned the expected 422 without query parameters; no server errors in the log. The tour was
  walked on desktop and at phone width. The real testing app the user had running on 8001 answers 401 because a
  login is configured in its `.env`; it was not stopped or changed.

## Remaining work and decisions

* Connect a real Google account and verify Drive/Gmail end to end (needs the account owner).
* Create the recovery code and run a restore exercise with the written code.
* Port `serve_local.py` (enforced login, security headers) before serving this copy beyond loopback.
* Intake: AI follow-up interpretation is wired but only tested with fakes; Ready/shipment/rule requests link
  to their pages rather than being drafted in the conversation.
* IMP-10 (second supplier layout adapter with mapping preview) not implemented; unknown layouts are refused.
* Release to the operational copy: run `scripts/migration_check.py` on a copy of its database first, then
  merge deliberately; nothing has been copied there.
* Business decisions still open: interface language, evidence retention period, multi-currency/VAT rules,
  multiple operator accounts.
