from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Customer
from app.services import dashboard_data
from app.validation import validate_system


def main() -> None:
    with SessionLocal() as session:
        data = dashboard_data(session)
        report = validate_system(session)
        aa = session.scalar(select(Customer).where(Customer.code == "AA"))
        print(
            {
                "order_total": str(data.totals.final),
                "required_prepayment": str(data.totals.required_prepayment),
                "ready": str(data.totals.ready),
                "saldo": str(data.totals.saldo),
                "paid": str(data.account.total_paid),
                "charged": str(data.account.total_charged),
                "payable": str(data.account.outstanding),
                "shipments": [item.name for item in data.shipments],
                "validation_errors": len(report.errors),
                "validation_warnings": len(report.warnings),
                "aa_id": aa.id,
            }
        )
    with TestClient(app) as client:
        for route in ["/", "/orders", "/customers", "/shipments", "/payments", "/validation", "/reports", "/import", "/audit"]:
            response = client.get(route)
            print(route, response.status_code, len(response.content))
            response.raise_for_status()


if __name__ == "__main__":
    main()
