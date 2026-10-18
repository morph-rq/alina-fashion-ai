"""Run the app's migrations against a COPY of a database and report what changed.

Usage:
    python scripts/migration_check.py path/to/fashion_orders.db [--keep]

The source file is opened read-only through SQLite's backup API; all work
happens in a temporary copy. The report compares row counts, allocations,
System Check codes and statement totals before and after, and lists the
recorded data-migration summaries. Nothing is written next to the source.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def copy_database(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def snapshot(db_path: Path) -> dict:
    with closing(sqlite3.connect(db_path)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(order_items)")}
        allocations = {}
        if "order_items" in tables:
            allocations = {row[0]: [row[1], row[2]] for row in connection.execute("SELECT id, customer_id, allocation_status FROM order_items")}
    return {"counts": counts, "allocations": allocations, "has_revision": "revision" in columns}


def business_report() -> dict:
    from app.database import SessionLocal
    from app.models import Customer, Season
    from app.services import customer_statement
    from app.validation import validate_system
    from sqlalchemy import select
    with SessionLocal() as session:
        report = validate_system(session)
        statements = {}
        for season in session.scalars(select(Season)).all():
            for customer in session.scalars(select(Customer)).all():
                statement = customer_statement(session, customer.id, season.name)
                if statement.items or statement.payments:
                    statements[f"{season.name}/{customer.code}"] = {
                        "ordered": str(statement.ordered), "charged": str(statement.account.total_charged),
                        "paid": str(statement.account.total_paid), "outstanding": str(statement.account.outstanding)}
        return {"errors": dict(Counter(issue.code for issue in report.errors)),
                "warnings": dict(Counter(issue.code for issue in report.warnings)), "statements": statements}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the migrated temporary copy")
    args = parser.parse_args()
    source = args.database.resolve()
    if not source.is_file():
        print(f"Database not found: {source}")
        return 2
    work = Path(tempfile.mkdtemp(prefix="migration-check-"))
    copy = work / "copy.db"
    copy_database(source, copy)
    before = snapshot(copy)
    os.environ["FASHION_DATABASE_URL"] = f"sqlite:///{copy.as_posix()}"
    from app.migrations import migrate_database, applied_versions
    first = migrate_database()
    second = migrate_database()  # A restart must not re-apply data migrations.
    after = snapshot(copy)
    with closing(sqlite3.connect(copy)) as connection:
        summaries = {row[0]: json.loads(row[1] or "{}") for row in connection.execute("SELECT version, summary FROM schema_migrations")}
    changed_allocations = {
        item_id: {"before": before["allocations"].get(item_id), "after": value}
        for item_id, value in after["allocations"].items() if before["allocations"].get(item_id) != value
    }
    report = {
        "source": str(source),
        "applied_first_run": first,
        "applied_on_restart": second,
        "recorded_versions": sorted(applied_versions()),
        "row_count_changes": {table: [before["counts"].get(table, 0), count] for table, count in after["counts"].items() if before["counts"].get(table, 0) != count},
        "allocation_changes": changed_allocations,
        "migration_summaries": summaries,
        "business_after": business_report(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not args.keep:
        from app.database import engine
        engine.dispose()
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"Migrated copy kept at {copy}")
    return 0 if not second else 1


if __name__ == "__main__":
    raise SystemExit(main())
