from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.calculations import signed_payment_amount, ZERO, item_amounts, money
from app.models import AllocationReview, Customer, FactoryPayment, ImportChange, ImportReview, Order, OrderItem, Payment, PaymentAllocation, ReadyBatch, Season
from app.services import customer_ready_due, customer_statement, factory_ready_due, ready_collection_status


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
        due = money(sum((customer_ready_due(item) for item in batch.items), ZERO))
        factory_paid = money(sum(session.scalars(select(FactoryPayment.amount).where(FactoryPayment.ready_batch_id == batch.id, FactoryPayment.stage == "ready")).all(), ZERO))
        collection = ready_collection_status(session, batch.id)
        factory_due = factory_ready_due(batch.items)
        missing = money(sum((max(required - collection.collected.get(customer_id, ZERO), ZERO)
                             for customer_id, required in collection.requirements.items()), ZERO))
        if due > ZERO and collection.completion_date is None:
            tasks.append(OperationTask("warning", f"Collect payments for {batch.name}", f"Customer-by-customer Ready collections are incomplete; {missing:,.2f} remains. Resolve any unallocated goods.", f"/reports/customers-ready/{batch.id}.zip", "Generate affected PDFs"))
        elif factory_due > ZERO and factory_paid < factory_due:
            tasks.append(OperationTask("error", f"Factory payment ready for {batch.name}", f"Ready collections are complete; wholesale factory liability remaining is {money(factory_due-factory_paid):,.2f}.", "/ready-batches", "Record factory payment"))
        unshipped = sum(1 for item in batch.items if item.status != "Cancelled" and item.shipment_link is None)
        if factory_due > ZERO and factory_paid >= factory_due and collection.completion_date and unshipped:
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
