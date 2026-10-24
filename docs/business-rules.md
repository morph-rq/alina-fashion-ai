# Business rules

## Single sources of truth

- Quantity, wholesale unit price, historical applied markup, status, customer allocation, Ready-batch assignment, shipment assignment, payment allocation, and factory payment are source facts.
- Item base, markup, final unit price, final total, order totals, Ready value, shipment totals, saldo, current charges, outstanding balance, and overpayment are calculated.
- `ShipmentItem` is the authoritative shipment assignment. `OrderItem.shipment_id` is an exposed property, not a second stored value.
- Workbook summary totals are retained only as source reconciliation values.

## Money

All arithmetic uses `Decimal`. Accounting values are quantized to two decimals with `ROUND_HALF_UP`. Whole-euro dashboard/PDF display is a separate operation.

```text
base_total = wholesale_unit_price × quantity
markup_amount = base_total × markup_percentage
final_total = base_total + markup_amount
```

Rules are keyed by Brand + Season. A rule stores markup, initial factory prepayment percentage, Ready balance percentage, transport percentage, exempt countries, expected-receipt days, and rounding policy. Historical item markup is retained as the applied value; a future-season rule never rewrites it.

## Order fulfillment

```text
ready_value = final value of active Ready items without a shipment
shipment_value = final value of active items assigned to that shipment
saldo = final_order_total - ready_value - sum(shipment_values)
required_prepayment = final_order_total × 50%
```

Cancelled items do not contribute to financial totals.

## Staged customer and factory liability

```text
order_initial_due   = round(active_order_value × initial rate)            (once per rate bucket)
row_initial_share   = largest-remainder share of order_initial_due by row value (cents add up exactly)
row_ready_share     = row_value − row_initial_share                        (when initial + Ready rates = 100%)
customer initial    = Σ row_initial_share of the customer's rows
customer Ready due  = Σ row_ready_share of the customer's rows in a Ready batch
current_merchandise_charge = initial + Ready due
total_charged = current_merchandise_charge + allocated shipment fees + explicit expenses
net payments  = Σ payments − Σ refunds
outstanding = max(total_charged − net payments, 0)
overpayment = max(net payments − total_charged, 0)

factory initial due = same allocation on the wholesale base (no markup)
factory Ready due   = Σ (row wholesale base − row factory initial share) for Ready rows
```

Shares are computed in `app/services.py` (`item_initial_shares`, `client_initial_charges`, `customer_ready_due`,
`factory_initial_due`, `factory_ready_due`). Statements, the Overview, System Check, its next actions, Ready & shipping and
Excel use these functions, so customer shares always reconcile to the order total and a fully Ready row is
charged exactly its value (previously the two halves were rounded separately and could exceed the value by a
cent). A purchase group's initial/shipment percentages override the brand rule for its rows. Factory
liability is always on the wholesale base: a selling-price override changes the customer charge, not the
factory debt.

Refunds are stored as positive amounts with type `refund`; every output shows them labelled "Refund" with a
negative sign (`calculations.signed_payment_amount`), and listed payment rows add up to the net figure.

The remaining percentage is never charged merely because goods exist in the order. It becomes due only when those goods enter a Ready batch. Customer payments may be allocated to `initial` or `ready`; a Ready allocation names its batch. Factory payments independently record the initial combined-order transfer or a Ready-batch transfer.

Expected receipt is available only after every affected customer has satisfied their own Ready liability for the batch **and** has no outstanding balance for the season (including fees). One customer's overpayment cannot mask another customer's missing payment. The estimate is the later of the Ready completion date and the last full-payment date, plus the configured number of days (14 by default). A later refund or new charge reopens the balance and withdraws the estimate. Estimates are rule-based, not carrier tracking.

Negative saldo is never silently netted; it is a blocking validation error.

## Shipment costs

For new shipments:

```text
transport_fee = eligible_customer_shipment_value × effective Brand+Season transport percentage
shipment_fees = transport_fee + fixed_cost + suitcase_cost
```

The SS26 default is 5%. A destination in the rule's exemption list (`CH` by default) receives 0% percentage transport. Fixed and suitcase costs remain separate and are not erased by that percentage exemption. Imported historical grouped transport/bag rows remain explicit expenses to preserve the workbook without inventing an allocation.

## Supplier allocation parsing

Supplier/detail tables are primary. Descriptions such as `AA 40`, `AA S`, and `AA 38, SV 40` are parsed before customer accounting. The importer preserves `source_file`, `source_sheet`, `source_row`, raw allocation description, and an allocation-group identifier. Aliases resolve to canonical customer codes before splitting. A split is accepted only when the inferred unit count reconciles to the source quantity; otherwise the product remains unallocated and an Allocation Review record is created.

## Incremental master imports

The same workbook hash is a no-op. A different file is compared using the stable source sheet and row retained on imported items. Unchanged rows are skipped. New rows and safe changes to items that have not entered an operational flow are applied. A source row that disappeared is cancelled rather than erased.

Ready, shipped, in-transit, arrived, delivered, or otherwise operationally advanced items are never silently rewritten by a later spreadsheet. Their before/after source values are stored in Import Change and routed to review. This makes repeated supplier updates safe without making Excel an unchecked overwrite channel.

## Ready-list automation

A supplier Ready list must identify products by article/SKU. Brand, customer/allocation, size, quantity, and a Ready flag are optional match constraints. A row is applied only when it resolves unambiguously to active master-order units. If a partial quantity becomes Ready, the remaining quantity is split into a separate row (`ready_importer.split_item`) that copies every descriptive, price, allocation and provenance field and collection membership, and records `split_from_item_id`. The remainder stays available to later rows of the same upload, so repeated partial rows for one SKU are neither lost nor sent to review. A Ready-list import can be reversed while no payment, factory payment or shipment depends on its batch.

A Ready import creates one batch, changes matched units to Ready, recalculates customer and factory liabilities from those same records, and generates new statements only for affected customers. Missing, over-quantity, cross-customer, and ambiguous matches are not guessed; they enter Import Review.

## Customer isolation

Customer statements start by filtering `OrderItem.customer_id`. Product totals, Ready, shipped, and merchandise charges are calculated only from that filtered set. Direct customer expenses are included. Shared shipment/order costs are allocated by the customer's value share. Payments are loaded only for the selected customer.

No customer PDF is created from a prebuilt season-wide dataset.

Customer PDFs are a customer-facing view, not an internal ledger export. They include the customer's name/reference, products, final customer prices, customer payments, delivery charges/status, expected delivery, current charges, and balance. They exclude phone/email/address, body or sizing notes, AI/source messages, wholesale prices, markup, profit, factory payments, internal payment notes/types, Ready-batch identifiers, and internal accounting explanations.

## Status and assignment

Allowed statuses: Ordered, Confirmed, Production, Ready, Assigned to shipment, In transit, Arrived, Delivered, Cancelled.

Selecting a shipment while an item is Ordered/Confirmed/Production/Ready automatically changes it to `Assigned to shipment`. Clearing a shipment does not silently select a replacement status; validation reports mismatches.

## Validation

Errors block a clean SYSTEM CHECK: ambiguous/unallocated product, customer/factory prepayment allocation mismatch, negative saldo, Ready/shipped over-allocation, duplicate customer/shipment assignment, shipment without Ready batch, payment over-allocation, or shipping status without a shipment.

Warnings include missing price/size, intentional markup exception, source-total mismatch, unallocated historical payment stage, factory-payment mismatch, payment mismatch, broken formulas, and open review records.

Financial discrepancies are reported, never automatically corrected.


## Price meaning and reverse rounding

A stated price must be identified as wholesale (factory) or final (customer), and per piece or for the line.
For a final price the app searches the cent values around `final / (1 + markup)` for a wholesale unit price
whose forward calculation gives exactly the stated final. If one exists it is used. If none exists (for
example EUR 100.00 at 35%), the stated final is kept exactly as the row's selling price and the nearest
wholesale is recorded as derived (`app/pricing.wholesale_from_final`). Line prices must divide into whole-cent
unit prices. Amounts are read without guessing the decimal locale: `1.200,50` and `1,200.50` are clear;
`1.200` or `1,200` is asked. Currencies other than EUR are not converted automatically.

## Corrections of operational rows

Rows that are Ready, in a shipment or past Ready status keep their quantity, price, markup and customer
unless an explicit correction reason is entered in the product editor (the reason is stored in the audit
log). Excel uploads and AI suggestions cannot change those fields on such rows. Every changed field is
audited with its channel; product rows carry a revision number and stale edits are refused.

## Startup data migrations

Data migrations are versioned (`schema_migrations`) and run once. The historical allocation re-check never
removes an existing customer assignment and skips rows with a resolved allocation note; it only allocates
rows that are still unallocated with one unambiguous parsed customer, and opens notes for ambiguous ones.
Run `scripts/migration_check.py <database>` against a copy before upgrading a workspace.

## Factory agreements, customer pricing and the assistant

Factory and customer percentages are independent; settlement, deposits, the preview → confirm rule, exact
selection, customer-isolated exports and voice confirmation rules are described in
[factory-and-voice-assistant.md](factory-and-voice-assistant.md).
