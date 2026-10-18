# Excel and AI intake workflow

Implemented in the isolated local app at `http://127.0.0.1:8001`. The live phone app on port 8000 is unchanged.

## Everyday use

1. Open **Excel files** and download the editable workbook, or the blank template for new products/seasons.
2. Edit the **Orders** sheet. Keep the headers, existing IDs and hidden exchange sheet. Leave ID empty for a new product. Set the status dropdown: yellow In process, green Done, white Not completed.
3. Upload the edited file, review the before/after values, fix any errors, then Apply. Download a fresh copy for the next edit.

Removing a spreadsheet row never deletes its product. Existing season/brand and customer names are references; customer assignment uses the code. New customer codes require names. Group IDs must exist in the same season; create groups and set seller, distributor and special costs in the app. Payments and shipment records are never overwritten by Excel.

The full business accounting export remains available under advanced reference reports; it is not the editable upload format. The original SS26 importer remains separate. Other supplier layouts, photos and PDFs can be read through **AI to table**. Large AI spreadsheets fail with an explanation rather than silently truncating rows; use the deterministic Excel workflow for large updates.

## AI and source evidence

AI drafts are editable table rows with customer, brand, SKU, description, color, explicit size, quantity, price meaning, status, and optional contact/delivery details. Missing size and ambiguous price meaning must be confirmed. Spreadsheet source row numbers and fill-color hints are supplied to AI. Color never records a payment or settles credit.

Automatic decorative image searches and photos in order/customer/library tables are removed. Existing image files are retained. AI still receives supported source images. New AI source files and the original message are permanently archived in `data/intake-sources`, with checksums and links from the draft and saved product. These files are included in the normal data backups.

Each rendered draft has an idempotency key. Retrying a saved draft returns its existing product. All browser-required fields are checked before a bulk save; a server failure stops the sequence, keeps earlier successful rows marked, and retries only unsaved rows. This sequence is not an all-or-nothing transaction; the Excel Apply operation is.

## Safety and recovery

Excel downloads keep their comparison baseline in `data/excel-exchange`. Upload only previews changes. Apply rechecks under a SQLite write lock and commits the whole validated set atomically. Edited rows with newer app changes are blocked. Unchanged rows do not overwrite anything. Once a download is applied, renamed/resaved copies of that same download are blocked too, preventing duplicate new rows.

Seller-credit ownership and dispatched group membership are protected. Product edits do not rewrite logistics status, source evidence or seller ledger entries. Cancelled source rows remain excluded from accounting until reviewed through the full editor.

**Issues & suggestions** links to product, rule, shipment, payment or source-review screens. Source review closure requires a recorded explanation. Allocation reviews cannot close until every exactly matched source product has a confirmed customer. Closing a source note does not dismiss automated data checks.

Automatic local backups are retained. Google Drive remains an optional connection requiring account setup/sign-in on **Storage & backups**; this change does not create a connection or send email.

## Verification

Run tests with an isolated database, never the user's order database. `tests/test_excel_exchange.py` covers roundtrip export/import, new rows/seasons, no deletion on omission, replay protection, stale-edit conflicts, invalid/formula inputs, seller-credit ownership and atomic rejection. `tests/test_issue_resolution.py` covers actionable issue links, audited source-review closure, unresolved allocation protection, original image archival and idempotent AI saves. Multimodal API serialization tests verify image bytes are still passed to the model; UI tests use mocked AI, not a paid live extraction call.

`scripts/qa_excel_preview.py` is a synthetic UI fixture on port 8001 using `tmp/ui-excel-fixture.db`. It must not run at the same time as the protected local preview. `scripts/run.ps1` starts the protected local app on port 8001 only.
