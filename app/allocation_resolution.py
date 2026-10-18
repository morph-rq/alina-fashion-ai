"""Resolve an open customer-allocation note from evidence (FIX-01 / FIX-05).

The operator states how the source units belong to customers and records the
evidence for that decision. Rows are split where needed (keeping provenance),
each unit gets exactly one customer, the note closes with the reason, and the
result reports the System Check difference before and after. Nothing is
inferred: every allocation line comes from the operator.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_change
from app.importer import CUSTOMER_NOISE, SIZE_WORDS
from app.item_service import ItemRuleError, change_item
from app.models import AllocationReview, Customer, CustomerAlias, OrderItem
from app.ready_importer import split_item
from app.source_reviews import source_items

MIN_REASON = 12


@dataclass
class AllocationOutcome:
    review: AllocationReview
    item_ids: list[int]
    errors_before: dict[str, int]
    errors_after: dict[str, int]


def candidate_customers(db: Session, review: AllocationReview) -> list[Customer]:
    """Customers whose code or alias literally appears in the source description."""
    text = f"{review.raw_description or ''} {review.raw_values or ''}"
    tokens = {token.upper() for token in re.findall(r"(?<![A-Za-z])([A-Za-z.]{2,12})(?![A-Za-z])", text)}
    tokens -= CUSTOMER_NOISE | SIZE_WORDS
    found: dict[int, Customer] = {}
    for customer in db.scalars(select(Customer)).all():
        if customer.code.upper() in tokens:
            found[customer.id] = customer
    for alias in db.scalars(select(CustomerAlias)).all():
        if alias.alias.upper() in tokens:
            found[alias.customer_id] = alias.customer
    return sorted(found.values(), key=lambda customer: customer.code)


def _error_counts(db: Session) -> dict[str, int]:
    from app.validation import validate_system
    return dict(Counter(issue.code for issue in validate_system(db).errors))


def resolve_allocation(db: Session, review: AllocationReview, lines: list[tuple[int, int]], reason: str,
                       actor: str = "web user") -> AllocationOutcome:
    """Allocate the review's source units to customers; ``lines`` is [(customer_id, quantity)]."""
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise ItemRuleError(f"Record the evidence for this allocation (at least {MIN_REASON} characters).", "reason")
    if review.status != "Open":
        raise ItemRuleError("This note is already resolved.")
    items = sorted((item for item in source_items(db, review) if item.status != "Cancelled"), key=lambda item: item.id)
    if not items:
        raise ItemRuleError("No product rows match this note exactly (same file, sheet and row). Fix the products first.")
    lines = [(customer_id, quantity) for customer_id, quantity in lines if quantity]
    if not lines or any(quantity < 0 for _customer, quantity in lines):
        raise ItemRuleError("Enter at least one customer and a positive quantity.")
    for customer_id, _quantity in lines:
        if db.get(Customer, customer_id) is None:
            raise ItemRuleError("Choose existing customers only. Create a new customer first if the evidence names one.")
    total_units = sum(item.quantity for item in items)
    allocated_units = sum(quantity for _customer, quantity in lines)
    if allocated_units != total_units:
        raise ItemRuleError(f"The source rows hold {total_units} unit(s); your allocation covers {allocated_units}. Every unit needs exactly one customer.")

    errors_before = _error_counts(db)
    queue = list(items)
    touched: list[int] = []
    for customer_id, quantity in lines:
        remaining = quantity
        while remaining:
            item = queue.pop(0)
            if item.quantity > remaining:
                queue.insert(0, split_item(db, item, item.quantity - remaining))
            change_item(db, item, {"customer_id": customer_id}, actor=actor, channel="allocation review",
                        reason=f"Allocation note #{review.id}: {reason}")
            remaining -= item.quantity
            touched.append(item.id)
    customers = {customer_id for customer_id, _quantity in lines}
    review.status = "Resolved"
    review.resolved_customer_id = next(iter(customers)) if len(customers) == 1 else None
    review.resolved_at = datetime.now(timezone.utc)
    review.resolution_note = reason
    record_change(db, entity_type="AllocationReview", entity_id=review.id, field="status", previous_value="Open",
                  new_value="Resolved", source_user=actor,
                  description=f"Allocated {total_units} unit(s) to {', '.join(db.get(Customer, c).code for c, _q in lines)}. Evidence: {reason}")
    db.flush()
    return AllocationOutcome(review=review, item_ids=touched, errors_before=errors_before, errors_after=_error_counts(db))
