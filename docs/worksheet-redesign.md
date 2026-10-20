# Workbook redesign — local preview

Open http://127.0.0.1:8001. The sibling phone app on port 8000 is unchanged.
The original Downloads workbook is not modified. A byte-identical copy is stored
under this workspace's `data/imports/ss26-original/`.

## Product coverage and source interpretation

The SS26 import has 143 product records, 146 ordered/planned units, 17 brands and
9 historical shipments. Every product record appears exactly once across the
32 yellow, 70 green and 41 white records. Three previously omitted The Row bags
(rows 13–15) are retained with broken-total warnings; the previously omitted
Dior skirt reference (row 5) is retained with zero units and no invented price.
The source's confirmed order value still reconciles to EUR 114,599.25.

Yellow means available / ordered with distributor / in process. Green means
done. White means not completed or unavailable. Worksheet status is stored
separately from legacy logistics status. Excel yellow/amber and green fills take
precedence; other hues are flagged, with existing shipment evidence used where
the fill does not match a stated color. White/shipment and yellow/shipment
conflicts keep their source color and are flagged. Source file, row, fills, raw values and customer notes remain
inspectable. Ambiguous customer allocations remain unallocated, not guessed.
Legacy cancelled product rows stay visible in the workbook and customer history,
without becoming confirmed merchandise charges just because a row exists.

## Groups and costs

Existing shipments become groups; other products are initially grouped by brand
and color. Distributor and seller names are intentionally blank where the source
does not provide them. Use Groups to create parties/groups; select products in
the workbook to move them. Credit history prevents unsafe reassignment.

Each group has its own initial percentage, shipment percentage, transport rate,
actual special cost and cost split method. Merchandise stages must total 100%.
Defaults: 50% initially, then 50% + 5% of eligible product selling value at
shipment. The 5% is not added a second time when credit is settled. CH customers
retain the existing transport exemption. Special costs are split by product
value, quantity or equally per product record, using a largest-remainder cent
allocation. Customer statements include each customer's share once. These are
additional actual costs, so do not re-enter historical expenses already imported.

Dispatch operates on an entire active yellow group and creates logistics links.
It does not assume payment, consume credit, or mark delivery complete. Completion
colors and invoice settlements are separate explicit actions.

## Seller credit

Customer receipts in the source do not establish who holds seller-side funds.
No seller payments are inferred from the imported EUR 106,175 of customer receipts.
Use Seller credit to record actual bank-confirmed initial, shipment or historical
prepayments, their date and reference. For white items within a mixed group,
attach the prepayment to the individual item. Group-level payments can optionally
be reserved for a named customer; otherwise they are distributor funds.

50% + 5% shipment payments remain prepayments until settled against a final invoice
or refunded. White-product balances can move to another group only with the same
seller, distributor, currency and customer ownership. Transfers create matching
debit/credit entries in one transaction; SQLite write locks prevent double-spend.
Submission keys prevent duplicate payments on resubmission. Entries are append-only
in the UI. This is an accounting record; no bank transfer is initiated by the app.

## Backups and Google Drive

The former Storage page is now **Settings → Recovery** (`/storage` redirects there); current Drive and Gmail
setup is described in `drive-gmail-setup.md`, which supersedes the steps below.

Storage creates versioned ZIP snapshots containing a consistent SQLite backup,
source workbooks, stored product images, library uploads and generated reports.
Each snapshot passes SQLite integrity, archive CRC and per-file SHA-256 checks.
Successful app edits queue a backup; the worker checks every 30 seconds while the
app runs. Backup state shows pending writes and errors. No automatic deletion is
performed; monitor disk/cloud quota. Configuration and OAuth tokens are excluded.

Google Drive is implemented but **not connected** in this preview. To activate:

1. In the intended Google Cloud project, enable Google Drive API and configure
   the OAuth consent screen. Create an OAuth **Web application** client.
2. Register `http://127.0.0.1:8001/storage/google/callback` exactly.
3. Download the client JSON and upload it on Storage. It is encrypted using
   Windows DPAPI for the Windows user running the app.
4. Click Connect with Google and sign in to the account that should own backups.
5. Click Back up now; verify a successful cloud-copy timestamp on Storage.

The `drive.file` scope permits app-created files, not all Drive/email contents.
Backups go to an app-created `ALINA Fashion Backups` folder. Uploads use resumable
chunks; completed copies are verified against Google's MD5 checksum. Failed
attempts preserve the local backup and retry while running. A SHA-256 app property
allows a completed-but-unacknowledged copy to be discovered without duplication.
No real-account upload was tested without authorization credentials. Mocked API
tests exercise upload and checksum confirmation. This is backup, not bidirectional
sync; email sending and public cloud deployment are not enabled.

OAuth setup details: https://developers.google.com/identity/protocols/oauth2/web-server
Scopes: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
Uploads: https://developers.google.com/workspace/drive/api/guides/manage-uploads

## Recovery

Download a verified snapshot and run `verify_snapshot` against it before restoring.
Extract into a separate folder; inspect `manifest.json` and the restored database.
Stop only this local app before replacing its `data` files, preserving the existing
data in another backup first. Do not restore over a running SQLite database. OAuth
credentials are deliberately not portable: reconnect Google on a new computer.
No restore/deployment is automatically applied to the phone app.

## Tests

Run from this workspace with a separate test database and test-process-only auth
configuration. The existing test suite and `tests/test_worksheet.py` exercise
source reconciliation, newly retained products, account isolation, exact cost
allocation, concurrent transfers, submission idempotency, custom payment stages,
source-safe exports, snapshot integrity and protected token storage. Background
backup work is isolated from user backups by `tests/conftest.py`.
