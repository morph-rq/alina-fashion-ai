from __future__ import annotations

from decimal import Decimal
import hashlib
import json

from sqlalchemy import inspect, select, text

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
}


def migrate_schema() -> None:
    """Apply small, additive SQLite migrations before ORM metadata creation."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in ALTERATIONS.items():
            if table not in existing_tables:
                continue
            existing_columns = {item["name"] for item in inspector.get_columns(table)}
            for column, sql_type in columns.items():
                if column not in existing_columns:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'))
    Base.metadata.create_all(bind=engine)


def backfill_corrected_workflow() -> None:
    from app.models import (
        BrandSeasonRule,
        AllocationReview,
        Customer,
        CustomerAlias,
        Order,
        OrderItem,
        ImportBatch,
        ReadyBatch,
        Shipment,
        Payment,
        Expense,
        PaymentAllocation,
        Season,
    )

    with SessionLocal.begin() as session:
        import_root = ROOT_DIR / "data" / "imports"
        untracked_batches = session.scalars(select(ImportBatch).where(ImportBatch.stored_path.is_(None))).all()
        if import_root.is_dir() and untracked_batches:
            files_by_name: dict[str, list] = {}
            for candidate in import_root.rglob("*"):
                if candidate.is_file():
                    files_by_name.setdefault(candidate.name, []).append(candidate)
            for batch in untracked_batches:
                for candidate in files_by_name.get(batch.filename, []):
                    if hashlib.sha256(candidate.read_bytes()).hexdigest() == batch.sha256:
                        batch.stored_path = candidate.resolve().relative_to(ROOT_DIR.resolve()).as_posix()
                        break

        for customer in session.scalars(select(Customer)).all():
            alias = customer.code.strip().upper()
            if alias and session.scalar(select(CustomerAlias).where(CustomerAlias.alias == alias)) is None:
                session.add(CustomerAlias(alias=alias, customer_id=customer.id))
            if alias == "AA":
                for built_in in ("A.A.", "ALINA", "ALINA A."):
                    if session.scalar(select(CustomerAlias).where(CustomerAlias.alias == built_in)) is None:
                        session.add(CustomerAlias(alias=built_in, customer_id=customer.id))

        default_season = session.scalar(select(Season).where(Season.name == settings.default_season))
        for payment in session.scalars(select(Payment).where(Payment.season_id.is_(None))).all():
            if payment.order_id:
                payment.season_id = session.get(Order, payment.order_id).season_id
                continue
            allocation = session.scalar(select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id, PaymentAllocation.ready_batch_id.is_not(None)).limit(1))
            if allocation:
                payment.season_id = session.get(ReadyBatch, allocation.ready_batch_id).season_id
            elif default_season:
                payment.season_id = default_season.id
        for expense in session.scalars(select(Expense).where(Expense.season_id.is_(None))).all():
            if expense.order_id:
                expense.season_id = session.get(Order, expense.order_id).season_id
            elif default_season:
                expense.season_id = default_season.id
        for shipment in session.scalars(select(Shipment).where(Shipment.season_id.is_(None))).all():
            first_item = session.scalar(select(OrderItem).where(OrderItem.shipment_link.has(shipment_id=shipment.id)).limit(1))
            if first_item:
                shipment.season_id = session.get(Order, first_item.order_id).season_id
            elif default_season:
                shipment.season_id = default_season.id

        orders = session.scalars(select(Order)).all()
        for order in orders:
            rule = session.scalar(
                select(BrandSeasonRule).where(
                    BrandSeasonRule.season_id == order.season_id,
                    BrandSeasonRule.brand_id == order.brand_id,
                )
            )
            if rule is None:
                item_markup = session.scalar(
                    select(OrderItem.markup_percentage)
                    .where(OrderItem.order_id == order.id)
                    .order_by(OrderItem.id)
                    .limit(1)
                )
                session.add(
                    BrandSeasonRule(
                        season_id=order.season_id,
                        brand_id=order.brand_id,
                        markup_percentage=item_markup if item_markup is not None else Decimal("0"),
                        initial_prepayment_percentage=Decimal("0.50"),
                        ready_balance_percentage=Decimal("0.50"),
                        transport_percentage=Decimal("0.05"),
                        transport_exempt_country_codes="CH",
                        expected_receipt_days=14,
                        notes="Migrated from the original SS26 workbook/prototype; item markup remains the historical applied value.",
                    )
                )

        session.flush()
        season_ids = sorted({order.season_id for order in orders})
        for season_id in season_ids:
            batch = session.scalar(
                select(ReadyBatch).where(
                    ReadyBatch.season_id == season_id,
                    ReadyBatch.name == "Ready 1 (Imported)",
                )
            )
            if batch is None:
                batch = ReadyBatch(
                    season_id=season_id,
                    name="Ready 1 (Imported)",
                    status="Historical",
                    notes="Historical Ready/shipped goods grouped during migration because the source workbook does not retain batch identity.",
                )
                session.add(batch)
                session.flush()
            order_ids = [order.id for order in orders if order.season_id == season_id]
            if order_ids:
                items = session.scalars(
                    select(OrderItem).where(OrderItem.order_id.in_(order_ids))
                ).all()
                for item in items:
                    if item.status != "Cancelled" and (item.status == "Ready" or item.shipment_link is not None):
                        item.ready_batch_id = item.ready_batch_id or batch.id
                    item.allocation_status = item.allocation_status or (
                        "Allocated" if item.customer_id is not None else "Review"
                    )
                    item.source_file = item.source_file or "Отчет_SS26 AA.xlsx"

        # Re-evaluate historical customer descriptions with the corrected parser.
        # Ambiguous rows are removed from customer accounts and preserved for review.
        from app.importer import SPECS, parse_customer_allocations

        batch = session.scalar(select(ImportBatch).order_by(ImportBatch.id).limit(1))
        groups: dict[tuple[str | None, int | None], list[OrderItem]] = {}
        for item in session.scalars(select(OrderItem)).all():
            groups.setdefault((item.source_sheet, item.source_row), []).append(item)
        for (sheet, row), items in groups.items():
            spec = next((candidate for candidate in SPECS if candidate.sheet == sheet and row in candidate.rows), None)
            if spec is None or not spec.customer_col:
                continue
            try:
                raw = json.loads(items[0].source_note or "{}")
            except json.JSONDecodeError:
                raw = {}
            description = raw.get(f"{spec.customer_col}{row}")
            total_quantity = sum(item.quantity for item in items)
            allocations, reason = parse_customer_allocations(description, total_quantity, "AA")
            group_id = f"migrated:{sheet}:{row}"
            for item in items:
                item.source_allocation_description = str(description or "") or None
                item.allocation_group_id = item.allocation_group_id or group_id
            if reason:
                for item in items:
                    item.customer_id = None
                    item.allocation_status = "Review"
                exists = session.scalar(
                    select(AllocationReview.id).where(
                        AllocationReview.sheet == (sheet or ""),
                        AllocationReview.row == row,
                        AllocationReview.status == "Open",
                    )
                )
                if exists is None:
                    session.add(
                        AllocationReview(
                            import_batch_id=batch.id if batch else None,
                            source_file=batch.filename if batch else "Отчет_SS26 AA.xlsx",
                            sheet=sheet or "",
                            row=row,
                            raw_description=str(description or "") or None,
                            raw_values=json.dumps(raw, ensure_ascii=False, default=str),
                            reason=reason,
                        )
                    )
            elif len(set(allocations)) == 1:
                code = allocations[0]
                customer = session.scalar(select(Customer).where(Customer.code == code))
                if customer is not None:
                    for item in items:
                        item.customer_id = customer.id
                        item.allocation_status = "Allocated"

        # Imported shipment transport/cost values already exist as explicit source expenses.
        # Keep them from acquiring the new default 5% a second time.
        for shipment in session.scalars(select(Shipment)).all():
            if shipment.use_rule_transport is None:
                shipment.use_rule_transport = False


def migrate_database() -> None:
    migrate_schema()
    backfill_corrected_workflow()
