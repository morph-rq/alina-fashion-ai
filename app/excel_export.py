from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.calculations import PAYMENT_TYPE_LABELS, final_total, final_unit_price, item_amounts, signed_payment_amount
from app.models import (
    Brand,
    BrandSeasonRule,
    Collection,
    CollectionItem,
    Customer,
    Expense,
    ImportBatch,
    LibraryFile,
    Order,
    OrderItem,
    Payment,
    Season,
    Shipment,
    ShipmentItem,
)
from app.services import shipment_summary
from app.models import PurchaseGroup, SellerCreditAccount, SellerCreditEntry
from app.worksheet import status_of, STATUS_LABELS, split_cost, account_balance


INK = "1E2423"
CREAM = "F7F4EE"
GOLD = "B49363"
BLUE = "DDE8ED"
STONE = "D7D2C8"
WHITE = "FFFFFF"
GREEN = "DCEBDD"
MONEY_FORMAT = '#,##0.00 "€"'
PERCENT_FORMAT = "0.0%"
DATE_FORMAT = "yyyy-mm-dd"


def _number(value: Decimal | int | None) -> float | int | None:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _safe_table_name(name: str, index: int) -> str:
    return (re.sub(r"[^A-Za-z0-9_]", "", name) or "Data")[:220] + f"{index}Table"


def _new_data_sheet(workbook: Workbook, title: str, subtitle: str, headers: list[str], rows: list[list], table_index: int):
    sheet = workbook.create_sheet(title[:31])
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(1, len(headers)))
    title_cell = sheet.cell(1, 1, title)
    title_cell.fill = PatternFill("solid", fgColor=INK)
    title_cell.font = Font(name="Aptos Display", size=18, bold=True, color=WHITE)
    title_cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 34
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(1, len(headers)))
    sheet.cell(2, 1, subtitle).font = Font(name="Aptos", size=9, italic=True, color="6C6B66")
    sheet.cell(2, 1).alignment = Alignment(vertical="center")
    sheet.row_dimensions[2].height = 24
    for column, header in enumerate(headers, 1):
        cell = sheet.cell(4, column, header)
        cell.fill = PatternFill("solid", fgColor=GOLD)
        cell.font = Font(name="Aptos", size=9, bold=True, color=WHITE)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    sheet.row_dimensions[4].height = 30
    for row_index, row in enumerate(rows, 5):
        for column, value in enumerate(row, 1):
            cell = sheet.cell(row_index, column, value)
            if isinstance(value, str):
                cell.data_type = 's'  # Text (SKUs, names, notes) is never interpreted as a formula or number.
            cell.font = Font(name="Aptos", size=9, color=INK)
            cell.alignment = Alignment(vertical="top", wrap_text=column in {5, 7, 19, 20, 24})
            if row_index % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=CREAM)
    sheet.freeze_panes = "A5"
    if rows:
        reference = f"A4:{sheet.cell(4 + len(rows), len(headers)).coordinate}"
        table = Table(displayName=_safe_table_name(title, table_index), ref=reference)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showFirstColumn=False, showLastColumn=False, showRowStripes=True, showColumnStripes=False)
        sheet.add_table(table)
    sheet.auto_filter.ref = f"A4:{sheet.cell(max(4, 4 + len(rows)), len(headers)).coordinate}"
    return sheet


def _finish_widths(sheet, widths: list[float]) -> None:
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = min(max(width, 8), 42)


def build_business_workbook(
    db: Session,
    *,
    season_id: int | None = None,
    customer_id: int | None = None,
    collection_id: int | None = None,
) -> bytes:
    """Create an auditable, editable Excel export for the selected business scope."""
    selected_season = db.get(Season, season_id) if season_id else None
    selected_customer = db.get(Customer, customer_id) if customer_id else None
    selected_collection = db.scalar(
        select(Collection).where(Collection.id == collection_id).options(selectinload(Collection.season), selectinload(Collection.items))
    ) if collection_id else None
    if season_id and selected_season is None:
        raise ValueError("Season not found")
    if customer_id and selected_customer is None:
        raise ValueError("Customer not found")
    if collection_id and selected_collection is None:
        raise ValueError("Collection not found")

    item_statement = select(OrderItem).options(
        selectinload(OrderItem.order).selectinload(Order.brand),
        selectinload(OrderItem.order).selectinload(Order.season),
        selectinload(OrderItem.customer),
        selectinload(OrderItem.ready_batch),
        selectinload(OrderItem.shipment_link).selectinload(ShipmentItem.shipment),
    ).order_by(OrderItem.id)
    if season_id:
        item_statement = item_statement.join(Order).where(Order.season_id == season_id)
    if customer_id:
        item_statement = item_statement.where(OrderItem.customer_id == customer_id)
    if collection_id:
        collection_item_ids = [link.order_item_id for link in selected_collection.items]
        item_statement = item_statement.where(OrderItem.id.in_(collection_item_ids or [-1]))
    items = db.scalars(item_statement).all()
    order_ids = sorted({item.order_id for item in items})
    customer_ids = sorted({item.customer_id for item in items if item.customer_id})

    order_statement = select(Order).options(selectinload(Order.brand), selectinload(Order.season)).order_by(Order.id)
    if season_id:
        order_statement = order_statement.where(Order.season_id == season_id)
    elif customer_id or collection_id:
        order_statement = order_statement.where(Order.id.in_(order_ids or [-1]))
    orders = db.scalars(order_statement).all()

    customer_statement = select(Customer).options(selectinload(Customer.aliases)).order_by(Customer.code)
    if customer_id:
        customer_statement = customer_statement.where(Customer.id == customer_id)
    elif collection_id:
        customer_statement = customer_statement.where(Customer.id.in_(customer_ids or [-1]))
    customers = db.scalars(customer_statement).all()

    rule_statement = select(BrandSeasonRule).options(selectinload(BrandSeasonRule.brand), selectinload(BrandSeasonRule.season)).order_by(BrandSeasonRule.id)
    if season_id:
        rule_statement = rule_statement.where(BrandSeasonRule.season_id == season_id)
    elif customer_id or collection_id:
        relevant_pairs = {(order.season_id, order.brand_id) for order in orders}
        rules = [rule for rule in db.scalars(rule_statement).all() if (rule.season_id, rule.brand_id) in relevant_pairs]
    else:
        rules = db.scalars(rule_statement).all()
    if season_id:
        rules = db.scalars(rule_statement).all()

    payment_statement = select(Payment).options(selectinload(Payment.season), selectinload(Payment.customer), selectinload(Payment.order)).order_by(Payment.date, Payment.id)
    if season_id:
        payment_statement = payment_statement.where(Payment.season_id == season_id)
    if customer_id:
        payment_statement = payment_statement.where(Payment.customer_id == customer_id)
    if collection_id:
        payment_statement = payment_statement.where(Payment.id == -1)
    payments = db.scalars(payment_statement).all()

    shipment_statement = select(Shipment).options(selectinload(Shipment.season), selectinload(Shipment.items)).order_by(Shipment.id)
    if season_id:
        shipment_statement = shipment_statement.where(Shipment.season_id == season_id)
    shipments = db.scalars(shipment_statement).all()
    if customer_id or collection_id:
        relevant_item_ids = {item.id for item in items}
        shipments = [shipment for shipment in shipments if any(link.order_item_id in relevant_item_ids for link in shipment.items)]

    expense_statement = select(Expense).options(selectinload(Expense.season), selectinload(Expense.customer), selectinload(Expense.order), selectinload(Expense.shipment)).order_by(Expense.id)
    if season_id:
        expense_statement = expense_statement.where(Expense.season_id == season_id)
    if customer_id:
        expense_statement = expense_statement.where(Expense.customer_id == customer_id)
    if collection_id:
        expense_statement = expense_statement.where(Expense.id == -1)
    expenses = db.scalars(expense_statement).all()

    collections_statement = select(Collection).options(selectinload(Collection.season), selectinload(Collection.items).selectinload(CollectionItem.order_item)).order_by(Collection.created_at)
    if collection_id:
        collections_statement = collections_statement.where(Collection.id == collection_id)
    elif season_id:
        collections_statement = collections_statement.where((Collection.season_id == season_id) | Collection.season_id.is_(None))
    collections = db.scalars(collections_statement).all()

    if selected_collection:
        scope_title = f"Collection · {selected_collection.name}"
    elif selected_customer:
        scope_title = f"Customer · {selected_customer.name or selected_customer.code}"
    elif selected_season:
        scope_title = f"Season · {selected_season.name}"
    else:
        scope_title = "Complete business data"
    generated = datetime.now().astimezone()
    subtitle = f"{scope_title} · generated {generated:%Y-%m-%d %H:%M} · editable source data and traceable formulas"

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.calculation.calcMode = "auto"
    workbook.properties.title = f"Fashion Orders — {scope_title}"
    workbook.properties.subject = "Fashion order operations and financial data export"
    workbook.properties.creator = "Fashion Orders"

    product_rows = []
    cost_shares = {}
    for group in db.scalars(select(PurchaseGroup)).all():
        cost_shares.update(split_cost(group.special_cost, sorted(group.items,key=lambda i:i.id), group.cost_method))
    for item in items:
        product_rows.append([
            item.id, item.order.season.name, item.order.brand.name, item.customer.code if item.customer else None,
            item.customer.name if item.customer else None, item.article, item.name, item.color, item.size, item.quantity,
            _number(item.wholesale_unit_price), _number(item.markup_percentage), None, None, item.status,
            item.ready_batch.name if item.ready_batch else None, item.shipment.name if item.shipment else None,
            item.requested_delivery_date, item.logistics_preference, item.customer_requirements, item.image_path,
            item.product_url, item.source_file, item.source_sheet, item.source_row,
            STATUS_LABELS[status_of(item)], item.purchase_group_id,
            item.purchase_group.name if item.purchase_group else None,
            item.purchase_group.distributor.name if item.purchase_group and item.purchase_group.distributor else None,
            item.purchase_group.seller if item.purchase_group else None,
            _number(cost_shares.get(item.id,0)), item.worksheet_note,
            _number(item_amounts(item).final) if item.status != "Cancelled" else 0, item.revision,
        ])
    products = _new_data_sheet(workbook, "Products", subtitle, [
        "ID", "Season", "Brand", "Customer code", "Customer", "Article / SKU", "Product", "Color", "Size", "Quantity",
        "Wholesale unit", "Markup", "Final unit", "Final total", "Status", "Ready batch", "Shipment",
        "Requested delivery", "Logistics", "Customer requirements", "Image path", "Product link", "Source file", "Source sheet", "Source row",
        "Worksheet status", "Group ID", "Shipment group", "Distributor", "Seller", "Group special cost share", "Worksheet note",
        "App final total", "Row revision",
    ], product_rows, 1)
    for row_index, item in enumerate(items, 5):
        fill = PatternFill('solid', fgColor={'process':'FFF5C5','done':'E4F1DD','pending':'FFFFFF'}[status_of(item)])
        for cell in products[row_index]:
            cell.fill = fill
        # Same rounding as the Decimal engine: line base rounded, markup rounded, cancelled rows count 0.
        r = row_index
        products.cell(r, 13, _number(item.selling_unit_price) if item.selling_unit_price is not None else f'=IF(O{r}="Cancelled",0,ROUND(K{r}*(1+L{r}),2))')
        if item.selling_unit_price is not None:
            products.cell(r, 14, f'=IF(O{r}="Cancelled",0,ROUND(M{r}*J{r},2))')
        else:
            products.cell(r, 14, f'=IF(O{r}="Cancelled",0,ROUND(K{r}*J{r},2)+ROUND(ROUND(K{r}*J{r},2)*L{r},2))')
        if item.product_url:
            products.cell(row_index, 22).hyperlink = item.product_url
            products.cell(row_index, 22).style = "Hyperlink"
    for column in (11, 13, 14, 33):
        for cell in products.iter_cols(min_col=column, max_col=column, min_row=5, max_row=max(5, 4 + len(items))):
            for entry in cell:
                entry.number_format = MONEY_FORMAT
    for entry in products.iter_cols(min_col=12, max_col=12, min_row=5, max_row=max(5, 4 + len(items))):
        for cell in entry:
            cell.number_format = PERCENT_FORMAT
    for entry in products.iter_cols(min_col=18, max_col=18, min_row=5, max_row=max(5, 4 + len(items))):
        for cell in entry:
            cell.number_format = DATE_FORMAT
    _finish_widths(products, [8, 11, 20, 13, 20, 19, 27, 15, 10, 9, 15, 10, 15, 15, 14, 16, 16, 16, 16, 30, 24, 32, 25, 18, 10])

    order_rows = []
    for order in orders:
        wholesale_total = sum((item_amounts(item).base for item in order.items if item.status != "Cancelled"), Decimal("0"))
        final_amount = sum((item_amounts(item).final for item in order.items if item.status != "Cancelled"), Decimal("0"))
        order_rows.append([
            order.id, order.season.name, order.brand.name, order.status, order.factory_ready_from, order.factory_ready_to,
            order.factory_ready_note, order.catalog_url, len(order.items), _number(wholesale_total),
            _number(final_amount), order.source_sheet,
        ])
    order_sheet = _new_data_sheet(workbook, "Orders", subtitle, ["ID", "Season", "Brand", "Status", "Ready from", "Ready to", "Ready note", "Catalog link", "Products", "Wholesale total", "Final total", "Source sheet"], order_rows, 2)
    for row_index, order in enumerate(orders, 5):
        if order.catalog_url:
            order_sheet.cell(row_index, 8).hyperlink = order.catalog_url
            order_sheet.cell(row_index, 8).style = "Hyperlink"
    for column in (10, 11):
        for row in range(5, 5 + len(orders)):
            order_sheet.cell(row, column).number_format = MONEY_FORMAT
    for column in (5, 6):
        for row in range(5, 5 + len(orders)):
            order_sheet.cell(row, column).number_format = DATE_FORMAT
    _finish_widths(order_sheet, [8, 12, 22, 14, 13, 13, 28, 34, 10, 16, 16, 18])

    customer_rows = [[
        customer.id, customer.code, customer.name, customer.phone, customer.email, customer.country_code, customer.address,
        customer.city, customer.postal_code, customer.preferred_contact, customer.notes,
        ", ".join(alias.alias for alias in customer.aliases), len([item for item in items if item.customer_id == customer.id]),
    ] for customer in customers]
    customer_sheet = _new_data_sheet(workbook, "Customers", subtitle, ["ID", "Code", "Name", "Phone", "Email", "Country", "Address", "City", "Postal code", "Preferred contact", "Notes", "Aliases", "Products in export"], customer_rows, 3)
    _finish_widths(customer_sheet, [8, 10, 23, 17, 27, 10, 35, 18, 13, 18, 36, 24, 16])

    rule_rows = [[
        rule.id, rule.season.name, rule.brand.name, _number(rule.markup_percentage), _number(rule.initial_prepayment_percentage),
        _number(rule.ready_balance_percentage), _number(rule.transport_percentage), _number(rule.special_cost),
        rule.transport_exempt_country_codes, rule.expected_receipt_days, rule.rounding_mode, rule.readiness_payment_logic, rule.notes,
    ] for rule in rules]
    rules_sheet = _new_data_sheet(workbook, "Rules", subtitle, ["ID", "Season", "Brand", "Markup", "Initial payment", "Ready payment", "Transport", "Special cost", "Exempt countries", "Receipt days", "Rounding", "Ready logic", "Notes"], rule_rows, 4)
    for column in (4, 5, 6, 7):
        for row in range(5, 5 + len(rules)):
            rules_sheet.cell(row, column).number_format = PERCENT_FORMAT
    for row in range(5, 5 + len(rules)):
        rules_sheet.cell(row, 8).number_format = MONEY_FORMAT
    _finish_widths(rules_sheet, [8, 12, 22, 11, 13, 13, 11, 14, 18, 12, 17, 20, 36])

    payment_rows = [[payment.id, payment.season.name if payment.season else None, payment.customer.code if payment.customer else None, payment.order.brand.name if payment.order else None, payment.date, _number(payment.amount), PAYMENT_TYPE_LABELS.get(payment.type, payment.type), _number(signed_payment_amount(payment.amount, payment.type)), payment.note] for payment in payments]
    payments_sheet = _new_data_sheet(workbook, "Payments", subtitle, ["ID", "Season", "Customer", "Brand / order", "Date", "Amount", "Type", "Net effect", "Note"], payment_rows, 5)
    for row in range(5, 5 + len(payments)):
        payments_sheet.cell(row, 5).number_format = DATE_FORMAT
        payments_sheet.cell(row, 6).number_format = MONEY_FORMAT
        payments_sheet.cell(row, 8).number_format = MONEY_FORMAT
    _finish_widths(payments_sheet, [8, 12, 13, 22, 13, 15, 16, 15, 38])

    shipment_rows = []
    for shipment in shipments:
        derived = shipment_summary(shipment)
        shipment_rows.append([
            shipment.id, shipment.season.name if shipment.season else None, shipment.name, shipment.date, shipment.status,
            shipment.destination_country_code, shipment.use_rule_transport, _number(shipment.transport_percentage),
            _number(derived.transport_fee), _number(shipment.fixed_cost), _number(shipment.suitcase_cost), _number(shipment.bag_cost),
            _number(derived.total_fees), len(shipment.items), shipment.notes,
        ])
    shipments_sheet = _new_data_sheet(workbook, "Shipments", subtitle, ["ID", "Season", "Shipment", "Date", "Status", "Destination", "Use rule", "Transport %", "Transport fee", "Fixed cost", "Suitcase", "Bags", "Total logistics", "Products", "Notes"], shipment_rows, 6)
    for row in range(5, 5 + len(shipments)):
        shipments_sheet.cell(row, 4).number_format = DATE_FORMAT
        shipments_sheet.cell(row, 8).number_format = PERCENT_FORMAT
        for column in range(9, 14):
            shipments_sheet.cell(row, column).number_format = MONEY_FORMAT
    _finish_widths(shipments_sheet, [8, 12, 20, 13, 14, 12, 11, 12, 15, 14, 14, 14, 17, 11, 38])

    expense_rows = [[expense.id, expense.season.name if expense.season else None, expense.customer.code if expense.customer else None, expense.order.brand.name if expense.order else None, expense.shipment.name if expense.shipment else None, expense.type, _number(expense.amount), expense.note] for expense in expenses]
    expenses_sheet = _new_data_sheet(workbook, "Expenses", subtitle, ["ID", "Season", "Customer", "Brand / order", "Shipment", "Type", "Amount", "Note"], expense_rows, 7)
    for row in range(5, 5 + len(expenses)):
        expenses_sheet.cell(row, 7).number_format = MONEY_FORMAT
    _finish_widths(expenses_sheet, [8, 12, 13, 22, 20, 16, 15, 38])

    collection_rows = []
    for collection in collections:
        for link in collection.items or [None]:
            linked_item = link.order_item if link else None
            collection_rows.append([
                collection.id, collection.name, collection.season.name if collection.season else None, collection.description,
                collection.created_at.replace(tzinfo=None) if collection.created_at else None,
                linked_item.id if linked_item else None, linked_item.article if linked_item else None, linked_item.name if linked_item else None,
            ])
    collections_sheet = _new_data_sheet(workbook, "Collections", subtitle, ["Collection ID", "Collection", "Season", "Description", "Created", "Product ID", "Article", "Product"], collection_rows, 8)
    for row in range(5, 5 + len(collection_rows)):
        collections_sheet.cell(row, 5).number_format = "yyyy-mm-dd hh:mm"
    _finish_widths(collections_sheet, [14, 24, 12, 36, 19, 12, 20, 28])

    if not customer_id and not collection_id:
        brands = db.scalars(select(Brand).order_by(Brand.name)).all()
        brand_rows = [[brand.id, brand.name, _number(brand.default_markup_percentage), brand.website_url, brand.notes] for brand in brands]
        brands_sheet = _new_data_sheet(workbook, "Brands", subtitle, ["ID", "Brand", "Default markup", "Official website", "Notes"], brand_rows, 9)
        for row_index, brand in enumerate(brands, 5):
            brands_sheet.cell(row_index, 3).number_format = PERCENT_FORMAT
            if brand.website_url:
                brands_sheet.cell(row_index, 4).hyperlink = brand.website_url
                brands_sheet.cell(row_index, 4).style = "Hyperlink"
        _finish_widths(brands_sheet, [8, 25, 16, 38, 40])

        library_files = db.scalars(select(LibraryFile).order_by(LibraryFile.uploaded_at)).all()
        imports = db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at)).all()
        file_rows = [["Library", row.filename, row.category, row.stored_path, row.size_bytes, row.uploaded_at.replace(tzinfo=None), row.note] for row in library_files]
        file_rows += [["Import", row.filename, "spreadsheet", row.stored_path, None, row.imported_at.replace(tzinfo=None), "Source removed" if row.source_deleted_at else None] for row in imports]
        files_sheet = _new_data_sheet(workbook, "Files", subtitle, ["Type", "Filename", "Category", "Stored path", "Bytes", "Added", "Note"], file_rows, 10)
        for row in range(5, 5 + len(file_rows)):
            files_sheet.cell(row, 6).number_format = "yyyy-mm-dd hh:mm"
        _finish_widths(files_sheet, [12, 32, 15, 42, 14, 19, 36])

    summary = workbook.create_sheet("Summary", 0)
    summary.sheet_view.showGridLines = False
    summary.merge_cells("A1:H1")
    summary["A1"] = "Fashion Orders · Business export"
    summary["A1"].fill = PatternFill("solid", fgColor=INK)
    summary["A1"].font = Font(name="Aptos Display", size=21, bold=True, color=WHITE)
    summary["A1"].alignment = Alignment(vertical="center")
    summary.row_dimensions[1].height = 40
    summary.merge_cells("A2:H2")
    summary["A2"] = subtitle
    summary["A2"].font = Font(name="Aptos", size=9, italic=True, color="6C6B66")
    summary["A4"] = "At a glance"
    summary["A4"].font = Font(name="Aptos Display", size=15, bold=True, color=INK)
    product_end = max(5, 4 + len(items))
    cards = [
        ("A6", "Products", len(items), "0"),
        ("C6", "Customers", len(customers), "0"),
        ("E6", "Wholesale", f'=SUMPRODUCT(ROUND(Products!K5:K{product_end}*Products!J5:J{product_end},2),--(Products!O5:O{product_end}<>"Cancelled"))', MONEY_FORMAT),
        ("G6", "Order revenue", f"=SUM('Products'!N5:N{product_end})", MONEY_FORMAT),
    ]
    for anchor, label, value, fmt in cards:
        column = summary[anchor].column
        summary.merge_cells(start_row=6, start_column=column, end_row=6, end_column=column + 1)
        summary.cell(6, column, label).fill = PatternFill("solid", fgColor=BLUE)
        summary.cell(6, column).font = Font(name="Aptos", size=9, bold=True, color=INK)
        summary.merge_cells(start_row=7, start_column=column, end_row=8, end_column=column + 1)
        summary.cell(7, column, value)
        summary.cell(7, column).fill = PatternFill("solid", fgColor=CREAM)
        summary.cell(7, column).font = Font(name="Aptos Display", size=18, bold=True, color=INK)
        summary.cell(7, column).alignment = Alignment(vertical="center")
        summary.cell(7, column).number_format = fmt
    summary["A11"] = "How to use this workbook"
    summary["A11"].font = Font(name="Aptos Display", size=14, bold=True, color=INK)
    notes = [
        "Use the filters in row 4 of each data sheet to find exactly what you need.",
        "Products keeps wholesale price and markup as inputs; Final unit and Final total are formulas using the app's cent rounding.",
        "App final total is the authoritative amount from the app. The Checks sheet compares it with the formulas once Excel has calculated.",
        "The app remains the source of truth. To change records, use the editable order workbook (Excel files) or the app.",
        "Links remain clickable. Product images are listed by their managed path rather than embedded at full resolution.",
    ]
    for row_index, note in enumerate(notes, 12):
        summary.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=8)
        summary.cell(row_index, 1, f"• {note}")
        summary.cell(row_index, 1).font = Font(name="Aptos", size=10, color="555550")
        summary.cell(row_index, 1).alignment = Alignment(wrap_text=True, vertical="top")
        summary.row_dimensions[row_index].height = 23
    summary.freeze_panes = "A4"
    for column in range(1, 9):
        summary.column_dimensions[get_column_letter(column)].width = 15
    summary.sheet_properties.pageSetUpPr.fitToPage = True
    summary.page_setup.fitToWidth = 1

    thin = Side(style="thin", color=STONE)
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(min_row=4, max_row=sheet.max_row, min_col=1, max_col=sheet.max_column):
            for cell in row:
                if cell.row == 4:
                    cell.border = Border(bottom=Side(style="medium", color=INK))
                elif cell.value is not None:
                    cell.border = Border(bottom=thin)
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1

    if not customer_id and not collection_id:
        groups = db.scalars(select(PurchaseGroup).order_by(PurchaseGroup.id)).all()
        if season_id:
            groups = [g for g in groups if g.season_id == season_id]
        group_ids = {g.id for g in groups}
        _new_data_sheet(workbook, 'Shipment groups', subtitle,
            ['Group ID','Name','Season','Distributor','Seller','Currency','Initial %','Shipment %','Transport %','Special cost','Cost allocation','Shipment','Notes'],
            [[g.id,g.name,g.season.name,g.distributor.name if g.distributor else None,g.seller,g.currency,
              float(g.initial_percentage),float(g.shipment_percentage),float(g.transport_percentage),float(g.special_cost),
              g.cost_method,g.shipment.name if g.shipment else None,g.notes] for g in groups], 30)
        accounts = [a for a in db.scalars(select(SellerCreditAccount)).all() if a.group_id in group_ids]
        account_ids = {a.id for a in accounts}
        _new_data_sheet(workbook, 'Seller accounts', subtitle,
            ['Account ID','Group','Distributor','Seller','Customer','Product ID','Currency','Held balance'],
            [[a.id,a.group.name,a.group.distributor.name if a.group.distributor else None,a.group.seller,
              a.customer.code if a.customer else 'Distributor funds',a.item_id,a.group.currency,float(account_balance(a))] for a in accounts],31)
        entries = [e for e in db.scalars(select(SellerCreditEntry).order_by(SellerCreditEntry.id)).all() if e.account_id in account_ids]
        _new_data_sheet(workbook, 'Seller ledger', subtitle,
            ['Entry ID','Date','Account ID','Operation','Stage','Movement','Reference','Operation key'],
            [[e.id,e.date,e.account_id,e.kind,e.stage,float(e.amount),e.reference,e.operation_key] for e in entries],32)
    if not customer_id and not collection_id:
        _master_summary(workbook, orders, items, subtitle)
        brand_views(workbook, products, items)
    _checks_sheet(workbook, db, items, orders, scope_title, generated)
    for table in products.tables.values():
        table.tableStyleInfo.showRowStripes = False
    products.freeze_panes = 'A5'
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


EXPORT_SCHEMA = "alina-business-workbook/2"


def _master_summary(workbook, orders, items, subtitle) -> None:
    """Brand + Season summary linked to Products (initial due shown from the app)."""
    from app.services import client_initial_charges
    shipment_names = sorted({item.shipment.name for item in items if item.shipment})
    rows = []
    for order in orders:
        app_initial = sum(client_initial_charges(order).values(), Decimal("0"))
        rows.append([order.season.name, order.brand.name, None, _number(app_initial), None,
                     *([None] * len(shipment_names)), None, order.factory_ready_from, order.factory_ready_to, order.factory_ready_note])
    master = _new_data_sheet(workbook, "Master summary", "Brand + Season summary · linked to Products · initial due as calculated by the app",
                             ["Season", "Brand", "Order value", "Initial due (app)", "Ready (not shipped)", *shipment_names,
                              "Saldo", "Factory ready from", "Factory ready to", "Readiness note"], rows, 70)
    last = max(5, len(items) + 4)
    saldo_col = 6 + len(shipment_names)
    for row in range(5, len(orders) + 5):
        base = f"SUMIFS(Products!$N$5:$N${last},Products!$B$5:$B${last},A{row},Products!$C$5:$C${last},B{row}"
        master.cell(row, 3, "=" + base + ")")
        master.cell(row, 5, "=" + base + f',Products!$P$5:$P${last},"<>",Products!$Q$5:$Q${last},"")')
        for col, name in enumerate(shipment_names, 6):
            master.cell(row, col, "=" + base + f",Products!$Q$5:$Q${last},{get_column_letter(col)}$4)")
        master.cell(row, saldo_col, f"=C{row}-SUM(E{row}:{get_column_letter(saldo_col - 1)}{row})")
        for col in [3, 4, 5, *range(6, saldo_col + 1)]:
            master.cell(row, col).number_format = MONEY_FORMAT
        for col in (saldo_col + 1, saldo_col + 2):
            master.cell(row, col).number_format = DATE_FORMAT
    _finish_widths(master, [14, 24, 18, 18, 18] + [18] * len(shipment_names) + [18, 22, 22, 36])
    workbook.move_sheet(master, offset=1 - workbook.worksheets.index(master))


def brand_views(workbook, products, items) -> None:
    """One tab per brand whose cells link to Products, so edits stay in one place."""
    from copy import copy
    import re as _re
    grouped: dict[str, list] = {}
    for source_row, item in enumerate(items, 5):
        grouped.setdefault(item.order.brand.name, []).append(source_row)
    used = set(workbook.sheetnames)
    for index, (brand, members) in enumerate(grouped.items(), 1):
        title = (_re.sub(r"[\[\]:*?/\\]", "-", brand)[:26] or "Brand").strip()
        while title in used:
            title = f"{title[:24]} {index}"
        used.add(title)
        sheet = _new_data_sheet(workbook, title, "Linked to Products · change values there or in the app",
                                ["Article / SKU", "Product", "Customer", "Color", "Size", "Quantity", "Wholesale unit",
                                 "Markup", "Final total", "Status", "Ready batch", "Shipment"], [], 100 + index)
        for row, source_row in enumerate(members, 5):
            for col, source_col in enumerate([6, 7, 4, 8, 9, 10, 11, 12, 14, 15, 16, 17], 1):
                original = products.cell(source_row, source_col)
                cell = sheet.cell(row, col, f'=IF(Products!{original.coordinate}="","",Products!{original.coordinate})')
                cell._style = copy(original._style)
        _finish_widths(sheet, [20, 30, 13, 16, 10, 10, 15, 10, 16, 16, 16, 16])


def _checks_sheet(workbook, db, items, orders, scope_title, generated) -> None:
    """Export metadata and reconciliation rows (formula result vs. authoritative app amount)."""
    from sqlalchemy import func
    from app.models import AuditLog
    active = [item for item in items if item.status != "Cancelled"]
    last = max(5, len(items) + 4)
    app_total = sum((item_amounts(item).final for item in active), Decimal("0"))
    sheet = workbook.create_sheet("Checks")
    sheet["A1"] = "Export checks"
    sheet["A1"].font = Font(name="Aptos Display", size=16, bold=True, color=INK)
    meta = [("Scope", scope_title), ("Generated", generated.strftime("%Y-%m-%d %H:%M:%S %Z")), ("Workbook schema", EXPORT_SCHEMA),
            ("Products", len(items)), ("Highest product revision", max((item.revision for item in items), default=0)),
            ("Last audit entry", db.scalar(select(func.max(AuditLog.id))) or 0)]
    for row, (label, value) in enumerate(meta, 3):
        sheet.cell(row, 1, label).font = Font(bold=True)
        cell = sheet.cell(row, 2, value)
        if isinstance(value, str):
            cell.data_type = "s"
    header = len(meta) + 4
    for col, title in enumerate(["Check", "Formula result", "App amount", "Status"], 1):
        sheet.cell(header, col, title).font = Font(bold=True)
    checks = [("Products: final total", f"=SUM(Products!N5:N{last})", app_total)]
    for order in orders:
        value = sum((item_amounts(item).final for item in order.items if item.status != "Cancelled" and item in items), Decimal("0"))
        checks.append((f"{order.season.name} / {order.brand.name}",
                       f'=SUMIFS(Products!$N$5:$N${last},Products!$B$5:$B${last},"{order.season.name}",Products!$C$5:$C${last},"{order.brand.name.replace(chr(34), chr(39))}")', value))
    for offset, (label, formula, value) in enumerate(checks, header + 1):
        sheet.cell(offset, 1, label).data_type = "s"
        sheet.cell(offset, 2, formula).number_format = MONEY_FORMAT
        sheet.cell(offset, 3, _number(value)).number_format = MONEY_FORMAT
        sheet.cell(offset, 4, f'=IF(ABS(B{offset}-C{offset})<0.005,"OK","CHECK")')
    sheet.cell(header + len(checks) + 2, 1, "Formula results appear after Excel or another spreadsheet application calculates the file. "
               "The app amounts are fixed values from the export moment.").alignment = Alignment(wrap_text=True)
    _finish_widths(sheet, [42, 18, 18, 12])


def verify_business_workbook(content: bytes) -> list[str]:
    """Recompute the Products formulas' rule from the cell inputs and compare with the app amounts.

    openpyxl does not calculate formulas, so this checks that each row's formula
    encodes the Decimal rule and that its inputs reproduce the authoritative amount.
    """
    from decimal import ROUND_HALF_UP
    from openpyxl import load_workbook
    def rounded(value):
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    problems = []
    sheet = load_workbook(BytesIO(content))["Products"]
    for row in range(5, sheet.max_row + 1):
        if sheet.cell(row, 1).value is None:
            continue
        quantity, unit, markup = sheet.cell(row, 10).value, sheet.cell(row, 11).value, sheet.cell(row, 12).value
        override, status, expected = sheet.cell(row, 13).value, sheet.cell(row, 15).value, Decimal(str(sheet.cell(row, 33).value or 0))
        formula = sheet.cell(row, 14).value
        if status == "Cancelled":
            computed = Decimal("0")
        elif isinstance(override, (int, float)):
            if formula != f'=IF(O{row}="Cancelled",0,ROUND(M{row}*J{row},2))':
                problems.append(f"Row {row}: unexpected override formula")
            computed = rounded(Decimal(str(override)) * quantity)
        else:
            if formula != f'=IF(O{row}="Cancelled",0,ROUND(K{row}*J{row},2)+ROUND(ROUND(K{row}*J{row},2)*L{row},2))':
                problems.append(f"Row {row}: unexpected total formula")
            base = rounded(Decimal(str(unit or 0)) * quantity)
            computed = base + rounded(base * Decimal(str(markup or 0)))
        if computed != expected:
            problems.append(f"Row {row}: formula rule gives {computed}, app amount is {expected}")
        for column in (4, 6, 7, 9):
            cell = sheet.cell(row, column)
            if cell.value is not None and cell.data_type != "s":
                problems.append(f"Row {row}: column {column} is not stored as text")
    return problems
