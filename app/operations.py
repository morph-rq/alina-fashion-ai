from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.calculations import signed_payment_amount, ZERO, item_amounts, money
from app.models import AllocationReview, Customer, FactoryPayment, ImportChange, ImportReview, Order, OrderItem, Payment, PaymentAllocation, ReadyBatch, Season
from app.services import customer_statement, order_rule


@dataclass(frozen=True)
class OperationTask:
    severity: str
    title: str
    detail: str
    action_url: str
    action_label: str


def operations_inbox(session: Session) -> list[OperationTask]:
    tasks: list[OperationTask] = []
    allocation_reviews = session.scalar(select(func.count()).select_from(AllocationReview).where(AllocationReview.status == "Open")) or 0
    if allocation_reviews:
        tasks.append(OperationTask("error", "Resolve customer allocations", f"{allocation_reviews} supplier rows cannot be assigned safely.", "/import#allocation-review", "Open Allocation Review"))
    import_reviews = session.scalar(select(func.count()).select_from(ImportReview).where(ImportReview.status == "Open")) or 0
    if import_reviews:
        tasks.append(OperationTask("warning", "Review imported source facts", f"{import_reviews} rows need a human decision.", "/import", "Open Import Review"))
    changed_reviews = session.scalar(select(func.count()).select_from(ImportChange).where(ImportChange.status == "Review")) or 0
    if changed_reviews:
        tasks.append(OperationTask("error", "Review supplier changes", f"{changed_reviews} changed rows were protected because operational history already exists.", "/import#import-changes", "Compare changes"))

    unallocated_payments = session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.type == "payment",
            ~Payment.allocations.any(),
        )
    ) or 0
    if unallocated_payments:
        tasks.append(OperationTask("warning", "Allocate customer payments", f"{unallocated_payments} payments are not linked to initial or Ready liabilities.", "/payments", "Match payments"))

    batches = session.scalars(select(ReadyBatch).options(
        selectinload(ReadyBatch.items).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
        selectinload(ReadyBatch.payment_allocations).selectinload(PaymentAllocation.payment),
    ).order_by(ReadyBatch.id)).all()
    for batch in batches:
        due = money(sum((item_amounts(item).final * order_rule(item.order).ready_balance_percentage for item in batch.items if item.status != "Cancelled"), ZERO))
        collected = money(sum((signed_payment_amount(allocation.amount, allocation.payment.type) for allocation in batch.payment_allocations if allocation.stage == "ready"), ZERO))
        factory_paid = money(sum(session.scalars(select(FactoryPayment.amount).where(FactoryPayment.ready_batch_id == batch.id, FactoryPayment.stage == "ready")).all(), ZERO))
        if due > ZERO and collected < due:
            tasks.append(OperationTask("warning", f"Collect payments for {batch.name}", f"Collected {collected:,.2f} of {due:,.2f}; {money(due-collected):,.2f} remains.", f"/reports/customers-ready/{batch.id}.zip", "Generate affected PDFs"))
        elif due > ZERO and factory_paid < due:
            tasks.append(OperationTask("error", f"Factory payment ready for {batch.name}", f"Customers are fully collected; factory Ready liability remaining is {money(due-factory_paid):,.2f}.", "/ready-batches", "Record factory payment"))
        unshipped = sum(1 for item in batch.items if item.status != "Cancelled" and item.shipment_link is None)
        if due > ZERO and factory_paid >= due and unshipped:
            tasks.append(OperationTask("info", f"Prepare shipment for {batch.name}", f"Factory liability is paid and {unshipped} product records are not yet assigned to a shipment.", "/shipments", "Create shipment"))

    outstanding = 0
    for customer in session.scalars(select(Customer)).all():
        try:
            if customer_statement(session, customer.id).account.outstanding > ZERO:
                outstanding += 1
        except LookupError:
            pass
    if outstanding:
        tasks.append(OperationTask("info", "Send outstanding statements", f"{outstanding} customers currently have an amount payable.", "/reports/customers-outstanding.zip", "Generate outstanding PDFs"))
    return tasks
