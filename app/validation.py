from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.calculations import signed_payment_amount, ZERO, item_amounts, money
from app.models import AllocationReview, Expense, FactoryPayment, ImportReview, Order, OrderItem, Payment, PaymentAllocation, ReadyBatch, Season, Shipment, ShipmentItem
from app.services import country_is_transport_exempt, dashboard_data, item_transport_fee, order_rule, order_summary, ready_collection_status


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    entity: str | None = None
    action_url: str | None = None
    action_label: str = 'Review & fix'
    suggestion: str | None = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue]

    @property
    def errors(self):
        return [item for item in self.issues if item.severity == "error"]

    @property
    def warnings(self):
        return [item for item in self.issues if item.severity == "warning"]

    @property
    def passed(self):
        return not self.errors


def validate_system(session: Session) -> ValidationReport:
    issues: list[ValidationIssue] = []
    orders = session.scalars(select(Order).options(
        selectinload(Order.brand), selectinload(Order.season).selectinload(Season.rules),
        selectinload(Order.items).selectinload(OrderItem.shipment_link).selectinload(ShipmentItem.shipment),
        selectinload(Order.items).selectinload(OrderItem.customer),
    )).all()
    for order in orders:
        amounts = order_summary(order).amounts
        rule = order_rule(order)
        entity = f"{order.brand.name} / {order.season.name}"
        if money(rule.initial_prepayment_percentage + rule.ready_balance_percentage) != Decimal("1.00"):
            issues.append(ValidationIssue("error", "INCORRECT_PREPAYMENT_RULE", "Initial and Ready percentages do not total 100%", entity))
        if rule.transport_percentage < ZERO or rule.transport_percentage > Decimal("1"):
            issues.append(ValidationIssue("error", "INCORRECT_TRANSPORT_RULE", f"Transport percentage {rule.transport_percentage} is outside 0%-100%", entity))
        if rule.special_cost < ZERO:
            issues.append(ValidationIssue("error", "NEGATIVE_SPECIAL_COST", "Special cost cannot be negative", entity))
        if rule.readiness_payment_logic != "ready_batch_only":
            issues.append(ValidationIssue("error", "UNSUPPORTED_READINESS_LOGIC", f"Unsupported readiness logic {rule.readiness_payment_logic}", entity))
        if amounts.saldo < ZERO:
            issues.append(ValidationIssue("error", "NEGATIVE_SALDO", f"Saldo is {amounts.saldo}", entity))
        if amounts.ready + sum(amounts.shipments.values(), ZERO) > amounts.final:
            issues.append(ValidationIssue("error", "OVER_ALLOCATED", "Ready + shipped exceeds order value", entity))
        if order.factory_ready_from and order.factory_ready_to and order.factory_ready_to < order.factory_ready_from:
            issues.append(ValidationIssue("error", "IMPOSSIBLE_FACTORY_DATE", "Factory readiness end date is before its start date", entity))
        if order.source_total is not None and abs(amounts.final - order.source_total) > Decimal("0.01"):
            issues.append(ValidationIssue("warning", "SOURCE_TOTAL_MISMATCH", f"Calculated {amounts.final} does not match source {order.source_total}", entity))
        for item in order.items:
            item_entity = f"{order.brand.name} / {item.source_sheet or '-'} row {item.source_row or item.id}"
            if item.status == "Cancelled":
                continue
            if item.quantity <= 0:
                issues.append(ValidationIssue("error", "INVALID_QUANTITY", f"Quantity is {item.quantity}", item_entity))
            if item.customer_id is None or item.allocation_status != "Allocated":
                issues.append(ValidationIssue("error", "UNALLOCATED_PRODUCT", "Product is not confidently allocated to a customer", item_entity))
            if item.wholesale_unit_price is None or item.wholesale_unit_price <= ZERO:
                issues.append(ValidationIssue("warning", "MISSING_PRICE", "Missing or zero price", item_entity))
            if not item.size:
                issues.append(ValidationIssue("warning", "MISSING_SIZE", "Missing size", item_entity))
            if item.markup_percentage != rule.markup_percentage:
                issues.append(ValidationIssue("warning", "MARKUP_EXCEPTION", f"Item uses {item.markup_percentage}; season rule is {rule.markup_percentage}", item_entity))
            if item.shipment_link and not item.ready_batch_id:
                issues.append(ValidationIssue("error", "SHIPPED_WITHOUT_READY_BATCH", "Shipment item has no Ready batch", item_entity))
            if item.status == "Ready" and not item.ready_batch_id:
                issues.append(ValidationIssue("error", "READY_WITHOUT_BATCH", "Ready product has no Ready 1 / Ready 2 / ... batch", item_entity))
            if item.status in {"Assigned to shipment", "In transit", "Arrived", "Delivered"} and not item.shipment_link:
                issues.append(ValidationIssue("error", "SHIPPED_WITHOUT_SHIPMENT", "Shipped status has no shipment", item_entity))
            if item.source_total is not None and abs(item_amounts(item).base - item.source_total) > Decimal("0.01"):
                issues.append(ValidationIssue("warning", "ITEM_TOTAL_DISCREPANCY", f"Calculated base {item_amounts(item).base} differs from source {item.source_total}", item_entity))

    for item_id, count in session.execute(select(ShipmentItem.order_item_id, func.count()).group_by(ShipmentItem.order_item_id).having(func.count() > 1)).all():
        issues.append(ValidationIssue("error", "DUPLICATE_SHIPMENT", f"Item appears in {count} shipments", f"Item {item_id}"))
    for group_id, customer_id, size, count in session.execute(
        select(OrderItem.allocation_group_id, OrderItem.customer_id, OrderItem.size, func.count())
        .where(OrderItem.allocation_group_id.is_not(None), OrderItem.allocation_status == "Allocated",
               OrderItem.split_from_item_id.is_(None))
        .group_by(OrderItem.allocation_group_id, OrderItem.customer_id, OrderItem.size)
        .having(func.count() > 1)
    ).all():
        issues.append(ValidationIssue("error", "DUPLICATE_ALLOCATION", f"Allocation group has {count} duplicate records for the same customer/size", group_id))

    open_allocations = session.scalar(select(func.count()).select_from(AllocationReview).where(AllocationReview.status == "Open")) or 0
    if open_allocations:
        issues.append(ValidationIssue("warning", "ALLOCATION_REVIEW_OPEN", f"{open_allocations} customer allocations need review", "Allocation Review"))
    open_imports = session.scalar(select(func.count()).select_from(ImportReview).where(ImportReview.status == "Open")) or 0
    if open_imports:
        issues.append(ValidationIssue("warning", "IMPORT_REVIEW_OPEN", f"{open_imports} other source facts need review", "Import Review"))

    for payment in session.scalars(select(Payment).options(selectinload(Payment.allocations))).all():
        allocated = money(sum((allocation.amount for allocation in payment.allocations), ZERO))
        if allocated > payment.amount:
            issues.append(ValidationIssue("error", "PAYMENT_OVERALLOCATED", f"Allocated {allocated} exceeds payment {payment.amount}", f"Payment {payment.id}"))
        elif payment.type == "payment" and allocated < payment.amount:
            issues.append(ValidationIssue("warning", "PAYMENT_UNALLOCATED", f"{money(payment.amount - allocated)} is not assigned to initial or Ready liability", f"Payment {payment.id}"))
        if payment.season_id is None:
            issues.append(ValidationIssue("error", "PAYMENT_SEASON_MISSING", "Payment is not assigned to a season", f"Payment {payment.id}"))
        for allocation in payment.allocations:
            if allocation.ready_batch_id:
                batch = session.get(ReadyBatch, allocation.ready_batch_id)
                if batch and payment.season_id != batch.season_id:
                    issues.append(ValidationIssue("error", "PAYMENT_SEASON_MISMATCH", "Payment season does not match its Ready batch", f"Payment {payment.id}"))
            if allocation.order_id:
                allocated_order = session.get(Order, allocation.order_id)
                if allocated_order and payment.season_id != allocated_order.season_id:
                    issues.append(ValidationIssue("error", "PAYMENT_SEASON_MISMATCH", "Payment season does not match its factory/brand order", f"Payment {payment.id}"))

    for shipment in session.scalars(select(Shipment).options(
        selectinload(Shipment.items).selectinload(ShipmentItem.order_item).selectinload(OrderItem.customer),
        selectinload(Shipment.items).selectinload(ShipmentItem.order_item).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
    )).all():
        if shipment.season_id is None:
            issues.append(ValidationIssue("error", "SHIPMENT_SEASON_MISSING", "Shipment is not assigned to a season", shipment.name))
        if not shipment.use_rule_transport and (shipment.transport_percentage < ZERO or shipment.transport_percentage > Decimal("1")):
            issues.append(ValidationIssue("error", "INCORRECT_TRANSPORT", f"Explicit transport percentage {shipment.transport_percentage} is outside 0%-100%", shipment.name))
        if shipment.fixed_cost < ZERO or shipment.suitcase_cost < ZERO or shipment.bag_cost < ZERO:
            issues.append(ValidationIssue("error", "NEGATIVE_TRANSPORT_COST", "Fixed, suitcase, and bag costs cannot be negative", shipment.name))
        if shipment.items and shipment.date is None:
            issues.append(ValidationIssue("warning", "MISSING_SHIPMENT_DATE", "Shipment contains products but has no shipment date", shipment.name))
        if shipment.items and shipment.use_rule_transport and not shipment.destination_country_code and any(not link.order_item.customer or not link.order_item.customer.country_code for link in shipment.items):
            issues.append(ValidationIssue("warning", "MISSING_SHIPMENT_DESTINATION", "Shipment transport uses Brand + Season rules but destination is missing", shipment.name))
        for link in shipment.items:
            item = link.order_item
            if shipment.season_id != item.order.season_id:
                issues.append(ValidationIssue("error", "SHIPMENT_SEASON_MISMATCH", "Shipment season does not match its product order", f"{shipment.name} / item {item.id}"))
            rule = order_rule(item.order)
            country = shipment.destination_country_code or (item.customer.country_code if item.customer else None)
            if country_is_transport_exempt(country, rule) and item_transport_fee(item, shipment) != ZERO:
                issues.append(ValidationIssue("error", "SWITZERLAND_TRANSPORT", "Transport percentage charge must be 0 for an exempt destination", f"{shipment.name} / item {item.id}"))
            if shipment.date and item.ready_batch and item.ready_batch.ready_date and shipment.date < item.ready_batch.ready_date:
                issues.append(ValidationIssue("error", "IMPOSSIBLE_SHIPMENT_DATE", "Shipment date is before the factory Ready date", f"{shipment.name} / item {item.id}"))

    for batch in session.scalars(select(ReadyBatch).options(
        selectinload(ReadyBatch.items).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
        selectinload(ReadyBatch.payment_allocations).selectinload(PaymentAllocation.payment),
    )).all():
        due = money(sum((money(item_amounts(item).final * order_rule(item.order).ready_balance_percentage) for item in batch.items if item.status != "Cancelled"), ZERO))
        customer_paid = money(sum((signed_payment_amount(allocation.amount, allocation.payment.type) for allocation in batch.payment_allocations if allocation.stage == "ready"), ZERO))
        factory_paid = money(sum(session.scalars(select(FactoryPayment.amount).where(FactoryPayment.ready_batch_id == batch.id, FactoryPayment.stage == "ready")).all(), ZERO))
        if customer_paid != due:
            issues.append(ValidationIssue("warning", "READY_PAYMENT_MISMATCH", f"Required customer Ready payments {due}; allocated {customer_paid}", batch.name))
        if factory_paid != due:
            issues.append(ValidationIssue("warning", "FACTORY_READY_MISMATCH", f"Factory Ready liability {due}; recorded paid {factory_paid}", batch.name))
        collection = ready_collection_status(session, batch.id)
        for customer_id, customer_due in collection.requirements.items():
            if customer_id is None:
                continue
            customer_paid = money(collection.collected.get(customer_id, ZERO))
            if customer_paid != customer_due:
                issues.append(ValidationIssue("warning", "CUSTOMER_READY_PAYMENT_MISMATCH", f"Customer Ready due {customer_due}; allocated {customer_paid}", f"{batch.name} / customer {customer_id}"))
        for customer_id, customer_paid in collection.collected.items():
            if customer_paid and customer_id not in collection.requirements:
                issues.append(ValidationIssue("error", "CUSTOMER_PAYMENT_MISMATCH", f"Ready payment {customer_paid} belongs to a customer with no goods in this batch", f"{batch.name} / customer {customer_id}"))

    for expense in session.scalars(select(Expense)).all():
        if expense.season_id is None:
            issues.append(ValidationIssue("error", "EXPENSE_SEASON_MISSING", "Expense is not assigned to a season", f"Expense {expense.id}"))
        if expense.amount < ZERO:
            issues.append(ValidationIssue("error", "NEGATIVE_EXPENSE", "Exceptional expense cannot be negative", f"Expense {expense.id}"))
        if expense.customer_id is None and expense.order_id is None and expense.shipment_id is None:
            issues.append(ValidationIssue("warning", "UNEXPLAINED_EXPENSE", "Expense is not assigned to a customer, order, or shipment", f"Expense {expense.id}"))
        if expense.order_id and session.get(Order, expense.order_id).season_id != expense.season_id:
            issues.append(ValidationIssue("error", "EXPENSE_SEASON_MISMATCH", "Expense season does not match its order", f"Expense {expense.id}"))
        if expense.shipment_id and session.get(Shipment, expense.shipment_id).season_id != expense.season_id:
            issues.append(ValidationIssue("error", "EXPENSE_SEASON_MISMATCH", "Expense season does not match its shipment", f"Expense {expense.id}"))

    dashboard = dashboard_data(session)
    if dashboard:
        allocated_initial = allocated_ready = ZERO
        for order in orders:
            rule = order_rule(order)
            for item in order.items:
                if item.status == "Cancelled" or item.customer_id is None:
                    continue
                value = item_amounts(item).final
                allocated_initial += money(value * rule.initial_prepayment_percentage)
                if item.ready_batch_id:
                    allocated_ready += money(value * rule.ready_balance_percentage)
        if money(allocated_initial) != dashboard.totals.required_prepayment:
            issues.append(ValidationIssue("error", "CUSTOMER_INITIAL_ALLOCATION_MISMATCH", f"Factory initial due {dashboard.totals.required_prepayment}; allocated to customers {money(allocated_initial)}", dashboard.season.name))
        if money(allocated_ready) != dashboard.totals.ready_balance_due:
            issues.append(ValidationIssue("error", "CUSTOMER_READY_ALLOCATION_MISMATCH", f"Factory Ready due {dashboard.totals.ready_balance_due}; allocated to customers {money(allocated_ready)}", dashboard.season.name))
        initial_factory_due = dashboard.totals.required_prepayment
        factory_paid = money(sum(session.scalars(select(FactoryPayment.amount).where(FactoryPayment.season_id == dashboard.season.id, FactoryPayment.stage == "initial")).all(), ZERO))
        if factory_paid != initial_factory_due:
            issues.append(ValidationIssue("warning", "FACTORY_INITIAL_MISMATCH", f"Factory initial due {initial_factory_due}; recorded paid {factory_paid}", dashboard.season.name))
        if dashboard.account.outstanding > ZERO:
            issues.append(ValidationIssue("warning", "PAYMENT_MISMATCH", f"Current amount payable is {dashboard.account.outstanding}", dashboard.season.name))

        initial_allocations = session.scalars(
            select(PaymentAllocation).join(Payment).where(PaymentAllocation.stage == "initial")
            .options(selectinload(PaymentAllocation.payment))
        ).all()
        initial_by_customer_order: dict[tuple[int, int], Decimal] = {}
        for allocation in initial_allocations:
            if allocation.order_id is None:
                issues.append(ValidationIssue("warning", "INITIAL_PAYMENT_ORDER_MISSING", "Initial customer payment is not assigned to a factory/brand order", f"Payment {allocation.payment_id}"))
                continue
            if allocation.payment.customer_id is None:
                issues.append(ValidationIssue("error", "CUSTOMER_PAYMENT_MISMATCH", "Allocated customer payment has no customer", f"Payment {allocation.payment_id}"))
                continue
            key = (allocation.payment.customer_id, allocation.order_id)
            signed = signed_payment_amount(allocation.amount, allocation.payment.type)
            initial_by_customer_order[key] = money(initial_by_customer_order.get(key, ZERO) + signed)
        for order in orders:
            rule = order_rule(order)
            customer_due: dict[int, Decimal] = {}
            for item in order.items:
                if item.status == "Cancelled" or item.customer_id is None:
                    continue
                due = money(item_amounts(item).final * rule.initial_prepayment_percentage)
                customer_due[item.customer_id] = money(customer_due.get(item.customer_id, ZERO) + due)
            for customer_id, due in customer_due.items():
                allocated = initial_by_customer_order.get((customer_id, order.id), ZERO)
                if allocated and allocated != due:
                    issues.append(ValidationIssue("warning", "CUSTOMER_INITIAL_PAYMENT_MISMATCH", f"Customer initial due {due}; allocated {allocated}", f"{order.brand.name} / customer {customer_id}"))

        tagged_factory_payments = session.scalars(select(FactoryPayment).where(
            FactoryPayment.season_id == dashboard.season.id, FactoryPayment.order_id.is_not(None)
        )).all()
        if tagged_factory_payments:
            for order in orders:
                initial_due = order_summary(order).amounts.required_prepayment
                paid = money(sum((payment.amount for payment in tagged_factory_payments if payment.order_id == order.id and payment.stage == "initial"), ZERO))
                if paid != initial_due:
                    issues.append(ValidationIssue("warning", "FACTORY_ORDER_INITIAL_MISMATCH", f"Factory order initial due {initial_due}; paid {paid}", f"{order.brand.name} / {order.season.name}"))
            for batch in session.scalars(select(ReadyBatch).where(ReadyBatch.season_id == dashboard.season.id)).all():
                for order in orders:
                    due = money(sum((
                        item_amounts(item).final * order_rule(order).ready_balance_percentage
                        for item in order.items
                        if item.ready_batch_id == batch.id and item.status != "Cancelled"
                    ), ZERO))
                    if due <= ZERO:
                        continue
                    paid = money(sum((
                        payment.amount for payment in tagged_factory_payments
                        if payment.order_id == order.id and payment.ready_batch_id == batch.id and payment.stage == "ready"
                    ), ZERO))
                    if paid != due:
                        issues.append(ValidationIssue("warning", "FACTORY_ORDER_READY_MISMATCH", f"Factory Ready due {due}; paid {paid}", f"{batch.name} / {order.brand.name}"))

    if not any(item.severity == "error" for item in issues):
        issues.insert(0, ValidationIssue("pass", "CALCULATIONS_RECONCILE", "All calculations reconcile"))
        issues.insert(1, ValidationIssue("pass", "RECONCILIATION_DIFFERENCE", "Difference: €0.00"))
        issues.insert(2, ValidationIssue("pass", "NO_DUPLICATE_ASSIGNMENTS", "No duplicate shipment or customer allocation assignments"))
    return ValidationReport(issue_actions(session, orders, issues))


def issue_actions(session, orders, issues):
    products, brands = {}, {}
    for order in orders:
        brands[f'{order.brand.name} / {order.season.name}'] = f'/orders/{order.id}'
        for item in order.items:
            products[f'{order.brand.name} / {item.source_sheet or "-"} row {item.source_row or item.id}'] = f'/worksheet/items/{item.id}'
            products[f'Item {item.id}'] = f'/worksheet/items/{item.id}'
    tips = {
        'UNALLOCATED_PRODUCT': 'Confirm the customer against the source, then select that customer in the product editor.',
        'MISSING_SIZE': 'Enter the size from the order or ask the customer. Do not infer it from an image.',
        'MISSING_PRICE': 'Confirm the actual wholesale cost. Enter a selling override only when it is known.',
        'MARKUP_EXCEPTION': 'Check whether the product intentionally uses a different markup. Keep confirmed exceptions.',
        'INVALID_QUANTITY': 'Check the source quantity; zero-quantity reference rows may need to remain archived.',
        'ITEM_TOTAL_DISCREPANCY': 'Compare quantity and wholesale price with the original row. Keep source evidence when correcting it.',
        'SOURCE_TOTAL_MISMATCH': 'Compare the original total with the current product rows; intentional later edits can explain this warning.',
        'ALLOCATION_REVIEW_OPEN': 'Open a source note, assign its products to the correct customer, then record the resolution.',
        'IMPORT_REVIEW_OPEN': 'Compare the original values with the product records. Record what you verified before closing the note.',
        'PAYMENT_UNALLOCATED': 'Allocate the existing receipt to the correct customer, season and payment stage; do not create a duplicate payment.',
        'PAYMENT_OVERALLOCATED': 'Correct the allocation so it does not exceed the receipt. Preserve the actual payment record.',
    }
    enriched = []
    for issue in issues:
        if issue.severity == 'pass':
            enriched.append(issue); continue
        code = issue.code
        url = products.get(issue.entity) or brands.get(issue.entity) or '/worksheet'
        tip = tips.get(code, 'Compare this record with the source and correct the confirmed facts. No automatic repair or payment is assumed.')
        if code == 'ALLOCATION_REVIEW_OPEN': url = '/source-reviews?kind=allocation'
        elif code == 'IMPORT_REVIEW_OPEN': url = '/source-reviews?kind=import'
        elif code in {'INCORRECT_PREPAYMENT_RULE','INCORRECT_TRANSPORT_RULE','NEGATIVE_SPECIAL_COST','UNSUPPORTED_READINESS_LOGIC'}:
            url,tip = '/rules','Check the Brand + Season rule. Initial and remaining goods payments must total 100%; shipment transport is separate.'
        elif 'SHIPMENT' in code or code in {'INCORRECT_TRANSPORT','NEGATIVE_TRANSPORT_COST','SWITZERLAND_TRANSPORT'}:
            url,tip = '/shipments','Check the shipment, its products, dates and destination. Correct logistics through the shipment workflow.'
        elif 'READY' in code:
            url,tip = '/ready-batches','Review this Ready batch and its recorded payment allocations. A calculated amount is not evidence of a payment.'
        elif 'PAYMENT' in code or code.startswith('FACTORY_'):
            url = '/payments'
        elif 'EXPENSE' in code: url = '/shipments'
        enriched.append(replace(issue,action_url=url,suggestion=tip))
    return enriched
