"""Compare a newly uploaded factory workbook with the orders already in the ledger, then merge it.

``compare`` matches every allocation in the file with the stored allocations of the same season and
brand by (article, colour, customer, size) and reports:

* new — in the file, not in the ledger (new rows, or more pieces than before);
* changed — same allocation, different factory price or quantity;
* cancelled — the file marks it cancelled, the ledger does not;
* missing — in the ledger, no longer in the file (asked about, never cancelled automatically);
* duplicates — the same allocation on two rows of the file;
* unresolved — rows whose allocation text could not be split into customers;
* unknown customers — tokens in the file that are no known customer code or alias;
* new orders — brands the season does not have yet.

``apply`` writes only what was shown: price/quantity/cancel changes go through ``ledger_actions``
(audited, undoable), new allocations through ``factory_import.commit_import`` restricted to the new
units. Factory terms of existing orders are left as they are (changing them is a separate, explicit step).
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.calculations import money
from app.factory_import import Analysis, ItemRow
from app.models import Brand, Customer, CustomerAlias, Order, OrderItem, Season


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def known_customers(db: Session) -> dict[str, str]:
    """Upper-case code or alias → customer code."""
    known = {c.code.upper(): c.code for c in db.scalars(select(Customer))}
    for alias in db.scalars(select(CustomerAlias)):
        known.setdefault(alias.alias.upper(), alias.customer.code)
    return known


def _file_units(analysis: Analysis, known: dict[str, str], token_map: dict[str, str] | None = None):
    """Per (brand, key) → {"qty", "rows", "price", "cancelled", "item"} from the file."""
    token_map = {k.upper(): v for k, v in (token_map or {}).items()}
    units: dict[tuple, dict] = {}
    unresolved: list[ItemRow] = []
    unknown: set[str] = set()
    for sheet in analysis.sheets:
        if sheet.kind != "order":
            continue
        for item in sheet.items:
            if not item.confident or not item.units or not all(u.get("customer") for u in item.units):
                unresolved.append(item)
                continue
            for unit in item.units:
                token = unit["customer"].upper()
                code = known.get(token) or (token_map.get(token) if token_map.get(token) not in (None, "", "new", "skip") else None)
                if code is None and token_map.get(token) == "new":
                    code = token
                if code is None:
                    unknown.add(token)
                    code = f"?{token}"
                key = (sheet.brand.casefold(), _norm(item.article or item.name), _norm(item.color), code, _norm(unit.get("size")))
                entry = units.setdefault(key, {"qty": 0, "rows": set(), "price": item.unit_price, "cancelled": False,
                                               "item": item, "brand": sheet.brand, "customer": code, "size": unit.get("size")})
                entry["qty"] += 1
                entry["rows"].add((sheet.name, item.row))
                entry["cancelled"] = entry["cancelled"] or item.status_hint == "Cancelled"
    return units, unresolved, unknown


def _ledger_units(db: Session, season: Season | None, brands: set[str]):
    units: dict[tuple, dict] = {}
    orders: dict[str, Order] = {}
    if season is None:
        return units, orders
    for order in db.scalars(select(Order).join(Brand).where(Order.season_id == season.id)):
        if order.brand.name.casefold() not in brands:
            continue
        orders[order.brand.name.casefold()] = order
        for item in order.items:
            code = item.customer.code if item.customer else "—"
            key = (order.brand.name.casefold(), _norm(item.article or item.name), _norm(item.color), code, _norm(item.size))
            entry = units.setdefault(key, {"qty": 0, "items": [], "cancelled": True, "price": None})
            entry["items"].append(item)
            if item.status != "Cancelled":
                entry["qty"] += item.quantity
                entry["cancelled"] = False
                entry["price"] = item.wholesale_unit_price
    return units, orders


def _label(entry_item: ItemRow | OrderItem, customer: str, size) -> str:
    name = getattr(entry_item, "name", None) or getattr(entry_item, "article", None) or "позиция"
    return f"{name} · {getattr(entry_item, 'article', None) or ''} · {customer} · р. {size or '—'}".replace(" ·  ·", " ·")


def compare(db: Session, analysis: Analysis, season_name: str | None, token_map: dict | None = None) -> dict:
    season_name = (season_name or analysis.season or "").upper() or None
    season = db.scalar(select(Season).where(Season.name == season_name)) if season_name else None
    known = known_customers(db)
    file_units, unresolved, unknown = _file_units(analysis, known, token_map)
    brands = {s.brand.casefold() for s in analysis.sheets if s.kind == "order"}
    ledger, orders = _ledger_units(db, season, brands)
    new, changed, cancelled, missing, duplicates = [], [], [], [], []
    for key, f in file_units.items():
        if len(f["rows"]) > 1:
            duplicates.append({"label": _label(f["item"], f["customer"], f["size"]),
                               "rows": sorted(f"{s} строка {r}" for s, r in f["rows"])})
        if key[0] not in orders:
            continue                      # whole order is new; counted below
        stored = ledger.get(key)
        row_ref = ", ".join(sorted(f"{s.strip()} стр. {r}" for s, r in f["rows"]))
        if stored is None or stored["cancelled"]:
            if not f["cancelled"]:
                new.append({"label": _label(f["item"], f["customer"], f["size"]), "quantity": f["qty"], "rows": row_ref,
                            "price": f["price"], "key": list(key), "sheet_rows": sorted([s, r] for s, r in f["rows"])})
            continue
        if f["cancelled"]:
            cancelled.append({"label": _label(f["item"], f["customer"], f["size"]), "rows": row_ref,
                              "item_ids": [i.id for i in stored["items"] if i.status != "Cancelled"]})
            continue
        changes = []
        if f["price"] is not None and stored["price"] is not None and money(Decimal(f["price"])) != money(stored["price"]):
            changes.append({"field": "price", "before": str(money(stored["price"])), "after": str(money(Decimal(f["price"])))})
        if f["qty"] != stored["qty"]:
            changes.append({"field": "quantity", "before": stored["qty"], "after": f["qty"]})
        if changes:
            changed.append({"label": _label(f["item"], f["customer"], f["size"]), "rows": row_ref, "changes": changes,
                            "item_ids": [i.id for i in stored["items"] if i.status != "Cancelled"]})
    unresolved_keys = {(s.brand.casefold(), _norm(i.article or i.name)) for s in analysis.sheets for i in s.items if i in unresolved}
    for key, stored in ledger.items():
        if stored["cancelled"] or key in file_units:
            continue
        if (key[0], key[1]) in unresolved_keys:
            continue                      # the row is still in the file, only its allocation is unclear
        item = stored["items"][0]
        missing.append({"label": _label(item, key[3], item.size), "item_ids": [i.id for i in stored["items"] if i.status != "Cancelled"],
                        "quantity": stored["qty"]})
    stored_articles = {(k[0], k[1]) for k in ledger}
    unresolved_new = [f"{s.name}:{i.row}" for s in analysis.sheets for i in s.items
                      if i in unresolved and s.brand.casefold() in orders and (s.brand.casefold(), _norm(i.article or i.name)) not in stored_articles]
    new_orders = []
    for sheet in analysis.sheets:
        if sheet.kind == "order" and sheet.brand.casefold() not in orders:
            new_orders.append({"brand": sheet.brand, "sheet": sheet.name, "items": len(sheet.items),
                               "base": str(money(sum((i.base for i in sheet.items), Decimal(0))))})
    return {"season": season_name, "season_exists": season is not None, "new": new, "changed": changed, "cancelled": cancelled,
            "missing": missing, "duplicates": duplicates, "unknown_customers": sorted(unknown),
            "unresolved": [{"sheet": i.sheet, "row": i.row, "label": i.name or i.article or "—", "text": i.allocation_text,
                            "issue": i.issue} for i in unresolved],
            "unresolved_new": unresolved_new, "new_orders": new_orders, "existing_orders": sorted(o.brand.name for o in orders.values()),
            "warnings": list(analysis.warnings)}


def summary_lines(diff: dict) -> list[str]:
    lines = []
    if diff["new_orders"]:
        lines.append("Новые заказы: " + ", ".join(f"{o['brand']} ({o['items']} поз.)" for o in diff["new_orders"]))
    for title, key in (("Новые позиции", "new"), ("Изменились", "changed"), ("Отменены в файле", "cancelled"),
                       ("Нет в новом файле", "missing"), ("Дубли в файле", "duplicates"), ("Без клиента / неясно", "unresolved")):
        if diff[key]:
            lines.append(f"{title}: {len(diff[key])}")
    if diff["unknown_customers"]:
        lines.append("Незнакомые обозначения клиентов: " + ", ".join(diff["unknown_customers"]))
    return lines


def update_actions(diff: dict, filename: str, cancel_missing: bool = False) -> list[dict]:
    """Ledger actions for changed / cancelled (and, if decided, missing) allocations."""
    actions = []
    for row in diff["changed"]:
        for change in row["changes"]:
            if change["field"] == "price":
                actions += [{"action": "update_item", "params": {"item_id": i, "wholesale_unit_price": change["after"]}} for i in row["item_ids"]]
            elif change["field"] == "quantity" and row["item_ids"]:
                first, rest = row["item_ids"][0], row["item_ids"][1:]
                remaining = int(change["after"])
                if rest:      # several stored pieces: keep the first, fold the quantity into it, cancel the others
                    actions.append({"action": "set_status", "params": {"item_ids": rest, "status": "Cancelled",
                                                                         "reason": f"Количество по файлу {filename}"}})
                actions.append({"action": "update_item", "params": {"item_id": first, "quantity": remaining}})
    ids = [i for row in diff["cancelled"] for i in row["item_ids"]]
    if ids:
        actions.append({"action": "set_status", "params": {"item_ids": ids, "status": "Cancelled", "reason": f"Отменено в файле {filename}"}})
    if cancel_missing:
        ids = [i for row in diff["missing"] for i in row["item_ids"]]
        if ids:
            actions.append({"action": "set_status", "params": {"item_ids": ids, "status": "Cancelled", "reason": f"Нет в файле {filename}"}})
    return actions


def import_decisions(diff: dict, analysis: Analysis, create_unknown: bool) -> dict:
    """``commit_import`` decisions that create only the new allocations (and whole new orders)."""
    token_map = {t: ("new" if create_unknown else "skip") for t in diff["unknown_customers"]}
    row_units: dict[str, list[dict]] = defaultdict(list)
    for row in diff["new"]:
        customer = row["key"][3]
        if customer.startswith("?") and not create_unknown:
            continue
        sheet, number = row["sheet_rows"][0]
        item = next(i for s in analysis.sheets if s.name == sheet for i in s.items if i.row == number)
        size = next((u.get("size") for u in item.units if _norm(u.get("size")) == row["key"][4]), None)
        row_units[f"{sheet}:{number}"] += [{"customer": customer.lstrip("?"), "size": size}] * int(row["quantity"])
    new_sheets = {o["sheet"] for o in diff["new_orders"]}
    only_rows = set(row_units) | set(diff.get("unresolved_new") or []) | \
        {f"{s.name}:{i.row}" for s in analysis.sheets if s.name in new_sheets for i in s.items}
    return {"season": diff["season"], "token_map": token_map, "row_units": dict(row_units), "only_rows": sorted(only_rows),
            "keep_existing_terms": True, "rates_confirmed": False}


def apply(db: Session, analysis: Analysis, original, diff: dict, *, create_unknown: bool = False,
          cancel_missing: bool = False, actor: str = "assistant") -> dict:
    from app import factory_import, ledger_actions as la
    from app.models import ImportBatch
    if db.scalar(select(ImportBatch).where(ImportBatch.sha256 == analysis.sha256)) is not None:
        return {"duplicate": True, "messages": ["Этот файл уже был загружен раньше — ничего нового."]}
    messages = []
    actions = update_actions(diff, analysis.filename, cancel_missing)
    if actions:
        messages += la.commit_actions(db, actions, actor, f"Обновление по файлу {analysis.filename}"[:300])
    decisions = import_decisions(diff, analysis, create_unknown)
    result = factory_import.commit_import(db, analysis, original, decisions, actor=actor)
    messages.append(f"Добавлено строк: {result.get('created', 0)}; на проверку: {result.get('review', 0)}.")
    return {"duplicate": False, "messages": messages, **result}
