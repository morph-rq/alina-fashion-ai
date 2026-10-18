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
* Run tests: `& ..\alina-fashion\.venv\Scripts\python.exe -m pytest -q` (now **168 passed**).
* Visual preview without login or live services: `python scripts\dev_preview.py` (127.0.0.1:8001 only).

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
