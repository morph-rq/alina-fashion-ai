from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db
from app.importer import import_workbook


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the database and import the SS26 workbook")
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    init_db()
    with SessionLocal() as session:
        try:
            summary = import_workbook(session, args.workbook)
            session.commit()
        except Exception:
            session.rollback()
            raise
    print(summary)


if __name__ == "__main__":
    main()
