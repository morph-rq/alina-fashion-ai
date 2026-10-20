# Finding your way (interface of 2026-10-20)

The app has four areas, always in the top bar (and the bottom bar on a phone):

| Area | What it is for |
| --- | --- |
| **Overview** `/` | The season at a glance: what is ordered, what customers still owe, the factory balance, goods Ready but not shipped, and a short **Next actions** list. Cards on the right show System Check, the Inbox and backups/connections. |
| **Workspace** `/worksheet` | All goods and money of a season. One switch with four views: **Products** (the colour-coded worksheet), **Brands** (season table and brand tables), **Ready & shipping** (Ready lists, shipments, expenses, groups, distributors) and **Money** (customer payments, factory payments, seller credit). **Excel & statements** sits in the same bar. |
| **Inbox** `/inbox` | Everything that arrives: new intake (message, photos, files), intakes in progress, captured uploads/Drive/e-mail, background jobs and source/allocation reviews. Nothing here changes goods or money until you save it. |
| **Customers** `/customers` | Each customer's profile, goods, money and statements, season by season. |

Three tools are on every page, to the right of the areas:

* **Search** (or **Ctrl K** / **Cmd K**): customers, phone numbers, SKUs, products, brands, seasons, files. Arrow keys move, Enter opens.
* **AI Help**: the business helper. It explains and can propose a rule change; a proposal is only applied after you review it.
* **Add**: start something new — an intake from photos/files/messages, a typed product, a customer payment, a supplier Ready list, an Excel upload, a new season or the bulk season setup.

Less frequent tools are behind the gear (**Settings** `/settings`), grouped as Business (rules, library),
Connections (Google Drive and Gmail, captured sources), Recovery (backups and restore, clean-up and reset) and
History and checks (audit history, System Check, imports and protected changes).

## Where the former tabs are now

| Former tab or menu item | Now | Old link |
| --- | --- | --- |
| Home / Dashboard | Overview | `/` |
| To-do | Overview › Next actions | `/operations` → `/#next-actions` |
| Orders | Workspace › Products | `/orders` → `/worksheet` (filters kept) |
| Worksheet | Workspace › Products | `/worksheet` |
| Season table, brand tables | Workspace › Brands | `/seasons/{season}`, `/orders/{id}` (unchanged) |
| Ready batches | Workspace › Ready & shipping › Ready | `/ready-batches` → `/logistics?focus=ready` |
| Shipments, exceptional expenses | Workspace › Ready & shipping › Shipments | `/shipments` → `/logistics?focus=shipments` |
| Shipment groups, distributors | Workspace › Ready & shipping › Groups | `/groups` → `/logistics?focus=groups` |
| Payments | Workspace › Money › Customer payments | `/payments` → `/money?focus=payments` |
| Factory payments | Workspace › Money › Factory, and on each Ready row | (form only) |
| Seller credit | Workspace › Money › Seller credit | `/credits` → `/money?focus=seller-credit` |
| Excel files | Workspace › Excel & statements › Edit in Excel | `/excel` |
| Import (original master workbook) | Workspace › Excel & statements › Import original master workbook; Settings › Imports and protected changes | `/import` |
| Product images | Workspace › Products › Images | `/product-images` |
| Add order / manual entry | Add › Product, typed in | `/manual-entry` |
| New table / season setup | Add › New season, Add › Season with many brands | `/manual-entry#new-season`, `/seasons/new` |
| Intake | Inbox (composer at the top) and Add › From photos, files or a message | `/intake` → `/inbox` |
| Source inbox | Inbox › Sources | `/inbox#sources` |
| AI intake (one page) | Inbox › Other ways to add › Quick AI draft table | `/ai-intake` |
| Source reviews | Inbox › Reviews | `/source-reviews` |
| Issues & suggestions (System Check) | Overview › System check card; Settings › History and checks | `/validation` |
| Customers, customer page | Customers | `/customers`, `/customers/{id}` |
| PDFs / Reports | Customers › Statements; Workspace › Excel & statements | `/reports` |
| Rules | Settings › Business | `/rules` |
| Library | Settings › Business | `/library` |
| Connections (Drive, Gmail) | Settings › Connections | `/connections` |
| Storage & backups | Settings › Recovery › Backups and restore | `/storage` → `/recovery` |
| Clean-up & reset | Settings › Recovery › Clean-up and reset | `/workspace/reset` |
| Audit log (footer) | Settings › History and checks; footer; each product and customer page shows its own recent history | `/audit` |
| More tools | Removed. Its items are in Add, Settings or the Workspace bar above. | — |

All old links keep working: they redirect to the new place and keep their `?season=` and other filters.

## Habits that still work

* Open a product from the worksheet, save, and you return to the same filtered list (season, brand, status,
  search) and a short "Saved …" note. The **Workspace** link brings back the last filtered list too.
* Payments, shipments and expenses are recorded in the **newest** season, as before. Pages showing an older
  season say so and link to the newest season instead of posting there silently.
* Forms in drawers keep what you typed when you close them; leaving the page with unsaved changes asks first.
* Statements stay customer-only. On a customer page the Excel menu keeps **Customer Excel** (safe to share)
  apart from **Internal Excel** (wholesale costs and markup, marked "Do not share").
