"""Workspace › Tables: describe a table, confirm the draft, then work in a spreadsheet-style grid.

Season/brand tables are real order tables (Season, Brand, Brand + Season rule, Order); existing
rules are never overwritten. Custom tables are free-form and never feed accounting. Every change
is audited; cell saves carry the row revision so a stale browser cannot overwrite newer values.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from app import item_service
from app.audit import record_change
from app.database import get_db
from app.models import Brand, BrandSeasonRule, Customer, CustomTable, CustomTableRow, Order, OrderItem, Season
from app.table_planner import COLUMN_TYPES, plan_table, rows_from_text

DEFAULT_MARKUP, DEFAULT_INITIAL, DEFAULT_TRANSPORT = Decimal("35"), Decimal("50"), Decimal("5")
ITEM_CELLS = {"name", "article", "color", "size", "quantity", "wholesale", "markup", "customer"}


def columns_of(table: CustomTable) -> list[dict]:
    return json.loads(table.columns_json or "[]")


def rows_of(db, table: CustomTable) -> list[CustomTableRow]:
    return db.scalars(select(CustomTableRow).where(CustomTableRow.table_id == table.id)
                      .order_by(CustomTableRow.position, CustomTableRow.id)).all()


def values_of(row: CustomTableRow) -> dict:
    return json.loads(row.values_json or "{}")


def column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def clean_cell(value: str, kind: str) -> str:
    """Store cells as text, but refuse values that do not fit a typed column."""
    value = (value or "").strip()[:2000]
    if not value:
        return ""
    if kind in {"number", "money"}:
        try:
            number = Decimal(value.replace(" ", "").replace("€", "").replace(",", "."))
        except InvalidOperation:
            raise ValueError("Enter a number.") from None
        return f"{number:.2f}" if kind == "money" else format(number.normalize(), "f")
    if kind == "date":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise ValueError("Enter the date as YYYY-MM-DD.") from None
    return value


def _percent(raw: str, label: str, default: Decimal) -> Decimal:
    raw = (raw or "").strip().replace(",", ".")
    try:
        value = Decimal(raw) if raw else default
    except InvalidOperation:
        raise HTTPException(422, f"{label} must be a number.") from None
    if value < 0 or value > 100:
        raise HTTPException(422, f"{label} must be between 0% and 100%.")
    return value / Decimal(100)


def create_season_tables(db, season_name: str, brands: list[tuple[str, str, str, str]]) -> tuple[Season, list[str], list[str]]:
    name = season_name.strip().upper()
    if not name or len(name) > 30:
        raise HTTPException(422, "Give the season a short name, for example FW27.")
    season = db.scalar(select(Season).where(func.upper(Season.name) == name))
    if season is None:
        season = Season(name=name)
        db.add(season)
        db.flush()
        record_change(db, entity_type="Season", entity_id=season.id, field=None, previous_value=None, new_value=name,
                      source_user="web user", description=f"Created season {name} from a table description")
    created, kept = [], []
    for brand_name, markup, initial, transport in brands:
        brand_name = " ".join(brand_name.split())[:200]
        if not brand_name:
            continue
        rule_values = (_percent(markup, "Markup", DEFAULT_MARKUP), _percent(initial, "Initial payment", DEFAULT_INITIAL),
                       _percent(transport, "Transport", DEFAULT_TRANSPORT))
        brand = db.scalar(select(Brand).where(func.lower(Brand.name) == brand_name.casefold()))
        if brand is None:
            brand = Brand(name=brand_name, default_markup_percentage=rule_values[0])
            db.add(brand)
            db.flush()
        if db.scalar(select(Order.id).where(Order.season_id == season.id, Order.brand_id == brand.id)):
            kept.append(brand.name)
            continue
        if not db.scalar(select(BrandSeasonRule.id).where(BrandSeasonRule.season_id == season.id, BrandSeasonRule.brand_id == brand.id)):
            db.add(BrandSeasonRule(season_id=season.id, brand_id=brand.id, markup_percentage=rule_values[0],
                                   initial_prepayment_percentage=rule_values[1], ready_balance_percentage=1 - rule_values[1],
                                   transport_percentage=rule_values[2], special_cost=Decimal(0),
                                   transport_exempt_country_codes="CH", expected_receipt_days=14))
        order = Order(season_id=season.id, brand_id=brand.id, status="Ordered")
        db.add(order)
        db.flush()
        record_change(db, entity_type="Order", entity_id=order.id, field=None, previous_value=None, new_value=brand.name,
                      source_user="web user", description=f"Created {brand.name} table in {name} from a table description")
        created.append(brand.name)
    return season, created, kept


def table_workbook(table: CustomTable, rows: list[CustomTableRow]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = table.name[:31].translate(str.maketrans("[]:*?/\\", "_______")) or "Table"
    columns = columns_of(table)
    sheet.append([c["name"] for c in columns])
    for row in rows:
        values = values_of(row)
        out = []
        for c in columns:
            v = values.get(c["key"], "")
            if v and c["type"] in {"number", "money"}:
                v = float(v)
            elif v and c["type"] == "date":
                v = date.fromisoformat(v)
            out.append(v)
        sheet.append(out)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="243B35")
    for index, c in enumerate(columns):
        letter = column_letter(index)
        sheet.column_dimensions[letter].width = min(max(len(c["name"]) + 4, 12), 40)
        if c["type"] == "money":
            for cell in sheet[letter][1:]:
                cell.number_format = "#,##0.00"
        elif c["type"] == "date":
            for cell in sheet[letter][1:]:
                cell.number_format = "yyyy-mm-dd"
    sheet.freeze_panes = "A2"
    if columns:
        sheet.auto_filter.ref = sheet.dimensions
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def install_tables(app, templates, context):
    def page(request, **values):
        db = values.pop("db")
        tables = db.scalars(select(CustomTable).order_by(CustomTable.name)).all()
        seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
        return templates.TemplateResponse(request, "tables.html", context(
            request, tables=tables, seasons=seasons, column_types=COLUMN_TYPES, columns_of=columns_of,
            row_counts={t.id: len(rows_of(db, t)) for t in tables}, **values))

    def get_table(db, table_id):
        table = db.get(CustomTable, table_id)
        if table is None:
            raise HTTPException(404, "Table not found.")
        return table

    @app.get("/tables")
    def tables_page(request: Request, db=Depends(get_db)):
        return page(request, db=db)

    @app.post("/tables/plan")
    def tables_plan(request: Request, description: str = Form(...), db=Depends(get_db)):
        try:
            plan = plan_table(description)
        except ValueError as exc:
            return page(request, db=db, error=str(exc), description=description)
        if plan.kind == "custom":
            plan.columns = [c.model_copy(update={"name": c.name[:1].upper() + c.name[1:]}) for c in plan.columns]
        return page(request, db=db, plan=plan, description=description)

    @app.post("/tables/season")
    async def tables_create_season(request: Request, db=Depends(get_db)):
        form = await request.form()
        brands = list(zip(form.getlist("brand_name"), form.getlist("markup"), form.getlist("initial"), form.getlist("transport")))
        season, created, kept = create_season_tables(db, str(form.get("season") or ""), brands)
        db.commit()
        note = f"created={len(created)}&kept={len(kept)}"
        return RedirectResponse(f"/seasons/{quote(season.name, safe='')}?{note}", 303)

    @app.post("/tables/custom")
    async def tables_create_custom(request: Request, db=Depends(get_db)):
        form = await request.form()
        name = " ".join(str(form.get("name") or "").split())[:120]
        if not name:
            raise HTTPException(422, "Give the table a name.")
        if db.scalar(select(CustomTable.id).where(func.lower(CustomTable.name) == name.casefold())):
            raise HTTPException(409, f"A table called {name} already exists. Choose another name.")
        columns = []
        for column_name, kind in zip(form.getlist("column_name"), form.getlist("column_type")):
            column_name = " ".join(str(column_name).split())[:60]
            if column_name:
                columns.append({"key": f"c{len(columns) + 1}", "name": column_name, "type": kind if kind in COLUMN_TYPES else "text"})
        if not columns:
            raise HTTPException(422, "Add at least one column.")
        table = CustomTable(name=name, description=str(form.get("description") or "")[:2000] or None, columns_json=json.dumps(columns))
        db.add(table)
        db.flush()
        try:
            rows = json.loads(str(form.get("rows_json") or "[]"))
        except ValueError:
            rows = []
        for position, cells in enumerate(rows[:500]):
            values = {}
            for column, cell in zip(columns, cells):
                try:
                    values[column["key"]] = clean_cell(str(cell), column["type"])
                except ValueError:
                    values[column["key"]] = str(cell)[:2000]   # keep what was described; the grid flags it
            db.add(CustomTableRow(table_id=table.id, position=position, values_json=json.dumps(values, ensure_ascii=False)))
        record_change(db, entity_type="CustomTable", entity_id=table.id, field=None, previous_value=None, new_value=name,
                      source_user="web user", description=f"Created table {name} with {len(columns)} columns and {len(rows[:500])} rows")
        db.commit()
        return RedirectResponse(f"/tables/{table.id}", 303)

    @app.get("/tables/{table_id}")
    def table_page(table_id: int, request: Request, db=Depends(get_db)):
        table = get_table(db, table_id)
        return templates.TemplateResponse(request, "table_grid.html", context(
            request, table=table, rows=rows_of(db, table), columns=columns_of(table), values_of=values_of, column_letter=column_letter,
            column_types=COLUMN_TYPES))

    @app.post("/tables/{table_id}/cell")
    async def table_cell(table_id: int, request: Request, db=Depends(get_db)):
        table = get_table(db, table_id)
        body = await request.json()
        row = db.get(CustomTableRow, int(body.get("row_id") or 0))
        column = next((c for c in columns_of(table) if c["key"] == body.get("key")), None)
        if row is None or row.table_id != table.id or column is None:
            return JSONResponse({"error": "This cell no longer exists. Reload the table."}, 404)
        if str(body.get("revision")) != str(row.revision):
            return JSONResponse({"error": "Someone changed this row meanwhile. Reload to see the newer value."}, 409)
        try:
            value = clean_cell(str(body.get("value") or ""), column["type"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, 422)
        values = values_of(row)
        previous = values.get(column["key"], "")
        if previous != value:
            values[column["key"]] = value
            row.values_json = json.dumps(values, ensure_ascii=False)
            record_change(db, entity_type="CustomTableRow", entity_id=row.id, field=column["name"], previous_value=previous,
                          new_value=value, source_user="web user", description=f"{table.name}: {column['name']} changed")
            try:
                db.commit()
            except StaleDataError:
                db.rollback()
                return JSONResponse({"error": "Someone changed this row meanwhile. Reload to see the newer value."}, 409)
        return {"value": value, "revision": row.revision}

    @app.post("/tables/{table_id}/rows")
    def table_add_row(table_id: int, count: int = Form(1), db=Depends(get_db)):
        table = get_table(db, table_id)
        start = max((r.position for r in rows_of(db, table)), default=-1) + 1
        for offset in range(max(1, min(count, 50))):
            db.add(CustomTableRow(table_id=table.id, position=start + offset, values_json="{}"))
        db.commit()
        return RedirectResponse(f"/tables/{table.id}#bottom", 303)

    @app.post("/tables/{table_id}/rows/describe")
    def table_rows_from_text(table_id: int, text: str = Form(...), db=Depends(get_db)):
        table = get_table(db, table_id)
        columns = columns_of(table)
        rows, note = rows_from_text(text, columns)
        start = max((r.position for r in rows_of(db, table)), default=-1) + 1
        for offset, cells in enumerate(rows):
            values = {}
            for column, cell in zip(columns, cells):
                try:
                    values[column["key"]] = clean_cell(str(cell), column["type"])
                except ValueError:
                    values[column["key"]] = str(cell)[:2000]
            db.add(CustomTableRow(table_id=table.id, position=start + offset, values_json=json.dumps(values, ensure_ascii=False)))
        if rows:
            record_change(db, entity_type="CustomTable", entity_id=table.id, field=None, previous_value=None, new_value=len(rows),
                          source_user="web user", description=f"{table.name}: added {len(rows)} rows from a description")
        db.commit()
        query = f"added={len(rows)}" + (f"&note={quote(note)}" if note else "")
        return RedirectResponse(f"/tables/{table.id}?{query}#bottom", 303)

    @app.post("/tables/{table_id}/rows/{row_id}/delete")
    def table_delete_row(table_id: int, row_id: int, db=Depends(get_db)):
        table = get_table(db, table_id)
        row = db.get(CustomTableRow, row_id)
        if row is None or row.table_id != table.id:
            raise HTTPException(404, "Row not found.")
        record_change(db, entity_type="CustomTableRow", entity_id=row.id, field=None, previous_value=row.values_json,
                      new_value=None, source_user="web user", description=f"{table.name}: deleted a row (values kept here)")
        db.delete(row)
        db.commit()
        return RedirectResponse(f"/tables/{table.id}", 303)

    @app.post("/tables/{table_id}/columns")
    def table_add_column(table_id: int, name: str = Form(...), column_type: str = Form("text"), db=Depends(get_db)):
        table = get_table(db, table_id)
        columns = columns_of(table)
        name = " ".join(name.split())[:60]
        if not name or len(columns) >= 60:
            raise HTTPException(422, "Give the column a name (at most 60 columns).")
        used = {c["key"] for c in columns}
        key = next(f"c{i}" for i in range(1, 1000) if f"c{i}" not in used)
        columns.append({"key": key, "name": name, "type": column_type if column_type in COLUMN_TYPES else "text"})
        table.columns_json = json.dumps(columns)
        record_change(db, entity_type="CustomTable", entity_id=table.id, field="columns", previous_value=None, new_value=name,
                      source_user="web user", description=f"{table.name}: added column {name}")
        db.commit()
        return RedirectResponse(f"/tables/{table.id}", 303)

    @app.post("/tables/{table_id}/delete")
    def table_delete(table_id: int, confirmation: str = Form(...), db=Depends(get_db)):
        table = get_table(db, table_id)
        if confirmation.strip().casefold() != table.name.casefold():
            raise HTTPException(422, "Type the table name exactly to delete it.")
        record_change(db, entity_type="CustomTable", entity_id=table.id, field=None, previous_value=table.name, new_value=None,
                      source_user="web user", description=f"Deleted table {table.name} with {len(rows_of(db, table))} rows")
        db.delete(table)
        db.commit()
        return RedirectResponse("/tables?deleted=1", 303)

    @app.get("/tables/{table_id}/export.xlsx")
    def table_export(table_id: int, db=Depends(get_db)):
        table = get_table(db, table_id)
        filename = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in table.name)[:60] or "table"
        return Response(table_workbook(table, rows_of(db, table)), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'})

    @app.post("/worksheet/items/{item_id}/cell")
    async def item_cell(item_id: int, request: Request, db=Depends(get_db)):
        """Save one product cell edited in a grid; Ready/shipped money fields stay in the product editor."""
        body = await request.json()
        field, raw = str(body.get("field") or ""), body.get("value")
        item = db.get(OrderItem, item_id)
        if item is None or field not in ITEM_CELLS:
            return JSONResponse({"error": "This cell cannot be edited here."}, 404)
        try:
            if field == "customer":
                code = str(raw or "").strip()
                customer = db.scalar(select(Customer).where(func.upper(Customer.code) == code.upper())) if code else None
                if code and customer is None:
                    return JSONResponse({"error": f"No customer with code {code}. Add the customer first."}, 422)
                values = {"customer_id": customer.id if customer else None}
            elif field == "wholesale":
                values = {"wholesale_unit_price": item_service.parse_money(raw, "Wholesale price", optional=True)}
            elif field == "markup":
                values = {"markup_percentage": item_service.parse_percent(raw)}
            else:
                values = {field: raw}
            revision = body.get("revision")
            item_service.change_item(db, item, values, actor="web user", channel="grid cell",
                                     expected_revision=int(revision) if str(revision).isdigit() else None,
                                     allow_operational_changes=False)
            db.commit()
        except item_service.ItemRuleError as exc:
            db.rollback()
            return JSONResponse({"error": str(exc)}, 409 if isinstance(exc, item_service.StaleItemError) else 422)
        except StaleDataError:
            db.rollback()
            return JSONResponse({"error": "This product changed meanwhile. Reload to see the newer values."}, 409)
        db.refresh(item)
        shown = {"customer": item.customer.code if item.customer else "", "wholesale": f"{item.wholesale_unit_price:.2f}" if item.wholesale_unit_price is not None else "",
                 "markup": f"{item.markup_percentage * 100:.0f}%" if item.markup_percentage is not None else ""}
        return {"value": shown.get(field, getattr(item, field, "") or ""), "revision": item.revision}
