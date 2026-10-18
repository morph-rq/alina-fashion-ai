from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_change
from app.calculations import ZERO, money
from app.models import Brand, BrandSeasonRule, Customer, Order, OrderItem, Season


SEASON_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,28}[A-Za-z0-9]$")
DEFAULT_INITIAL = Decimal("0.50")
DEFAULT_READY = Decimal("0.50")
DEFAULT_TRANSPORT = Decimal("0.05")


@dataclass(frozen=True)
class CustomerDraft:
    code: str
    name: str | None
    country_code: str | None


@dataclass(frozen=True)
class BrandDraft:
    name: str
    markup_percentage: Decimal
    initial_percentage: Decimal
    ready_percentage: Decimal
    transport_percentage: Decimal
    exempt_countries: str
    receipt_days: int


@dataclass(frozen=True)
class ProductDraft:
    source_row: int
    brand_name: str
    customer_code: str
    article: str | None
    name: str | None
    color: str | None
    size: str | None
    quantity: int
    wholesale_unit_price: Decimal
    markup_percentage: Decimal
    base_total: Decimal
    final_total: Decimal
    initial_due: Decimal


@dataclass
class SeasonDraft:
    season_name: str
    customers: list[CustomerDraft] = field(default_factory=list)
    brands: list[BrandDraft] = field(default_factory=list)
    products: list[ProductDraft] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def order_total(self) -> Decimal:
        return money(sum((row.final_total for row in self.products), ZERO))

    @property
    def initial_due(self) -> Decimal:
        return money(sum((row.initial_due for row in self.products), ZERO))

    @property
    def can_create(self) -> bool:
        return not self.errors and bool(self.brands)


def _percentage(raw: str, label: str, errors: list[str], default: Decimal) -> Decimal:
    value = raw.strip()
    if not value:
        return default
    try:
        result = Decimal(value) / Decimal("100")
    except InvalidOperation:
        errors.append(f"{label} must be a number between 0 and 100")
        return default
    if result < ZERO or result > Decimal("1"):
        errors.append(f"{label} must be between 0 and 100")
        return default
    return result


def _normalized_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def _find_season(session: Session, name: str) -> Season | None:
    return session.scalar(select(Season).where(func.lower(Season.name) == name.lower()))


def _copied_rules(session: Session, season_name: str) -> dict[str, BrandSeasonRule]:
    if not season_name.strip():
        return {}
    source = _find_season(session, season_name.strip())
    if source is None:
        return {}
    rules = session.scalars(
        select(BrandSeasonRule)
        .join(Brand)
        .where(BrandSeasonRule.season_id == source.id)
    ).all()
    return {rule.brand.name.casefold(): rule for rule in rules}


def build_season_draft(
    session: Session,
    *,
    season_name: str,
    primary_customer_code: str,
    primary_customer_name: str,
    primary_country_code: str,
    additional_customers: str,
    brand_rows: str,
    product_rows: str,
    copy_rules_from: str,
    default_markup: str,
    initial_percentage: str,
    ready_percentage: str,
    transport_percentage: str,
    exempt_countries: str,
    receipt_days: int,
) -> SeasonDraft:
    normalized_season = season_name.strip().upper()
    draft = SeasonDraft(season_name=normalized_season)
    if not normalized_season or not SEASON_NAME_RE.fullmatch(normalized_season):
        draft.errors.append("Season must be 2-30 letters/numbers, for example FW26 or SS27")
    elif _find_season(session, normalized_season):
        draft.errors.append(f"Season {normalized_season} already exists")

    default_markup_value = _percentage(default_markup, "Default markup", draft.errors, Decimal("0.35"))
    initial_value = _percentage(initial_percentage, "Initial prepayment", draft.errors, DEFAULT_INITIAL)
    ready_value = _percentage(ready_percentage, "Ready balance", draft.errors, DEFAULT_READY)
    transport_value = _percentage(transport_percentage, "Transport", draft.errors, DEFAULT_TRANSPORT)
    if money(initial_value + ready_value) != Decimal("1.00"):
        draft.errors.append("Initial and Ready percentages must add up to 100%")
    if receipt_days < 0 or receipt_days > 365:
        draft.errors.append("Expected receipt days must be between 0 and 365")

    primary_code = _normalized_code(primary_customer_code)
    primary_country = primary_country_code.strip().upper()
    if not primary_code:
        draft.errors.append("Primary customer code is required")
    if primary_country and not re.fullmatch(r"[A-Z]{2}", primary_country):
        draft.errors.append("Primary destination must be a two-letter country code, for example IT or CH")
    customers: dict[str, CustomerDraft] = {}
    if primary_code:
        customers[primary_code] = CustomerDraft(primary_code, primary_customer_name.strip() or None, primary_country or None)

    for line_number, raw_line in enumerate(additional_customers.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parts = [part.strip() for part in raw_line.split("|")]
        code = _normalized_code(parts[0] if parts else "")
        name = parts[1] if len(parts) > 1 and parts[1] else None
        country = parts[2].upper() if len(parts) > 2 and parts[2] else None
        if not code:
            draft.errors.append(f"Additional customer line {line_number}: customer code is required")
            continue
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            draft.errors.append(f"Additional customer line {line_number}: country must be a two-letter code")
            continue
        if code in customers:
            draft.errors.append(f"Customer {code} is listed more than once")
            continue
        customers[code] = CustomerDraft(code, name, country)
    draft.customers = list(customers.values())

    copied = _copied_rules(session, copy_rules_from)
    brands: dict[str, BrandDraft] = {}
    for line_number, raw_line in enumerate(brand_rows.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parts = [part.strip() for part in raw_line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            draft.errors.append(f"Brand line {line_number}: brand name is required")
            continue
        key = name.casefold()
        if key in brands:
            draft.errors.append(f"Brand {name} is listed more than once")
            continue
        source_rule = copied.get(key)
        markup = (
            _percentage(parts[1], f"{name} markup", draft.errors, default_markup_value)
            if len(parts) > 1 and parts[1]
            else (source_rule.markup_percentage if source_rule else default_markup_value)
        )
        brands[key] = BrandDraft(
            name=name,
            markup_percentage=markup,
            initial_percentage=source_rule.initial_prepayment_percentage if source_rule else initial_value,
            ready_percentage=source_rule.ready_balance_percentage if source_rule else ready_value,
            transport_percentage=source_rule.transport_percentage if source_rule else transport_value,
            exempt_countries=source_rule.transport_exempt_country_codes if source_rule else (exempt_countries.strip().upper() or "CH"),
            receipt_days=source_rule.expected_receipt_days if source_rule else receipt_days,
        )
    if not brands:
        draft.errors.append("Add at least one brand, one per line")
    draft.brands = list(brands.values())

    rows = list(csv.reader(io.StringIO(product_rows), delimiter="\t")) if product_rows.strip() else []
    if rows and rows[0] and rows[0][0].strip().casefold() in {"brand", "бренд"}:
        rows = rows[1:]
        source_offset = 2
    else:
        source_offset = 1
    seen_products: set[tuple[str, str, str, str]] = set()
    for row_index, raw in enumerate(rows, start=source_offset):
        if not any(cell.strip() for cell in raw):
            continue
        cells = [cell.strip() for cell in raw] + [""] * max(0, 9 - len(raw))
        brand_name, customer_raw, article, product_name, color, size, quantity_raw, price_raw, markup_raw = cells[:9]
        brand = brands.get(brand_name.casefold())
        if brand is None:
            draft.errors.append(f"Product row {row_index}: brand '{brand_name or '-'}' is not listed above")
            continue
        customer_code = _normalized_code(customer_raw) or primary_code
        if customer_code not in customers:
            draft.errors.append(f"Product row {row_index}: customer {customer_code or '-'} is not defined")
            continue
        if not article and not product_name:
            draft.errors.append(f"Product row {row_index}: article or product name is required")
            continue
        try:
            quantity = int(quantity_raw)
        except ValueError:
            draft.errors.append(f"Product row {row_index}: quantity must be a whole number")
            continue
        if quantity < 1:
            draft.errors.append(f"Product row {row_index}: quantity must be at least 1")
            continue
        try:
            price = money(Decimal(price_raw))
        except (InvalidOperation, ValueError):
            draft.errors.append(f"Product row {row_index}: wholesale price is invalid")
            continue
        if price <= ZERO:
            draft.errors.append(f"Product row {row_index}: wholesale price must be greater than zero")
            continue
        markup = _percentage(markup_raw, f"Product row {row_index} markup", draft.errors, brand.markup_percentage) if markup_raw else brand.markup_percentage
        duplicate_key = (brand.name.casefold(), customer_code, article.casefold(), size.casefold())
        if duplicate_key in seen_products:
            draft.errors.append(f"Product row {row_index}: duplicate allocation for {article or product_name} / {size or '-'} / {customer_code}; combine the quantity instead")
            continue
        seen_products.add(duplicate_key)
        base = money(price * quantity)
        final = money(base * (Decimal("1") + markup))
        draft.products.append(ProductDraft(
            source_row=row_index,
            brand_name=brand.name,
            customer_code=customer_code,
            article=article or None,
            name=product_name or None,
            color=color or None,
            size=size or None,
            quantity=quantity,
            wholesale_unit_price=price,
            markup_percentage=markup,
            base_total=base,
            final_total=final,
            initial_due=money(final * brand.initial_percentage),
        ))
    if not draft.products:
        draft.warnings.append("No products were pasted. The season and empty brand tables will still be created.")
    return draft


def create_season_workspace(
    session: Session,
    draft: SeasonDraft,
    *,
    source_user: str = "web user",
    staged_image_token: str | None = None,
    image_matches: dict[int, object] | None = None,
    image_match_methods: dict[int, str] | None = None,
) -> Season:
    if not draft.can_create:
        raise ValueError("Season draft contains validation errors")
    season = Season(name=draft.season_name)
    session.add(season)
    session.flush()

    customer_ids: dict[str, int] = {}
    for row in draft.customers:
        customer = session.scalar(select(Customer).where(func.lower(Customer.code) == row.code.lower()))
        if customer is None:
            customer = Customer(code=row.code, name=row.name, country_code=row.country_code)
            session.add(customer)
            session.flush()
        customer_ids[row.code] = customer.id

    orders: dict[str, Order] = {}
    for row in draft.brands:
        brand = session.scalar(select(Brand).where(func.lower(Brand.name) == row.name.lower()))
        if brand is None:
            brand = Brand(name=row.name, default_markup_percentage=row.markup_percentage)
            session.add(brand)
            session.flush()
        session.add(BrandSeasonRule(
            season_id=season.id,
            brand_id=brand.id,
            markup_percentage=row.markup_percentage,
            initial_prepayment_percentage=row.initial_percentage,
            ready_balance_percentage=row.ready_percentage,
            transport_percentage=row.transport_percentage,
            transport_exempt_country_codes=row.exempt_countries,
            expected_receipt_days=row.receipt_days,
            readiness_payment_logic="ready_batch_only",
            notes="Created by seasonal table wizard",
        ))
        order = Order(season_id=season.id, brand_id=brand.id, status="Ordered", source_sheet=row.name)
        session.add(order)
        session.flush()
        orders[row.name.casefold()] = order

    items_by_source_row: dict[int, OrderItem] = {}
    for row in draft.products:
        item = OrderItem(
            order_id=orders[row.brand_name.casefold()].id,
            customer_id=customer_ids[row.customer_code],
            article=row.article,
            name=row.name,
            color=row.color,
            size=row.size,
            quantity=row.quantity,
            wholesale_unit_price=row.wholesale_unit_price,
            markup_percentage=row.markup_percentage,
            status="Ordered",
            source_file="Season workspace wizard",
            source_sheet=row.brand_name,
            source_row=row.source_row,
            source_allocation_description=row.customer_code,
            allocation_status="Allocated",
        )
        session.add(item)
        items_by_source_row[row.source_row] = item

    created_image_paths = []
    if staged_image_token and image_matches:
        session.flush()
        from app.image_manager import persist_matches

        created_image_paths = persist_matches(
            staged_image_token,
            season.name,
            image_matches,
            items_by_source_row,
            image_match_methods,
        )

    record_change(
        session,
        entity_type="Season",
        entity_id=season.id,
        field=None,
        previous_value=None,
        new_value=season.name,
        source_user=source_user,
        description=f"Created {season.name} seasonal workspace with {len(draft.brands)} brand tables and {len(draft.products)} product rows",
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        for path in created_image_paths:
            path.unlink(missing_ok=True)
        raise
    if staged_image_token:
        from app.image_manager import discard_stage

        discard_stage(staged_image_token)
    return season


def intake_schema() -> dict[str, object]:
    """Machine-readable source-fact contract for a future intake assistant."""
    return {
        "version": 1,
        "purpose": "Collect source facts for a seasonal fashion order workspace",
        "calculation_policy": "AI may collect and normalize facts; only deterministic Decimal functions calculate money",
        "required": ["season_name", "primary_customer_code", "brands"],
        "customers": {"format": "CODE | Name | ISO country", "example": "SV | Sofia | CH"},
        "brands": {"format": "Brand | markup percent", "example": "Loro Piana Woman | 35"},
        "products": {
            "format": "tab-separated",
            "columns": ["Brand", "Customer", "Article", "Product", "Color", "Size", "Quantity", "Wholesale Price", "Markup %"],
        },
        "images": {
            "format": "JPG, PNG, or WEBP",
            "matching": "exact article/SKU filenames attach automatically; other filenames receive deterministic suggestions from article, product name, brand, and color",
            "example": "FAQ9434.jpg auto-matches article FAQ9434; navy-coat-front.jpg waits for a user-selected product",
            "uncertain_policy": "suggest for review and require explicit user confirmation; never guess or calculate with AI",
        },
        "derived_fields": ["base_total", "markup_amount", "final_total", "initial_due", "ready_due", "season_totals", "customer_balances"],
    }
