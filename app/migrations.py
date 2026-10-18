"""Database upgrades.

Two kinds of change are applied at startup:

* Additive schema changes (new tables/columns). These are idempotent DDL.
* Versioned data migrations. Each runs once, inside one transaction, and is
  recorded in ``schema_migrations`` with a JSON summary of the facts it changed.
  A data migration never runs again on restart, so reviewed corrections made
  after it are never reinterpreted.

Test migrations on a copy of a database (``scripts/migration_check.py``) before
pointing an operational workspace at new code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Callable

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.config import ROOT_DIR, settings


ALTERATIONS: dict[str, dict[str, str]] = {
    "customers": {
        "country_code": "VARCHAR(2)",
        "phone": "VARCHAR(50)",
        "email": "VARCHAR(200)",
        "address": "VARCHAR(500)",
        "city": "VARCHAR(150)",
        "postal_code": "VARCHAR(30)",
        "preferred_contact": "VARCHAR(50)",
        "notes": "TEXT",
    },
    "brands": {"website_url": "VARCHAR(1000)", "notes": "TEXT"},
    "orders": {"catalog_url": "VARCHAR(1000)"},
    "shipments": {
        "season_id": "INTEGER REFERENCES seasons(id)",
        "use_rule_transport": "BOOLEAN NOT NULL DEFAULT 1",
        "destination_country_code": "VARCHAR(2)",
        "bag_cost": "NUMERIC(18, 2) NOT NULL DEFAULT 0",
    },
    "order_items": {
        "purchase_group_id": "INTEGER REFERENCES purchase_groups(id)",
        "worksheet_status": "VARCHAR(20)",
        "source_fill": "VARCHAR(40)",
        "worksheet_note": "TEXT",
        "selling_unit_price": "NUMERIC(18, 2)",
        "ready_batch_id": "INTEGER REFERENCES ready_batches(id)",
        "source_file": "VARCHAR(500)",
        "source_allocation_description": "TEXT",
        "allocation_status": "VARCHAR(30) NOT NULL DEFAULT 'Allocated'",
        "allocation_group_id": "VARCHAR(80)",
        "image_source_filename": "VARCHAR(500)",
        "image_match_method": "VARCHAR(60)",
        "image_updated_at": "DATETIME",
        "image_source_url": "VARCHAR(1000)",
        "image_context_url": "VARCHAR(1000)",
        "product_url": "VARCHAR(1000)",
        "requested_delivery_date": "DATE",
        "logistics_preference": "VARCHAR(100)",
        "customer_requirements": "TEXT",
        "ai_source_text": "TEXT",
        "contact_on_pdf_send": "BOOLEAN NOT NULL DEFAULT 0",
        "split_from_item_id": "INTEGER REFERENCES order_items(id)",
        "revision": "INTEGER NOT NULL DEFAULT 1",
    },
    "brand_season_rules": {
        "special_cost": "NUMERIC(18, 2) NOT NULL DEFAULT 0",
        "readiness_payment_logic": "VARCHAR(60) NOT NULL DEFAULT 'ready_batch_only'",
    },
    "import_batches": {
        "stored_path": "VARCHAR(1000)",
        "source_deleted_at": "DATETIME",
    },
    "factory_payments": {
        "order_id": "INTEGER REFERENCES orders(id)",
    },
    "payments": {
        "season_id": "INTEGER REFERENCES seasons(id)",
    },
    "expenses": {
        "season_id": "INTEGER REFERENCES seasons(id)",
    },
    "allocation_reviews": {
        "resolution_note": "TEXT",
        "resolved_at": "DATETIME",
    },
    "import_reviews": {
        "resolution_note": "TEXT",
        "resolved_at": "DATETIME",
    },
}


def migrate_schema(bind=None) -> None:
    """Apply small, additive SQLite migrations before ORM metadata creation."""
    bind = bind or engine
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as connection:
        for table, columns in ALTERATIONS.items():
            if table not in existing_tables:
                continue
            existing_columns = {item["name"] for item in inspector.get_columns(table)}
            for column, sql_type in columns.items():
                if column not in existing_columns:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'))
    Base.metadata.create_all(bind=bind)


@dataclass(frozen=True)
class DataMigration:
    version: str
    description: str
    apply: Callable[[Session], dict]


def _legacy_fill_missing_links(session: Session) -> dict:
    """Fill values that older databases left NULL. Never overwrites a set value."""
    from app.models import (
        BrandSeasonRule, Customer, CustomerAlias, Expense, ImportBatch, Order, OrderItem,
        Payment, PaymentAllocation, ReadyBatch, Season, Shipment,
    )
    changed: dict[str, list] = {"import_paths": [], "aliases": [], "payment_seasons": [], "expense_seasons": [],
                                "shipment_seasons": [], "rules": [], "historical_ready_items": [],
                                "shipment_transport_explicit": []}
    import_root = ROOT_DIR / "data" / "imports"
    untracked = session.scalars(select(ImportBatch).where(ImportBatch.stored_path.is_(None))).all()
    if import_root.is_dir() and untracked:
        files_by_name: dict[str, list] = {}
        for candidate in import_root.rglob("*"):
            if candidate.is_file():
                files_by_name.setdefault(candidate.name, []).append(candidate)
        for batch in untracked:
            for candidate in files_by_name.get(batch.filename, []):
                if hashlib.sha256(candidate.read_bytes()).hexdigest() == batch.sha256:
                    batch.stored_path = candidate.resolve().relative_to(ROOT_DIR.resolve()).as_posix()
                    changed["import_paths"].append(batch.id)
                    break

    for customer in session.scalars(select(Customer)).all():
        alias = customer.code.strip().upper()
        if alias and session.scalar(select(CustomerAlias).where(CustomerAlias.alias == alias)) is None:
            session.add(CustomerAlias(alias=alias, customer_id=customer.id))
            changed["aliases"].append(alias)
        if alias == "AA":
            for built_in in ("A.A.", "ALINA", "ALINA A."):
                if session.scalar(select(CustomerAlias).where(CustomerAlias.alias == built_in)) is None:
                    session.add(CustomerAlias(alias=built_in, customer_id=customer.id))
                    changed["aliases"].append(built_in)

    default_season = session.scalar(select(Season).where(Season.name == settings.default_season))
    for payment in session.scalars(select(Payment).where(Payment.season_id.is_(None))).all():
        if payment.order_id:
            payment.season_id = session.get(Order, payment.order_id).season_id
        else:
            allocation = session.scalar(select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id, PaymentAllocation.ready_batch_id.is_not(None)).limit(1))
            if allocation:
                payment.season_id = session.get(ReadyBatch, allocation.ready_batch_id).season_id
            elif default_season:
                payment.season_id = default_season.id
        changed["payment_seasons"].append(payment.id)
    for expense in session.scalars(select(Expense).where(Expense.season_id.is_(None))).all():
        if expense.order_id:
            expense.season_id = session.get(Order, expense.order_id).season_id
        elif default_season:
            expense.season_id = default_season.id
        changed["expense_seasons"].append(expense.id)
    for shipment in session.scalars(select(Shipment).where(Shipment.season_id.is_(None))).all():
        first_item = session.scalar(select(OrderItem).where(OrderItem.shipment_link.has(shipment_id=shipment.id)).limit(1))
        if first_item:
            shipment.season_id = session.get(Order, first_item.order_id).season_id
        elif default_season:
            shipment.season_id = default_season.id
        changed["shipment_seasons"].append(shipment.id)

    orders = session.scalars(select(Order)).all()
    for order in orders:
        rule = session.scalar(select(BrandSeasonRule).where(
            BrandSeasonRule.season_id == order.season_id, BrandSeasonRule.brand_id == order.brand_id))
        if rule is None:
            item_markup = session.scalar(select(OrderItem.markup_percentage).where(OrderItem.order_id == order.id).order_by(OrderItem.id).limit(1))
            session.add(BrandSeasonRule(
                season_id=order.season_id, brand_id=order.brand_id,
                markup_percentage=item_markup if item_markup is not None else Decimal("0"),
                initial_prepayment_percentage=Decimal("0.50"), ready_balance_percentage=Decimal("0.50"),
                transport_percentage=Decimal("0.05"), transport_exempt_country_codes="CH", expected_receipt_days=14,
                notes="Migrated from the original SS26 workbook/prototype; item markup remains the historical applied value.",
            ))
            changed["rules"].append(order.id)
    session.flush()

    # Historical Ready/shipped goods predate Ready batches. Group them once.
    for season_id in sorted({order.season_id for order in orders}):
        order_ids = [order.id for order in orders if order.season_id == season_id]
        legacy_items = [
            item for item in session.scalars(select(OrderItem).where(OrderItem.order_id.in_(order_ids))).all()
            if item.status != "Cancelled" and item.ready_batch_id is None and (item.status == "Ready" or item.shipment_link is not None)
        ]
        if not legacy_items:
            continue
        batch = session.scalar(select(ReadyBatch).where(ReadyBatch.season_id == season_id, ReadyBatch.name == "Ready 1 (Imported)"))
        if batch is None:
            batch = ReadyBatch(season_id=season_id, name="Ready 1 (Imported)", status="Historical",
                               notes="Historical Ready/shipped goods grouped during migration because the source workbook does not retain batch identity.")
            session.add(batch)
            session.flush()
        for item in legacy_items:
            item.ready_batch_id = batch.id
            changed["historical_ready_items"].append(item.id)

    # Imported shipment transport/cost values already exist as explicit source expenses.
    for shipment in session.scalars(select(Shipment).where(Shipment.use_rule_transport.is_(None))).all():
        shipment.use_rule_transport = False
        changed["shipment_transport_explicit"].append(shipment.id)
    return {key: value for key, value in changed.items() if value}


def _legacy_allocation_reparse(session: Session) -> dict:
    """Re-check imported customer descriptions with the corrected parser, once.

    Unallocated rows with one unambiguous parsed customer are allocated. Rows whose
    description is ambiguous are flagged for review; an existing customer
    assignment is never removed, because it may be an operator's reviewed decision.
    """
    from app.importer import SPECS, parse_customer_allocations
    from app.models import AllocationReview, Customer, ImportBatch, OrderItem

    allocated, flagged = [], []
    batch = session.scalar(select(ImportBatch).order_by(ImportBatch.id).limit(1))
    groups: dict[tuple[str | None, int | None], list] = {}
    for item in session.scalars(select(OrderItem).where(OrderItem.source_row.is_not(None))).all():
        groups.setdefault((item.source_sheet, item.source_row), []).append(item)
    for (sheet, row), items in groups.items():
        spec = next((candidate for candidate in SPECS if candidate.sheet == sheet and row in candidate.rows), None)
        if spec is None or not spec.customer_col:
            continue
        try:
            raw = json.loads(items[0].source_note or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or f"{spec.customer_col}{row}" not in raw:
            continue  # Not an imported workbook row (for example, an AI or manual row).
        description = raw.get(f"{spec.customer_col}{row}")
        reviewed = session.scalar(select(AllocationReview.id).where(
            AllocationReview.sheet == (sheet or ""), AllocationReview.row == row, AllocationReview.status != "Open"))
        if reviewed:
            continue
        allocations, reason = parse_customer_allocations(description, sum(item.quantity for item in items), "AA")
        for item in items:
            item.source_allocation_description = item.source_allocation_description or (str(description or "") or None)
            item.allocation_group_id = item.allocation_group_id or f"migrated:{sheet}:{row}"
        if reason:
            exists = session.scalar(select(AllocationReview.id).where(
                AllocationReview.sheet == (sheet or ""), AllocationReview.row == row, AllocationReview.status == "Open"))
            if exists is None:
                session.add(AllocationReview(
                    import_batch_id=batch.id if batch else None,
                    source_file=items[0].source_file or (batch.filename if batch else ""),
                    sheet=sheet or "", row=row, raw_description=str(description or "") or None,
                    raw_values=json.dumps(raw, ensure_ascii=False, default=str), reason=reason))
                flagged.append([sheet, row])
            for item in items:
                if item.customer_id is None:
                    item.allocation_status = "Review"
        elif len(set(allocations)) == 1:
            customer = session.scalar(select(Customer).where(Customer.code == allocations[0]))
            for item in items:
                if customer is not None and item.customer_id is None:
                    item.customer_id = customer.id
                    item.allocation_status = "Allocated"
                    allocated.append(item.id)
    return {"allocated_item_ids": allocated, "flagged_rows": flagged}


def _record_split_lineage(session: Session) -> dict:
    """Older Ready splits marked lineage by suffixing ':split' to the allocation group."""
    from app.models import OrderItem
    linked = []
    for item in session.scalars(select(OrderItem).where(OrderItem.allocation_group_id.like("%:split"))).all():
        parent_key = item.allocation_group_id.removesuffix(":split")
        parent = None
        if parent_key.isdigit():
            parent = session.get(OrderItem, int(parent_key))
        if parent is None:
            parent = session.scalar(select(OrderItem).where(
                OrderItem.allocation_group_id == parent_key, OrderItem.id < item.id).order_by(OrderItem.id).limit(1))
        if parent is not None and item.split_from_item_id is None:
            item.split_from_item_id = parent.id
            linked.append(item.id)
    return {"linked_item_ids": linked}


DATA_MIGRATIONS: list[DataMigration] = [
    DataMigration("0001_legacy_fill_missing_links", "Fill season, alias, rule and Ready-batch links missing in older databases", _legacy_fill_missing_links),
    DataMigration("0002_legacy_allocation_reparse", "Flag ambiguous imported customer descriptions without removing assignments", _legacy_allocation_reparse),
    DataMigration("0003_split_lineage", "Record parent rows for earlier Ready splits", _record_split_lineage),
]


def _ensure_registry(bind) -> None:
    with bind.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(100) PRIMARY KEY, description TEXT NOT NULL, "
            "applied_at VARCHAR(40) NOT NULL, summary TEXT)"
        ))


def applied_versions(bind=None) -> set[str]:
    bind = bind or engine
    _ensure_registry(bind)
    with bind.connect() as connection:
        return {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}


def run_data_migrations(session_factory=None, bind=None) -> list[str]:
    bind = bind or engine
    session_factory = session_factory or SessionLocal
    done = applied_versions(bind)
    newly_applied = []
    for migration in DATA_MIGRATIONS:
        if migration.version in done:
            continue
        with session_factory.begin() as session:
            summary = migration.apply(session)
            session.flush()
            session.execute(text(
                "INSERT INTO schema_migrations (version, description, applied_at, summary) VALUES (:v, :d, :t, :s)"
            ), {"v": migration.version, "d": migration.description,
                "t": datetime.now(timezone.utc).isoformat(), "s": json.dumps(summary, ensure_ascii=False, default=str)})
        newly_applied.append(migration.version)
    return newly_applied


def migrate_database() -> list[str]:
    migrate_schema()
    return run_data_migrations()
