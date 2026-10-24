# Interface feature map (old → new)

Updated 2026-10-20 for the minimal-navigation redesign of the testing copy. A shorter, operator-facing version
is [navigation-guide.md](navigation-guide.md).

Inventory source: route registrations in `app/*.py`, every template in `app/templates`, form actions,
JavaScript handlers and the test suite. The old shell had **22 main-navigation links** (6 primary,
16 under "More tools", `/excel` listed twice) plus a 5-item mobile bar, three separate in-page tab rows
(`wb-tabs`) and a footer link.

## New navigation

| Primary area | URL | Also highlighted for |
| --- | --- | --- |
| Overview | `/` | `/operations`, `/validation` |
| Workspace | `/worksheet` | products, brand tables, seasons, Ready & shipping, Money, Excel, import, product images, manual entry |
| Inbox | `/inbox` | `/intake…`, `/ai-intake`, `/source-reviews…`, `/intake-sources…`, `/sources/…` |
| Customers | `/customers` | `/customers/{id}`, `/reports` |

Utility controls in the top bar (the bottom bar on phones keeps the same four areas plus **Add**):
**Search** (Ctrl K, `/api/search`), **AI Help** (business helper drawer), **Add** menu, **Settings** (`/settings`).
Settings is highlighted for `/settings`, `/rules`, `/library`, `/connections`, `/recovery`, `/storage`,
`/workspace/reset` and `/audit`.

Workspace has one switch with four views: **Products** (`/worksheet`), **Brands** (`/brands` → `/seasons/{season}`
→ `/orders/{id}`), **Ready & shipping** (`/logistics`), **Money** (`/money`). Excel and statement downloads sit in
the Workspace bar menu next to the records they export.

Settings (`/settings`) groups: **Business** (rules, library), **Connections** (Drive/Gmail, captured sources),
**Recovery** (backups/restore, clean-up & reset), **History and checks** (audit history, System Check, imports
and protected changes).

## Capability map

Legend for *Check* (all on synthetic data, never on operational data):
**B** = used in the browser on the isolated preview, **H** = submitted over HTTP exactly as the page renders the
form (field names, hidden `next`, defaults), **T** = automated test, **R** = redirect/compatibility test in
`tests/test_ui_navigation.py`, **—** = not re-exercised (route and template behaviour unchanged).

### Overview, To-do, checks, history

| Capability | Old route / action | New location | How the operator reaches it | Check |
| --- | --- | --- | --- | --- |
| Season summary (order value, payable, Ready, saldo, factory) | `/` (dashboard) and `/seasons/{s}` | Overview figures + Brands view | Overview | B T |
| To-do / next actions | `/operations` | Overview "Next actions"; `/operations` → `/#next-actions` | Overview | B R |
| System Check (Issues & suggestions) | `/validation` | Overview status card and Settings → `/validation` | Overview → System check | B T |
| Source notes / allocation reviews | `/source-reviews…` | Inbox "Reviews" → same pages | Inbox | B T (allocation resolved, System Check errors 2 → 0) |
| Audit history | footer `/audit` | Settings → History; footer; product/customer pages show their own recent history (`/audit?entity=…&id=…`) | Settings, product/customer page | B T |
| Connection / backup status | `/connections`, `/recovery` | Overview status card + Settings cards | Overview | B |

### Orders and worksheet

| Capability | Old route / action | New location | How to reach | Check |
| --- | --- | --- | --- | --- |
| Colour-coded worksheet (3 states, legend) | `/`, `/orders`, `/worksheet` (3 copies) | Workspace › Products `/worksheet`; `/orders` → `/worksheet` (query kept) | Workspace | B T R |
| Search, season, customer, distributor, status, arrange-by, group filters | `GET /worksheet?…` | same params, plus new `brand` filter; filters fold behind one button on phones | Workspace filter bar | B T |
| Optional columns | (always shown) | "All columns" toggle (group cost, held, distributor, shipment/source), remembered per browser | Workspace › Products | B |
| Bulk group / status change | `POST /worksheet/bulk` | sticky selection bar; returns to the same filtered list with "Updated N" | select rows | B T |
| Group unassigned products | `POST /worksheet/group-unassigned` | Products view callout | Workspace | — |
| Product detail editing (status, customer, note, facts, prices, correction reason) | `/worksheet/items/{id}` | same page; returns to the filtered worksheet (`next`) | click a row | B T |
| Product image add/replace | `POST /items/{id}/image` (no UI in the old copy) | Product page → Photo (`return_to=item`) | product page | H T |
| Product history | `/audit` only | Product page → History | product page | B |
| Brand table editor (logistics status, Ready batch, shipment, pricing, add row) | `/orders/{id}`, `/brands/{id}` | Workspace › Brands › brand table | Workspace › Brands | B T |
| Factory readiness dates | `POST /orders/{id}/readiness` | brand table | Brands | H |
| Manual product entry | `/manual-entry`, `POST /orders/{id}/items` | Add → Product, typed in; brand table form | Add | B T |
| Quick new season | `POST /seasons/quick-create` | Add → New season (`/manual-entry#new-season`) | Add | H T |
| Bulk season setup wizard, pasted rows, image staging | `/seasons/new`, `/seasons/preview`, `POST /seasons` | Add → Season with many brands | Add | T |
| Season master by brand | `/seasons/{season}` | Workspace › Brands | Workspace | B T |
| Product image library | `/product-images` | Workspace › Products → Images | Workspace | B T |

### Inbox (intake, sources, drafts)

| Capability | Old route / action | New location | How to reach | Check |
| --- | --- | --- | --- | --- |
| New intake (message + uploads) | `/intake` + `POST /intake` | Inbox composer (same POST) and Add menu; `GET /intake` → `/inbox` | Inbox | B H T R |
| Intake conversation, questions, draft rows, evidence, save once, receipts | `/intake/{id}…` | unchanged routes, Inbox shell | Inbox list | B H T (answer by Enter, correction "Actually size 42", saved once, replay returned the same receipt) |
| Continue / reopen drafts | `/intake` list | Inbox "Intakes" list with state | Inbox | B |
| Captured sources (uploads, Drive, e-mail) with states, children, ignore, review in intake | `/inbox` | Inbox "Sources" section, state filter chips | Inbox | B T |
| Failed background jobs + retry, queue | `/inbox` | Inbox "Background work" (only when present) | Inbox | T |
| Legacy one-page AI intake | `/ai-intake`, `/ai-intake/analyze`, `/ai-intake/confirm` | Inbox "Other ways to add" → Quick AI draft table | Inbox | B T |
| Saved AI source archive | `/intake-sources/{token}` | product and draft links | product page | T |

### Customers

| Capability | Old route / action | New location | How to reach | Check |
| --- | --- | --- | --- | --- |
| Customer search / balances by season | `/customers?season=` | Customers | Customers | B T |
| Profile, contact details, aliases | `/customers/{id}`, `POST /customers/{id}/settings` | customer page → Profile drawer; returns with the season | Customers | B T |
| Goods, shipments, charges, payments | `/customers/{id}` | customer page sections | Customers | B T |
| Record payment/refund for this customer | `/payments` only | customer page → Record payment (same `POST /payments`), newest season only | Customers | T |
| Statement PDF, customer Excel (shareable), internal Excel (private) | customer page buttons | customer page header; private file marked "Do not share" | Customers | H T (customer workbook has no wholesale and no other customers; internal one has wholesale) |
| All / outstanding / Ready-batch statement ZIPs | `/reports` | Customers → Statements menu and `/reports`; Workspace Excel menu; Ready rows | Customers | H |

### Ready, shipments, groups, money

| Capability | Old route / action | New location | How to reach | Check |
| --- | --- | --- | --- | --- |
| Process supplier Ready list (+ affected-customer PDFs) | `/ready-batches`, `POST /ready-batches/process` | Ready & shipping; Add → Supplier Ready list; `/ready-batches` → `/logistics` (season/result/generated kept) | Workspace | H R (1 unit assigned, affected ZIP generated, result shown) |
| Create Ready batch | `POST /ready-batches` | Ready & shipping → New | Workspace | H |
| Ready batch ledger | `/ready-batches` | Ready & shipping | Workspace | B |
| Shipments (rule/explicit transport, CH exemption, fixed/suitcase/bag) | `/shipments`, `POST /shipments` | Ready & shipping | Workspace | B H R (own 7 % rate stored when "use the rule" is off) |
| Tables by description (season/brand tables, custom tables), grid editing, row/column add, Excel export | — | Workspace › Tables `/tables`, `/tables/{id}` | Workspace | B T |
| Edit product cells in place | — | Worksheet (colour/size/qty), Upload check (customer/colour/size/qty/wholesale/markup); `POST /worksheet/items/{id}/cell` | Workspace | B T |
| Claude API key and model (Sonnet 5.5 / Haiku 5.5) | `.env` only | Settings › AI assistant `/settings/ai` | Settings | B T |
| Correct shipment costs / exceptional expenses | `POST /shipments/{id}/costs`, `POST /expenses/{id}` | Ready & shipping › Edit costs / Edit | Workspace | B T |
| Exceptional expenses | `POST /expenses` | Ready & shipping | Workspace | H |
| Distributors, shipment groups, group settings, dispatch | `/groups`, `POST /distributors`, `/groups…` | Ready & shipping › Groups | Workspace | H R (dispatch refusal for ineligible products shown on a readable page) |
| Customer payments / refunds ledger | `/payments`, `POST /payments` | Workspace › Money | Workspace | B H T R |
| Factory payments | `POST /factory-payments` (form on the Ready page) | Money and each Ready row (batch preset) | Workspace | B H |
| Seller prepayments, credit transfer, settle/refund | `/credits`, `POST /credits/*` | Money › Seller credit | Workspace | H R (a repeated submission with the same key wrote one entry) |

### Excel, reports, documents

| Capability | Old route / action | New location | How to reach | Check |
| --- | --- | --- | --- | --- |
| Editable workbook download / blank template | `/excel/download` | Workspace Excel menu, Excel page | Workspace | H |
| Upload edits → preview → apply | `/excel`, `POST /excel/preview`, `POST /excel/apply` | Excel page (Workspace) | Workspace Excel menu | H T (edited colour visible in the worksheet) |
| Season / business / collection workbooks | `/exports/…` | Excel menu, library | Workspace | H T |
| Master workbook import, history, Import Review, protected changes, source delete | `/import`, `POST /import`, `/imports/…` | Workspace Excel menu → Import original master workbook; Settings → Imports and protected changes | Workspace | B T |
| Upload check: problems of one upload, colour filters, bulk mark, coloured Excel export, save to Drive | `/imports/{id}/check`, `/imports/{id}/check.xlsx`, `POST /imports/{id}/check/drive`, `/imports/latest/check` | Opens after an upload; Excel menu → Check the last upload; import history | Workspace | B T |
| Library: collections, files, brand/catalog/product links | `/library…` | Settings › Business | Settings | B T |

### Business helper and administration

| Capability | Old route / action | New location | How to reach | Check |
| --- | --- | --- | --- | --- |
| Business helper, projected profit, reviewed rule proposals | AI Help drawer, `/api/assistant/help`, `POST /assistant/apply-rule` | AI Help in the top bar (every page) | top bar | B T |
| Global search | Ctrl K panel | top bar button and Ctrl/Cmd K; arrow keys and Enter | top bar | B T |
| Business rules | `/rules`, `POST /rules/{id}` | Settings › Business (save asks for confirmation) | Settings | B H |
| Drive / Gmail connections, sources, sync, backfill, picker, exports publish | `/connections…` | Settings › Connections | Settings | B T (no live Google calls; Continue stays disabled without a client) |
| Backups, recovery code, restore exercise, downloads | `/recovery…`, `/storage…` (redirects) | Settings › Recovery | Settings | B T (backup and restore exercise inside the preview folder) |
| Clean-up & reset, Ready-import reversal | `/workspace/reset`, `/imports/{id}/reverse-ready` | Settings › Recovery | Settings | T |

## Checks performed

* **Automated tests**: `..\alina-fashion\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` — 257 passed
  (190 before this work plus 67 in `tests/test_ui_navigation.py`), including the extended photo-upload test.
* **Isolated preview**: `scripts/ui_preview.py` copies `app/` into `tmp/ui-preview`, seeds synthetic seasons,
  brands, customers, products and payments there and serves 127.0.0.1:8001 with AI, search, Google and the
  background worker switched off. Nothing reads or writes `data/` of this copy or the operational app.
* **Browser** (desktop and 375 px phone width): Overview, all four Workspace views, Inbox, an intake
  conversation, Customers and a customer page, Statements, Excel, Import, Product images, Add a product, the
  season table, Settings, Rules, Library, Connections, Recovery, Audit; drawers (payment, factory payment from a
  Ready row, profile, shipment); Add menu; search with Ctrl K, arrows and Enter; AI Help without AI; Escape
  returning focus to the opener; the leave-page prompt after an unsaved edit.
* **HTTP**: every Ready & shipping, Money, rules, readiness, photo and quick-season form submitted as rendered;
  Excel round trip; nine statement/export downloads with content checks; old URLs and their redirects.

## Problems found and fixed during the work

* Intake receipts showed stale "after" balances (the order's rows were cached before the save):
  `drafts.commit_session` now re-reads them; covered in `tests/test_intake_workflow.py`.
* Pressing Enter in an intake answer that also had choice buttons submitted the first choice; a hidden default
  submit button now sends the typed answer.
* The factory-payment order list could be empty; it now lists the season's brand orders.
* The shipment "use the Brand + Season rule" checkbox could not be switched off (an unchecked box fell back to
  the default "on"); it is now a pair of radio buttons with the same field name.
* A damaged image upload produced a server error (Pillow raises `SyntaxError` for a bad PNG checksum); it is now
  refused with "unsupported or damaged image" (`tests/test_season_builder.py`).
* AI Help named the old tabs ("Home", "To-do", "More tools") in its fallback text and page map; both use the new
  names now.

## Deliberate limitations and gaps found

* **Payments, shipments and expenses are recorded in the newest season.** `POST /payments`, `/shipments` and
  `/expenses` use the most recent season, as before. The new pages say so next to each form; the customer page
  and Money view hide the payment form for older seasons instead of silently posting to the newest one.
  Changing this is a backend decision and is not part of this task.
* **System Check can fail for an order without a Brand + Season rule** (`validate_system` reads a markup the
  default rule does not have). The Overview catches this and shows the error instead of failing; the backend
  is unchanged and the cause should be fixed separately.
* The old **manual image search** API (`/api/orders/{id}/image-suggestions`) had no screen in this copy and
  still has none; the product page photo upload covers manual add/replace.
* There is **no standalone "new customer" form** in this copy. Customers are created by seasons, intake, AI
  intake and Excel uploads. The Add menu does not invent one.
* A repeated seller-credit submission (same key) returns to `/credits` (→ Money › Seller credit) rather than to
  the drawer's return target; nothing is written twice.
* Product search results open the product's row on its brand table (`/orders/{id}#product-N`), as before; the
  row is now highlighted.
* The library lists an imported spreadsheet under its stored (hashed) file name, as before.
* `dashboard.html` and `orders.html` were not rendered by any route; they were removed with the other replaced
  templates (`operations`, `intake_list`, `ready_batches`, `shipments`, `payments`, `purchase_groups`,
  `seller_credits`, `storage`) and the old style sheets.
