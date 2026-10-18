from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderItemUpdate(BaseModel):
    customer_id: int | None = None
    article: str | None = None
    name: str | None = None
    color: str | None = None
    size: str | None = None
    quantity: int = Field(ge=1)
    wholesale_unit_price: Decimal = Field(ge=0)
    markup_percentage: Decimal = Field(ge=0, le=1)
    status: str
    shipment_id: int | None = None

    @field_validator("article", "name", "color", "size")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class DashboardSummary(BaseModel):
    season: str
    order_total: Decimal
    required_prepayment: Decimal
    ready_balance_due: Decimal
    ready: Decimal
    saldo: Decimal
    total_paid: Decimal
    total_charged: Decimal
    outstanding: Decimal
    overpayment: Decimal
    factory_required: Decimal
    factory_paid: Decimal
    factory_balance: Decimal
    shipments: dict[str, Decimal]


class ValidationIssueSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    severity: str
    code: str
    message: str
    entity: str | None = None
    action_url: str | None = None
    action_label: str = 'Review & fix'
    suggestion: str | None = None


class ValidationReportSchema(BaseModel):
    passed: bool
    error_count: int
    warning_count: int
    issues: list[ValidationIssueSchema]
