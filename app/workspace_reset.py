"""Explicitly scoped destructive actions (FIX-06).

Four different actions used to hide behind one "delete imported data" button:

* **Remove a stored source file** — deletes one uploaded workbook; the records
  created from it stay (``/imports/{id}/source/delete``).
* **Reverse a Ready-list import** — unassigns the goods it marked Ready, only while
  no customer payment, factory payment or shipment depends on that batch.
* **Restore a snapshot** — recovery into a new folder (see app.recovery).
* **Reset the whole workspace** — deletes every business record, including
  manually entered products, customers, payments and the audit history, plus all
  managed files. It always writes a verified recovery snapshot first and stops if
  that fails.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.database import Base
from app.models import (
    AuditLog, Customer, FactoryPayment, ImportBatch, ImportChange, ImportReview, OrderItem, Payment, PaymentAllocation,
    ReadyBatch, Season, ShipmentItem,
)
from app.recovery import SnapshotInfo, create_snapshot

RESET_PHRASE = "RESET WORKSPACE"
MANAGED_DIRECTORIES = ("data/imports", "data/images", "data/staged-images", "data/library", "data/reports",
                       "data/intake-sources", "data/sources", "data/excel-exchange", "output/pdf")
CATEGORIES = {
    "Business records": ["seasons", "brands", "brand_season_rules", "orders", "order_items", "customers", "customer_aliases",
                         "payments", "payment_allocations", "factory_payments", "ready_batches", "shipments", "shipment_items",
                         "expenses", "purchase_groups", "distributors", "seller_credit_accounts", "seller_credit_entries",
                         "collections", "collection_items"],
    "Imports and source reviews": ["import_batches", "import_reviews", "import_changes", "allocation_reviews", "library_files"],
    "Intake drafts and evidence": ["intake_confirmations", "intake_sessions", "intake_messages", "source_items", "source_blobs",
                                   "draft_rows", "draft_questions", "operation_receipts"],
    "Audit history": ["audit_logs"],
}


@dataclass
class ResetScope:
    tables: dict[str, dict[str, int]]
    directories: dict[str, dict[str, int]]

    @property
    def total_rows(self) -> int:
        return sum(sum(group.values()) for group in self.tables.values())


def reset_scope(db: Session, root: Path | None = None) -> ResetScope:
    root = root or ROOT_DIR
    present = {table.name: table for table in Base.metadata.sorted_tables}
    tables: dict[str, dict[str, int]] = {}
    for category, names in CATEGORIES.items():
        counts = {name: db.scalar(select(func.count()).select_from(present[name])) or 0 for name in names if name in present}
        tables[category] = {name: count for name, count in counts.items() if count}
    other = [name for name in present if not any(name in names for names in CATEGORIES.values())]
    extra = {name: db.scalar(select(func.count()).select_from(present[name])) or 0 for name in other}
    if any(extra.values()):
        tables["Other records"] = {name: count for name, count in extra.items() if count}
    directories = {}
    for relative in MANAGED_DIRECTORIES:
        path = root / relative
        files = [child for child in path.rglob("*") if child.is_file()] if path.is_dir() else []
        if files:
            directories[relative] = {"files": len(files), "bytes": sum(child.stat().st_size for child in files)}
    return ResetScope(tables=tables, directories=directories)


def _clear_managed_directory(root: Path, relative: str) -> None:
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise RuntimeError(f"Refusing to clear unmanaged path: {resolved}")
    if not resolved.is_dir():
        return
    for child in resolved.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def reset_workspace(db: Session, confirmation: str, root: Path | None = None) -> SnapshotInfo:
    """Snapshot, then delete every business record and managed file."""
    root = root or ROOT_DIR
    if confirmation.strip() != RESET_PHRASE:
        raise ValueError(f"Type {RESET_PHRASE} exactly to confirm the reset.")
    db.commit()
    snapshot = create_snapshot(db, reason="Before whole-workspace reset", root=root)
    try:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    except Exception:
        db.rollback()
        raise
    for relative in MANAGED_DIRECTORIES:
        _clear_managed_directory(root, relative)
    return snapshot


def ready_import_reversal_blockers(db: Session, batch: ImportBatch) -> tuple[ReadyBatch | None, list[str]]:
    summary = json.loads(batch.summary_json or "{}")
    ready = db.get(ReadyBatch, summary.get("ready_batch_id")) if summary.get("ready_batch_id") else None
    if ready is None:
        return None, ["This import did not create a Ready batch; a master import is reversed by restoring a snapshot."]
    blockers = []
    if db.scalar(select(func.count()).select_from(PaymentAllocation).where(PaymentAllocation.ready_batch_id == ready.id)):
        blockers.append("Customer payments are allocated to this Ready batch.")
    if db.scalar(select(func.count()).select_from(FactoryPayment).where(FactoryPayment.ready_batch_id == ready.id)):
        blockers.append("Factory payments reference this Ready batch.")
    if db.scalar(select(func.count()).select_from(ShipmentItem).join(OrderItem).where(OrderItem.ready_batch_id == ready.id)):
        blockers.append("Goods from this Ready batch are already in a shipment.")
    return ready, blockers


def reverse_ready_import(db: Session, batch: ImportBatch, reason: str, actor: str = "web user") -> int:
    """Unassign goods marked Ready by one Ready-list import. Split rows stay split (with lineage)."""
    from app.audit import record_change
    if len((reason or "").strip()) < 12:
        raise ValueError("Explain why this import is being reversed (at least 12 characters).")
    ready, blockers = ready_import_reversal_blockers(db, batch)
    if blockers:
        raise ValueError(" ".join(blockers))
    count = 0
    for item in db.scalars(select(OrderItem).where(OrderItem.ready_batch_id == ready.id)).all():
        item.ready_batch_id = None
        item.status = "Ordered"
        count += 1
        record_change(db, entity_type="OrderItem", entity_id=item.id, field="ready_batch_id", previous_value=ready.id,
                      new_value=None, source_user=actor, description=f"Reversed Ready import {batch.filename}: {reason.strip()}")
    for review in db.scalars(select(ImportReview).where(ImportReview.import_batch_id == batch.id, ImportReview.status == "Open")).all():
        review.status = "Reversed"
    for change in db.scalars(select(ImportChange).where(ImportChange.import_batch_id == batch.id)).all():
        change.status = "Reversed"
    ready.status = "Reversed"
    # Keep the import record but free its file hash, so a corrected list (or the same one) can be imported again.
    summary = json.loads(batch.summary_json or "{}") | {"reversed": True, "original_sha256": batch.sha256, "reversal_reason": reason.strip()}
    batch.summary_json = json.dumps(summary, ensure_ascii=False)
    batch.sha256 = f"reversed-{batch.id}-{batch.sha256[:40]}"
    record_change(db, entity_type="ImportBatch", entity_id=batch.id, field="status", previous_value="Applied", new_value="Reversed",
                  source_user=actor, description=f"Reversed Ready import {batch.filename} ({count} rows). {reason.strip()}")
    return count
