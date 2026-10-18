# Alina Fashion: project description and improvement plan

Updated: 18 October 2026  
Status: editable project brief, current-state description, and proposed development backlog.

## 1. Product direction

Alina Fashion should let an operator manage the business through a simple table and a helpful conversation. The operator can upload photographs, screenshots, spreadsheets, PDFs, text, or supported voice recordings; explain what they want; answer only the questions needed to resolve missing or conflicting facts; and receive correctly recorded orders and a reliable Excel workbook.

The same workflow should support correcting existing information, allocating goods to customers, processing supplier Ready lists, recording payments, preparing shipments, and producing private customer statements.

**The central requirement:** make information easy to add and change while preserving accurate money, customer separation, source evidence, and a clear history of corrections.

This document distinguishes current functionality from proposed functionality. Planned items are requirements, not claims that the app already implements them. The current-state analysis focuses on the main `alina-fashion` working copy, including its uncommitted files. The testing copy was checked for relevant differences, but was not fully audited.

## 2. The three folders and where development belongs

| Folder | Role | Working agreement |
| --- | --- | --- |
| `alina-fashion` | Main operational application and its business data | Preserve existing records; use this document as a description of the inspected main copy. |
| `alina-fashion-local` | Separate development and testing application | Implement and test application improvements here first. It has separate source files, uploads, reports, and a database. |
| `alina-fashion-recovery` | Earlier snapshots containing data and outputs | Treat as recovery material, not a running application or an automatically synchronized backup. |

The main and testing copies have diverged. Their databases and source files do not synchronize automatically. They share the main copy's Python installation and, according to the local development notes, AI credentials and the SearXNG service. Package changes and shared-service changes can therefore affect both.

The testing copy contains additional structures for intake confirmation tokens, distributors, purchase groups, and seller credit accounts. These are useful candidates for reuse, but their behavior, migration needs, and relationship to main-copy features must be checked before integration. An intake confirmation token alone is not a persistent conversational drafting system.

The main Windows launcher binds to loopback port 8000; the testing launcher uses loopback port 8001. Local notes also describe a tunnel-based operational setup, while a Render deployment configuration exists separately. The actual external deployment was not verified during this analysis. Preserve the existing access arrangement during development.

Relevant files: [local development notes](C:/Users/user/Documents/ChatGPT/alina-fashion/LOCAL_DEVELOPMENT.md), [testing-copy instructions](C:/Users/user/Documents/ChatGPT/alina-fashion-local/AGENTS.md).

## 3. How the current application works

### 3.1 Technology and request flow

The backend is Python with FastAPI. SQLAlchemy maps business records into SQLite. Jinja2 renders browser pages, and JavaScript adds search, browser dictation, image selection, and draft-saving interactions. openpyxl reads and writes Excel files; ReportLab generates PDF statements. Optional integrations use Gemini for extraction and assistance, SearXNG/Google Custom Search for images, and Threema Gateway for outbound messages.

```text
Uploaded evidence / typed information / Excel changes
                          |
                          v
              Extraction, matching, and review
                          |
                          v
                Validated business records
                          |
                          v
                 SQLAlchemy + SQLite
                          |
                          v
          Decimal calculations and aggregation
                          |
              +-----------+-----------+
              v           v           v
         Dashboard    System Check   Customer accounts
                                      |
                                      v
                              Excel / PDF / ZIP
```

On startup, the application adds missing database structures and runs legacy-data backfills. Most totals are calculated when requested rather than stored as independent editable values.

### 3.2 The business records

| Concept | Meaning |
| --- | --- |
| Customer and aliases | A person/account and alternate references used in supplier documents. |
| Brand + Season rule | Commission, initial/Ready percentages, transport terms, exemptions, special costs, and receipt days for a particular brand and season. |
| Order | One combined brand order in a season, containing goods allocated to several customers. |
| Order item | A product allocation with customer, SKU, size, color, quantity, wholesale unit price, historical applied markup, status, and evidence fields. |
| Ready batch | Goods that have become available; assignment triggers the configured remaining liability. |
| Customer payment and allocation | Money received or refunded, plus its initial or Ready-stage allocation. |
| Factory payment | A separately recorded supplier payment. Customer receipts do not automatically pay a factory. |
| Shipment and shipment item | Shipping details and the authoritative assignment of product rows to a shipment. |
| Expense and goods group | Direct/shared charges, responsible buyers/shippers, and reviewed fee allocations. |
| Import review, allocation review, and audit log | Source ambiguities, protected changes, and a history of recorded actions. |
| Collection and library file | Saved product groupings and supporting documents. |

A row can represent multiple identical units. Partial readiness may split it into separate rows. Each resulting row has one customer allocation and at most one shipment assignment. Customer and brand definitions are shared, while orders and financial records are scoped to seasons.

### 3.3 Financial rules

For active goods:

```text
wholesale total = wholesale unit price × quantity
commission      = wholesale total × applied markup
customer value  = wholesale total + commission

customer initial charge = full active customer value × initial percentage
customer Ready charge   = value assigned to Ready batches × Ready percentage
customer current charge = initial charge + Ready charge + applicable fees

factory initial due = active wholesale value × initial percentage
factory Ready due   = Ready wholesale value × Ready percentage

balance due = max(current charges − net customer payments, 0)
credit      = max(net customer payments − current charges, 0)
```

Decimal arithmetic and cent rounding with `ROUND_HALF_UP` determine accounting values. Whole-euro display is separate. Initial customer shares are allocated from the rounded brand-order prepayment using proportional weights and fractional remainders, so the shares reconcile exactly.

Example: one item costs EUR 1,000 wholesale, has a 30% commission, and uses 50/50 payment terms. The customer initially owes EUR 650 and owes another EUR 650 when it becomes Ready. Factory obligations are EUR 500 initially and another EUR 500 when Ready. Applicable 5% transport adds EUR 65 to the customer charge.

`Saldo` measures merchandise still awaiting fulfillment:

```text
saldo = full order value − unshipped Ready value − shipped value
```

It is separate from customer debt. Moving Ready goods into a shipment changes the fulfillment category without charging for the merchandise again. Cancelled goods are excluded from financial totals.

Historical item markup is preserved. Editing payment percentages or fees can still change the calculated liabilities of an existing season, so the improved editor must show those consequences.

### 3.4 Current operator workflow

1. Create a season with customers, brands, terms, and optional pasted products, or import the reference master workbook.
2. Add or correct product rows and customer assignments.
3. Import a supplier Ready list or assign goods to a Ready batch.
4. Record customer payments and allocate them to initial/Ready obligations; record factory transfers separately.
5. Create shipments and assign Ready goods. Add applicable transport and explicit costs.
6. Review To-do and System Check, then download Excel/PDF statements or prepare a Threema draft.

Delivery estimates require each affected customer to satisfy their own Ready obligation and full current season charges, including fees. The later payment-completion date plus the longest applicable receipt interval determines the estimate. Refunds or new charges can reopen balances and remove estimates. These are rule-based estimates, not carrier tracking results.

### 3.5 Current input and Excel capabilities

| Capability | Current behavior |
| --- | --- |
| Master workbook import | Tailored mappings for the reference workbook's 17 brand tables, fixed row ranges, and summary sheet. It is not a general parser for every supplier spreadsheet. |
| Incremental master import | Identical file hashes are skipped. Safe changes are applied; changed operational goods are preserved for review. Source positions and raw values remain traceable. |
| Supplier Ready import | SKU-based matching with optional brand, customer, size, quantity, and Ready constraints. Partial quantities can be split. Unresolved rows enter review. |
| Manual entry | Creates and edits source facts; derived amounts recalculate. |
| AI intake | Extracts editable drafts from text and supported attachments, suggests catalog/brand matches, and surfaces missing information. Saving requires validated business fields. |
| Images | Exact-SKU matching, manual assignment, temporary previews, managed storage, and optional web suggestions. |
| Internal Excel export | Product rows, linked brand views, master summaries, operational ledgers, formulas, and thumbnails. |
| Excel edit upload | Reviews approved columns on the exported Products sheet, checks row revisions again on apply, and saves atomically. Operational/grouped goods are protected from bulk editing. |
| Private customer exports | Filtered customer workbooks and PDFs; customer workbook edits do not synchronize back into the app. |

AI intake currently creates forms and follow-up questions, but the main copy does not provide a durable, resumable conversation that repeatedly merges answers into a saved draft. Browser-level saved-row tracking is also weaker than server-side idempotency across reloads and retries.

The current API calls Gemini directly. Generic AI configuration names do not yet provide a provider-independent implementation. Product sizes must come from explicit evidence or the operator; body measurements or photographs cannot establish a size reliably. Uploaded documents are evidence, and instructions embedded inside them must not become application commands.

### 3.6 Reports, messaging, and recovery

Customer statements filter by customer and season before building totals. PDFs expose customer-facing prices, products, delivery charges, payments, and balances. Internal wholesale costs, commissions, source messages, and factory payments are excluded. Private Excel uses its own allowlisted customer view; its contents are not identical to the PDF's.

The current Threema implementation supports Basic text messages and end-to-end Gateway sending with attachments. Sending is explicit. Attachment snapshots are hash-checked, and a committed send claim protects against repeated clicks. An uncertain result is not automatically retried. Gateway acceptance does not confirm delivery or reading. Receiving messages and accessing personal chat histories are not implemented.

The main Windows launcher requires login credentials, restricts local browser access, and makes verified Windows-account-encrypted backups at startup and every six hours while running. These are local recovery copies. The live SQLite file is an ordinary file; the separate Render launch does not run this Windows backup loop.

Deleting a stored source workbook preserves normalized financial records. The current global reset deletes every mapped database table and several managed directories, including manually entered information and audit records; its label and scope need correction before reset becomes part of a friendlier workflow.

## 4. Verified baseline and known gaps

The preceding analysis in this chat ran the main copy's tests with isolated databases and live credentials disabled: **82 tests passed**. Against an in-memory copy of the existing main database, **23 checked pages/API endpoints returned HTTP 200**. Live AI, image search, external hosting, and Threema delivery were not integration-tested.

The main data snapshot contained 1 season, 17 brand orders, 139 product rows, 3 customers, 11 customer payments, 9 shipments, one historical Ready batch, and no recorded factory payments. Product rows are not the same as physical unit counts. Season order value was EUR 114,599.25.

System Check reported **6 errors and 39 warnings** on that copied data. Four products were unallocated; their exclusion from customer accounts caused two allocation-total differences. Other warnings included historical payments without stage allocations, missing sizes/dates, and payment differences. Passing software tests does not resolve these business-data issues.

| ID | Finding | Required improvement |
| --- | --- | --- |
| FIX-01 | Four unresolved allocations and their reconciliation differences | Resolve from evidence through the UI, retaining the source and reason. Never assign a customer merely to make totals pass. |
| FIX-02 | PDF payment rows label refunds as positive “Payment received” entries, although balances subtract refunds | Print a clear refund label and consistent signed amounts; verify payment rows reconcile to the summary. |
| FIX-03 | Main AI intake lacks durable conversation/draft state and durable save idempotency | Add saved sessions, field-level state, draft revisions, and unique confirmation keys. Review the testing copy's existing confirmation mechanism first. |
| FIX-04 | Master import relies on fixed reference-workbook mappings | Add versioned layout adapters and a reusable column-mapping preview. Preserve existing reference import behavior. |
| FIX-05 | Review lists often explain a problem without offering one complete resolution flow | Add actionable resolution screens and link each resolution to the affected rows and recalculated result. |
| FIX-06 | Global reset clears all business/audit tables, despite an import-oriented label | Separate source cleanup, import reversal, archive, and whole-workspace reset; show scope and preserve a recovery snapshot. |
| FIX-07 | Documentation and feature sets differ between copies | Compare capabilities, choose the tested implementation for each feature, and maintain one explicit release baseline. |
| FIX-08 | Main route module is roughly 2,300 lines and contains several kinds of business mutation | Move shared rules into services used by manual forms, AI intake, Excel edits, and imports. |

Additional regression targets need reproduction before being called confirmed defects: repeated partial Ready rows for the same SKU in one upload; incomplete provenance when splitting a row; final-price-to-wholesale reverse rounding; reconciliation of shared shipping costs and manual transport overrides; and legacy backfills preserving reviewed corrections across restart.

## 5. The improved experience

### 5.1 One workspace for conversation, records, and evidence

Use three connected views on desktop, with mobile-friendly switching:

- **Conversation:** upload information, describe an action, and answer focused questions.
- **Draft/table:** inspect and directly edit the structured rows; clearly distinguish extracted facts, defaults, confirmed values, missing fields, and calculated amounts.
- **Evidence:** open the photograph, source excerpt, spreadsheet cell, or document page supporting a field.

Offer clear actions: Save draft, Review changes, Save records, Download Excel, and Resolve issue. Autosave draft progress. Reopening the app should restore incomplete work and the next unanswered question.

Questions should resolve uncertainty. Facts already supplied or confirmed should not be requested again. Optional information should not block unrelated work, and an unavailable AI service should leave manual editing usable.

### 5.2 Supported operator requests

The assistant should recognize these actions and prepare structured changes:

| Request | Expected result |
| --- | --- |
| “Add these products from the photos and this message.” | Separate editable draft rows, matched customers/brands, and questions for missing facts. |
| “Change Maria's coat from size 38 to 40.” | Identify the exact existing row; clarify if several match; show the correction and record its history. |
| “These three items are Ready.” | Match quantities to available units and prepare Ready assignments without duplicating goods. |
| “Record this customer's payment.” | Confirm customer, amount, currency, date, payment/refund meaning, and stage where required; check for duplicates. |
| “Move these goods into shipment 4.” | Validate Ready assignment, season, quantity, and existing shipment links before applying the move. |
| “Give me the updated Excel and Maria's statement.” | Generate current internal Excel plus a separately filtered customer export. |
| “What is still missing?” | Return the next actionable questions/issues with links to affected records. |

### 5.3 Example conversation

**Operator uploads:** a coat photograph and a screenshot containing “Maria Ivanova, The Row, black coat, 38, 1200.”

**Assistant:** “I found one coat for Maria Ivanova: black, size 38, amount 1,200. Is that the wholesale unit price or the final customer price?”

**Operator:** “Wholesale, in euros. One piece, FW26.”

The assistant fills those facts. If more than one The Row table exists for FW26, it asks which table applies. If no table exists, it proposes creating one and asks for commission/payment terms only when there is no approved applicable preset. It does not silently reuse an unrelated table.

The draft then shows the customer, season, brand table, product, size, quantity, EUR wholesale unit price, applicable commission, calculated customer total, and linked image. An unreadable article stays unresolved; the operator can provide it or explicitly use an allowed internal reference. A product photo is not treated as proof of an article number.

**Operator:** “Actually size 40.”

The same draft updates to size 40 and preserves the correction history. It does not create a second coat.

**Operator chooses Save and Download Excel.** The application revalidates the draft, checks revisions/duplicates, commits once, and produces the current workbook with the row in Products and its brand view. If the download fails after saving, retrying the download does not create another order.

This conversation is proposed behavior, not a description of the current main intake page.

## 6. Requirements for reliable intake and correction

### 6.1 Extraction and evidence

Store source facts with a supporting attachment/message reference and, where available, a page, cell, or text location. Retain the original reading alongside a normalized value. A suggested normalized customer/brand must be distinguishable from a confirmed match.

Each field should carry its value, origin, review state, and revision. Useful states are explicit, defaulted, suggested, missing, conflicting, and operator-confirmed. Model confidence can inform presentation, but it is not proof. Evidence quality, exact catalog matches, and business validation determine whether clarification is required.

Mixed files must separate customers, products, sizes, and quantities correctly. Repeated evidence should reinforce an existing draft rather than create duplicates. An explicit later operator correction supersedes an earlier extraction, while the source remains available for audit.

### 6.2 Questions and required fields

Ask one focused question or a small coherent batch at a time. Prefer selectable matches with a text answer available. Allow “I don't know—save as incomplete.”

Before posting an order, establish customer, season, brand table/terms, product identity, quantity, a usable price and its meaning, and currency. Apply a size requirement appropriate to the product type: explicit size for sized clothing; an explicit one-size/not-applicable designation where valid. Missing optional color, image, or delivery preference should not prevent saving a complete permitted order.

Clarify whether a price is per unit or for the line, wholesale or customer-final, and whether any stated discount/tax is already included. The present accounting model is EUR-oriented; explicit currency mismatches must be resolved, not silently summed. Add tax, discount, or foreign-exchange treatment only through an agreed deterministic rule.

Customer initials and approximate names may be ambiguous. Show existing candidates before creating a new customer. Preserve article identifiers as text, including leading zeros. Apply date and decimal-locale normalization only when the meaning is clear.

### 6.3 Safe edits and one calculation path

Create a shared mutation service for product creation/correction, allocations, Ready assignment, shipment moves, payments, and rule changes. Manual entry, AI, and Excel should call the same business validations.

Show before/after values and material consequences: changed balances, Ready requirements, shipment charges, or delivery estimates. Protect operational records with explicit correction/splitting/reallocation actions instead of unrestricted bulk overwrites. Keep an audit record and provide compensating corrections where a financial reversal is needed.

Recheck row revisions when saving, not only when opening a draft. Reject stale changes with a useful conflict view. A server-side idempotency key must make retries return the previously saved result. Batch operations should report the outcome of each row without reapplying completed rows.

### 6.4 Persistence and proposed structures

These are conceptual additions, subject to comparison with the testing copy:

| Structure | Responsibility |
| --- | --- |
| Intake session | Selected season/action, current state, timestamps, and ownership/context. |
| Source attachment/message | Bounded stored evidence, hash, media type, provenance, retention policy, and extraction status. |
| Draft record | Proposed new or existing target row, extracted/confirmed facts, revision, and commit result. |
| Field evidence/conflict | Field origin, conflicting candidates, chosen value, and resolution reason. |
| Question and answer | Pending clarification and its structured answer, tied to the right draft/field. |
| Confirmation/operation receipt | Unique operation key, applied draft revision, result IDs, and retry outcome. |
| Export revision | Workbook type, scope, schema/template version, generation time, and source revision. |

A practical session flow is: collecting evidence → analysing → needs answers → ready for review → saved/exportable. Saving and exporting need separate outcomes because a successful database commit must remain successful if file generation or downloading fails.

## 7. What “fill Excel correctly” means

Use the application database as the authoritative business record, then generate the workbook from validated records and the existing Decimal calculation engine. Edits to approved Excel fields return through a reviewed change set. Generated customer exports remain a separate sharing format.

For the original supplier/master workbook, support a named, versioned template adapter if preserving that exact layout is required. The operator chooses the target workbook type. Unknown layouts require a mapping preview or clarification; they must not be silently interpreted through the reference workbook's fixed positions.

### Excel acceptance requirements

- Preserve IDs and revision metadata for app round trips; detect duplicate IDs and stale exports.
- Write prices and quantities as numeric cells, percentages with correct underlying values, dates consistently, and SKUs/customer references as text.
- Reconcile Products, brand views, master totals, customer initial shares, Ready quantities, shipments, fees, and payment/refund balances.
- Preserve Decimal accounting decisions at export boundaries. Excel formula results must be compared with authoritative amounts; document any unavoidable display differences.
- Treat source text beginning with formula-like characters as text. Imported editable fields must not execute supplied formulas or external links.
- Preserve the selected template's sheet names, required formulas, formats, and image placement. Mark unavailable images without blocking valid accounting rows.
- Distinguish editable source columns from generated totals and snapshots. Explain which changes will be imported before upload application.
- Handle additional product rows and supported layout versions without silently omitting them.
- Run structural and financial validation before download. Include export time, scope, and revision so an operator can identify a stale file.
- Keep internal business data and private customer exports separate, with allowlist-based tests for customer isolation.

Excel formula caches may be empty until a calculation-capable spreadsheet application opens the file. If reliable cached previews are required, choose and validate a controlled recalculation step; do not claim that openpyxl evaluates formulas.

## 8. Prioritized implementation backlog

Priority meanings: P0 = correctness or preservation prerequisite; P1 = the next useful end-to-end operator workflow; P2 = broader usability and operational maturity.

| ID | Priority | Deliverable | Acceptance check |
| --- | --- | --- | --- |
| IMP-01 | P0 | Compare main/testing features and establish a release baseline | A feature/migration map identifies what to reuse; testing cannot change main data or shared services. |
| IMP-02 | P0 | Correct refund presentation and reproduce financial edge cases | Refund rows reconcile to net payments; agreed rounding and partial-quantity examples pass across UI, Excel, and PDF. |
| IMP-03 | P0 | Improve allocation/import resolution and reset scope | Ambiguous allocations can be resolved with evidence; reset preview accurately identifies affected data and recovery options. |
| IMP-04 | P1 | Persistent multimodal drafts | Upload, refresh, close, and reopen without losing draft facts, sources, answers, or status. |
| IMP-05 | P1 | Iterative question-and-answer intake | Answers update the correct draft; existing confirmed facts remain; unresolved conflicts block only the affected action. |
| IMP-06 | P1 | Shared validation, revisions, and idempotent save | Manual, AI, and Excel paths enforce the same rules; retries and double clicks produce one result. |
| IMP-07 | P1 | Searchable, directly editable order workspace | Filter by customer, brand, season, status, and issue; edit permitted facts with useful feedback and history. |
| IMP-08 | P1 | Save-and-export workflow | Validated drafts become correct Products/brand/master rows; export retries do not duplicate records. |
| IMP-09 | P1 | Guided correction of existing goods | Natural-language corrections locate the target, clarify ambiguity, show consequences, and preserve audit history. |
| IMP-10 | P2 | Reusable Excel layout mappings | A second supported supplier layout imports through a previewed adapter; unknown layouts are clearly identified. |
| IMP-11 | P2 | Clearer payment and shipping workspaces | Allocate receipts across obligations where required; maintain refunds, partial shipments, fees, and receipt estimates consistently. |
| IMP-12 | P2 | Background extraction/export progress and recovery | Long tasks show progress, can resume/retry safely, and leave saved facts accessible on provider failure. |
| IMP-13 | P2 | Deliberate supplier/group/credit support | Reuse testing-copy concepts after agreeing the ledger model; avoid duplicate competing group/credit systems. |
| IMP-14 | P2 | Operational hardening and maintainable modules | Versioned migrations, dependency baseline, useful redacted diagnostics, tested restore, and reduced repeated aggregation. |

### Recommended delivery sequence

**Phase A — establish correctness:** IMP-01 through IMP-03, representative fixtures, documented money/rounding policy, and a tested copy-based migration path.

**Phase B — deliver the core requested experience:** persistent drafts, focused questions, direct field edits, shared save validation, and one reliable Excel export path. This is the first complete milestone: upload → answer → correct → save once → download correct Excel → reopen later.

**Phase C — expand everyday operations:** conversational corrections, reusable supplier mappings, richer payments/shipping, and targeted issue resolution.

**Phase D — improve scale and reliability:** background jobs where needed, query optimization, supplier credits, access controls if multiple operators are required, and a controlled release process.

Avoid rewriting the whole application before Phase B. Reuse its calculation engine, customer filtering, Excel revision checks, image storage, and tested workflow behavior. Add a thin conversational orchestration layer over shared domain services.

## 9. Completion scenarios for the first milestone

| Scenario | Expected outcome |
| --- | --- |
| Photo + customer text + price screenshot | One coherent draft with linked evidence and focused questions for ambiguous price/size/table facts. |
| Several customers and products in one upload | Separate allocations; no cross-customer merging or duplicated quantities. |
| Missing size | An incomplete saved draft and a size question; no invented size from a photo or body measurements. |
| Conflicting price sources | Both values remain inspectable; the chosen value and its reason are recorded. |
| “Change size to 40” in the same session | The intended draft changes; no second product is created. |
| Reload or return tomorrow | Draft facts, answered questions, and pending work survive. |
| Double click, retry, or lost response during Save | The operation is applied once and returns the saved result. |
| Price stated as final customer price | An agreed reverse-rounding policy preserves or explicitly resolves the stated final amount. |
| Excel edited after an app correction | A stale-row conflict is shown; the newer app value is preserved until resolved. |
| Partial Ready quantities, including repeated rows | Total quantity/value remain consistent; quantities are neither lost nor allocated twice. |
| Refund or newly added fee | Net payments/balance and delivery estimates update consistently in every output. |
| Private client export | Only allowed records/fields for the chosen customer and season appear. |
| AI unavailable | Existing drafts remain usable; the operator can finish permitted fields manually. |
| Export fails after Save | Saved records remain visible; retrying export makes no additional business mutation. |

These scenarios are proposed acceptance criteria. They are not all covered by the existing 82-test baseline.

## 10. Decisions to confirm during development

Use these questions when their answers become necessary; they should not prevent correcting established defects or building reusable draft foundations.

| Decision | Proposed starting point | Status |
| --- | --- | --- |
| Which Excel is the primary deliverable? | App-generated master + brand workbook first; add the original workbook as a separately versioned adapter if required. | Needs operator confirmation |
| Which interface languages? | Preserve source language; confirm preferred English/Russian interface and question language. | Open |
| Which evidence formats matter first? | Photos/screenshots, PDF, XLSX/CSV, typed text; retain supported audio where practical. DOCX extraction needs an explicit supported path. | Open |
| What can be saved incomplete? | Drafts freely; only validated, complete permitted facts become accounting rows. | Proposed |
| What may auto-fill? | Explicit evidence and visible approved defaults; ambiguous customer, size, price meaning, or season requires clarification. | Proposed |
| How should reviewed financial corrections work? | Show consequences; preserve history; use reversal/reallocation actions for operational records. | Proposed |
| Are multiple currencies, VAT, or discounts required? | Keep EUR baseline; agree explicit pricing/conversion rules before accepting unsupported amounts. | Open |
| Do multiple operators need separate accounts? | Keep current operator workflow initially; introduce identity/roles before shared concurrent operation requires them. | Open |
| How long should source uploads be retained? | Agree a bounded evidence-retention and deletion policy separate from accounting records. | Open |
| When should external messages be sent? | Explicitly prepared/reviewed drafts; keep record saving and message sending as separate actions. | Proposed |

## 11. Source map and maintenance

| Area | Main implementation |
| --- | --- |
| Web workflows and startup | [main.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/main.py), [workflow_routes.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/workflow_routes.py) |
| Data model and upgrades | [models.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/models.py), [migrations.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/migrations.py) |
| Accounting and aggregation | [calculations.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/calculations.py), [services.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/services.py) |
| Master and Ready imports | [importer.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/importer.py), [ready_importer.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/ready_importer.py) |
| Seasonal setup and AI | [season_builder.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/season_builder.py), [ai_intake.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/ai_intake.py) |
| Images | [image_manager.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/image_manager.py) |
| Excel and PDF | [excel_export.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/excel_export.py), [excel_views.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/excel_views.py), [excel_edit.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/excel_edit.py), [pdf_report.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/pdf_report.py) |
| Groups and exception handling | [goods_groups.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/goods_groups.py), [operations.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/operations.py), [validation.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/validation.py) |
| Messaging and recovery | [threema.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/threema.py), [threema_files.py](C:/Users/user/Documents/ChatGPT/alina-fashion/app/threema_files.py), [serve_local.py](C:/Users/user/Documents/ChatGPT/alina-fashion/scripts/serve_local.py), [local_backup.py](C:/Users/user/Documents/ChatGPT/alina-fashion/scripts/local_backup.py) |

Keep this document editable. Mark an IMP/FIX item complete only after its acceptance checks pass; record the implementation location, validation evidence, date, and any remaining limitation. Update the current-state sections after each accepted release. Store business rules in their authoritative rule documentation and code, avoiding contradictory copies.

### Progress log

| Date | Item | Result / evidence | Remaining work |
| --- | --- | --- | --- |
| 2026-10-18 | Project analysis and planning document | Main-copy code reviewed; 82 tests passed; 23 route checks passed against a copied database; data issues and improvement requirements documented. | Implement and validate the prioritized backlog in the isolated testing copy. |

## 12. Implementation status in the testing copy (2026-10-18)

This section was added in `alina-fashion-local`; the copy in the operational folder is unchanged. Details,
evidence and remaining work: `docs/IMPLEMENTATION_PROGRESS.md`.

| ID | Status | Evidence |
| --- | --- | --- |
| FIX-01 / FIX-05 | Implemented (resolution flow); the 4 real unallocated rows still need the operator's evidence | `allocation_resolution.py`, tests |
| FIX-02 | Implemented | `test_refund_is_labelled_and_signed_in_customer_pdf`, Excel/private workbook tests |
| FIX-03 | Implemented | `drafts.py`, `test_intake_workflow.py` |
| FIX-04 / IMP-10 | Not implemented (layout adapters); unknown layouts are refused | — |
| FIX-06 | Implemented | `workspace_reset.py`, reset test with restore |
| FIX-07 | Capability map recorded; one baseline in this copy | `IMPLEMENTATION_PROGRESS.md` |
| FIX-08 | Partly: shared item, liability and draft services; `main.py` routes still large | `item_service.py`, `services.py` |
| IMP-01–IMP-03 | Implemented | stage 1–2 tests, migration checks |
| IMP-04–IMP-09 | Implemented (Ready/shipment requests link to their pages) | intake tests |
| IMP-11 | Not started beyond refunds and payment drafts | — |
| IMP-12 | Implemented (durable jobs, progress states, retries) | `test_jobs.py` |
| IMP-13 | Decision: keep purchase groups; no second group system | — |
| IMP-14 | Partly: versioned migrations, tested restore, redacted errors | — |
| Drive / Gmail | Implemented, tested against simulated Google; real account setup pending | `docs/drive-gmail-setup.md` |
| Recovery | Implemented; local restore exercise passed on real testing data | `docs/backup-restore.md` |

Test suite: 186 passed (was 108 + 1 failing).
