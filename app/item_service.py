"""Shared product-row mutations for forms, the worksheet, AI drafts and Excel edits.

Every channel parses its own input format, then calls ``create_item`` or
``change_item``. Those functions apply one set of business rules:

* values are validated the same way (money with at most two decimals, whole
  quantities, markup 0–100%, text kept verbatim so SKUs keep leading zeros);
* season consistency of Ready batches, shipments and purchase groups;
* the seller-credit ownership lock;
* operational protection — rows that are Ready, in a shipment or past Ready
  status cannot have quantity, price, markup or customer silently rewritten. An
  explicit correction reason is required (``reason``); channels that cannot
  collect one (Excel, AI) are refused and pointed at the product editor;
* optimistic concurrency via ``OrderItem.revision``;
* one audit record per changed field, with the channel and actor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_change
from app.calculations import ZERO, money
from app.models import (
    AllocationReview, Customer, Order, OrderItem, PurchaseGroup, ReadyBatch, SellerCreditAccount, Shipment, ShipmentItem,
)

STATUSES = ["Ordered", "Confirmed", "Production", "Ready", "Assigned to shipment", "In transit", "Arrived", "Delivered", "Cancelled"]
SHIPPED_STATUSES = {"Assigned to shipment", "In transit", "Arrived", "Delivered"}
WORKSHEET_STATES = {"process", "done", "pending"}
MAX_MONEY = Decimal("999999999.99")
MAX_QUANTITY = 1_000_000
TEXT_LIMITS = {"article": 200, "name": 300, "color": 200, "size": 100, "worksheet_note": 5000,
               "logistics_preference": 100, "customer_requirements": 5000}
# Changing these on an operational row alters posted Ready/shipment liabilities.
FINANCIAL_FIELDS = {"quantity", "wholesale_unit_price", "selling_unit_price", "markup_percentage", "customer_id"}
EDITABLE_FIELDS = set(TEXT_LIMITS) | FINANCIAL_FIELDS | {
    "status", "ready_batch_id", "shipment_id", "worksheet_status", "purchase_group_id", "requested_delivery_date",
}


class ItemRuleError(ValueError):
    """A business-rule violation, optionally tied to one field."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class StaleItemError(ItemRuleError):
    pass


@dataclass
class FieldChange:
    field: str
    before: Any
    after: Any


@dataclass
class ItemResult:
    item: OrderItem
    changes: list[FieldChange] = field(default_factory=list)
    created: bool = False


# --- Parsing shared by every input channel -------------------------------------

def parse_money(value: Any, label: str, *, optional: bool = False, positive: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise ItemRuleError(f"{label} is required.")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise ItemRuleError(f"{label} must be a number.") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_MONEY or (positive and amount == 0):
        raise ItemRuleError(f"{label} must be a {'positive' if positive else 'non-negative'} amount up to {MAX_MONEY}.")
    if amount != money(amount):
        raise ItemRuleError(f"{label} can have at most two decimals.")
    return money(amount)


def parse_percent(value: Any, label: str = "Markup") -> Decimal:
    """Accept a percentage as typed by an operator (35 means 35%)."""
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).replace(" ", "").replace(",", ".").rstrip("%"))
    except InvalidOperation:
        raise ItemRuleError(f"{label} must be a percentage such as 35.") from None
    if not number.is_finite() or number < 0 or number > 100:
        raise ItemRuleError(f"{label} must be between 0% and 100%.")
    return number / Decimal("100")


def parse_quantity(value: Any, *, minimum: int = 1) -> int:
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ItemRuleError("Quantity must be a whole number.", "quantity") from None
    if not number.is_finite() or number != number.to_integral_value() or number < minimum or number > MAX_QUANTITY:
        raise ItemRuleError(f"Quantity must be a whole number from {minimum} to {MAX_QUANTITY:,}.", "quantity")
    return int(number)


def clean_text(value: Any, field_name: str) -> str | None:
    """Keep identifiers verbatim (leading zeros, inner spacing); only trim the ends."""
    if value is None:
        return None
    text = str(value).strip()
    limit = TEXT_LIMITS.get(field_name)
    if limit and len(text) > limit:
        raise ItemRuleError(f"{field_name.replace('_', ' ').capitalize()} is too long (maximum {limit} characters).", field_name)
    return text or None


# --- Rules -----------------------------------------------------------------------

def is_operational(item: OrderItem) -> bool:
    return bool(item.ready_batch_id or item.shipment_link is not None or item.status in SHIPPED_STATUSES | {"Ready"})


def _season_checks(db: Session, order: Order, values: dict) -> None:
    batch_id, shipment_id, group_id = values.get("ready_batch_id"), values.get("shipment_id"), values.get("purchase_group_id")
    if batch_id:
        batch = db.get(ReadyBatch, batch_id)
        if batch is None or batch.season_id != order.season_id:
            raise ItemRuleError("Ready batch does not belong to this season.", "ready_batch_id")
    if shipment_id:
        shipment = db.get(Shipment, shipment_id)
        if shipment is None or shipment.season_id != order.season_id:
            raise ItemRuleError("Shipment does not belong to this season.", "shipment_id")
        if not batch_id:
            raise ItemRuleError("A product must belong to a Ready batch before shipment assignment.", "shipment_id")
    if group_id:
        group = db.get(PurchaseGroup, group_id)
        if group is None or group.season_id != order.season_id:
            raise ItemRuleError("Purchase group must exist in this season.", "purchase_group_id")
    if values.get("customer_id") and db.get(Customer, values["customer_id"]) is None:
        raise ItemRuleError("Customer not found.", "customer_id")
    status = values.get("status")
    if status is not None and status not in STATUSES:
        raise ItemRuleError("Unsupported status.", "status")
    if status == "Ready" and not batch_id:
        raise ItemRuleError("Ready status requires a Ready batch.", "status")
    if values.get("worksheet_status") is not None and values["worksheet_status"] not in WORKSHEET_STATES:
        raise ItemRuleError("Choose a valid worksheet status.", "worksheet_status")


def _derived_status(status: str, batch_id: int | None, shipment_id: int | None) -> str:
    if shipment_id and status in {"Ordered", "Confirmed", "Production", "Ready"}:
        return "Assigned to shipment"
    if batch_id and status in {"Ordered", "Confirmed", "Production"}:
        return "Ready"
    return status


def _sync_allocation_review(db: Session, item: OrderItem, actor: str, channel: str, reason: str | None) -> None:
    review = db.scalar(select(AllocationReview).where(
        AllocationReview.sheet == (item.source_sheet or ""), AllocationReview.row == item.source_row,
        AllocationReview.status == "Open").order_by(AllocationReview.id.desc()))
    if review is not None and item.source_file and review.source_file and review.source_file != item.source_file:
        review = None  # Same sheet/row in another file: not this row's note.
    if item.customer_id and review is not None:
        review.status = "Resolved"
        review.resolved_customer_id = item.customer_id
        review.resolved_at = datetime.now(timezone.utc)
        review.resolution_note = f"Customer assigned via {channel} by {actor}." + (f" {reason}" if reason else "")
    elif not item.customer_id and item.status != "Cancelled" and review is None:
        db.add(AllocationReview(
            source_file=item.source_file or channel, sheet=item.source_sheet or item.order.brand.name,
            row=item.source_row or item.id, raw_description=item.source_allocation_description,
            raw_values=json.dumps({"article": item.article, "product": item.name, "size": item.size, "quantity": item.quantity}, ensure_ascii=False),
            reason="Product has no customer; assign it before customer accounting"))


# --- Mutations --------------------------------------------------------------------

def create_item(db: Session, order: Order, values: dict, *, actor: str, channel: str,
                source_file: str | None = None, source_sheet: str | None = None, source_note: str | None = None,
                ai_source_text: str | None = None) -> ItemResult:
    """Create one product row. ``values`` holds parsed field values."""
    values = dict(values)
    unknown = set(values) - EDITABLE_FIELDS
    if unknown:
        raise ItemRuleError(f"Unsupported product fields: {', '.join(sorted(unknown))}.")
    values.setdefault("status", "Ordered")
    values.setdefault("markup_percentage", _rule_markup(order))
    values["quantity"] = parse_quantity(values.get("quantity", 1))
    for name in TEXT_LIMITS:
        if name in values:
            values[name] = clean_text(values[name], name)
    _season_checks(db, order, values)
    values["status"] = _derived_status(values["status"], values.get("ready_batch_id"), values.get("shipment_id"))
    shipment_id = values.pop("shipment_id", None)
    customer = db.get(Customer, values["customer_id"]) if values.get("customer_id") else None
    item = OrderItem(order_id=order.id, **values, source_file=source_file or channel,
                     source_sheet=source_sheet or order.source_sheet or order.brand.name,
                     source_note=source_note, ai_source_text=ai_source_text,
                     source_allocation_description=customer.code if customer else None,
                     allocation_status="Allocated" if customer else "Review")
    db.add(item)
    db.flush()
    if item.source_row is None:
        item.source_row = item.id
    if shipment_id:
        db.add(ShipmentItem(shipment_id=shipment_id, order_item_id=item.id))
    db.flush()
    db.refresh(item, ["shipment_link"])
    _sync_allocation_review(db, item, actor, channel, None)
    record_change(db, entity_type="OrderItem", entity_id=item.id, field=None, previous_value=None,
                  new_value=f"{order.brand.name} {item.article or item.name or item.id}", source_user=actor,
                  description=f"Created {order.brand.name} product row via {channel} for customer {customer.code if customer else 'Needs Review'}")
    return ItemResult(item=item, created=True)


def _rule_markup(order: Order) -> Decimal:
    from app.services import order_rule
    return order_rule(order).markup_percentage


def change_item(db: Session, item: OrderItem, values: dict, *, actor: str, channel: str,
                expected_revision: int | None = None, reason: str | None = None,
                allow_operational_changes: bool = True) -> ItemResult:
    """Apply parsed field changes to an existing row and audit each changed field.

    ``allow_operational_changes`` is False for channels that cannot collect a
    correction reason; such channels may not alter financial fields of a Ready or
    shipped row. When it is True a non-empty ``reason`` is still required.
    """
    values = dict(values)
    unknown = set(values) - EDITABLE_FIELDS
    if unknown:
        raise ItemRuleError(f"Unsupported product fields: {', '.join(sorted(unknown))}.")
    if expected_revision is not None and item.revision != expected_revision:
        raise StaleItemError("This product changed after you opened it. Reload it, check the newer values, then apply your correction again.")
    for name in TEXT_LIMITS:
        if name in values:
            values[name] = clean_text(values[name], name)
    if "quantity" in values:
        # Zero-quantity reference rows may keep their quantity; a change must be at least 1.
        unchanged = str(values["quantity"]).strip() == str(item.quantity)
        values["quantity"] = parse_quantity(values["quantity"], minimum=0 if unchanged else 1)
    current = {name: (item.shipment_id if name == "shipment_id" else getattr(item, name)) for name in EDITABLE_FIELDS}
    merged = current | values
    _season_checks(db, item.order, merged)
    if "status" in values or "ready_batch_id" in values or "shipment_id" in values:
        values["status"] = _derived_status(merged["status"], merged.get("ready_batch_id"), merged.get("shipment_id"))
        if values["status"] != item.status and "worksheet_status" not in values:
            values["worksheet_status"] = "done" if values["status"] in {"Delivered", "Arrived"} else "pending" if values["status"] == "Cancelled" else "process"
    changed = {name: value for name, value in values.items() if current.get(name) != value}
    if not changed:
        return ItemResult(item=item)
    if "customer_id" in changed and db.scalar(select(SellerCreditAccount.id).where(SellerCreditAccount.item_id == item.id)):
        raise ItemRuleError("This product has seller-credit history; its customer identity is locked.", "customer_id")
    financial = FINANCIAL_FIELDS & set(changed)
    if financial and is_operational(item):
        if not allow_operational_changes:
            raise ItemRuleError(
                "This product is Ready or shipped. Correct quantity, price or customer in the product editor, "
                "where the correction reason and its effect on balances are recorded.", sorted(financial)[0])
        if not (reason or "").strip():
            raise ItemRuleError("This product is Ready or shipped: enter a correction reason for changing "
                                + ", ".join(sorted(financial)) + ".", "reason")
    result = ItemResult(item=item)
    description_prefix = f"{item.customer.code if item.customer else '-'} - {item.order.brand.name} {item.article or item.name or item.id}"
    for name, value in changed.items():
        before = current.get(name)
        if name == "shipment_id":
            if item.shipment_link is not None:
                db.delete(item.shipment_link)
                db.flush()
            if value:
                db.add(ShipmentItem(shipment_id=value, order_item_id=item.id))
            db.flush()
            db.refresh(item, ["shipment_link"])
        else:
            setattr(item, name, value)
        result.changes.append(FieldChange(name, before, value))
        record_change(db, entity_type="OrderItem", entity_id=item.id, field=name, previous_value=before, new_value=value,
                      source_user=actor,
                      description=f"{description_prefix}: {name} changed {before} -> {value} via {channel}" + (f". Reason: {reason.strip()}" if reason and reason.strip() else ""))
    if "customer_id" in changed:
        customer = db.get(Customer, item.customer_id) if item.customer_id else None
        item.allocation_status = "Allocated" if customer else "Review"
        db.flush()
        _sync_allocation_review(db, item, actor, channel, reason)
    db.flush()
    return result


def split_quantity(db: Session, item: OrderItem, quantity: int, *, actor: str, reason: str) -> OrderItem:
    """Split units off a row (for corrections or reallocation) keeping provenance."""
    from app.ready_importer import split_item
    if not (reason or "").strip():
        raise ItemRuleError("Enter the reason for splitting this row.", "reason")
    remainder = split_item(db, item, quantity)
    record_change(db, entity_type="OrderItem", entity_id=item.id, field="quantity", previous_value=item.quantity + quantity,
                  new_value=item.quantity, source_user=actor, description=f"Split {quantity} unit(s) into row {remainder.id}. Reason: {reason.strip()}")
    return remainder


def parse_optional_date(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ItemRuleError(f"Invalid {label}; use YYYY-MM-DD.") from None
