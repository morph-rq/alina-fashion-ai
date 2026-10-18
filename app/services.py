from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.calculations import AccountAmounts, OrderAmounts, ZERO, calculate_account, calculate_order, item_amounts, money, transport_fee
from app.models import BrandSeasonRule, Customer, Expense, FactoryPayment, Order, OrderItem, Payment, PaymentAllocation, ReadyBatch, Season, Shipment, ShipmentItem
from app.models import PurchaseGroup
from app.worksheet import split_cost


DEFAULT_RULE = SimpleNamespace(
    initial_prepayment_percentage=Decimal("0.50"),
    ready_balance_percentage=Decimal("0.50"),
    transport_percentage=Decimal("0.05"),
    special_cost=Decimal("0"),
    transport_exempt_country_codes="CH",
    expected_receipt_days=14,
    readiness_payment_logic="ready_batch_only",
)


def order_rule(order: Order):
    rules = getattr(order.season, "rules", []) if getattr(order, "season", None) else []
    return next((rule for rule in rules if rule.brand_id == order.brand_id), DEFAULT_RULE)


def country_is_transport_exempt(country_code: str | None, rule) -> bool:
    if not country_code:
        return False
    exemptions = {value.strip().upper() for value in (rule.transport_exempt_country_codes or "").split(",") if value.strip()}
    return country_code.strip().upper() in exemptions


@dataclass
class OrderSummary:
    order: Order
    amounts: OrderAmounts


@dataclass
class ShipmentSummary:
    shipment: Shipment
    merchandise_value: Decimal
    transport_fee: Decimal
    fixed_costs: Decimal
    total_fees: Decimal


@dataclass
class DashboardData:
    season: Season
    orders: list[OrderSummary]
    shipments: list[Shipment]
    shipment_summaries: dict[int, ShipmentSummary]
    totals: OrderAmounts
    expenses_total: Decimal
    shipment_fees_total: Decimal
    account: AccountAmounts
    factory_required: Decimal
    factory_paid: Decimal
    factory_account: AccountAmounts


def load_season(session: Session, season_name: str | None = None) -> Season | None:
    query = select(Season).options(
            selectinload(Season.rules),
            selectinload(Season.ready_batches),
            selectinload(Season.orders).selectinload(Order.brand),
            selectinload(Season.orders).selectinload(Order.items).selectinload(OrderItem.shipment_link).selectinload(ShipmentItem.shipment),
            selectinload(Season.orders).selectinload(Order.items).selectinload(OrderItem.customer),
            selectinload(Season.orders).selectinload(Order.items).selectinload(OrderItem.ready_batch),
        )
    if season_name:
        query = query.where(Season.name == season_name)
    else:
        query = query.order_by(Season.id.desc())
    return session.scalar(query.limit(1))


def order_summary(order: Order) -> OrderSummary:
    rule = order_rule(order)
    return OrderSummary(order=order, amounts=calculate_order(order.items, rule.initial_prepayment_percentage, rule.ready_balance_percentage))


def item_transport_fee(item: OrderItem, shipment: Shipment) -> Decimal:
    if item.status == "Cancelled":
        return ZERO
    rule = order_rule(item.order)
    country = shipment.destination_country_code or (item.customer.country_code if item.customer else None)
    if country_is_transport_exempt(country, rule):
        return ZERO
    percentage = rule.transport_percentage if shipment.use_rule_transport else shipment.transport_percentage
    return transport_fee(item_amounts(item).final, percentage)


def shipment_summary(shipment: Shipment) -> ShipmentSummary:
    active_items = [link.order_item for link in shipment.items if link.order_item.status != "Cancelled"]
    merchandise = money(sum((item_amounts(item).final for item in active_items), ZERO))
    if shipment.transport_eligible_value_override is not None and not shipment.use_rule_transport:
        calculated_transport = transport_fee(shipment.transport_eligible_value_override, shipment.transport_percentage)
    else:
        calculated_transport = money(sum((item_transport_fee(item, shipment) for item in active_items), ZERO))
    fixed = money(shipment.fixed_cost + shipment.suitcase_cost + shipment.bag_cost)
    return ShipmentSummary(shipment=shipment, merchandise_value=merchandise, transport_fee=calculated_transport, fixed_costs=fixed, total_fees=money(calculated_transport + fixed))


def dashboard_data(session: Session, season_name: str | None = None) -> DashboardData | None:
    season = load_season(session, season_name)
    if season is None:
        return None
    order_ids = {order.id for order in season.orders}
    shipments = list(session.scalars(
        select(Shipment).where(Shipment.season_id == season.id).options(
            selectinload(Shipment.items).selectinload(ShipmentItem.order_item).selectinload(OrderItem.customer),
            selectinload(Shipment.items).selectinload(ShipmentItem.order_item).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
        ).order_by(Shipment.date, Shipment.name)
    ).all())
    shipments = [shipment for shipment in shipments if any(link.order_item.order_id in order_ids for link in shipment.items)]
    summaries = [order_summary(order) for order in sorted(season.orders, key=lambda item: item.brand.name)]
    shipment_summaries = {item.id: shipment_summary(item) for item in shipments}
    totals = OrderAmounts()
    fields = ("base", "markup", "final", "required_prepayment", "ready_balance_due", "ready", "saldo", "current_merchandise_charge")
    for summary in summaries:
        for field_name in fields:
            setattr(totals, field_name, getattr(totals, field_name) + getattr(summary.amounts, field_name))
        for shipment_id, value in summary.amounts.shipments.items():
            totals.shipments[shipment_id] = totals.shipments.get(shipment_id, ZERO) + value
    for field_name in fields:
        setattr(totals, field_name, money(getattr(totals, field_name)))
    totals.shipments = {key: money(value) for key, value in totals.shipments.items()}
    expenses = session.scalars(select(Expense).where(Expense.season_id == season.id)).all()
    rule_special_costs = sum((order_rule(order).special_cost for order in season.orders), ZERO)
    rule_special_costs += sum(session.scalars(select(PurchaseGroup.special_cost).where(PurchaseGroup.season_id == season.id)), ZERO)
    expenses_total = money(sum((expense.amount for expense in expenses if not expense.order_id or expense.order_id in order_ids), ZERO) + rule_special_costs)
    shipment_fees_total = money(sum((item.total_fees for item in shipment_summaries.values()), ZERO))
    paid = sum(((-payment.amount if payment.type == "refund" else payment.amount) for payment in session.scalars(select(Payment).where(Payment.season_id == season.id)).all()), ZERO)
    account = calculate_account(paid, money(totals.current_merchandise_charge + expenses_total + shipment_fees_total))
    factory_required = money(totals.required_prepayment + totals.ready_balance_due)
    factory_paid = money(sum(session.scalars(
        select(FactoryPayment.amount).where(FactoryPayment.season_id == season.id)
    ).all(), ZERO))
    factory_account = calculate_account(factory_paid, factory_required)
    return DashboardData(
        season, summaries, shipments, shipment_summaries, totals, expenses_total,
        shipment_fees_total, account, factory_required, factory_paid, factory_account,
    )


@dataclass(frozen=True)
class ReadyCollectionStatus:
    requirements: dict[int | None, Decimal]
    collected: dict[int, Decimal]
    completion_date: date | None


def ready_collection_status(session: Session, batch_id: int) -> ReadyCollectionStatus:
    """Require every affected customer to satisfy their own Ready liability."""
    batch = session.scalar(
        select(ReadyBatch)
        .where(ReadyBatch.id == batch_id)
        .options(
            selectinload(ReadyBatch.items)
            .selectinload(OrderItem.order)
            .selectinload(Order.season)
            .selectinload(Season.rules)
        )
    )
    if batch is None:
        raise LookupError("Ready batch not found")
    requirements: dict[int | None, Decimal] = {}
    for item in batch.items:
        if item.status == "Cancelled":
            continue
        due = money(item_amounts(item).final * (item.purchase_group.shipment_percentage if item.purchase_group else order_rule(item.order).ready_balance_percentage))
        requirements[item.customer_id] = money(requirements.get(item.customer_id, ZERO) + due)

    allocations = list(session.scalars(
        select(PaymentAllocation)
        .join(Payment)
        .where(
            PaymentAllocation.ready_batch_id == batch_id,
            PaymentAllocation.stage == "ready",
            Payment.season_id == batch.season_id,
        )
        .options(selectinload(PaymentAllocation.payment))
        .order_by(Payment.date, Payment.id, PaymentAllocation.id)
    ).all())
    collected: dict[int, Decimal] = {}
    completion_by_customer: dict[int, date | None] = {}
    for allocation in allocations:
        customer_id = allocation.payment.customer_id
        if customer_id is None:
            continue
        signed = -allocation.amount if allocation.payment.type == "refund" else allocation.amount
        previous = collected.get(customer_id, ZERO)
        current = money(previous + signed)
        collected[customer_id] = current
        due = requirements.get(customer_id, ZERO)
        if due > ZERO and previous < due <= current:
            completion_by_customer[customer_id] = allocation.payment.date
        elif current < due:
            completion_by_customer[customer_id] = None

    if not requirements or None in requirements:
        completion_date = None
    else:
        completed_dates = []
        for customer_id, due in requirements.items():
            if due <= ZERO:
                continue
            if collected.get(customer_id, ZERO) < due or completion_by_customer.get(customer_id) is None:
                completion_date = None
                break
            completed_dates.append(completion_by_customer[customer_id])
        else:
            completion_date = max(completed_dates) if completed_dates else None
    return ReadyCollectionStatus(requirements, collected, completion_date)


@dataclass
class CustomerStatement:
    customer: Customer
    season: Season
    items: list[OrderItem]
    payments: list[Payment]
    ordered: Decimal
    initial_prepayment: Decimal
    ready: Decimal
    ready_balance_due: Decimal
    shipped: Decimal
    transport_costs: Decimal
    fixed_costs: Decimal
    allocated_costs: Decimal
    merchandise_charge: Decimal
    account: AccountAmounts
    shipments: dict[int, list[OrderItem]] = field(default_factory=dict)
    expected_receipts: dict[int, date | None] = field(default_factory=dict)
    shipment_costs: dict[int, Decimal] = field(default_factory=dict)
    shipment_expected_receipts: dict[int, date | None] = field(default_factory=dict)
    archived_items: list[OrderItem] = field(default_factory=list)
    group_special_costs: Decimal = ZERO


def customer_statement(session: Session, customer_id: int, season_name: str | None = None) -> CustomerStatement:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError("Customer not found")
    season = load_season(session, season_name)
    if season is None:
        raise LookupError("Season not found")
    items = [item for order in season.orders for item in order.items if item.customer_id == customer.id and item.status != "Cancelled"]
    ordered = initial = ready = ready_due = shipped = ZERO
    shipments: dict[int, list[OrderItem]] = {}
    per_order: dict[int, list[OrderItem]] = {}
    per_batch: dict[int, list[OrderItem]] = {}
    for item in items:
        value = item_amounts(item).final
        rule = order_rule(item.order)
        ordered += value
        group = item.purchase_group
        initial += money(value * (group.initial_percentage if group else rule.initial_prepayment_percentage))
        per_order.setdefault(item.order_id, []).append(item)
        if item.ready_batch_id:
            ready_due += money(value * (group.shipment_percentage if group else rule.ready_balance_percentage))
            per_batch.setdefault(item.ready_batch_id, []).append(item)
        if item.shipment_link:
            shipped += value
            shipments.setdefault(item.shipment_link.shipment_id, []).append(item)
        elif item.ready_batch_id or item.status == "Ready":
            ready += value

    transport_costs = fixed_costs = ZERO
    shipment_costs: dict[int, Decimal] = {}
    shipment_objects = session.scalars(select(Shipment).where(Shipment.season_id == season.id).options(
        selectinload(Shipment.items).selectinload(ShipmentItem.order_item).selectinload(OrderItem.customer),
        selectinload(Shipment.items).selectinload(ShipmentItem.order_item).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
    )).all()
    for shipment in shipment_objects:
        my_items = shipments.get(shipment.id, [])
        if not my_items:
            continue
        my_transport = money(sum((item_transport_fee(item, shipment) for item in my_items), ZERO))
        transport_costs += my_transport
        all_value = sum((item_amounts(link.order_item).final for link in shipment.items if link.order_item.status != "Cancelled"), ZERO)
        my_value = sum((item_amounts(item).final for item in my_items), ZERO)
        if all_value > ZERO:
            my_fixed = money((shipment.fixed_cost + shipment.suitcase_cost + shipment.bag_cost) * my_value / all_value)
            fixed_costs += my_fixed
        else:
            my_fixed = ZERO
        shipment_costs[shipment.id] = money(my_transport + my_fixed)

    direct_expenses = ZERO
    for expense in session.scalars(select(Expense).where(Expense.season_id == season.id)).all():
        if expense.customer_id == customer.id:
            direct_expenses += expense.amount
            if expense.shipment_id in shipments:
                shipment_costs[expense.shipment_id] = money(shipment_costs.get(expense.shipment_id, ZERO) + expense.amount)
        elif expense.customer_id is not None:
            continue
        elif expense.shipment_id in shipments:
            links = session.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id == expense.shipment_id)).all()
            total = sum((item_amounts(link.order_item).final for link in links), ZERO)
            mine = sum((item_amounts(item).final for item in shipments[expense.shipment_id]), ZERO)
            if total > ZERO:
                allocated_expense = money(expense.amount * mine / total)
                direct_expenses += allocated_expense
                shipment_costs[expense.shipment_id] = money(shipment_costs.get(expense.shipment_id, ZERO) + allocated_expense)
        elif expense.order_id in per_order:
            order = session.get(Order, expense.order_id)
            total = order_summary(order).amounts.final if order else ZERO
            mine = sum((item_amounts(item).final for item in per_order[expense.order_id]), ZERO)
            if total > ZERO:
                direct_expenses += money(expense.amount * mine / total)

    for order_id, my_items in per_order.items():
        order = my_items[0].order
        special_cost = order_rule(order).special_cost
        if special_cost <= ZERO:
            continue
        total = order_summary(order).amounts.final
        mine = sum((item_amounts(item).final for item in my_items), ZERO)
        if total > ZERO:
            direct_expenses += money(special_cost * mine / total)

    payments = list(session.scalars(select(Payment).where(
        Payment.customer_id == customer.id, Payment.season_id == season.id
    ).order_by(Payment.date, Payment.id)).all())
    paid = sum(((-payment.amount if payment.type == "refund" else payment.amount) for payment in payments), ZERO)
    expected_receipts: dict[int, date | None] = {}
    for batch_id, batch_items in per_batch.items():
        batch = session.get(ReadyBatch, batch_id)
        all_batch_items = [item for item in (batch.items if batch else []) if item.status != "Cancelled"]
        due = sum((money(item_amounts(item).final * (item.purchase_group.shipment_percentage if item.purchase_group else order_rule(item.order).ready_balance_percentage)) for item in all_batch_items), ZERO)
        collection = ready_collection_status(session, batch_id)
        if due > ZERO and collection.completion_date:
            days = max(order_rule(item.order).expected_receipt_days for item in all_batch_items)
            expected_receipts[batch_id] = collection.completion_date + timedelta(days=days)
        else:
            expected_receipts[batch_id] = None

    shipment_expected_receipts: dict[int, date | None] = {}
    for shipment_id, shipment_items in shipments.items():
        batch_ids = {item.ready_batch_id for item in shipment_items if item.ready_batch_id}
        dates = [expected_receipts.get(batch_id) for batch_id in batch_ids]
        shipment_expected_receipts[shipment_id] = max(dates) if dates and all(dates) else None

    group_special_costs = ZERO
    for group in session.scalars(select(PurchaseGroup).where(PurchaseGroup.season_id == season.id)).all():
        shares = split_cost(group.special_cost, sorted(group.items, key=lambda i:i.id), group.cost_method)
        group_special_costs += sum((shares[i.id] for i in group.items if i.customer_id == customer.id), ZERO)
    direct_expenses += group_special_costs
    merchandise_charge = money(initial + ready_due)
    allocated_costs = money(transport_costs + fixed_costs + direct_expenses)
    return CustomerStatement(
        customer=customer, season=season,
        items=sorted(items, key=lambda item: (item.order.brand.name, item.name or "", item.id)),
        payments=payments, ordered=money(ordered), initial_prepayment=money(initial), ready=money(ready),
        ready_balance_due=money(ready_due), shipped=money(shipped), transport_costs=money(transport_costs),
        fixed_costs=money(fixed_costs), allocated_costs=allocated_costs, merchandise_charge=merchandise_charge,
        account=calculate_account(paid, merchandise_charge + allocated_costs), shipments=shipments,
        expected_receipts=expected_receipts, shipment_costs=shipment_costs,
        shipment_expected_receipts=shipment_expected_receipts,
        archived_items=[i for order in season.orders for i in order.items if i.customer_id == customer.id and i.status == 'Cancelled'],
        group_special_costs=money(group_special_costs),
    )
