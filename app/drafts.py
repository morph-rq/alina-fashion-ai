"""Persistent conversational drafts (IMP-04/05/06/08/09).

A session holds operator messages, captured sources, draft rows and questions.
Every draft field records its value, state, origin, evidence and history:

  explicit    – read from evidence (or matched exactly to an existing record)
  defaulted   – an approved default, visible to the operator (e.g. EUR)
  suggested   – a candidate the operator must choose or accept
  missing     – required and unknown
  conflicting – sources disagree; candidates are kept
  confirmed   – stated or chosen by the operator; extraction never overrides it

Questions are derived from blocking issues, so an answered fact is never asked
again and optional facts never block. Saving goes through ``commit_session``,
which is idempotent per operation key and refuses a draft that changed after
review. Money is computed only by the Decimal engine at save time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import re
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import item_service
from app.audit import record_change
from app.calculations import ZERO, final_unit_price, money
from app.config import settings
from app.customers import match_customers, new_customer_code
from app.models import (
    Brand, BrandSeasonRule, Customer, DraftQuestion, DraftRow, IntakeMessage, IntakeSession, OperationReceipt, Order,
    OrderItem, Payment, PaymentAllocation, Season, SourceItem,
)
from app.pricing import parse_amount_text, wholesale_from_final

STATES = ("explicit", "defaulted", "suggested", "missing", "conflicting", "confirmed")
RESOLVED_STATES = {"explicit", "defaulted", "confirmed"}
ONE_SIZE = "One size"
ITEM_FIELDS = ("customer", "order", "article", "name", "color", "size", "quantity", "price", "price_meaning",
               "price_basis", "currency", "requested_delivery_date", "logistics_preference", "customer_requirements", "image")
PAYMENT_FIELDS = ("customer", "amount", "currency", "payment_date", "payment_type", "stage", "note")
FIELD_LABELS = {"customer": "Customer", "order": "Brand table", "article": "Article / SKU", "name": "Product", "color": "Color",
                "size": "Size", "quantity": "Quantity", "price": "Price", "price_meaning": "Price meaning",
                "price_basis": "Price per", "currency": "Currency", "amount": "Amount", "payment_date": "Date",
                "payment_type": "Payment or refund", "stage": "Stage", "note": "Note", "image": "Image",
                "requested_delivery_date": "Requested delivery", "logistics_preference": "Logistics",
                "customer_requirements": "Customer requirements"}
DEFAULT_TERMS = {"initial": "0.50", "ready": "0.50", "transport": "0.05", "exempt": "CH", "receipt_days": 14}


class DraftError(ValueError):
    pass


class StaleDraftError(DraftError):
    pass


@dataclass
class Issue:
    row_id: int | None
    field: str
    kind: str  # missing | ambiguous | conflict | invalid | confirm
    text: str
    options: list[dict] = field(default_factory=list)
    blocking: bool = True

    @property
    def key(self) -> str:
        return f"{self.row_id}:{self.field}:{self.kind}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Field storage -----------------------------------------------------------------

def fields_of(row: DraftRow) -> dict[str, dict]:
    return json.loads(row.fields_json or "{}")


def save_fields(row: DraftRow, data: dict[str, dict]) -> None:
    row.fields_json = json.dumps(data, ensure_ascii=False, default=str)


def value_of(row: DraftRow, name: str, data: dict | None = None):
    entry = (data if data is not None else fields_of(row)).get(name)
    return entry.get("value") if entry else None


def state_of(row: DraftRow, name: str, data: dict | None = None) -> str:
    entry = (data if data is not None else fields_of(row)).get(name)
    return entry.get("state", "missing") if entry else "missing"


def set_field(data: dict, name: str, value, state: str, origin: str, *, evidence: list | None = None,
              raw: str | None = None, candidates: list | None = None) -> str:
    """Record a field value; returns the resulting state.

    An operator-confirmed value is never replaced by extraction: a differing
    extracted value is kept as an alternative for the record. Two explicit
    readings that disagree become a conflict that keeps both candidates.
    """
    if state not in STATES:
        raise DraftError(f"Unknown field state {state}")
    entry = data.get(name) or {"history": [], "evidence": []}
    previous = {key: entry.get(key) for key in ("value", "state", "origin")}
    extraction = origin.startswith(("extraction", "match", "default"))
    if evidence:
        entry.setdefault("evidence", []).extend(item for item in evidence if item not in entry.get("evidence", []))
    if entry.get("state") == "confirmed" and extraction:
        if value is not None and value != entry.get("value"):
            entry.setdefault("alternatives", []).append({"value": value, "origin": origin, "at": utcnow().isoformat()})
        data[name] = entry
        return "confirmed"
    if (extraction and state == "explicit" and entry.get("state") == "explicit" and entry.get("value") is not None
            and value is not None and value != entry.get("value") and entry.get("origin", "").startswith("extraction")):
        options = entry.get("candidates") or [{"value": entry["value"], "origin": entry.get("origin")}]
        if all(option["value"] != value for option in options):
            options.append({"value": value, "origin": origin})
        entry.update(candidates=options, state="conflicting", origin="conflict")
        entry.setdefault("history", []).append(previous | {"at": utcnow().isoformat()})
        data[name] = entry
        return "conflicting"
    if previous.get("value") is not None or previous.get("state"):
        entry.setdefault("history", []).append(previous | {"at": utcnow().isoformat()})
    entry.update(value=value, state=state, origin=origin)
    if raw is not None:
        entry["raw"] = raw
    if candidates is not None:
        entry["candidates"] = candidates
    elif state in RESOLVED_STATES:
        entry.pop("candidates", None)
    data[name] = entry
    return state


# --- Sessions ----------------------------------------------------------------------

def new_session(db: Session, season: Season | None, title: str = "New intake", action: str = "add_goods") -> IntakeSession:
    session = IntakeSession(public_id=secrets.token_hex(16), title=title[:200] or "New intake",
                            season_id=season.id if season else None, action=action)
    db.add(session)
    db.flush()
    return session


def get_session(db: Session, public_id: str) -> IntakeSession:
    session = db.scalar(select(IntakeSession).where(IntakeSession.public_id == public_id).options(
        selectinload(IntakeSession.rows), selectinload(IntakeSession.messages), selectinload(IntakeSession.questions),
        selectinload(IntakeSession.sources)))
    if session is None:
        raise DraftError("Intake session not found.")
    return session


def touch(session: IntakeSession) -> None:
    session.revision = (session.revision or 0) + 1
    session.updated_at = utcnow()


def add_message(db: Session, session: IntakeSession, role: str, text: str, source_ids: list[int] | None = None) -> IntakeMessage:
    message = IntakeMessage(session_id=session.id, role=role, text=text[:20000], source_item_ids=json.dumps(source_ids or []))
    db.add(message)
    session.messages.append(message)
    db.flush()
    return message


def _next_position(session: IntakeSession) -> int:
    return max((row.position for row in session.rows), default=0) + 1


def add_row(db: Session, session: IntakeSession, kind: str = "new_item", data: dict | None = None,
            target: OrderItem | None = None) -> DraftRow:
    row = DraftRow(session_id=session.id, position=_next_position(session), kind=kind,
                   target_item_id=target.id if target else None, target_revision=target.revision if target else None)
    save_fields(row, data or {})
    db.add(row)
    session.rows.append(row)
    db.flush()
    return row


def active_rows(session: IntakeSession) -> list[DraftRow]:
    return [row for row in session.rows if row.status == "draft"]


# --- Resolution of references against existing records ------------------------------

def _norm(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def season_orders(db: Session, session: IntakeSession) -> list[Order]:
    if not session.season_id:
        return []
    return list(db.scalars(select(Order).where(Order.season_id == session.season_id)
                           .options(selectinload(Order.brand)).order_by(Order.id)).all())


def resolve_customer(db: Session, data: dict, raw_name: str | None, raw_code: str | None, origin: str, evidence: list) -> None:
    if not raw_name and not raw_code:
        return
    exact, candidates = match_customers(db, raw_name, raw_code)
    raw = " / ".join(filter(None, [raw_name, raw_code]))
    if exact is not None:
        set_field(data, "customer", {"id": exact.id}, "explicit", "match:" + origin, evidence=evidence, raw=raw)
        return
    options = [{"value": {"id": c.id}, "label": f"{c.code} · {c.name or ''} ({c.reason})"} for c in candidates]
    if raw_name:
        options.append({"value": {"new_name": raw_name.strip()}, "label": f"Create new customer “{raw_name.strip()}”"})
    set_field(data, "customer", None, "suggested", origin, evidence=evidence, raw=raw, candidates=options)


def _brand_preset(db: Session, brand_name: str, season_id: int | None) -> tuple[dict | None, str | None]:
    """Terms from the same brand's most recent rule in another season (an approved preset)."""
    brand = db.scalar(select(Brand).where(func.lower(Brand.name) == brand_name.strip().casefold()))
    if brand is None:
        return None, None
    rule = db.scalar(select(BrandSeasonRule).where(BrandSeasonRule.brand_id == brand.id, BrandSeasonRule.season_id != season_id)
                     .order_by(BrandSeasonRule.season_id.desc()).limit(1))
    if rule is None:
        return None, None
    terms = {"markup": str(rule.markup_percentage), "initial": str(rule.initial_prepayment_percentage),
             "ready": str(rule.ready_balance_percentage), "transport": str(rule.transport_percentage),
             "exempt": rule.transport_exempt_country_codes, "receipt_days": rule.expected_receipt_days}
    return terms, f"copied from {brand.name} {rule.season.name}"


def resolve_order(db: Session, session: IntakeSession, data: dict, brand_text: str | None, origin: str, evidence: list) -> None:
    orders = season_orders(db, session)
    if brand_text:
        query = _norm(brand_text)
        exact = [order for order in orders if _norm(order.brand.name) == query]
        if len(exact) == 1:
            set_field(data, "order", {"id": exact[0].id}, "explicit", "match:" + origin, evidence=evidence, raw=brand_text)
            return
        close = [order for order in orders if query and (query in _norm(order.brand.name) or _norm(order.brand.name) in query)]
        options = [{"value": {"id": order.id}, "label": order.brand.name} for order in (exact or close)]
        terms, source = _brand_preset(db, brand_text, session.season_id)
        new_value = {"new_brand": brand_text.strip(), **({"terms": terms, "terms_source": source} if terms else {})}
        options.append({"value": new_value, "label": f"Create a new “{brand_text.strip()}” table in this season"
                        + (f" (terms {source})" if source else " (you will be asked for its terms)")})
        set_field(data, "order", None, "suggested", origin, evidence=evidence, raw=brand_text, candidates=options)
    elif len(orders) == 1:
        set_field(data, "order", {"id": orders[0].id}, "suggested", "match:only table in season", candidates=[
            {"value": {"id": orders[0].id}, "label": f"{orders[0].brand.name} (the only table in this season)"}])


# --- Merging extracted evidence -----------------------------------------------------

def _row_identity(db: Session, row: DraftRow) -> tuple:
    data = fields_of(row)
    customer = value_of(row, "customer", data) or {}
    raw_customer = (data.get("customer") or {}).get("raw")
    return (customer.get("id") or _norm(raw_customer), _norm(value_of(row, "article", data)) or _norm(value_of(row, "name", data)),
            _norm(value_of(row, "color", data)), _norm(value_of(row, "size", data)))


def merge_extraction(db: Session, session: IntakeSession, bundle, sources_by_name: dict[str, int], fallback_sources: list[int], *, merge_existing: bool = True) -> list[DraftRow]:
    """Turn an AI extraction bundle into draft rows, reinforcing matching rows instead of duplicating them."""
    touched = []
    for facts in bundle.orders:
        source_ids = [sources_by_name[name] for name in facts.source_attachment_names if name in sources_by_name] or fallback_sources
        origin = "extraction:" + ",".join(str(i) for i in source_ids)
        evidence = [{"source_item_id": sid, "quote": quote[:200]} for sid in source_ids[:1] for quote in facts.evidence_quotes] \
            or [{"source_item_id": sid} for sid in source_ids]
        data: dict[str, dict] = {}
        resolve_customer(db, data, facts.customer_name, facts.customer_code, origin, evidence)
        resolve_order(db, session, data, facts.brand, origin, evidence)
        for name, value in (("article", facts.article), ("name", facts.product_name), ("color", facts.color)):
            if value:
                set_field(data, name, value.strip(), "explicit", origin, evidence=evidence, raw=value)
        if facts.explicit_size:
            set_field(data, "size", facts.explicit_size.strip(), "explicit", origin, evidence=evidence, raw=facts.explicit_size)
        set_field(data, "quantity", facts.quantity, "explicit" if facts.quantity_explicit else "defaulted",
                  origin if facts.quantity_explicit else "default:quantity not stated", evidence=evidence)
        if facts.stated_price:
            reading = parse_amount_text(facts.stated_price)
            if reading.amount is not None:
                set_field(data, "price", str(reading.amount), "explicit", origin, evidence=evidence, raw=facts.stated_price)
            else:
                set_field(data, "price", None, "missing", origin, evidence=evidence, raw=facts.stated_price)
                data["price"]["problem"] = reading.problem
            currency = (facts.price_currency or reading.currency or "").upper() or None
            if currency:
                set_field(data, "currency", currency, "explicit", origin, evidence=evidence)
        if facts.price_type in {"wholesale", "final"}:
            set_field(data, "price_meaning", facts.price_type, "explicit", origin, evidence=evidence)
        if facts.price_basis in {"unit", "line"}:
            set_field(data, "price_basis", facts.price_basis, "explicit", origin, evidence=evidence)
        if facts.requested_delivery_date:
            set_field(data, "requested_delivery_date", facts.requested_delivery_date, "explicit", origin, evidence=evidence)
        if facts.logistics_preference:
            set_field(data, "logistics_preference", facts.logistics_preference, "explicit", origin, evidence=evidence)
        images = [sid for sid in source_ids if any(s.id == sid and s.media_type.startswith("image/") for s in session.sources)]
        if images:
            set_field(data, "image", images[0], "suggested", origin, evidence=evidence)
        candidate = DraftRow(fields_json=json.dumps(data, default=str))
        identity = _row_identity(db, candidate)
        existing = next((row for row in active_rows(session) if row.kind == "new_item" and _row_identity(db, row) == identity
                         and any(identity)), None) if merge_existing else None
        if existing is None:
            touched.append(add_row(db, session, "new_item", data))
        else:
            merged = fields_of(existing)
            for name, entry in data.items():
                set_field(merged, name, entry.get("value"), entry.get("state", "explicit"), entry.get("origin", origin),
                          evidence=entry.get("evidence"), raw=entry.get("raw"), candidates=entry.get("candidates"))
            save_fields(existing, merged)
            touched.append(existing)
    touch(session)
    return touched


# --- Operator edits and answers ----------------------------------------------------

ONE_SIZE_WORDS = {"one size", "onesize", "os", "o/s", "n/a", "no size", "единый", "безразмерный", "taglia unica", "tu"}


def parse_operator_value(db: Session, session: IntakeSession, name: str, raw: Any):
    """Validate and normalise an operator-entered value for a draft field."""
    text = raw.strip() if isinstance(raw, str) else raw
    if isinstance(text, dict):
        return text
    if name == "customer":
        if not text:
            return None
        exact, _candidates = match_customers(db, text, text)
        return {"id": exact.id} if exact else {"new_name": text}
    if name == "order":
        if not text:
            return None
        orders = season_orders(db, session)
        exact = [order for order in orders if _norm(order.brand.name) == _norm(text)]
        if len(exact) == 1:
            return {"id": exact[0].id}
        terms, source = _brand_preset(db, text, session.season_id)
        return {"new_brand": text, **({"terms": terms, "terms_source": source} if terms else {})}
    if name == "quantity":
        return item_service.parse_quantity(text)
    if name in {"price", "amount"}:
        reading = parse_amount_text(str(text))
        if reading.amount is None:
            raise DraftError(reading.problem or "Enter an amount such as 1200 or 1200.50.")
        return str(reading.amount)
    if name == "price_meaning":
        lowered = str(text).casefold()
        if any(word in lowered for word in ("whole", "factory", "опт", "cost")):
            return "wholesale"
        if any(word in lowered for word in ("final", "customer", "retail", "selling", "клиент")):
            return "final"
        raise DraftError("Choose wholesale (factory) or final (customer) price.")
    if name == "price_basis":
        lowered = str(text).casefold()
        if lowered in {"unit", "per unit", "piece", "per piece", "each"}:
            return "unit"
        if lowered in {"line", "total", "for all", "whole line"}:
            return "line"
        raise DraftError("Choose per unit or for the whole line.")
    if name == "currency":
        code = str(text).upper().strip()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise DraftError("Enter a three-letter currency code such as EUR.")
        return code
    if name == "size":
        if not text:
            raise DraftError("Enter the size exactly as stated, or One size if the product has none.")
        return ONE_SIZE if str(text).casefold() in ONE_SIZE_WORDS else str(text)
    if name in {"requested_delivery_date", "payment_date"}:
        return item_service.parse_optional_date(text, FIELD_LABELS[name]).isoformat() if text else None
    if name == "payment_type":
        lowered = str(text).casefold()
        if "refund" in lowered or "return" in lowered or "возврат" in lowered:
            return "refund"
        if lowered in {"payment", "paid", "receipt"} or "pay" in lowered:
            return "payment"
        raise DraftError("Choose payment or refund.")
    if name == "stage":
        if text not in {"initial", "ready", "unallocated"}:
            raise DraftError("Choose initial, ready or unallocated.")
        return text
    if name == "markup":
        return str(item_service.parse_percent(text))
    if name == "image":
        return int(text) if str(text).isdigit() else None
    return item_service.clean_text(text, {"name": "name", "article": "article", "color": "color"}.get(name, name)) if text else None


def edit_field(db: Session, session: IntakeSession, row: DraftRow, name: str, raw: Any, *, expected_revision: int | None = None,
               origin: str = "operator") -> DraftRow:
    if row.status != "draft":
        raise DraftError("This row is already saved. Start a correction instead.")
    if expected_revision is not None and row.revision != expected_revision:
        raise StaleDraftError("This draft row changed in another window. Reload to see the newer values.")
    allowed = PAYMENT_FIELDS if row.kind == "payment" else ITEM_FIELDS + ("markup",)
    if name not in allowed:
        raise DraftError(f"{name} cannot be edited on this row.")
    data = fields_of(row)
    if name == "markup":
        order = value_of(row, "order", data) or {}
        if not order.get("new_brand"):
            raise DraftError("Markup is set on a new brand table only; existing tables use their rule.")
        order = dict(order, terms=dict(order.get("terms") or DEFAULT_TERMS, markup=parse_operator_value(db, session, "markup", raw)),
                     terms_source="operator")
        set_field(data, "order", order, "confirmed", origin)
    else:
        value = parse_operator_value(db, session, name, raw)
        set_field(data, name, value, "confirmed" if value is not None else "missing", origin)
    save_fields(row, data)
    touch(session)
    db.flush()
    return row


def answer_question(db: Session, session: IntakeSession, question: DraftQuestion, *, choice: int | None = None,
                    text: str | None = None) -> None:
    if question.status != "open":
        return  # Answering twice (double click) changes nothing.
    options = json.loads(question.options_json or "[]")
    if choice is not None:
        if not 0 <= choice < len(options):
            raise DraftError("Choose one of the listed answers.")
        value = options[choice]["value"]
    elif text is not None and text.strip():
        value = text.strip()
    else:
        raise DraftError("Choose an answer or type one.")
    if question.field == "target_row" and isinstance(value, dict) and "patch" in value:
        target = next(row for row in session.rows if row.id == value["row_id"])
        for name, patch_value in value["patch"].items():
            edit_field(db, session, target, name, patch_value, origin="operator message")
    elif question.field == "target_item" and isinstance(value, dict):
        _create_correction_row(db, session, db.get(OrderItem, value["item_id"]), value["field"], value["new_value"])
    else:
        row = next(row for row in session.rows if row.id == question.draft_row_id)
        if question.field == "save_incomplete":
            data = fields_of(row)
            data["_incomplete_ack"] = {"value": True, "state": "confirmed", "origin": "operator"}
            save_fields(row, data)
        else:
            edit_field(db, session, row, question.field, value, origin="operator answer")
    question.status = "answered"
    question.answer_json = json.dumps({"choice": choice, "text": text, "value": value}, ensure_ascii=False, default=str)
    question.answered_at = utcnow()
    touch(session)
    db.flush()


# --- Messages from the operator ----------------------------------------------------

_SIZE = re.compile(r"\bsize\s*[:=]?\s*([A-Za-z0-9./]+)|\bразмер\s*([A-Za-z0-9./]+)", re.IGNORECASE)
_QTY = re.compile(r"\b(\d{1,4})\s*(?:pcs|pc|pieces?|units?|шт)\b|\b(?:qty|quantity)\s*[:=]?\s*(\d{1,4})\b|\b(one|two|three) pieces?\b", re.IGNORECASE)
_WORDS = {"one": 1, "two": 2, "three": 3}
_CORRECTION = re.compile(r"(?:change|correct|fix|update)\s+(?P<target>.+?)\s+from\s+(?:size\s+)?(?P<old>[\w./-]+)\s+to\s+(?:size\s+)?(?P<new>[\w./-]+)", re.IGNORECASE)
_ROW_REF = re.compile(r"(?:row|#)\s*(\d{1,3})\b", re.IGNORECASE)


def _message_patch(text: str) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    size = _SIZE.search(text)
    if size:
        patch["size"] = size.group(1) or size.group(2)
    elif re.search(r"\bone size\b", text, re.IGNORECASE):
        patch["size"] = ONE_SIZE
    quantity = _QTY.search(text)
    if quantity:
        patch["quantity"] = quantity.group(1) or quantity.group(2) or _WORDS[quantity.group(3).casefold()]
    lowered = text.casefold()
    if re.search(r"\bwholesale\b|\bfactory price\b|\bоптов", lowered):
        patch["price_meaning"] = "wholesale"
    elif re.search(r"\bfinal (?:customer )?price\b|\bcustomer price\b|\bretail\b", lowered):
        patch["price_meaning"] = "final"
    if re.search(r"\bper (?:unit|piece)\b|\beach\b", lowered):
        patch["price_basis"] = "unit"
    elif re.search(r"\bfor all\b|\bin total\b|\bline total\b", lowered):
        patch["price_basis"] = "line"
    if re.search(r"\beuros?\b|\beur\b|€", lowered):
        patch["currency"] = "EUR"
    return patch


def _create_correction_row(db: Session, session: IntakeSession, item: OrderItem, name: str, new_value: str) -> DraftRow:
    data: dict[str, dict] = {}
    value = parse_operator_value(db, session, name, new_value)
    set_field(data, name, value, "confirmed", "operator message")
    data[name]["before"] = getattr(item, {"product_name": "name"}.get(name, name))
    return add_row(db, session, "update_item", data, target=item)


def find_existing_goods(db: Session, session: IntakeSession, target_text: str, field_name: str, old_value: str) -> list[OrderItem]:
    query = select(OrderItem).join(Order).where(OrderItem.status != "Cancelled").options(
        selectinload(OrderItem.customer), selectinload(OrderItem.order).selectinload(Order.brand))
    if session.season_id:
        query = query.where(Order.season_id == session.season_id)
    words = set(_norm(re.sub(r"'s\b", "", target_text)).split())
    matches = []
    for item in db.scalars(query).all():
        current = getattr(item, field_name)
        if _norm(str(current)) != _norm(old_value):
            continue
        haystack = set(_norm(" ".join(filter(None, [item.name, item.article, item.color, item.order.brand.name,
                                                    item.customer.name if item.customer else "", item.customer.code if item.customer else ""]))).split())
        if words and len(words & haystack) >= min(2, len(words)):
            matches.append(item)
    return matches


_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_DOTTED = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_PAYMENT = re.compile(r"\b(refund(?:ed)?|returned money|payment|paid|transfer(?:red)?|received)\b|возврат|оплат", re.IGNORECASE)
_AMOUNT_NEAR = re.compile(r"(?:€|eur|euro|usd|\$|chf|£)?\s*\d[\d\s.,']*\d(?:\s*(?:€|eur|euros?|usd|chf|£))?|\d", re.IGNORECASE)
LINKED_ACTIONS = [
    (re.compile(r"\bready\b|\bготов", re.IGNORECASE), "ready", "/ready-batches",
     "Ready goods are assigned with the Ready-list upload or on the Ready goods page, which matches available units and splits partial quantities without duplicating goods."),
    (re.compile(r"\bshipment|\bship\b|\bsped\b|отправ", re.IGNORECASE), "shipment", "/shipments",
     "Shipments are prepared on the Shipments page, which checks Ready assignment, season and existing shipment links before moving goods."),
    (re.compile(r"\bexcel\b|\bworkbook\b|\bstatement\b|\bpdf\b|\bexport", re.IGNORECASE), "export", "/reports",
     "Downloads are generated from saved records: the business workbook at /exports/business.xlsx, the editable workbook at /excel, and customer statements on Customer PDFs."),
    (re.compile(r"\bmarkup\b|\bcommission\b|\bterms\b|\bprepayment rule", re.IGNORECASE), "rule", "/rules",
     "Brand + Season terms are changed on Business rules, which shows the effect on existing balances before you confirm."),
]


def parse_date_text(text: str) -> tuple[str | None, str | None]:
    """ISO dates are clear; d.m.yyyy is used only when the day is above 12."""
    match = _DATE_ISO.search(text)
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat(), None
        except ValueError:
            return None, f"{match[0]} is not a valid date."
    match = _DATE_DOTTED.search(text)
    if match:
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        if first > 12 and second <= 12:
            return date(year, second, first).isoformat(), None
        if second > 12 and first <= 12:
            return date(year, first, second).isoformat(), None
        return None, f"{match[0]} could be day/month or month/day; enter the date as YYYY-MM-DD."
    return None, None


def start_payment_row(db: Session, session: IntakeSession, text: str) -> DraftRow:
    data: dict[str, dict] = {}
    words = re.sub(r"\b(?:record|payment|paid|refund(?:ed)?|of|for|from|on|the|a|an|received|transfer(?:red)?)\b", " ", text, flags=re.IGNORECASE)
    candidates = [customer for customer in db.scalars(select(Customer)).all()
                  if (customer.name and _norm(customer.name) in _norm(words)) or re.search(rf"\b{re.escape(customer.code)}\b", text)]
    if len(candidates) == 1:
        set_field(data, "customer", {"id": candidates[0].id}, "explicit", "operator message")
    else:
        set_field(data, "customer", None, "suggested" if candidates else "missing", "operator message",
                  candidates=[{"value": {"id": c.id}, "label": f"{c.code} · {c.name or ''}"} for c in candidates])
    amounts = [m.group(0) for m in _AMOUNT_NEAR.finditer(_DATE_ISO.sub(" ", _DATE_DOTTED.sub(" ", text)))
               if re.search(r"\d", m.group(0)) and len(re.sub(r"\D", "", m.group(0))) >= 2]
    if len(amounts) == 1:
        reading = parse_amount_text(amounts[0])
        if reading.amount is not None:
            set_field(data, "amount", str(reading.amount), "explicit", "operator message", raw=amounts[0])
            set_field(data, "currency", reading.currency or settings.currency, "explicit" if reading.currency else "defaulted", "operator message")
    iso, problem = parse_date_text(text)
    if iso:
        set_field(data, "payment_date", iso, "explicit", "operator message")
    elif problem:
        set_field(data, "payment_date", None, "missing", "operator message")
    is_refund = bool(re.search(r"refund|returned money|возврат", text, re.IGNORECASE))
    set_field(data, "payment_type", "refund" if is_refund else "payment", "explicit", "operator message")
    set_field(data, "stage", "unallocated", "defaulted", "default:allocate on the Payments page")
    set_field(data, "note", text[:500], "explicit", "operator message")
    return add_row(db, session, "payment", data)


def handle_operator_message(db: Session, session: IntakeSession, text: str, *, has_sources: bool = False) -> list[str]:
    """Apply what a follow-up message clearly says to the existing draft; return assistant notes."""
    notes: list[str] = []
    if re.search(r"what(?:'s| is) (?:still )?missing|что ещё нужно|what do you need", text, re.IGNORECASE):
        open_questions = [q.text for q in session.questions if q.status == "open"]
        return ["Still needed: " + " ".join(f"({i}) {t}" for i, t in enumerate(open_questions, 1))] if open_questions else ["Nothing is missing; review and save."]
    if _PAYMENT.search(text) and not has_sources and session.action in {"payment", "add_goods"} and not active_rows(session):
        row = start_payment_row(db, session, text)
        touch(session)
        return [f"Prepared a {value_of(row, 'payment_type')} draft. Check the customer, amount and date; possible duplicates are listed before saving."]
    for pattern, action, url, explanation in LINKED_ACTIONS:
        if pattern.search(text) and not has_sources and not active_rows(session) and not _CORRECTION.search(text):
            return [f"{explanation} Open {url}."]
    correction = _CORRECTION.search(text)
    if correction:
        field_name = "size" if re.search(r"\bsize\b", text, re.IGNORECASE) or len(correction.group("new")) <= 4 else "name"
        matches = find_existing_goods(db, session, correction.group("target"), field_name, correction.group("old"))
        draft_targets = [row for row in active_rows(session) if row.kind == "new_item"
                         and _norm(value_of(row, field_name) or "") == _norm(correction.group("old"))]
        if draft_targets and not matches:
            for row in draft_targets[:1]:
                edit_field(db, session, row, field_name, correction.group("new"), origin="operator message")
            notes.append(f"Updated the draft: {FIELD_LABELS[field_name].lower()} {correction.group('old')} → {correction.group('new')}.")
        elif len(matches) == 1:
            _create_correction_row(db, session, matches[0], field_name, correction.group("new"))
            notes.append(f"Prepared a correction for saved product #{matches[0].id}: {field_name} {correction.group('old')} → {correction.group('new')}. Review and save to apply it.")
        elif matches:
            db.add(DraftQuestion(session_id=session.id, field="target_item", key=f"msg:{secrets.token_hex(4)}:target_item",
                                 text=f"Several saved products match “{correction.group('target')}”. Which one should change?",
                                 options_json=json.dumps([{"value": {"item_id": item.id, "field": field_name, "new_value": correction.group("new")},
                                                           "label": f"#{item.id} {item.order.brand.name} {item.name or item.article} · {item.customer.code if item.customer else '-'} · {field_name} {getattr(item, field_name)}"}
                                                          for item in matches], ensure_ascii=False)))
            notes.append("Several saved products match; choose the right one below.")
        else:
            notes.append("No saved product in this season matches that description and value. Check the customer, product or current value.")
        touch(session)
        return notes
    patch = _message_patch(text)
    season = re.search(r"\b((?:SS|FW|AW|PF|RS)\d{2})\b", text, re.IGNORECASE)
    if season and session.season and season.group(1).upper() != session.season.name.upper():
        notes.append(f"This session is for {session.season.name}; the message mentions {season.group(1).upper()}. Start a session for that season if needed.")
    if not patch:
        return notes
    reference = _ROW_REF.search(text)
    rows = [row for row in active_rows(session) if row.kind == "new_item"]
    if reference:
        targets = [row for row in rows if row.position == int(reference.group(1))]
    elif len(rows) == 1:
        targets = rows
    else:
        unresolved = [row for row in rows if any(state_of(row, name) not in RESOLVED_STATES for name in patch)]
        targets = unresolved if len(unresolved) == 1 else []
    if not rows:
        if not has_sources:
            notes.append("There is no draft row yet. Add the goods first, or use Add row to enter them manually.")
        return notes
    if len(targets) != 1:
        description = ", ".join(f"{FIELD_LABELS[name].lower()} {value}" for name, value in patch.items())
        db.add(DraftQuestion(session_id=session.id, field="target_row", key=f"msg:{secrets.token_hex(4)}:target_row",
                             text=f"Which draft row does “{description}” apply to?",
                             options_json=json.dumps([{"value": {"row_id": row.id, "patch": patch}, "label": row_label(db, row)} for row in rows],
                                                     ensure_ascii=False, default=str)))
        notes.append("I need to know which row that applies to.")
        return notes
    for name, value in patch.items():
        edit_field(db, session, targets[0], name, value, origin="operator message")
    notes.append(f"Updated row {targets[0].position}: " + ", ".join(f"{FIELD_LABELS[name].lower()} {value}" for name, value in patch.items()) + ".")
    return notes


# --- Validation and questions ------------------------------------------------------

def row_label(db: Session, row: DraftRow) -> str:
    data = fields_of(row)
    customer = value_of(row, "customer", data) or {}
    who = (db.get(Customer, customer["id"]).code if customer.get("id") else customer.get("new_name")) or (data.get("customer") or {}).get("raw") or "?"
    what = value_of(row, "name", data) or value_of(row, "article", data) or "product"
    return f"Row {row.position}: {who} · {what} · size {value_of(row, 'size', data) or '?'}"


def _order_markup(db: Session, order_value: dict) -> Decimal | None:
    if order_value.get("id"):
        from app.services import order_rule
        order = db.get(Order, order_value["id"])
        return order_rule(order).markup_percentage if order else None
    terms = order_value.get("terms") or {}
    return Decimal(terms["markup"]) if terms.get("markup") is not None else None


def row_issues(db: Session, session: IntakeSession, row: DraftRow) -> list[Issue]:
    data = fields_of(row)
    issues: list[Issue] = []

    def need(name, text, options=None, kind="missing"):
        issues.append(Issue(row.id, name, kind, text, options or []))

    if row.kind == "update_item":
        item = db.get(OrderItem, row.target_item_id)
        if item is None:
            need("target_item", "The product to correct no longer exists.", kind="invalid")
        elif row.target_revision is not None and item.revision != row.target_revision:
            need("target_item", f"Product #{item.id} changed after this correction was prepared. Discard this row and describe the correction again.", kind="invalid")
        return issues
    if row.kind == "payment":
        for name, text in (("customer", "Which customer made this payment?"), ("amount", "What amount was paid?"),
                           ("payment_date", "On what date?"), ("payment_type", "Is this a payment received or a refund?")):
            if state_of(row, name, data) not in RESOLVED_STATES:
                need(name, text, data.get(name, {}).get("candidates") or [])
        currency = value_of(row, "currency", data)
        if currency and currency != settings.currency:
            need("currency", f"The amount is in {currency}. Accounting is in {settings.currency}; enter the {settings.currency} amount received.", kind="invalid")
        return issues
    if not session.season_id:
        need("season", "This intake has no season. Start a new intake for the right season.", kind="invalid")
    customer_state = state_of(row, "customer", data)
    if customer_state not in RESOLVED_STATES:
        raw = (data.get("customer") or {}).get("raw")
        need("customer", f"Who is this for{f' (source says “{raw}”)' if raw else ''}?", data.get("customer", {}).get("candidates") or [],
             "ambiguous" if customer_state == "suggested" else "missing")
    order_state = state_of(row, "order", data)
    order_value = value_of(row, "order", data) or {}
    if order_state not in RESOLVED_STATES:
        options = data.get("order", {}).get("candidates") or [{"value": {"id": order.id}, "label": order.brand.name} for order in season_orders(db, session)]
        need("order", "Which brand table does this belong to?", options, "ambiguous" if options else "missing")
    elif order_value.get("new_brand") and _order_markup(db, order_value) is None:
        need("markup", f"No earlier terms exist for {order_value['new_brand']}. What commission (markup %) applies? Payment terms default to 50% initial / 50% Ready and 5% transport (CH exempt).")
    if not value_of(row, "name", data) and not value_of(row, "article", data):
        need("name", "What is the product (name or article)?")
    quantity_state = state_of(row, "quantity", data)
    if quantity_state == "defaulted":
        need("quantity", "The quantity was not stated. How many pieces?", [{"value": 1, "label": "1 piece"}], "confirm")
    elif quantity_state not in RESOLVED_STATES:
        need("quantity", "How many pieces?")
    price_entry = data.get("price") or {}
    if state_of(row, "price", data) == "conflicting":
        need("price", "The sources state different prices. Which is correct?",
             [{"value": option["value"], "label": f"{option['value']} ({option.get('origin', '')})"} for option in price_entry.get("candidates", [])], "conflict")
    elif state_of(row, "price", data) not in RESOLVED_STATES:
        need("price", price_entry.get("problem") or "What is the price?")
    if value_of(row, "price", data) is not None and Decimal(value_of(row, "price", data)) <= 0 and value_of(row, "price_meaning", data) == "final":
        need("price", "A final customer price must be above zero.", kind="invalid")
    if state_of(row, "price_meaning", data) not in RESOLVED_STATES and value_of(row, "price", data):
        need("price_meaning", f"Is {value_of(row, 'price', data)} the wholesale (factory) price or the final customer price?",
             [{"value": "wholesale", "label": "Wholesale / factory"}, {"value": "final", "label": "Final customer price"}])
    quantity = value_of(row, "quantity", data) or 1
    if int(quantity) > 1 and value_of(row, "price", data) and state_of(row, "price_basis", data) not in RESOLVED_STATES:
        need("price_basis", f"Is {value_of(row, 'price', data)} per piece or for all {quantity} pieces?",
             [{"value": "unit", "label": "Per piece"}, {"value": "line", "label": f"For all {quantity}"}])
    currency = value_of(row, "currency", data) or settings.currency
    if currency != settings.currency:
        need("currency", f"The price is in {currency}. Accounting is in {settings.currency}; enter the agreed {settings.currency} price (no automatic conversion).", kind="invalid")
    if state_of(row, "size", data) not in RESOLVED_STATES:
        need("size", "What size? (Enter it exactly as stated, or One size.) Sizes are never inferred from photos or measurements.",
             [{"value": ONE_SIZE, "label": ONE_SIZE}])
    if value_of(row, "price", data) and value_of(row, "price_basis", data) == "line" and int(quantity) > 1:
        line = Decimal(value_of(row, "price", data))
        if money(line / int(quantity)) * int(quantity) != line:
            need("price", f"{line} for {quantity} pieces is not a whole-cent unit price. Enter the per-piece price.", kind="invalid")
    return issues


def session_issues(db: Session, session: IntakeSession) -> list[Issue]:
    return [issue for row in active_rows(session) for issue in row_issues(db, session, row)]


def sync_questions(db: Session, session: IntakeSession) -> list[DraftQuestion]:
    """Open a question for each blocking issue; resolve questions whose issue has gone."""
    issues = {issue.key: issue for issue in session_issues(db, session)}
    open_questions = [q for q in session.questions if q.status == "open"]
    by_key = {q.key: q for q in open_questions}
    for question in open_questions:
        if question.field in {"target_row", "target_item"}:
            continue  # Clarifications of a message stay until answered.
        issue = issues.get(question.key)
        if issue is None:
            question.status = "resolved"
            question.answered_at = question.answered_at or utcnow()
        else:
            question.text, question.options_json = issue.text, json.dumps(issue.options, ensure_ascii=False, default=str)
    for key, issue in issues.items():
        if key not in by_key and issue.kind != "invalid":
            question = DraftQuestion(session_id=session.id, draft_row_id=issue.row_id, field=issue.field, key=key,
                                     text=issue.text, options_json=json.dumps(issue.options, ensure_ascii=False, default=str))
            db.add(question)
            session.questions.append(question)
    db.flush()
    return [q for q in session.questions if q.status == "open"]


def update_state(db: Session, session: IntakeSession) -> None:
    if session.state in {"saved"} and not active_rows(session):
        return
    issues = session_issues(db, session)
    if not active_rows(session):
        session.state = "saved" if any(row.status == "committed" for row in session.rows) else "collecting"
    elif issues:
        session.state = "needs_answers"
    else:
        session.state = "ready_for_review"


def refresh(db: Session, session: IntakeSession) -> None:
    sync_questions(db, session)
    update_state(db, session)
    db.flush()


# --- Review preview and saving ------------------------------------------------------

@dataclass
class RowPlan:
    row: DraftRow
    order: Order | None
    new_order: dict | None
    customer: Customer | None
    new_customer_name: str | None
    values: dict
    notes: list[str]
    customer_unit: Decimal | None = None
    line_value: Decimal | None = None


def plan_row(db: Session, session: IntakeSession, row: DraftRow) -> RowPlan:
    data = fields_of(row)
    customer_value = value_of(row, "customer", data) or {}
    order_value = value_of(row, "order", data) or {}
    order = db.get(Order, order_value["id"]) if order_value.get("id") else None
    markup = _order_markup(db, order_value) or ZERO
    quantity = int(value_of(row, "quantity", data))
    amount = Decimal(value_of(row, "price", data))
    unit = money(amount / quantity) if value_of(row, "price_basis", data) == "line" else amount
    notes = []
    selling = None
    if value_of(row, "price_meaning", data) == "wholesale":
        wholesale = unit
    else:
        reverse = wholesale_from_final(unit, markup)
        wholesale, selling = reverse.wholesale, reverse.selling_unit_price
        if reverse.reason:
            notes.append(reverse.reason)
    values = {"article": value_of(row, "article", data), "name": value_of(row, "name", data), "color": value_of(row, "color", data),
              "size": value_of(row, "size", data), "quantity": quantity, "wholesale_unit_price": wholesale,
              "selling_unit_price": selling, "markup_percentage": markup,
              "logistics_preference": value_of(row, "logistics_preference", data),
              "customer_requirements": value_of(row, "customer_requirements", data)}
    if value_of(row, "requested_delivery_date", data):
        values["requested_delivery_date"] = date.fromisoformat(value_of(row, "requested_delivery_date", data))
    customer_unit = selling if selling is not None else final_unit_price(wholesale, markup)
    return RowPlan(row=row, order=order, new_order=order_value if order_value.get("new_brand") else None,
                   customer=db.get(Customer, customer_value["id"]) if customer_value.get("id") else None,
                   new_customer_name=customer_value.get("new_name"), values=values, notes=notes,
                   customer_unit=customer_unit, line_value=money(customer_unit * quantity))


def _ensure_order(db: Session, session: IntakeSession, new_order: dict, actor: str) -> Order:
    name = new_order["new_brand"].strip()
    brand = db.scalar(select(Brand).where(func.lower(Brand.name) == name.casefold()))
    terms = dict(DEFAULT_TERMS, **(new_order.get("terms") or {}))
    if brand is None:
        brand = Brand(name=name, default_markup_percentage=Decimal(terms["markup"]))
        db.add(brand)
        db.flush()
    order = db.scalar(select(Order).where(Order.season_id == session.season_id, Order.brand_id == brand.id))
    if order is not None:
        return order
    if db.scalar(select(BrandSeasonRule).where(BrandSeasonRule.season_id == session.season_id, BrandSeasonRule.brand_id == brand.id)) is None:
        db.add(BrandSeasonRule(season_id=session.season_id, brand_id=brand.id, markup_percentage=Decimal(terms["markup"]),
                               initial_prepayment_percentage=Decimal(terms["initial"]), ready_balance_percentage=Decimal(terms["ready"]),
                               transport_percentage=Decimal(terms["transport"]), transport_exempt_country_codes=terms["exempt"],
                               expected_receipt_days=int(terms["receipt_days"]),
                               notes=f"Created from intake session {session.public_id[:8]}; terms {new_order.get('terms_source') or 'entered by operator'}."))
    order = Order(season_id=session.season_id, brand_id=brand.id, status="Ordered")
    db.add(order)
    db.flush()
    record_change(db, entity_type="Order", entity_id=order.id, field=None, previous_value=None, new_value=brand.name,
                  source_user=actor, description=f"Created {brand.name} table in {session.season.name} from intake ({new_order.get('terms_source') or 'operator terms'})")
    return order


def _statement_totals(db: Session, customer_ids: set[int], season_name: str | None) -> dict[int, dict]:
    from app.services import customer_statement
    totals = {}
    for customer_id in customer_ids:
        try:
            statement = customer_statement(db, customer_id, season_name)
        except LookupError:
            continue
        totals[customer_id] = {"ordered": str(statement.ordered), "charged": str(statement.account.total_charged),
                               "paid": str(statement.account.total_paid), "outstanding": str(statement.account.outstanding)}
    return totals


def _attach_image(db: Session, item: OrderItem, source_id: int, created_files: list) -> None:
    from app.image_manager import store_manual_image
    from app.source_store import read_blob
    source = db.get(SourceItem, source_id)
    if source is None or source.blob is None or not source.media_type.startswith("image/"):
        return
    relative_path, absolute_path = store_manual_image(item.id, source.original_name or "image.jpg", read_blob(source.blob))
    created_files.append(absolute_path)
    item.image_path = relative_path
    item.image_source_filename = source.original_name
    item.image_match_method = "intake_attachment_operator_saved"
    item.image_updated_at = utcnow()


def commit_session(db: Session, session: IntakeSession, *, operation_key: str, expected_revision: int,
                   actor: str = "web user") -> dict:
    """Apply all complete draft rows once. A retry with the same key returns the first result."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", operation_key or ""):
        raise DraftError("Missing or invalid save key; reload the page.")
    if db.get_bind().dialect.name == "sqlite":
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    receipt = db.get(OperationReceipt, operation_key)
    if receipt is not None:
        return json.loads(receipt.result_json) | {"replayed": True}
    db.expire_all()  # Re-read the draft inside the write lock.
    session = get_session(db, session.public_id)
    rows = active_rows(session)
    if not rows:
        previous = db.scalar(select(OperationReceipt).where(OperationReceipt.session_id == session.id).order_by(OperationReceipt.created_at.desc()).limit(1))
        if previous is not None:
            return json.loads(previous.result_json) | {"replayed": True}
        raise DraftError("There is nothing to save in this draft.")
    if session.revision != expected_revision:
        raise StaleDraftError("The draft changed after you reviewed it. Review the updated draft, then save again.")
    issues = session_issues(db, session)
    if issues:
        raise DraftError("Answer the open questions first: " + "; ".join(issue.text for issue in issues[:3]))
    plans = [plan_row(db, session, row) for row in rows if row.kind == "new_item"]
    corrections = [row for row in rows if row.kind == "update_item"]
    payments = [row for row in rows if row.kind == "payment"]
    affected = {plan.customer.id for plan in plans if plan.customer}
    affected |= {db.get(OrderItem, row.target_item_id).customer_id for row in corrections if db.get(OrderItem, row.target_item_id).customer_id}
    affected |= {value_of(row, "customer")["id"] for row in payments if (value_of(row, "customer") or {}).get("id")}
    season_name = session.season.name if session.season else None
    before = _statement_totals(db, affected, season_name)
    created_files: list = []
    results = []
    try:
        new_orders: dict[str, Order] = {}
        new_customers: dict[str, Customer] = {}
        for plan in plans:
            order = plan.order
            if order is None:
                key = plan.new_order["new_brand"].casefold()
                order = new_orders.get(key) or _ensure_order(db, session, plan.new_order, actor)
                new_orders[key] = order
            customer = plan.customer
            if customer is None:
                # Several rows for the same new customer create one customer, not MI and MI2.
                customer = new_customers.get(_norm(plan.new_customer_name))
            if customer is None:
                customer = Customer(code=new_customer_code(db, plan.new_customer_name), name=plan.new_customer_name)
                db.add(customer)
                db.flush()
                new_customers[_norm(plan.new_customer_name)] = customer
                record_change(db, entity_type="Customer", entity_id=customer.id, field=None, previous_value=None, new_value=customer.code,
                              source_user=actor, description=f"Created customer {customer.code} ({customer.name}) from intake")
                affected.add(customer.id)
            values = {key: value for key, value in plan.values.items() if value is not None or key == "selling_unit_price"}
            values["customer_id"] = customer.id
            data = fields_of(plan.row)
            source_ids = sorted({entry["source_item_id"] for entry_name, entry_value in data.items() if isinstance(entry_value, dict)
                                 for entry in entry_value.get("evidence", []) if isinstance(entry, dict) and entry.get("source_item_id")})
            item = item_service.create_item(
                db, order, values, actor=actor, channel="Intake", source_file="Intake",
                source_note=json.dumps({"intake_session": session.public_id, "draft_row": plan.row.id, "source_items": source_ids}),
                ai_source_text="\n".join(message.text for message in session.messages if message.role == "operator")[:20000] or None).item
            if value_of(plan.row, "image"):
                try:
                    _attach_image(db, item, value_of(plan.row, "image"), created_files)
                except ValueError as exc:  # An optional image never blocks a complete order.
                    plan.notes.append(f"Image not attached: {exc}")
            plan.row.status, plan.row.committed_item_id = "committed", item.id
            results.append({"row": plan.row.position, "action": "created", "item_id": item.id, "order_id": order.id,
                            "customer_unit": str(plan.customer_unit), "line_value": str(plan.line_value), "notes": plan.notes})
        for row in corrections:
            item = db.get(OrderItem, row.target_item_id)
            data = fields_of(row)
            changes = {name: entry["value"] for name, entry in data.items() if not name.startswith("_")}
            operator_text = next((m.text for m in reversed(session.messages) if m.role == "operator"), "")
            result = item_service.change_item(db, item, changes, actor=actor, channel="Intake correction",
                                              expected_revision=row.target_revision,
                                              reason=f"Intake {session.public_id[:8]}: {operator_text[:300]}")
            row.status, row.committed_item_id = "committed", item.id
            results.append({"row": row.position, "action": "corrected", "item_id": item.id,
                            "changes": [{"field": c.field, "before": str(c.before), "after": str(c.after)} for c in result.changes]})
        for row in payments:
            payment = _create_payment_from_row(db, session, row, actor)
            row.status = "committed"
            results.append({"row": row.position, "action": "payment", "payment_id": payment.id,
                            "type": payment.type, "amount": str(payment.amount)})
        db.flush()
        # Order.items was loaded for the "before" totals; re-read it so "after" includes the rows just saved.
        db.expire_all()
        after = _statement_totals(db, affected, season_name)
        for source in session.sources:
            if source.state not in {"failed", "ignored"}:
                source.state, source.state_reason = "applied", f"Saved in intake {session.public_id[:8]}"
        result = {"operation_key": operation_key, "session": session.public_id, "saved_at": utcnow().isoformat(),
                  "rows": results, "before": before, "after": after, "replayed": False}
        db.add(OperationReceipt(key=operation_key, kind="intake_commit", session_id=session.id,
                                session_revision=session.revision, result_json=json.dumps(result, ensure_ascii=False, default=str)))
        touch(session)
        session.state = "saved"
        add_message(db, session, "assistant", f"Saved {len(results)} row(s). Downloads are separate; retrying a download never saves again.")
        db.commit()
    except Exception:
        db.rollback()
        from pathlib import Path
        for path in created_files:
            Path(path).unlink(missing_ok=True)
        raise
    return result


def _create_payment_from_row(db: Session, session: IntakeSession, row: DraftRow, actor: str) -> Payment:
    data = fields_of(row)
    customer_id = value_of(row, "customer", data)["id"]
    payment = Payment(season_id=session.season_id, customer_id=customer_id,
                      date=date.fromisoformat(value_of(row, "payment_date", data)),
                      amount=Decimal(value_of(row, "amount", data)), type=value_of(row, "payment_type", data),
                      note=value_of(row, "note", data) or f"Recorded from intake {session.public_id[:8]}")
    db.add(payment)
    db.flush()
    stage = value_of(row, "stage", data)
    if stage in {"initial", "ready"}:
        db.add(PaymentAllocation(payment_id=payment.id, stage=stage, amount=payment.amount))
    record_change(db, entity_type="Payment", entity_id=payment.id, field=None, previous_value=None, new_value=str(payment.amount),
                  source_user=actor, description=f"Recorded {payment.type} {payment.amount} for customer {customer_id} from intake")
    return payment


def possible_duplicate_payments(db: Session, row: DraftRow) -> list[Payment]:
    data = fields_of(row)
    customer = value_of(row, "customer", data) or {}
    if not customer.get("id") or not value_of(row, "amount", data):
        return []
    return list(db.scalars(select(Payment).where(Payment.customer_id == customer["id"],
                                                 Payment.amount == Decimal(value_of(row, "amount", data)))).all())


def row_view(db: Session, session: IntakeSession, row: DraftRow) -> dict:
    """Display data for one row: each field's value, state and evidence, plus the plan if complete."""
    data = fields_of(row)
    display = {}
    names = PAYMENT_FIELDS if row.kind == "payment" else ITEM_FIELDS
    for name in names:
        entry = data.get(name) or {}
        value = entry.get("value")
        if name == "customer" and isinstance(value, dict):
            customer = db.get(Customer, value["id"]) if value.get("id") else None
            text = f"{customer.code} · {customer.name or ''}" if customer else f"New: {value.get('new_name')}"
        elif name == "order" and isinstance(value, dict):
            order = db.get(Order, value["id"]) if value.get("id") else None
            terms = value.get("terms") or {}
            text = order.brand.name if order else f"New table: {value.get('new_brand')}" + (f" ({Decimal(terms['markup']) * 100:.0f}% markup)" if terms.get("markup") else "")
        elif name == "image" and value:
            text = f"Attachment #{value}"
        else:
            text = "" if value is None else str(value)
        display[name] = {"text": text, "state": entry.get("state", "missing"), "origin": entry.get("origin", ""),
                         "raw": entry.get("raw"), "evidence": entry.get("evidence", []), "history": entry.get("history", []),
                         "alternatives": entry.get("alternatives", []), "before": entry.get("before")}
    issues = row_issues(db, session, row) if row.status == "draft" else []
    plan = None
    if row.kind == "new_item" and not issues and row.status == "draft":
        try:
            plan = plan_row(db, session, row)
        except Exception:  # noqa: BLE001 - incomplete values simply have no plan yet
            plan = None
    target = db.get(OrderItem, row.target_item_id) if row.target_item_id else None
    return {"row": row, "fields": display, "issues": issues, "plan": plan, "target": target,
            "duplicates": possible_duplicate_payments(db, row) if row.kind == "payment" else []}
