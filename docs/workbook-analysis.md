# SS26 workbook analysis

## Source inspected

Primary source: `Отчет_SS26 AA.xlsx` (18 sheets, approximately 26 MB). The supplied PDFs were used as visual and chronological cross-checks, not as arithmetic sources for the importer.

The workbook package contains a damaged embedded image (`xl/media/image63.png`, bad CRC-32). Excel-cached values and formulas are still readable after replacing only the damaged media member in a temporary copy. The original file is never modified.

## Workbook structure

The master sheet is named `SS25`, but its visible title is `ЗАКАЗЫ SS26`. It contains:

- rows 4-20: brand summary;
- row 22: season totals;
- rows 26-36: payment entries;
- row 39: total paid;
- rows 41-58: current charges, shipments, transport, and bag costs;
- row 60: total charged;
- row 61: amount payable.

Brand/detail sheets:

1. `BV `
2. `Dior main `
3. `Balenciaga main`
4. `The ROW main (PRE)`
5. `Zimmerman`
6. `Kiton Women `
7. `KITON MEN `
8. `Valentino`
9. `brunello cucinelli women`
10. `Brunello Cucinelli MEN `
11. `Celine pre`
12. `THE ROW pre`
13. `Loro Piana women `
14. `loro piana men `
15. `DIOR CRUIZ SSv 26`
16. `Alaia pre`
17. `YSL pre `

Trailing spaces in several sheet names are significant in Excel formulas.

## Inputs versus calculated cells

Typical manually entered facts are product description, article, color, size-grid quantities, wholesale price, customer note/code, Ready allocation, shipment allocation, payment date/amount, readiness note, and explicit transport/bag charge.

Typical calculated cells are row quantity totals, wholesale row totals, markup amounts, final brand totals, Ready/shipment totals including markup, 50% prepayment, season totals, saldo, and current payment position.

The workbook has no single standardized detail-table layout. Each brand sheet uses a different set of columns, so the prototype importer uses an explicit per-sheet mapping rather than heuristic column guessing.

## Markup and rounding

Observed brand/order markups:

- 25%: Zimmerman;
- 30%: Alaia, Balenciaga, Celine, Kiton, YSL;
- 35%: BV, Dior, The Row, Valentino, Brunello Cucinelli, Loro Piana.

Brand sheets generally calculate markup on wholesale totals, then add markup to obtain the final total. Ready and shipment columns hold wholesale allocations; their bottom totals receive the same markup.

Detail sheets retain cents. The master dashboard and several PDFs display whole euros. Example: `1028 × 35% = 359.80`, final `1387.80`, displayed as `360` markup and `1388` final. The app stores accounting values at two decimals and applies `ROUND_HALF_UP` only for display when a whole-euro view is requested.

## Summary logic found

- Per-order prepayment: `order total × 50%`.
- Per-order saldo: `final order total - Ready - all shipment columns`.
- Workbook totals: order value `114,599.25`; required 50% `57,299.625`; Ready `26,396.00`; saldo `17,851.05`.
- Total payments in the inspected workbook: `106,175`.
- Current-charge prepayment row is **not** 50% of the original order total. Cell `C41` is `N22 × 50%`: 50% of the still-unfulfilled saldo.
- The legacy workbook current-charge formula is therefore `Ready + shipped + 50% of remaining saldo`. The corrected application does **not** preserve this as an opaque shortcut: it represents the equivalent business stages as initial prepayment plus the remaining percentage for items in a Ready batch.
- Explicit transport/bag lines are added afterward. Workbook total charged is approximately `110,731.26`; payable is approximately `4,556.26`.

Transport examples include grouped eligible values and 5% fees, such as `34,038 × 5% = 1,701.90`, displayed/entered as `1,702`.

## Ready, shipments, and later PDFs

The workbook contains Ready plus `Sped 1`, `2`, `3`, `8`, `10`, `11`, `12`, `14`, and `16`. `SS26 AA 2.pdf` is a later snapshot that also shows `Sped 17`, `18`, and `21`. The application discovers shipments dynamically and can create those later batches, but the Excel importer does not pretend they exist in the older workbook.

Shipment PDFs show item-level customer allocation and size, including `AA`, and confirm that shipment documents are product filtered.

## Customer codes

`AA` appears in the workbook title/context and product notes. Other explicit codes include `BB` and `Sp`; aliases are normalized before customer lookup. Blank customer fields default to `AA` only under this import profile because the file is the AA report.

The corrected parser works per source unit. `AA 40` and `AA S` allocate to AA; a reconciled description such as `AA 38, SV 40` with quantity 2 becomes two customer items. Four actual Dior rows say `IdS/SV` plus `AA` but have source quantity 1. Assigning either customer would be a guess, so the migrated database sets those products to unallocated and stores file, sheet, row, raw description, raw row, and reason in the dedicated Allocation Review table.

## Source discrepancies retained for review

- `THE ROW pre` contains broken `#REF!` formulas in rows 13-15.
- `KITON MEN` contains cached `#VALUE!` cells and size/quantity inconsistencies.
- Several rows have quantity and price but a blank/zero total (for example parts of Dior Cruise, Kiton, Loro Piana, and The Row). These are imported as `Cancelled` so the database initially reconciles to the workbook order total; each is also flagged for review.
- Readiness ranges usually omit a year, and at least one range uses `31/04`. They are retained as source notes rather than converted into guessed dates.
- Some transport lines group several shipments. Historical values are imported as explicit expenses because allocating them to one shipment would be an unsupported guess.

## Import policy

Every confidently interpreted fact is imported. Allocation ambiguity goes to `Allocation Review`; other uncertainty goes to `Import Review`. Rows are never discarded. Derived database totals are not imported as editable facts; workbook summary totals are retained only as reconciliation references.

The supplier/detail sheets are treated as the primary incoming product source. The master sheet remains the reconciliation/payment source. Historical fulfilled items are grouped under `Ready 1 (Imported)` because the source has Ready and Sped columns but no durable Ready-batch identifier.
