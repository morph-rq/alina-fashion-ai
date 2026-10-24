"""Pages of the simplified interface that combine existing records.

Nothing here writes data. Every form on these pages posts to the original routes
(``/payments``, ``/factory-payments``, ``/ready-batches…``, ``/shipments``, ``/expenses``,
``/groups…``, ``/distributors``, ``/credits/…``), so their validation, audit records and
duplicate-operation guards are unchanged.
"""
from __future__ import annotations

import secrets
from datetime import date

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.calculations import ZERO, item_amounts, money, signed_payment_amount
from app.database import get_db
from app.models import (AllocationReview, AuditLog, Brand, BrandSeasonRule, Connection, Customer, Distributor, Expense,
                        FactoryPayment, ImportChange, ImportReview, Job, Order, OrderItem, OutboxItem, Payment,
                        PaymentAllocation, ReadyBatch, Season, SellerCreditEntry, Shipment, ShipmentItem, SourceItem)
from app.models import IntakeSession
from app.services import customer_ready_due, dashboard_data, factory_ready_due, shipment_summary
from app.worksheet import worksheet_data

ATTENTION_STATES = ("needs_answers", "ready_for_review", "analysing", "collecting")


def seasons_newest_first(db: Session) -> list[Season]:
    return db.scalars(select(Season).order_by(Season.id.desc())).all()


def newest_season(db: Session) -> Season | None:
    return db.scalar(select(Season).order_by(Season.id.desc()).limit(1))


def pick_season(db: Session, value: str = "") -> Season | None:
    """Resolve ``?season=`` given as a name or an id; fall back to the newest season."""
    value = (value or "").strip()
    if not value:
        return newest_season(db)
    season = db.scalar(select(Season).where(Season.name == value))
    if season is None and value.isdigit():
        season = db.get(Season, int(value))
    if season is None:
        raise HTTPException(404, "Season not found")
    return season


def ready_rows(db: Session, season: Season | None) -> list[dict]:
    """Ready batch ledger rows, computed exactly as on the former Ready batches page."""
    if season is None:
        return []
    batches = db.scalars(
        select(ReadyBatch).where(ReadyBatch.season_id == season.id).options(
            selectinload(ReadyBatch.items).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
            selectinload(ReadyBatch.payment_allocations).selectinload(PaymentAllocation.payment),
        ).order_by(ReadyBatch.id)
    ).all()
    rows = []
    for batch in batches:
        value = money(sum((item_amounts(item).final for item in batch.items if item.status != "Cancelled"), ZERO))
        due = money(sum((customer_ready_due(item) for item in batch.items), ZERO))
        customer_paid = money(sum((signed_payment_amount(allocation.amount, allocation.payment.type)
                                   for allocation in batch.payment_allocations if allocation.stage == "ready"), ZERO))
        factory_paid = money(sum(db.scalars(select(FactoryPayment.amount).where(
            FactoryPayment.ready_batch_id == batch.id, FactoryPayment.stage == "ready")).all(), ZERO))
        factory_due = factory_ready_due(batch.items)
        rows.append({"batch": batch, "value": value, "due": due, "customer_paid": customer_paid,
                     "factory_due": factory_due, "factory_paid": factory_paid,
                     "factory_balance": max(money(factory_due - factory_paid), ZERO)})
    return rows


def season_orders(db: Session, season: Season | None) -> list[Order]:
    if season is None:
        return []
    return db.scalars(select(Order).where(Order.season_id == season.id)
                      .options(selectinload(Order.brand), selectinload(Order.season)).order_by(Order.id)).all()


def overview_context(db: Session, season_value: str = "") -> dict:
    from app import outbox
    from app.operations import operations_inbox
    from app.validation import validate_system

    season = pick_season(db, season_value)
    data = dashboard_data(db, season.name) if season else None
    # Overview must open even when one check cannot run; the failure is shown, not hidden.
    check_error = None
    try:
        report = validate_system(db)
    except Exception as exc:  # noqa: BLE001 - reported on the page
        report, check_error = None, f"{type(exc).__name__}: {exc}"
    try:
        tasks = operations_inbox(db)
    except Exception as exc:  # noqa: BLE001 - reported on the page
        tasks, check_error = [], check_error or f"{type(exc).__name__}: {exc}"
    sessions = db.scalars(select(IntakeSession).where(IntakeSession.state.in_(ATTENTION_STATES))
                          .order_by(IntakeSession.updated_at.desc()).limit(6)).all()
    source_counts = dict(db.execute(select(SourceItem.state, func.count()).where(SourceItem.parent_id.is_(None))
                                    .group_by(SourceItem.state)).all())
    failed_jobs = db.scalar(select(func.count()).select_from(Job).where(Job.state == "dead")) or 0
    open_reviews = (db.scalar(select(func.count()).select_from(ImportReview).where(ImportReview.status == "Open")) or 0) + \
        (db.scalar(select(func.count()).select_from(AllocationReview).where(AllocationReview.status == "Open")) or 0)
    connections = db.scalars(select(Connection).where(Connection.status != "disconnected").order_by(Connection.id)).all()
    pending_outbox = db.scalar(select(func.count()).select_from(OutboxItem)
                               .where(OutboxItem.state.in_(["queued", "uploading", "failed"]))) or 0
    unshipped_ready = ZERO
    if data:
        unshipped_ready = data.totals.ready
    return dict(
        season=season, seasons=seasons_newest_first(db), data=data, tasks=tasks, report=report, check_error=check_error,
        sessions=sessions, source_counts=source_counts, failed_jobs=failed_jobs, open_reviews=open_reviews,
        recovery=outbox.recovery_status(db), connections=connections, pending_outbox=pending_outbox,
        unshipped_ready=unshipped_ready,
    )


def install_ui(app, templates, context):
    @app.get("/brands")
    def brands_view(season: str = "", db: Session = Depends(get_db)):
        """Workspace › Brands: the season master table, by brand."""
        chosen = pick_season(db, season)
        if chosen is None:
            return RedirectResponse("/seasons/new", 303)
        from urllib.parse import quote
        return RedirectResponse(f"/seasons/{quote(chosen.name, safe='')}", 303)

    @app.get("/logistics")
    def logistics_page(request: Request, season: str = "", db: Session = Depends(get_db)):
        chosen = pick_season(db, season)
        newest = newest_season(db)
        shipments = db.scalars(
            select(Shipment).where(Shipment.season_id == (chosen.id if chosen else -1))
            .options(selectinload(Shipment.items).selectinload(ShipmentItem.order_item))
            .order_by(Shipment.date, Shipment.name)
        ).all()
        expenses = db.scalars(
            select(Expense).where(Expense.season_id == (chosen.id if chosen else -1)).options(
                selectinload(Expense.shipment), selectinload(Expense.order).selectinload(Order.brand), selectinload(Expense.customer),
            ).order_by(Expense.id.desc())
        ).all()
        sheet = worksheet_data(db)
        return templates.TemplateResponse(request, "logistics.html", context(
            request, season=chosen, seasons=seasons_newest_first(db), newest=newest,
            is_newest=bool(chosen and newest and chosen.id == newest.id),
            rows=ready_rows(db, chosen), orders=season_orders(db, chosen),
            summaries=[shipment_summary(shipment) for shipment in shipments], expenses=expenses,
            customers=db.scalars(select(Customer).order_by(Customer.code)).all(),
            distributors=db.scalars(select(Distributor).order_by(Distributor.name)).all(),
            groups=sheet["groups"], group_amounts=sheet["group_amounts"], status_labels=sheet["status_labels"],
            generated=request.query_params.get("generated"), ready_result=request.query_params.get("result"),
        ))

    @app.get("/money")
    def money_page(request: Request, season: str = "", db: Session = Depends(get_db)):
        chosen = pick_season(db, season)
        newest = newest_season(db)
        payments = db.scalars(
            select(Payment).where(Payment.season_id == (chosen.id if chosen else -1))
            .options(selectinload(Payment.customer), selectinload(Payment.order).selectinload(Order.brand), selectinload(Payment.allocations))
            .order_by(Payment.date.desc(), Payment.id.desc())
        ).all()
        factory_payments = db.scalars(
            select(FactoryPayment).where(FactoryPayment.season_id == (chosen.id if chosen else -1))
            .options(selectinload(FactoryPayment.order).selectinload(Order.brand), selectinload(FactoryPayment.ready_batch))
            .order_by(FactoryPayment.date.desc(), FactoryPayment.id.desc())
        ).all()
        sheet = worksheet_data(db)
        rows = ready_rows(db, chosen)
        return templates.TemplateResponse(request, "money.html", context(
            request, season=chosen, seasons=seasons_newest_first(db), newest=newest,
            is_newest=bool(chosen and newest and chosen.id == newest.id),
            data=dashboard_data(db, chosen.name) if chosen else None,
            payments=payments, factory_payments=factory_payments, rows=rows,
            ready_batches=[row["batch"] for row in reversed(rows)], orders=season_orders(db, chosen),
            customers=db.scalars(select(Customer).order_by(Customer.code)).all(),
            entries=db.scalars(select(SellerCreditEntry).options(selectinload(SellerCreditEntry.account))
                               .order_by(SellerCreditEntry.id.desc())).all(),
            today=date.today(), token=secrets.token_hex(16), sheet_rows=sheet["rows"], groups=sheet["groups"],
            accounts=sheet["accounts"], balances=sheet["balances"], held=sheet["held"], available=sheet["available"],
            reusable=sheet["reusable"],
        ))

    @app.get("/settings")
    def settings_page(request: Request, db: Session = Depends(get_db)):
        from app import outbox
        connections = db.scalars(select(Connection).where(Connection.status != "disconnected").order_by(Connection.id)).all()
        return templates.TemplateResponse(request, "settings.html", context(
            request,
            rule_count=db.scalar(select(func.count()).select_from(BrandSeasonRule)) or 0,
            brand_count=db.scalar(select(func.count()).select_from(Brand)) or 0,
            connections=connections,
            pending_outbox=db.scalar(select(func.count()).select_from(OutboxItem)
                                     .where(OutboxItem.state.in_(["queued", "uploading", "failed"]))) or 0,
            recovery=outbox.recovery_status(db),
            last_audit=db.scalar(select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(1)),
            protected_changes=db.scalar(select(func.count()).select_from(ImportChange).where(ImportChange.status == "Review")) or 0,
            ai_status=__import__("app.ai_settings", fromlist=["status"]).status(),
            voice_status=__import__("app.voice", fromlist=["status"]).status(),
        ))
