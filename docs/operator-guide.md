# Operator guide (testing copy)

Open the app at <http://127.0.0.1:8001>. The top bar has four areas — **Overview**, **Workspace**, **Inbox**,
**Customers** — and Search (Ctrl K), AI Help, **Add** and the Settings gear. On a phone the four areas and Add
are in the bottom bar. [navigation-guide.md](navigation-guide.md) lists where every former tab went.
The everyday path is the **Inbox**.

## 1. Record goods, corrections or payments in the Inbox

1. **Inbox → New intake** (or **Add → From photos, files or a message**). Choose the season, type what
   happened, attach photos, screenshots, PDFs, spreadsheets, text or voice notes, and press **Start intake**.
   Files are stored (and checksummed) before anything analyses them.
2. The page has three parts (tabs on a phone):
   * **Conversation** — your messages, the assistant's notes and up to three focused questions. Answer with a
     button or by typing. Answers update the existing draft row; they never create a second order.
   * **Draft** — one card per proposed row. Each field shows its state:
     `explicit` (read from evidence or an exact match), `defaulted` (a visible default such as EUR),
     `suggested` (choose a candidate), `missing`, `conflicting` (sources disagree; both values are kept) and
     `confirmed` (set by you; extraction never overrides it). Edit any field directly; changes save
     immediately and keep their history.
   * **Evidence** — every stored source with its state, size and checksum. Images preview inline; other files
     open as downloads. Text inside documents is treated as evidence, never as an instruction.
3. Follow-up messages are understood for common corrections: `Actually size 40`, `Wholesale, in euros. One
   piece`, `qty 2`, `row 2 size M`. If it is unclear which row is meant, you are asked.
4. **Corrections of saved goods**: `Change Maria's coat from size 38 to 40` finds the saved product. If several
   match you choose one. The correction is a draft row until you save it.
5. **Payments**: `Maria Ivanova paid 650 EUR on 2026-09-01` (or `refund …`) prepares a payment row. Possible
   duplicates (same customer and amount) are shown before saving. Dates like `3.9.2026` are asked, because
   day/month order is ambiguous.
6. **Ready lists, shipments, rule changes and exports** are recognised and linked to their dedicated pages,
   which check quantities, seasons and links before changing anything.
7. **Review and save.** When nothing is missing, **Save records** (or **Save and download Excel**) applies the
   draft once. Double clicks, retries and lost responses return the same receipt. The receipt shows each
   affected customer's balances before and after, plus downloads. Downloading never saves again, so a failed
   download can simply be retried.

What is required before saving a product: customer, brand table (existing, or a new one with terms), product
name or article, quantity, price with its meaning (wholesale/final) and basis (per piece/line), EUR, and a
size (or `One size`). Colour, images and notes are optional and never block saving. Sizes are never inferred
from photos or body measurements.

Without AI configured, everything still works by hand: **Add product row**, fill the fields from the evidence,
save.

## 2. Captured sources

**Inbox → Sources** lists every upload, Drive file and e-mail with its state: captured, queued,
analysing, needs answers, ready for review, applied, failed (with the reason and a retry) or ignored (with
your reason). **Review in intake** opens an intake for a synced item. Synced evidence never posts goods or
money by itself.

## 3. Unallocated products and import notes

**System Check** (the card on **Overview**, also under Settings → History and checks) lists the findings with
links; source and allocation notes are also listed under **Inbox → Reviews**. For a customer-allocation note, open it,
read the original description, then allocate the units to customers and record the evidence (for example
"supplier e-mail of 3 Oct lists AA two, KI one"). Rows are split where needed, keeping their provenance; the
page shows System Check errors before and after. Do not assign a customer just to make totals pass.

## 4. Correcting saved products

Product rows can be edited on the product page (**Workspace → Products**, click a row; saving returns to the
same filtered list) or in the brand table (**Workspace → Brands**). For Ready or shipped rows, changing quantity, price, markup or
customer requires a correction reason, which is stored in the audit history. If someone else changed the row
after you opened it, the save is refused; reload and apply your change again.

## 5. Excel

* **Season workbook / business workbook** (`/exports/...`): Products (formulas follow the app's cent
  rounding; *App final total* is the authoritative amount), a Master summary, one linked tab per brand, and a
  **Checks** sheet showing formula result vs. app amount ("OK" once Excel has calculated) plus the export time,
  scope and revision. Text such as SKUs is stored as text, so leading zeros survive.
* **Editable order workbook** (**Workspace → Excel & statements → Edit in Excel**): download, edit, upload,
  review the proposed changes, apply.
  Stale rows are refused; Ready/shipped financial fields cannot be changed through Excel.
* **Customer workbook** (customer page → **Excel → Customer Excel**) is safe to share with that customer: no
  wholesale prices, markup, notes, sources or other customers. **Internal Excel** contains costs; do not share it.

## 6. Clean-up and reset

**Settings → Recovery → Clean-up and reset** separates: removing a stored source file (records stay), reversing a Ready
import (only while nothing depends on it), restoring a snapshot, and resetting the whole workspace. A reset
deletes **every** record including manual entries and the audit history, after writing a verified recovery
snapshot first. Type `RESET WORKSPACE` to confirm.

## 7. Connections and recovery

**Settings → Connections** (Google Drive and Gmail) and **Settings → Recovery** (backups and restore). See
`drive-gmail-setup.md` and `backup-restore.md`.

## 8. Ready lists, shipments and money

**Workspace → Ready & shipping** holds Ready lists (process a supplier list or create a batch), factory payments
per Ready batch, shipments and exceptional expenses, shipment groups and distributors. **Workspace → Money**
holds customer payments and refunds, factory payments and seller credit. Payments, shipments and expenses are
recorded in the newest season; pages showing an older season say so.
