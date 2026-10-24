"""Serve an isolated copy of the app with synthetic records for interface checks.

    python scripts/ui_preview.py            # copy app/, seed once, serve 127.0.0.1:8001
    python scripts/ui_preview.py --reseed   # start again from fresh synthetic data
    python scripts/ui_preview.py --port 8002   # when the real app already uses 8001

The copy lives in tmp/ui-preview (its own database, uploads, reports and backups), so the
testing copy's data folder is never touched. AI, image search, Google connections, the HTTP
login and the background worker are switched off, so nothing is sent to an external service.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
PREVIEW = WORKSPACE / "tmp" / "ui-preview"


def sync_code() -> None:
    target = PREVIEW / "app"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(WORKSPACE / "app", target, ignore=shutil.ignore_patterns("__pycache__"))


def configure_environment() -> None:
    os.environ.update({
        "FASHION_DATABASE_URL": f"sqlite:///{(PREVIEW / 'data' / 'fashion_orders.db').as_posix()}",
        "FASHION_APP_USERNAME": "", "FASHION_APP_PASSWORD": "",
        "FASHION_AI_API_KEY": "", "FASHION_ANTHROPIC_API_KEY": "", "FASHION_GEMINI_API_KEY": "",
        "FASHION_GOOGLE_CSE_API_KEY": "", "FASHION_GOOGLE_CSE_ID": "", "FASHION_SEARXNG_URL": "",
        "FASHION_GOOGLE_CLIENT_ID": "", "FASHION_GOOGLE_CLIENT_SECRET": "",
        "FASHION_GOOGLE_API_KEY": "", "FASHION_GOOGLE_APP_ID": "",
        "FASHION_BACKGROUND_WORKER": "0", "FASHION_PUBLIC_BASE_URL": "http://127.0.0.1:8001",
    })
    os.chdir(PREVIEW)  # no .env here, so no workspace secrets are read
    sys.path.insert(0, str(PREVIEW))


def seed() -> None:
    """Clearly synthetic records covering every view: seasons, states, money, groups, reviews."""
    from app.audit import record_change
    from app.database import SessionLocal, init_db
    from app import item_service
    from app.models import (Brand, BrandSeasonRule, Customer, CustomerAlias, Distributor, Expense, FactoryPayment,
                            Order, Payment, PaymentAllocation, PurchaseGroup, ReadyBatch, Season, Shipment)
    from app.worksheet import get_account
    from app.models import SellerCreditEntry

    (PREVIEW / "data").mkdir(parents=True, exist_ok=True)
    init_db()
    db = SessionLocal()
    if db.query(Season).count():
        db.close()
        return
    ss, fw = Season(name="SS26"), Season(name="FW26")
    db.add_all([ss, fw])
    db.flush()
    brands = {name: Brand(name=name, default_markup_percentage=D(markup)) for name, markup in
              [("Atelier Nord", "0.30"), ("Maison Verre", "0.35"), ("Studio Lume", "0.30")]}
    db.add_all(brands.values())
    customers = {code: Customer(code=code, name=name, country_code=country, phone=phone, email=email, city=city) for code, name, country, phone, email, city in [
        ("AN", "Anna Novak", "IT", "+39 000 0001", "anna@example.test", "Milano"),
        ("BL", "Bianca Lund", "CH", "+41 000 0002", None, "Zürich"),
        ("CR", "Clara Rossi", "DE", None, "clara@example.test", "Berlin"),
    ]}
    db.add_all(customers.values())
    db.flush()
    db.add(CustomerAlias(alias="ANOV", customer_id=customers["AN"].id))
    orders = {}
    for season, names in [(ss, ["Atelier Nord"]), (fw, ["Atelier Nord", "Maison Verre", "Studio Lume"])]:
        for name in names:
            brand = brands[name]
            db.add(BrandSeasonRule(season_id=season.id, brand_id=brand.id, markup_percentage=brand.default_markup_percentage,
                                   initial_prepayment_percentage=D("0.50"), ready_balance_percentage=D("0.50"),
                                   transport_percentage=D("0.05"), special_cost=D("0"), transport_exempt_country_codes="CH",
                                   expected_receipt_days=14))
            orders[(season.name, name)] = Order(season_id=season.id, brand_id=brand.id, status="Ordered")
            db.add(orders[(season.name, name)])
    db.flush()
    distributor = Distributor(name="Fornitore Uno (synthetic)", contact="orders@fornitore.test")
    db.add(distributor)
    db.flush()
    group = PurchaseGroup(name="FW26 · Milan · Group 01", season_id=fw.id, distributor_id=distributor.id, seller="Seller Uno", special_cost=D("30"))
    group2 = PurchaseGroup(name="FW26 · Paris · Group 02", season_id=fw.id)
    db.add_all([group, group2])
    db.flush()
    ready = ReadyBatch(season_id=fw.id, name="Ready 1", ready_date=date.today() - timedelta(days=10), status="Open")
    db.add(ready)
    db.flush()
    shipment = Shipment(season_id=fw.id, name="Sped 1", date=date.today() - timedelta(days=3), status="In transit",
                        use_rule_transport=True, transport_percentage=D("0"), fixed_cost=D("25"), suitcase_cost=D("0"), bag_cost=D("8"))
    db.add(shipment)
    db.flush()

    rows = [
        # season, brand, customer, article, name, colour, size, qty, wholesale, worksheet status, extras
        ("FW26", "Atelier Nord", "AN", "AN-COAT-01", "Wool wrap coat", "Navy", "38", 1, "980", "process", {"purchase_group_id": group.id}),
        ("FW26", "Atelier Nord", "BL", "AN-KNIT-04", "Cashmere crew knit", "Ecru", "M", 2, "310", "done", {"purchase_group_id": group.id, "ready_batch_id": ready.id}),
        ("FW26", "Atelier Nord", "CR", "AN-TROU-02", "Pleated trousers", "Charcoal", "", 1, "260", "pending", {"purchase_group_id": group.id}),
        ("FW26", "Maison Verre", "AN", "MV-BAG-11", "Structured tote", "Black", "One size", 1, "1450", "process", {"ready_batch_id": ready.id, "shipment_id": shipment.id}),
        ("FW26", "Maison Verre", "CR", "MV-SCARF-3", "Silk scarf", "Ivory", "One size", 3, "120", "done", {"ready_batch_id": ready.id}),
        ("FW26", "Maison Verre", None, "MV-BELT-07", "Leather belt", "Tan", "85", 1, "190", "pending", {}),
        ("FW26", "Studio Lume", "BL", "SL-DRS-21", "Bias slip dress", "Moss", "36", 1, "540", "process", {"purchase_group_id": group2.id}),
        ("FW26", "Studio Lume", "AN", "SL-BLZ-09", "Boxy blazer", "Stone", "40", 1, "690", "pending", {"purchase_group_id": group2.id}),
        ("SS26", "Atelier Nord", "AN", "AN-LIN-01", "Linen shirt", "White", "S", 2, "140", "done", {}),
        ("SS26", "Atelier Nord", "CR", "AN-SAN-05", "Leather sandal", "Cognac", "39", 1, "220", "done", {}),
    ]
    created = []
    for season_name, brand_name, code, article, name, colour, size, qty, wholesale, state, extras in rows:
        order = orders[(season_name, brand_name)]
        values = {"customer_id": customers[code].id if code else None, "article": article, "name": name, "color": colour,
                  "size": size, "quantity": qty, "wholesale_unit_price": D(wholesale), "worksheet_status": state, **extras}
        created.append(item_service.create_item(db, order, values, actor="synthetic seed", channel="Synthetic preview data").item)
    created[2].worksheet_note = "Synthetic: supplier note says the size is unconfirmed."
    db.flush()
    db.add(Payment(season_id=fw.id, customer_id=customers["AN"].id, order_id=orders[("FW26", "Atelier Nord")].id,
                   date=date.today() - timedelta(days=20), amount=D("637.00"), type="payment", note="Synthetic bank transfer"))
    db.add(Payment(season_id=fw.id, customer_id=customers["BL"].id, date=date.today() - timedelta(days=6), amount=D("300.00"), type="payment", note="Synthetic, not yet allocated"))
    db.add(Payment(season_id=fw.id, customer_id=customers["CR"].id, date=date.today() - timedelta(days=2), amount=D("50.00"), type="refund", note="Synthetic refund"))
    db.flush()
    first = db.query(Payment).filter(Payment.customer_id == customers["AN"].id).first()
    db.add(PaymentAllocation(payment_id=first.id, order_id=first.order_id, stage="initial", amount=first.amount))
    db.add(Payment(season_id=ss.id, customer_id=customers["AN"].id, date=date(2026, 3, 2), amount=D("364.00"), type="payment", note="Synthetic SS26 payment"))
    db.add(FactoryPayment(season_id=fw.id, order_id=orders[("FW26", "Atelier Nord")].id, date=date.today() - timedelta(days=18), amount=D("1000.00"), stage="initial", note="Synthetic factory transfer"))
    db.add(Expense(season_id=fw.id, shipment_id=shipment.id, type="exceptional", amount=D("40.00"), note="Synthetic customs handling"))
    account = get_account(db, group, created[2])
    db.add(SellerCreditEntry(account_id=account.id, amount=D("200.00"), kind="prepayment", stage="initial", date=date.today() - timedelta(days=15),
                             reference="SYN-BANK-001", operation_key="seed:prepay"))
    record_change(db, entity_type="Customer", entity_id=customers["AN"].id, field="city", previous_value=None, new_value="Milano",
                  source_user="synthetic seed", description="Updated AN city")
    db.commit()
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reseed", action="store_true", help="delete the preview data and seed again")
    parser.add_argument("--no-serve", action="store_true", help="prepare only")
    parser.add_argument("--port", type=int, default=8001, help="port on 127.0.0.1 (use another one when 8001 is busy)")
    args = parser.parse_args()
    if args.reseed:
        for name in ("data", "backups"):
            shutil.rmtree(PREVIEW / name, ignore_errors=True)
    PREVIEW.mkdir(parents=True, exist_ok=True)
    sync_code()
    configure_environment()
    os.environ["FASHION_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{args.port}"
    seed()
    if not args.no_serve:
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
