from decimal import Decimal as D
from io import BytesIO

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader
from sqlalchemy import select

from app.calculations import calculate_order, item_amounts
from app.main import projected_profit
from app.models import AuditLog, OrderItem
from app.pdf_report import build_customer_pdf
from app.ready_importer import _clone_remainder
from app.services import customer_statement
from test_workspace_features import workspace_session, workspace_client


def update_fields(item):
    return dict(customer_id=str(item.customer_id), article=item.article, name=item.name,
                quantity="2", wholesale_unit_price="100", markup_percentage="30", status="Ordered")


def test_custom_price_preserves_cost_and_flows_to_statements_exports_and_profit(workspace_client, workspace_session, tmp_path):
    item = workspace_session.scalar(select(OrderItem))
    response = workspace_client.post(f"/items/{item.id}", data=update_fields(item) | {"selling_unit_price": "115.25"})
    assert response.status_code == 200
    workspace_session.refresh(item)
    assert item.wholesale_unit_price == D("100")
    assert item.markup_percentage == D("0.30")
    assert item.selling_unit_price == D("115.25")
    assert item_amounts(item).final == D("230.50")
    assert item_amounts(item).markup == D("30.50")
    assert calculate_order([item], D("0.5")).required_prepayment == D("115.25")
    assert projected_profit(workspace_session)["projected_profit"] == D("30.50")
    statement = customer_statement(workspace_session, item.customer_id, "AW26")
    assert item_amounts(statement.items[0]).final == D("230.50")
    export = workspace_client.get("/exports/business.xlsx")
    sheet = load_workbook(BytesIO(export.content))["Products"]
    assert sheet["K5"].value == 100
    assert sheet["M5"].value == 115.25
    assert sheet["N5"].value == "=IFERROR(M5*J5,0)"
    assert 'value="115.25"' in response.text
    assert "230.50" in workspace_client.get(f"/customers/{item.customer_id}").text
    pdf = build_customer_pdf(workspace_session, item.customer_id, output_dir=tmp_path, season_name="AW26")
    pdf_text = " ".join(page.extract_text() for page in PdfReader(pdf).pages)
    assert "230.50" in pdf_text
    assert workspace_session.scalar(select(AuditLog).where(AuditLog.field == "selling_unit_price")) is not None
    remainder = _clone_remainder(item, 1)
    assert item_amounts(remainder).final == D("115.25")


def test_clear_override_restores_markup_but_omitted_field_preserves_it(workspace_client, workspace_session):
    item = workspace_session.scalar(select(OrderItem))
    item.selling_unit_price = D("80")
    workspace_session.commit()
    assert workspace_client.post(f"/items/{item.id}", data=update_fields(item)).status_code == 200
    workspace_session.refresh(item)
    assert item.selling_unit_price == D("80")
    assert item_amounts(item).markup == D("-40")
    assert workspace_client.post(f"/items/{item.id}", data=update_fields(item) | {"selling_unit_price": ""}).status_code == 200
    workspace_session.refresh(item)
    assert item.selling_unit_price is None
    assert item_amounts(item).final == D("260")


@pytest.mark.parametrize("price", ["-1", "NaN", "Infinity", "abc"])
def test_invalid_price_does_not_change_item(workspace_client, workspace_session, price):
    item = workspace_session.scalar(select(OrderItem))
    response = workspace_client.post(f"/items/{item.id}", data=update_fields(item) | {"selling_unit_price": price})
    assert response.status_code == 422
    workspace_session.refresh(item)
    assert item.selling_unit_price is None
    assert item.quantity == 1


def test_manual_create_accepts_zero_selling_price(workspace_client, workspace_session):
    original = workspace_session.scalar(select(OrderItem))
    response = workspace_client.post(f"/orders/{original.order_id}/items", data=update_fields(original) | {"selling_unit_price": "0"})
    assert response.status_code == 200
    item = workspace_session.scalar(select(OrderItem).order_by(OrderItem.id.desc()))
    assert item.selling_unit_price == D("0")
    assert item.wholesale_unit_price == D("100")
    assert item_amounts(item).final == D("0")


def test_ai_confirmation_accepts_custom_price_with_actual_wholesale(workspace_client, workspace_session):
    original = workspace_session.scalar(select(OrderItem))
    fields = dict(order_id=str(original.order_id), customer_name="Demo Buyer", product_name="Coat",
                  size="38", quantity="1", stated_price="100", price_type="wholesale",
                  source_text="Wholesale 100, selling 119.99", selling_unit_price="119.99", save_mode="json")
    response = workspace_client.post("/ai-intake/confirm", data=fields)
    assert response.status_code == 200
    item = workspace_session.scalar(select(OrderItem).order_by(OrderItem.id.desc()))
    assert item.wholesale_unit_price == D("100")
    assert item_amounts(item).final == D("119.99")
    rejected = workspace_client.post("/ai-intake/confirm", data=fields | {"price_type": "final"})
    assert rejected.status_code == 422
