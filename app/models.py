from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


MONEY = Numeric(18, 2, asdecimal=True)
PERCENTAGE = Numeric(8, 5, asdecimal=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntakeConfirmation(Base):
    __tablename__ = 'intake_confirmations'
    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey('order_items.id', ondelete='CASCADE'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(String(2), index=True)
    phone: Mapped[str | None] = mapped_column(String(50), index=True)
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(150))
    postal_code: Mapped[str | None] = mapped_column(String(30))
    preferred_contact: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    order_items: Mapped[list[OrderItem]] = relationship(back_populates="customer")
    payments: Mapped[list[Payment]] = relationship(back_populates="customer")
    aliases: Mapped[list[CustomerAlias]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class CustomerAlias(Base):
    __tablename__ = "customer_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    customer: Mapped[Customer] = relationship(back_populates="aliases")


class Distributor(Base):
    __tablename__ = "distributors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    contact: Mapped[str | None] = mapped_column(String(500))


class PurchaseGroup(Base):
    __tablename__ = "purchase_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    distributor_id: Mapped[int | None] = mapped_column(ForeignKey("distributors.id"), index=True)
    seller: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    initial_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0.50"))
    shipment_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0.50"))
    transport_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0.05"))
    special_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    cost_method: Mapped[str] = mapped_column(String(20), default="value")
    notes: Mapped[str | None] = mapped_column(Text)
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"))
    distributor: Mapped[Distributor | None] = relationship()
    season: Mapped[Season] = relationship()
    shipment: Mapped[Shipment | None] = relationship()
    items: Mapped[list[OrderItem]] = relationship(back_populates="purchase_group")


class SellerCreditAccount(Base):
    __tablename__ = "seller_credit_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("purchase_groups.id"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    group: Mapped[PurchaseGroup] = relationship()
    item: Mapped[OrderItem | None] = relationship()
    customer: Mapped[Customer | None] = relationship()
    entries: Mapped[list[SellerCreditEntry]] = relationship(back_populates="account")


class SellerCreditEntry(Base):
    __tablename__ = "seller_credit_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("seller_credit_accounts.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    kind: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str | None] = mapped_column(String(30))
    date: Mapped[date] = mapped_column(Date)
    reference: Mapped[str] = mapped_column(String(500))
    operation_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    account: Mapped[SellerCreditAccount] = relationship(back_populates="entries")


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    default_markup_percentage: Mapped[Decimal | None] = mapped_column(PERCENTAGE)
    website_url: Mapped[str | None] = mapped_column(String(1000))
    notes: Mapped[str | None] = mapped_column(Text)
    orders: Mapped[list[Order]] = relationship(back_populates="brand")


class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    orders: Mapped[list[Order]] = relationship(back_populates="season")
    rules: Mapped[list[BrandSeasonRule]] = relationship(back_populates="season", cascade="all, delete-orphan")
    ready_batches: Mapped[list[ReadyBatch]] = relationship(back_populates="season", cascade="all, delete-orphan")


class BrandSeasonRule(Base):
    __tablename__ = "brand_season_rules"
    __table_args__ = (UniqueConstraint("season_id", "brand_id", name="uq_rule_season_brand"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    markup_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE)
    initial_prepayment_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0.50"))
    ready_balance_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0.50"))
    transport_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0.05"))
    special_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    transport_exempt_country_codes: Mapped[str] = mapped_column(String(200), default="CH")
    expected_receipt_days: Mapped[int] = mapped_column(Integer, default=14)
    rounding_mode: Mapped[str] = mapped_column(String(40), default="ROUND_HALF_UP")
    readiness_payment_logic: Mapped[str] = mapped_column(String(60), default="ready_batch_only")
    notes: Mapped[str | None] = mapped_column(Text)
    season: Mapped[Season] = relationship(back_populates="rules")
    brand: Mapped[Brand] = relationship()


class ReadyBatch(Base):
    __tablename__ = "ready_batches"
    __table_args__ = (UniqueConstraint("season_id", "name", name="uq_ready_batch_season_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    ready_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(50), default="Open")
    notes: Mapped[str | None] = mapped_column(Text)
    season: Mapped[Season] = relationship(back_populates="ready_batches")
    items: Mapped[list[OrderItem]] = relationship(back_populates="ready_batch")
    payment_allocations: Mapped[list[PaymentAllocation]] = relationship(back_populates="ready_batch")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("season_id", "brand_id", name="uq_order_season_brand"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    factory_ready_from: Mapped[date | None] = mapped_column(Date)
    factory_ready_to: Mapped[date | None] = mapped_column(Date)
    factory_ready_note: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(50), default="Ordered")
    source_total: Mapped[Decimal | None] = mapped_column(MONEY)
    source_sheet: Mapped[str | None] = mapped_column(String(200))
    catalog_url: Mapped[str | None] = mapped_column(String(1000))
    season: Mapped[Season] = relationship(back_populates="orders")
    brand: Mapped[Brand] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list[Payment]] = relationship(back_populates="order")
    factory_payments: Mapped[list[FactoryPayment]] = relationship(back_populates="order")
    expenses: Mapped[list[Expense]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    purchase_group_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_groups.id"), index=True)
    worksheet_status: Mapped[str | None] = mapped_column(String(20))
    source_fill: Mapped[str | None] = mapped_column(String(40))
    worksheet_note: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    ready_batch_id: Mapped[int | None] = mapped_column(ForeignKey("ready_batches.id"), index=True)
    article: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(300))
    color: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    wholesale_unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    selling_unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    markup_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(50), default="Ordered")
    image_path: Mapped[str | None] = mapped_column(String(500))
    image_source_filename: Mapped[str | None] = mapped_column(String(500))
    image_match_method: Mapped[str | None] = mapped_column(String(60))
    image_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    image_source_url: Mapped[str | None] = mapped_column(String(1000))
    image_context_url: Mapped[str | None] = mapped_column(String(1000))
    product_url: Mapped[str | None] = mapped_column(String(1000))
    requested_delivery_date: Mapped[date | None] = mapped_column(Date)
    logistics_preference: Mapped[str | None] = mapped_column(String(100))
    customer_requirements: Mapped[str | None] = mapped_column(Text)
    ai_source_text: Mapped[str | None] = mapped_column(Text)
    contact_on_pdf_send: Mapped[bool] = mapped_column(Boolean, default=False)
    source_sheet: Mapped[str | None] = mapped_column(String(200))
    source_file: Mapped[str | None] = mapped_column(String(500))
    source_row: Mapped[int | None] = mapped_column(Integer)
    source_total: Mapped[Decimal | None] = mapped_column(MONEY)
    source_note: Mapped[str | None] = mapped_column(Text)
    source_allocation_description: Mapped[str | None] = mapped_column(Text)
    allocation_status: Mapped[str] = mapped_column(String(30), default="Allocated")
    allocation_group_id: Mapped[str | None] = mapped_column(String(80), index=True)
    # Lineage of a partial-quantity split; the remainder points at the row it came from.
    split_from_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id"), index=True)
    # Optimistic-concurrency revision; SQLAlchemy increments it on every update.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": revision}
    order: Mapped[Order] = relationship(back_populates="items")
    purchase_group: Mapped[PurchaseGroup | None] = relationship(back_populates="items")
    customer: Mapped[Customer | None] = relationship(back_populates="order_items")
    ready_batch: Mapped[ReadyBatch | None] = relationship(back_populates="items")
    shipment_link: Mapped[ShipmentItem | None] = relationship(
        back_populates="order_item", cascade="all, delete-orphan", uselist=False
    )

    @property
    def shipment_id(self) -> int | None:
        return self.shipment_link.shipment_id if self.shipment_link else None

    @property
    def shipment(self) -> Shipment | None:
        return self.shipment_link.shipment if self.shipment_link else None


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    type: Mapped[str] = mapped_column(String(50), default="payment")
    note: Mapped[str | None] = mapped_column(Text)
    season: Mapped[Season | None] = relationship()
    customer: Mapped[Customer | None] = relationship(back_populates="payments")
    order: Mapped[Order | None] = relationship(back_populates="payments")
    allocations: Mapped[list[PaymentAllocation]] = relationship(back_populates="payment", cascade="all, delete-orphan")


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    ready_batch_id: Mapped[int | None] = mapped_column(ForeignKey("ready_batches.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    payment: Mapped[Payment] = relationship(back_populates="allocations")
    ready_batch: Mapped[ReadyBatch | None] = relationship(back_populates="payment_allocations")
    order: Mapped[Order | None] = relationship()


class FactoryPayment(Base):
    __tablename__ = "factory_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    ready_batch_id: Mapped[int | None] = mapped_column(ForeignKey("ready_batches.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    stage: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text)
    season: Mapped[Season] = relationship()
    order: Mapped[Order | None] = relationship(back_populates="factory_payments")
    ready_batch: Mapped[ReadyBatch | None] = relationship()


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(50), default="Assigned")
    transport_percentage: Mapped[Decimal] = mapped_column(PERCENTAGE, default=Decimal("0"))
    use_rule_transport: Mapped[bool] = mapped_column(Boolean, default=True)
    destination_country_code: Mapped[str | None] = mapped_column(String(2))
    transport_eligible_value_override: Mapped[Decimal | None] = mapped_column(MONEY)
    fixed_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    suitcase_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    bag_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    season: Mapped[Season | None] = relationship()
    items: Mapped[list[ShipmentItem]] = relationship(back_populates="shipment", cascade="all, delete-orphan")
    expenses: Mapped[list[Expense]] = relationship(back_populates="shipment")


class ShipmentItem(Base):
    __tablename__ = "shipment_items"
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_items.id"), primary_key=True, unique=True
    )
    shipment: Mapped[Shipment] = relationship(back_populates="items")
    order_item: Mapped[OrderItem] = relationship(back_populates="shipment_link")


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(Text)
    season: Mapped[Season | None] = relationship()
    shipment: Mapped[Shipment | None] = relationship(back_populates="expenses")
    order: Mapped[Order | None] = relationship(back_populates="expenses")
    customer: Mapped[Customer | None] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary_json: Mapped[str | None] = mapped_column(Text)
    stored_path: Mapped[str | None] = mapped_column(String(1000))
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportReview(Base):
    __tablename__ = "import_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    sheet: Mapped[str] = mapped_column(String(200))
    row: Mapped[int | None] = mapped_column(Integer)
    raw_values: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="Open")
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_batch: Mapped[ImportBatch] = relationship()


class ImportChange(Base):
    __tablename__ = "import_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    source_file: Mapped[str] = mapped_column(String(500))
    sheet: Mapped[str] = mapped_column(String(200))
    row: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="Applied")
    previous_values: Mapped[str | None] = mapped_column(Text)
    new_values: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    import_batch: Mapped[ImportBatch] = relationship()


class AllocationReview(Base):
    __tablename__ = "allocation_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), index=True)
    source_file: Mapped[str] = mapped_column(String(500))
    sheet: Mapped[str] = mapped_column(String(200))
    row: Mapped[int | None] = mapped_column(Integer)
    raw_description: Mapped[str | None] = mapped_column(Text)
    raw_values: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="Open")
    resolved_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_batch: Mapped[ImportBatch | None] = relationship()
    resolved_customer: Mapped[Customer | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(100))
    field: Mapped[str | None] = mapped_column(String(100))
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_user: Mapped[str] = mapped_column(String(100), default="system")
    description: Mapped[str] = mapped_column(Text)


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    season: Mapped[Season | None] = relationship()
    items: Mapped[list[CollectionItem]] = relationship(back_populates="collection", cascade="all, delete-orphan")


class CollectionItem(Base):
    __tablename__ = "collection_items"
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"), primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    collection: Mapped[Collection] = relationship(back_populates="items")
    order_item: Mapped[OrderItem] = relationship()


class LibraryFile(Base):
    __tablename__ = "library_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(1000), unique=True)
    category: Mapped[str] = mapped_column(String(60), default="other")
    content_type: Mapped[str | None] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Durable intake: unified source inbox, drafts, questions and receipts ---------

class SourceBlob(Base):
    """Content-addressed stored bytes. One row per distinct content hash."""
    __tablename__ = "source_blobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(150))
    storage_path: Mapped[str] = mapped_column(String(500), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceItem(Base):
    """One piece of evidence in the inbox, whatever channel it arrived through.

    Identity is (channel, account, remote id, remote version), so a replayed Drive
    change or Gmail message is recognised. The same bytes arriving by two
    channels are two SourceItems sharing one SourceBlob: provenance stays
    separate and nothing about business identity is implied.
    """
    __tablename__ = "source_items"
    __table_args__ = (UniqueConstraint("channel", "account", "remote_id", "remote_version", name="uq_source_identity"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), index=True)  # upload | text | drive | gmail
    account: Mapped[str] = mapped_column(String(200), default="")
    remote_id: Mapped[str] = mapped_column(String(300))
    remote_version: Mapped[str] = mapped_column(String(200), default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("source_items.id"), index=True)  # attachment -> message
    blob_id: Mapped[int | None] = mapped_column(ForeignKey("source_blobs.id"), index=True)
    original_name: Mapped[str] = mapped_column(String(500), default="")
    media_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream")
    source_link: Mapped[str | None] = mapped_column(String(1000))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    state: Mapped[str] = mapped_column(String(30), default="captured", index=True)
    state_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("intake_sessions.id"), index=True)
    remote_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    blob: Mapped[SourceBlob | None] = relationship()
    session: Mapped[IntakeSession | None] = relationship(back_populates="sources")


class IntakeSession(Base):
    __tablename__ = "intake_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="New intake")
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    action: Mapped[str] = mapped_column(String(40), default="add_goods")
    state: Mapped[str] = mapped_column(String(30), default="collecting", index=True)
    state_reason: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    season: Mapped[Season | None] = relationship()
    messages: Mapped[list[IntakeMessage]] = relationship(back_populates="session", order_by="IntakeMessage.id", cascade="all, delete-orphan")
    rows: Mapped[list[DraftRow]] = relationship(back_populates="session", order_by="DraftRow.position", cascade="all, delete-orphan")
    questions: Mapped[list[DraftQuestion]] = relationship(back_populates="session", order_by="DraftQuestion.id", cascade="all, delete-orphan")
    sources: Mapped[list[SourceItem]] = relationship(back_populates="session", order_by="SourceItem.id")


class IntakeMessage(Base):
    __tablename__ = "intake_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("intake_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # operator | assistant | system
    text: Mapped[str] = mapped_column(Text, default="")
    source_item_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    session: Mapped[IntakeSession] = relationship(back_populates="messages")


class DraftRow(Base):
    """A proposed new product row or a correction to an existing one.

    ``fields_json`` maps field name to {value, state, origin, evidence, candidates,
    history}. State is explicit, defaulted, suggested, missing, conflicting or
    confirmed (set by the operator).
    """
    __tablename__ = "draft_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("intake_sessions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30), default="new_item")  # new_item | update_item
    target_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id"), index=True)
    target_revision: Mapped[int | None] = mapped_column(Integer)
    fields_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | committed | excluded
    committed_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __mapper_args__ = {"version_id_col": revision}
    session: Mapped[IntakeSession] = relationship(back_populates="rows")


class DraftQuestion(Base):
    __tablename__ = "draft_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("intake_sessions.id"), index=True)
    draft_row_id: Mapped[int | None] = mapped_column(ForeignKey("draft_rows.id"), index=True)
    field: Mapped[str] = mapped_column(String(60))
    key: Mapped[str] = mapped_column(String(120), index=True)  # stable identity: row:field:reason
    text: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | answered | resolved | dismissed
    answer_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session: Mapped[IntakeSession] = relationship(back_populates="questions")


class OperationReceipt(Base):
    """Result of an idempotent business operation; a retry returns this record."""
    __tablename__ = "operation_receipts"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("intake_sessions.id"), index=True)
    session_revision: Mapped[int | None] = mapped_column(Integer)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    """Durable background work. The table, not memory, is the record of pending work."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued | running | done | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WorkerLease(Base):
    """Single-owner lease so only one worker process drains the queue."""
    __tablename__ = "worker_leases"
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- External connections (Google Drive / Gmail) -----------------------------------

class Connection(Base):
    """An app-level account connection. Tokens are stored in the secret store, never here."""
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("provider", "account_email", name="uq_connection_account"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), default="google")
    account_email: Mapped[str] = mapped_column(String(320))
    account_subject: Mapped[str | None] = mapped_column(String(100))  # Stable provider account id
    granted_scopes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="connected")  # connected | paused | needs_reconnect | disconnected
    status_reason: Mapped[str | None] = mapped_column(Text)
    secret_ref: Mapped[str] = mapped_column(String(100))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    sources: Mapped[list[ConnectionSource]] = relationship(back_populates="connection", order_by="ConnectionSource.id")


class ConnectionSource(Base):
    """A deliberately selected input (Drive file/folder, Gmail query) or output destination."""
    __tablename__ = "connection_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # drive_file | drive_folder | gmail_query | drive_outputs | drive_recovery
    remote_id: Mapped[str | None] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(300), default="")
    query: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    checkpoint: Mapped[str | None] = mapped_column(Text)
    checkpoint_kind: Mapped[str | None] = mapped_column(String(40))  # drive_changes | drive_listing | gmail_history | gmail_full
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    connection: Mapped[Connection] = relationship(back_populates="sources")


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("connection_sources.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | partial | failed
    seen: Mapped[int] = mapped_column(Integer, default=0)
    captured: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)


class OutboxItem(Base):
    """Outgoing work to a remote destination (exports, recovery archives). Queued while offline."""
    __tablename__ = "outbox_items"
    __table_args__ = (UniqueConstraint("destination_id", "content_sha256", name="uq_outbox_content"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    destination_id: Mapped[int] = mapped_column(ForeignKey("connection_sources.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # export | recovery
    filename: Mapped[str] = mapped_column(String(300))
    local_path: Mapped[str] = mapped_column(String(1000))
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(20), default="queued")  # queued | uploading | uploaded | verified | failed
    remote_id: Mapped[str | None] = mapped_column(String(300))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


ModelDict = dict[str, Any]
