from __future__ import annotations

import base64
import asyncio
import secrets
import json
import hashlib
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.audit import record_change
from app import ai_intake
from app.brand_catalog import BRAND_SUGGESTIONS
from app.calculations import PAYMENT_TYPE_LABELS, signed_payment_amount, ZERO, display_money, final_total, final_unit_price, item_amounts, money
from app.config import ROOT_DIR, settings
from app.database import Base, get_db, init_db
from app.excel_export import build_business_workbook
from app.importer import import_workbook
from app.image_manager import cleanup_expired_stages, decode_assignments, discard_stage, match_images, staged_image_path, stage_images, store_external_image, store_manual_image
from app.models import (
    AllocationReview,
    AuditLog,
    Brand,
    BrandSeasonRule,
    Collection,
    CollectionItem,
    Customer,
    CustomerAlias,
    Expense,
    FactoryPayment,
    ImportBatch,
    ImportChange,
    ImportReview,
    IntakeConfirmation,
    LibraryFile,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    ReadyBatch,
    Season,
    Shipment,
    ShipmentItem,
    SellerCreditAccount,
)
from app.pdf_report import build_all_customer_pdfs, build_customer_pdf
from app.pricing import wholesale_from_final
from app import item_service
from app.drafts import FIELD_LABELS
from app.operations import operations_inbox
from app.ready_importer import process_ready_workbook
from app.schemas import DashboardSummary, ValidationReportSchema
from app.season_builder import build_season_draft, create_season_workspace, intake_schema
from app.services import customer_ready_due, customer_statement, dashboard_data, factory_ready_due, order_rule, order_summary, shipment_summary
from app.validation import validate_system
from app.worksheet_routes import render_worksheet, install_worksheet, amount_value
from app.storage import install_storage
from app.intake_archive import archive_sources, bundle_path, install_intake_sources


STATUSES = [
    "Ordered",
    "Confirmed",
    "Production",
    "Ready",
    "Assigned to shipment",
    "In transit",
    "Arrived",
    "Delivered",
    "Cancelled",
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    workers = []
    if settings.background_worker:
        from app import jobs
        from app.database import SessionLocal
        # Pending jobs live in the database; this loop resumes them after a restart.
        workers.append(asyncio.create_task(jobs.worker_loop(SessionLocal)))
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def protect_hosted_app(request: Request, call_next):
    """Require HTTP Basic credentials only when deployment credentials are set."""
    username = settings.app_username.strip()
    password = settings.app_password
    if not username or not password or request.url.path == "/health":
        return await call_next(request)

    authorization = request.headers.get("Authorization", "")
    supplied_username = ""
    supplied_password = ""
    if authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            supplied_username, supplied_password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            pass

    valid_username = secrets.compare_digest(supplied_username, username)
    valid_password = secrets.compare_digest(supplied_password, password)
    if not (valid_username and valid_password):
        return Response(
            f"Sign in to {settings.app_name}",
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{settings.app_name}", charset="UTF-8"'},
        )
    return await call_next(request)


app.mount("/static", StaticFiles(directory=ROOT_DIR / "app" / "static"), name="static")
(ROOT_DIR / "data" / "images").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=ROOT_DIR / "data" / "images"), name="media")
templates = Jinja2Templates(directory=ROOT_DIR / "app" / "templates")
templates.env.globals["item_amounts"] = item_amounts
templates.env.globals["signed_payment"] = signed_payment_amount
templates.env.globals["payment_labels"] = PAYMENT_TYPE_LABELS


def money_filter(value) -> str:
    if value is None:
        return "-"
    return f"{money(value):,.2f}".replace(",", " ")


def whole_filter(value) -> str:
    if value is None:
        return "-"
    return f"{display_money(value):,.0f}".replace(",", " ")


def percent_filter(value) -> str:
    if value is None:
        return "-"
    return f"{Decimal(value) * 100:.0f}%"


def media_url(path: str | None) -> str | None:
    if not path:
        return None
    return "/media/" + path.replace("\\", "/").removeprefix("data/images/")


templates.env.filters["money"] = money_filter
templates.env.filters["whole"] = whole_filter
templates.env.filters["percent"] = percent_filter
templates.env.filters["media"] = media_url


def context(request: Request, **kwargs):
    return {
        "request": request,
        "app_name": settings.app_name,
        "currency": settings.currency,
        "statuses": STATUSES,
        "brand_suggestions": BRAND_SUGGESTIONS,
        "labels": FIELD_LABELS,
        **kwargs,
    }


def active_season(db: Session) -> Season | None:
    """Return the most recently created season used by everyday workflows."""
    return db.scalar(select(Season).order_by(Season.id.desc()).limit(1))


def selected_season(db: Session, season_name: str = "") -> Season | None:
    """Resolve an explicit season, falling back to the newest season."""
    if season_name:
        season = db.scalar(select(Season).where(Season.name == season_name))
        if season is None:
            raise HTTPException(404, "Season not found")
        return season
    return active_season(db)


def automatic_customer_code(db: Session, name: str) -> str:
    """Create a short unique internal code without asking the user to invent one."""
    clean_name = name.strip()
    existing = db.scalars(select(Customer).order_by(Customer.id)).all()
    for customer in existing:
        if clean_name and (customer.name or "").strip().casefold() == clean_name.casefold():
            return customer.code
    base = ai_intake.suggested_customer_code(clean_name)
    if base == "NEW":
        base = "CUST"
    used = {customer.code.casefold() for customer in existing}
    if base.casefold() not in used:
        return base
    suffix = 2
    while f"{base}{suffix}".casefold() in used:
        suffix += 1
    return f"{base}{suffix}"


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/dashboard", response_model=DashboardSummary)
def dashboard_api(db: Session = Depends(get_db)):
    data = dashboard_data(db)
    if data is None:
        raise HTTPException(404, "Season not imported")
    return DashboardSummary(
        season=data.season.name,
        order_total=data.totals.final,
        required_prepayment=data.totals.required_prepayment,
        ready_balance_due=data.totals.ready_balance_due,
        ready=data.totals.ready,
        saldo=data.totals.saldo,
        total_paid=data.account.total_paid,
        total_charged=data.account.total_charged,
        outstanding=data.account.outstanding,
        overpayment=data.account.overpayment,
        factory_required=data.factory_required,
        factory_paid=data.factory_paid,
        factory_balance=data.factory_account.outstanding,
        shipments={
            shipment.name: data.totals.shipments.get(shipment.id, ZERO)
            for shipment in data.shipments
        },
    )


@app.get("/api/validation", response_model=ValidationReportSchema)
def validation_api(db: Session = Depends(get_db)):
    report = validate_system(db)
    return ValidationReportSchema(
        passed=report.passed,
        error_count=len(report.errors),
        warning_count=len(report.warnings),
        issues=report.issues,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    return render_worksheet(request, db, templates, context)


@app.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "operations.html", context(request, tasks=operations_inbox(db)))


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, db: Session = Depends(get_db)):
    return render_worksheet(request, db, templates, context)


@app.get("/manual-entry", response_class=HTMLResponse)
def manual_entry_page(request: Request, db: Session = Depends(get_db)):
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.brand), selectinload(Order.season), selectinload(Order.items))
        .order_by(Order.id.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "manual_entry.html",
        context(request, orders=orders, quick_error=None, quick_values={}),
    )


@app.post("/seasons/quick-create", response_class=HTMLResponse)
def quick_create_season(
    request: Request,
    season_name: str = Form(...),
    brand_name: str = Form(""),
    customer_code: str = Form(""),
    customer_name: str = Form(""),
    country_code: str = Form(""),
    default_markup: str = Form("35"),
    next_path: str = Form(""),
    db: Session = Depends(get_db),
):
    return_to_ai = next_path == "/ai-intake"
    values = {
        "season_name": season_name,
        "brand_name": brand_name,
        "customer_code": customer_code,
        "customer_name": customer_name,
        "country_code": country_code,
        "default_markup": default_markup,
    }
    if return_to_ai:
        clean_season_name = season_name.strip()
        clean_brand_name = brand_name.strip()
        clean_customer_name = customer_name.strip()
        errors: list[str] = []
        if not clean_season_name:
            errors.append("Season name is required")
        if db.scalar(select(Season).where(Season.name == clean_season_name)) is not None:
            errors.append("This season already exists")
        try:
            markup = required_decimal(default_markup, "default markup") / Decimal("100")
            if markup < ZERO or markup > Decimal("1"):
                errors.append("Default markup must be between 0% and 100%")
        except HTTPException as exc:
            markup = Decimal("0.35")
            errors.append(str(exc.detail))
        if errors:
            return templates.TemplateResponse(
                request,
                "ai_intake.html",
                context(request, **_ai_intake_context(
                    db,
                    drafts=[], message="", error=None, warning=None, follow_up_questions=[], source_summary=None,
                    quick_error=" ".join(errors), quick_values=values,
                )),
                status_code=422,
            )

        season = Season(name=clean_season_name)
        db.add(season)
        db.flush()
        if clean_brand_name:
            brand = db.scalar(select(Brand).where(func.lower(Brand.name) == clean_brand_name.casefold()))
            if brand is None:
                brand = Brand(name=clean_brand_name, default_markup_percentage=markup)
                db.add(brand)
                db.flush()
            db.add(BrandSeasonRule(
                season_id=season.id,
                brand_id=brand.id,
                markup_percentage=markup,
                initial_prepayment_percentage=Decimal("0.50"),
                ready_balance_percentage=Decimal("0.50"),
                transport_percentage=Decimal("0.05"),
                special_cost=ZERO,
                transport_exempt_country_codes="CH",
                expected_receipt_days=14,
            ))
            db.add(Order(season_id=season.id, brand_id=brand.id, status="Ordered"))
        if clean_customer_name:
            effective_customer_code = customer_code.strip() or automatic_customer_code(db, clean_customer_name)
            existing_customer = db.scalar(select(Customer).where(Customer.code == effective_customer_code.upper()))
            if existing_customer is None:
                db.add(Customer(
                    code=effective_customer_code.upper(),
                    name=clean_customer_name,
                    country_code=country_code.strip().upper() or None,
                ))
        db.commit()
        return RedirectResponse(f"/ai-intake?season_created={season.name}", status_code=303)

    effective_customer_code = customer_code.strip() or automatic_customer_code(db, customer_name)
    draft = _season_draft(
        db,
        season_name=season_name,
        primary_customer_code=effective_customer_code,
        primary_customer_name=customer_name,
        primary_country_code=country_code,
        additional_customers="",
        brand_rows=brand_name,
        product_rows="",
        copy_rules_from="",
        default_markup=default_markup,
        initial_percentage="50",
        ready_percentage="50",
        transport_percentage="5",
        exempt_countries="CH",
        receipt_days=14,
    )
    if not draft.can_create:
        if return_to_ai:
            return templates.TemplateResponse(
                request,
                "ai_intake.html",
                context(request, **_ai_intake_context(
                    db,
                    drafts=[], message="", error=None, warning=None, follow_up_questions=[], source_summary=None,
                    quick_error=" ".join(draft.errors), quick_values=values,
                )),
                status_code=422,
            )
        orders = db.scalars(
            select(Order)
            .options(selectinload(Order.brand), selectinload(Order.season), selectinload(Order.items))
            .order_by(Order.id.desc())
        ).all()
        return templates.TemplateResponse(
            request,
            "manual_entry.html",
            context(request, orders=orders, quick_error=" ".join(draft.errors), quick_values=values),
            status_code=422,
        )
    try:
        season = create_season_workspace(db, draft, source_user="web user")
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This season or brand table was created already. Refresh and try again.") from None
    order = db.scalar(select(Order).where(Order.season_id == season.id).order_by(Order.id).limit(1))
    if order is None:
        raise HTTPException(500, "The season was created without a brand table")
    if return_to_ai:
        return RedirectResponse(f"/ai-intake?season_created={season.name}", status_code=303)
    return RedirectResponse(f"/orders/{order.id}?season_created=1#manual-entry", status_code=303)


@app.get("/product-images", response_class=HTMLResponse)
def product_images_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    items = db.scalars(
        select(OrderItem)
        .where(OrderItem.image_path.is_not(None))
        .options(
            selectinload(OrderItem.customer),
            selectinload(OrderItem.order).selectinload(Order.brand),
            selectinload(OrderItem.order).selectinload(Order.season),
        )
        .order_by(OrderItem.image_updated_at.desc(), OrderItem.id.desc())
    ).all()
    query = q.strip().casefold()
    if query:
        def searchable(item: OrderItem) -> str:
            return " ".join(filter(None, [
                item.article, item.name, item.color, item.size, item.image_source_filename,
                item.order.brand.name, item.order.season.name,
                item.customer.code if item.customer else None,
                item.customer.name if item.customer else None,
            ])).casefold()

        query_terms = query.split()
        items = [item for item in items if all(term in searchable(item) for term in query_terms)]
    return templates.TemplateResponse(
        request,
        "product_images.html",
        context(request, items=items, q=q.strip()),
    )


@app.get("/api/orders/{order_id}/image-suggestions")
def manual_image_suggestions(
    order_id: int,
    product_name: str = "",
    article: str = "",
    color: str = "",
    db: Session = Depends(get_db),
):
    order = db.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.brand)))
    if order is None:
        raise HTTPException(404, "Brand table not found")
    if not product_name.strip() and not article.strip():
        raise HTTPException(422, "Enter a product name or article first")
    query = " ".join(part for part in (order.brand.name, product_name.strip(), article.strip(), color.strip()) if part)
    try:
        candidates = ai_intake.search_product_images(query, limit=8)
    except ValueError as exc:
        return {"query": query, "candidates": [], "warning": str(exc)}
    return {
        "query": query,
        "candidates": [
            {
                "title": candidate.title,
                "image_url": candidate.image_url,
                "thumbnail_url": candidate.thumbnail_url,
                "context_url": candidate.context_url,
            }
            for candidate in candidates
        ],
        "warning": None if candidates else "No matching images were found. Try adding an article number or color.",
    }


@app.get("/api/image-thumbnails/{token}")
def searxng_image_thumbnail(token: str):
    """Relay a short-lived SearXNG thumbnail so remote phones never receive localhost URLs."""
    source_url = ai_intake.resolve_searxng_thumbnail(token)
    if source_url is None:
        raise HTTPException(404, "Image preview expired; run the image search again")
    if not ai_intake.searxng_thumbnail_source_is_allowed(source_url):
        raise HTTPException(422, "Image preview source was invalid")
    try:
        upstream = httpx.get(source_url, timeout=15, follow_redirects=False)
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Image preview is temporarily unavailable") from exc
    content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/") or len(upstream.content) > 3 * 1024 * 1024:
        raise HTTPException(422, "Image preview response was invalid")
    return Response(
        upstream.content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=900", "X-Content-Type-Options": "nosniff"},
    )


def _season_form_context(db: Session) -> dict[str, object]:
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    return {
        "seasons": seasons,
        "sample_products": "Brand\tCustomer\tArticle\tProduct\tColor\tSize\tQuantity\tWholesale Price\tMarkup %\nLoro Piana Woman\tAA\tFAQ9434\tCoat\tNavy\t40\t1\t1028\t35",
    }


def _season_draft(
    db: Session,
    *,
    season_name: str,
    primary_customer_code: str,
    primary_customer_name: str,
    primary_country_code: str,
    additional_customers: str,
    brand_rows: str,
    product_rows: str,
    copy_rules_from: str,
    default_markup: str,
    initial_percentage: str,
    ready_percentage: str,
    transport_percentage: str,
    exempt_countries: str,
    receipt_days: int,
):
    return build_season_draft(
        db,
        season_name=season_name,
        primary_customer_code=primary_customer_code,
        primary_customer_name=primary_customer_name,
        primary_country_code=primary_country_code,
        additional_customers=additional_customers,
        brand_rows=brand_rows,
        product_rows=product_rows,
        copy_rules_from=copy_rules_from,
        default_markup=default_markup,
        initial_percentage=initial_percentage,
        ready_percentage=ready_percentage,
        transport_percentage=transport_percentage,
        exempt_countries=exempt_countries,
        receipt_days=receipt_days,
    )


@app.get("/seasons/new", response_class=HTMLResponse)
def new_season_page(request: Request, db: Session = Depends(get_db)):
    cleanup_expired_stages()
    return templates.TemplateResponse(
        request,
        "season_new.html",
        context(request, draft=None, values={}, staged_image_token="", staged_images=[], image_by_row={}, image_suggestions={}, image_assignments={}, **_season_form_context(db)),
    )


@app.post("/seasons/preview", response_class=HTMLResponse)
def preview_season(
    request: Request,
    season_name: str = Form(...),
    primary_customer_code: str = Form(...),
    primary_customer_name: str = Form(""),
    primary_country_code: str = Form(""),
    additional_customers: str = Form(""),
    brand_rows: str = Form(...),
    product_rows: str = Form(""),
    copy_rules_from: str = Form(""),
    default_markup: str = Form("35"),
    initial_percentage: str = Form("50"),
    ready_percentage: str = Form("50"),
    transport_percentage: str = Form("5"),
    exempt_countries: str = Form("CH"),
    receipt_days: int = Form(14),
    staged_image_token: str = Form(""),
    image_assignments_json: str = Form("{}"),
    images: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    values = {
        "season_name": season_name,
        "primary_customer_code": primary_customer_code,
        "primary_customer_name": primary_customer_name,
        "primary_country_code": primary_country_code,
        "additional_customers": additional_customers,
        "brand_rows": brand_rows,
        "product_rows": product_rows,
        "copy_rules_from": copy_rules_from,
        "default_markup": default_markup,
        "initial_percentage": initial_percentage,
        "ready_percentage": ready_percentage,
        "transport_percentage": transport_percentage,
        "exempt_countries": exempt_countries,
        "receipt_days": receipt_days,
    }
    draft = _season_draft(db, **values)
    uploads = [(item.filename or "image", item.file.read()) for item in images if item.filename]
    stage = stage_images(uploads, existing_token=staged_image_token or None)
    assignments, assignment_errors = decode_assignments(image_assignments_json)
    image_result = match_images(draft.products, stage.token, assignments)
    draft.errors.extend(stage.errors)
    draft.errors.extend(assignment_errors)
    draft.errors.extend(image_result.errors)
    draft.warnings.extend(image_result.warnings)
    return templates.TemplateResponse(
        request,
        "season_new.html",
        context(
            request,
            draft=draft,
            values=values,
            staged_image_token=stage.token or "",
            staged_images=stage.images,
            image_by_row=image_result.by_source_row,
            image_suggestions=image_result.suggestions_by_image,
            image_assignments=image_result.assignments,
            **_season_form_context(db),
        ),
        status_code=422 if draft.errors else 200,
    )


@app.post("/seasons")
def create_season(
    season_name: str = Form(...),
    primary_customer_code: str = Form(...),
    primary_customer_name: str = Form(""),
    primary_country_code: str = Form(""),
    additional_customers: str = Form(""),
    brand_rows: str = Form(...),
    product_rows: str = Form(""),
    copy_rules_from: str = Form(""),
    default_markup: str = Form("35"),
    initial_percentage: str = Form("50"),
    ready_percentage: str = Form("50"),
    transport_percentage: str = Form("5"),
    exempt_countries: str = Form("CH"),
    receipt_days: int = Form(14),
    staged_image_token: str = Form(""),
    image_assignments_json: str = Form("{}"),
    source_user: str = Form("web user"),
    db: Session = Depends(get_db),
):
    draft = _season_draft(
        db,
        season_name=season_name,
        primary_customer_code=primary_customer_code,
        primary_customer_name=primary_customer_name,
        primary_country_code=primary_country_code,
        additional_customers=additional_customers,
        brand_rows=brand_rows,
        product_rows=product_rows,
        copy_rules_from=copy_rules_from,
        default_markup=default_markup,
        initial_percentage=initial_percentage,
        ready_percentage=ready_percentage,
        transport_percentage=transport_percentage,
        exempt_countries=exempt_countries,
        receipt_days=receipt_days,
    )
    assignments, assignment_errors = decode_assignments(image_assignments_json)
    image_result = match_images(draft.products, staged_image_token or None, assignments)
    draft.errors.extend(assignment_errors)
    draft.errors.extend(image_result.errors)
    if not draft.can_create:
        raise HTTPException(422, "; ".join(draft.errors))
    try:
        season = create_season_workspace(
            db,
            draft,
            source_user=source_user,
            staged_image_token=staged_image_token or None,
            image_matches=image_result.by_source_row,
            image_match_methods=image_result.match_methods,
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "The season changed while it was being created. Preview it again.") from None
    return RedirectResponse(f"/seasons/{season.name}?created=1", status_code=303)


@app.get("/staged-images/{token}/{stored_filename}", include_in_schema=False)
def staged_image_preview(token: str, stored_filename: str):
    path = staged_image_path(token, stored_filename)
    if path is None:
        raise HTTPException(404, "Staged image not found")
    return FileResponse(path)


@app.post("/staged-images/{token}/discard")
def discard_staged_images(token: str, confirmation: str = Form(...)):
    if confirmation != "DISCARD_STAGED_IMAGES":
        raise HTTPException(400, "Invalid discard confirmation")
    discard_stage(token)
    return RedirectResponse("/seasons/new?images=discarded", status_code=303)


@app.get("/api/season-intake/schema")
def season_intake_schema() -> dict[str, object]:
    return intake_schema()


def _ai_intake_context(db: Session, **values) -> dict[str, object]:
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    return {
        "seasons": seasons,
        "ai_configured": ai_intake.ai_configured(),
        "image_search_configured": ai_intake.image_search_configured(),
        **values,
    }


@app.get("/ai-intake", response_class=HTMLResponse)
def ai_intake_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "ai_intake.html", context(request, **_ai_intake_context(
        db, drafts=[], message="", error=None, warning=None, follow_up_questions=[], source_summary=None,
        quick_error=None, quick_values={},
    )))


@app.post("/ai-intake/analyze", response_class=HTMLResponse)
def analyze_ai_intake(
    request: Request,
    message: str = Form(""),
    season_name: str = Form(...),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    season = db.scalar(select(Season).where(Season.name == season_name))
    if season is None:
        raise HTTPException(422, "Choose an existing season")
    uploaded_sources = [
        (item.filename or "attachment", item.content_type or "application/octet-stream", item.file.read(ai_intake.MAX_ATTACHMENT_BYTES + 1))
        for item in attachments if item.filename
    ]
    try:
        bundle = ai_intake.analyze_submission(message, uploaded_sources)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "ai_intake.html",
            context(request, **_ai_intake_context(db, drafts=[], message=message, selected_season=season_name, error=str(exc), warning=None, follow_up_questions=[], source_summary=None)),
            status_code=422,
        )
    source_bundle_token = archive_sources(message, uploaded_sources)
    staged = stage_images([
        (name, content) for name, mime, content in uploaded_sources
        if mime.split(";", 1)[0].casefold() in {"image/jpeg", "image/png", "image/webp"}
    ])
    orders = db.scalars(select(Order).where(Order.season_id == season.id).options(selectinload(Order.brand), selectinload(Order.season)).order_by(Order.id)).all()
    drafts = []
    warnings = list(staged.errors)
    for index, facts in enumerate(bundle.orders):
        matches = ai_intake.catalog_matches(db, facts, season_name)
        order_suggestion = ai_intake.suggest_order(orders, facts, matches)
        if order_suggestion.order is None and not facts.brand and not any("brand" in issue.casefold() for issue in facts.missing_or_ambiguous):
            facts.missing_or_ambiguous.append("Provide the brand name before saving")
        associated_images = [image for image in staged.images if image.original_filename in set(facts.source_attachment_names)]
        drafts.append({
            "facts": facts,
            "source_bundle_token": source_bundle_token,
            "confirmation_token": secrets.token_hex(16),
            "matches": matches,
            "image_candidates": [],
            "staged_images": associated_images,
            "staged_image_token": staged.token or "",
            "suggested_code": automatic_customer_code(db, facts.customer_name or ""),
            "suggested_order_id": order_suggestion.order.id if order_suggestion.order else None,
            "order_suggestion_reason": order_suggestion.reason,
        })

    # Source images are read by multimodal intake; do not fetch decorative product images.
    return templates.TemplateResponse(
        request, "ai_intake.html",
        context(request, **_ai_intake_context(
            db, drafts=drafts, message=message, selected_season=season_name, orders=orders, error=None,
            warning=" ".join(warnings) or None, follow_up_questions=bundle.follow_up_questions,
            source_summary=bundle.source_summary,
        )),
    )


@app.post("/ai-intake/confirm")
def confirm_ai_intake(
    request: Request,
    order_id: str = Form(""), season_name: str = Form(""), brand_name: str = Form(""), new_brand_markup: str = Form("35"),
    customer_code: str = Form(""), customer_name: str = Form(...), customer_phone: str = Form(""), country_code: str = Form(""),
    article: str = Form(""), product_name: str = Form(...), color: str = Form(""), size: str = Form(""), quantity: int = Form(..., ge=1),
    stated_price: str = Form(...), price_type: str = Form(...), requested_delivery_date: str = Form(""), logistics_preference: str = Form(""),
    customer_requirements: str = Form(""), source_text: str = Form(""), contact_on_pdf_send: bool = Form(False),
    image_url: str = Form(""), image_context_url: str = Form(""), db: Session = Depends(get_db),
    staged_image_token: str = Form(""), staged_image_filename: str = Form(""),
    save_mode: str = Form("redirect"),
    selling_unit_price: str = Form(""),
    source_bundle_token: str = Form(""), confirmation_token: str = Form(""),
    worksheet_state: str = Form('pending'),
):
    if worksheet_state not in {'process','done','pending'}:
        raise HTTPException(422,'Choose a valid worksheet status.')
    if confirmation_token:
        if not re.fullmatch(r'[a-f0-9]{32}', confirmation_token):
            raise HTTPException(422, 'Invalid draft key. Analyze the source again.')
        db.execute(text('BEGIN IMMEDIATE'))
        previous = db.get(IntakeConfirmation, confirmation_token)
        if previous:
            saved = db.get(OrderItem, previous.item_id)
            redirect_url = f'/worksheet/items/{saved.id}'
            if save_mode == 'batch' or 'application/json' in request.headers.get('accept', ''):
                return JSONResponse(dict(ok=True,item_id=saved.id,order_id=saved.order_id,redirect=redirect_url))
            return RedirectResponse(redirect_url,303)
    if source_bundle_token:
        try: bundle_path(source_bundle_token)
        except ValueError as exc: raise HTTPException(422,str(exc)) from None
    order = None
    if order_id.strip():
        try:
            selected_order_id = int(order_id)
        except ValueError:
            raise HTTPException(422, "Choose a valid Brand table") from None
        order = db.scalar(select(Order).where(Order.id == selected_order_id).options(selectinload(Order.brand), selectinload(Order.season).selectinload(Season.rules)))
        if order is None:
            raise HTTPException(404, "Brand table not found")
    else:
        clean_brand_name = brand_name.strip()
        if not clean_brand_name:
            raise HTTPException(422, "Provide a brand name or choose an existing Brand table")
        season = db.scalar(select(Season).where(Season.name == season_name.strip()))
        if season is None:
            raise HTTPException(422, "Choose an existing season")
        markup = amount_value(new_brand_markup, "New brand markup") / Decimal("100")
        if markup < ZERO or markup > Decimal("1"):
            raise HTTPException(422, "New brand markup must be between 0% and 100%")
        brand = db.scalar(select(Brand).where(func.lower(Brand.name) == clean_brand_name.casefold()))
        if brand is None:
            brand = Brand(name=clean_brand_name, default_markup_percentage=markup)
            db.add(brand)
            db.flush()
        rule = db.scalar(select(BrandSeasonRule).where(
            BrandSeasonRule.season_id == season.id,
            BrandSeasonRule.brand_id == brand.id,
        ))
        if rule is None:
            db.add(BrandSeasonRule(
                season_id=season.id,
                brand_id=brand.id,
                markup_percentage=markup,
                initial_prepayment_percentage=Decimal("0.50"),
                ready_balance_percentage=Decimal("0.50"),
                transport_percentage=Decimal("0.05"),
                special_cost=ZERO,
                transport_exempt_country_codes="CH",
                expected_receipt_days=14,
            ))
        order = db.scalar(select(Order).where(Order.season_id == season.id, Order.brand_id == brand.id))
        if order is None:
            order = Order(season_id=season.id, brand_id=brand.id, status="Ordered")
            db.add(order)
        db.flush()
        order = db.scalar(
            select(Order)
            .where(Order.id == order.id)
            .options(selectinload(Order.brand), selectinload(Order.season).selectinload(Season.rules))
        )
    if not size.strip():
        raise HTTPException(422, "Confirm the product size before saving")
    if not product_name.strip():
        raise HTTPException(422, "Confirm the product name before saving")
    if price_type not in {"wholesale", "final"}:
        raise HTTPException(422, "Confirm whether the stated price is wholesale or customer-final")
    stated = amount_value(stated_price, "Stated price", positive=True)
    if stated <= ZERO:
        raise HTTPException(422, "Price must be positive")
    markup = order_rule(order).markup_percentage
    selling_price = optional_selling_price(selling_unit_price)
    if selling_price is not None and price_type != "wholesale":
        raise HTTPException(422, "For a custom selling price, enter the actual wholesale cost as Stated price and choose Wholesale / factory.")
    price_note = None
    if price_type == "wholesale":
        wholesale = stated
    else:
        reverse = wholesale_from_final(stated, markup)
        wholesale, selling_price, price_note = reverse.wholesale, reverse.selling_unit_price, reverse.reason
    if not customer_name.strip():
        raise HTTPException(422, "Customer name is required")
    code = automatic_customer_code(db, customer_name)
    customer = db.scalar(select(Customer).where(Customer.code == code))
    if customer is None:
        customer = Customer(code=code, name=customer_name.strip(), phone=customer_phone.strip() or None, country_code=country_code.strip().upper() or None)
        db.add(customer)
        db.flush()
    else:
        if customer_phone.strip():
            customer.phone = customer_phone.strip()
        if country_code.strip():
            customer.country_code = country_code.strip().upper()
    item = OrderItem(
        order_id=order.id, customer_id=customer.id, article=article.strip() or None, name=product_name.strip(), color=color.strip() or None,
        size=size.strip(), quantity=quantity, wholesale_unit_price=wholesale, markup_percentage=markup, status="Ordered", worksheet_status=worksheet_state,
        selling_unit_price=selling_price,
        requested_delivery_date=parse_optional_date(requested_delivery_date, "requested delivery date"),
        logistics_preference=logistics_preference.strip() or None, customer_requirements=customer_requirements.strip() or None,
        ai_source_text=source_text.strip(), contact_on_pdf_send=contact_on_pdf_send,
        source_file="AI intake", source_sheet=order.brand.name, source_allocation_description=code, allocation_status="Allocated",
        image_source_url=image_url.strip() or None, image_context_url=image_context_url.strip() or None,
    )
    db.add(item)
    db.flush()
    item.source_row = item.id
    if source_bundle_token:
        item.source_note = f'AI source archive: {source_bundle_token}'
    if confirmation_token:
        db.add(IntakeConfirmation(token=confirmation_token,item_id=item.id))
    created_image_path = None
    if staged_image_filename.strip():
        source = staged_image_path(staged_image_token.strip(), staged_image_filename.strip())
        if source is None:
            db.rollback()
            raise HTTPException(422, "The selected uploaded product image expired; analyze the source again")
        try:
            relative_path, created_image_path = store_manual_image(item.id, staged_image_filename.strip(), source.read_bytes())
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
        item.image_path = relative_path
        item.image_source_filename = staged_image_filename.strip()
        item.image_match_method = "ai_attachment_user_confirmed"
        item.image_updated_at = datetime.now()
    elif image_url.strip():
        try:
            relative_path, created_image_path = store_external_image(item.id, image_url.strip())
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
        item.image_path = relative_path
        item.image_source_filename = image_url.strip()
        item.image_match_method = "confirmed_web_search"
        item.image_updated_at = datetime.now()
    record_change(
        db, entity_type="OrderItem", entity_id=item.id, field=None, previous_value=None, new_value=item.article or item.name,
        source_user="AI intake form",
        description=f"Created AI intake row for {code}: {order.brand.name} {item.article or item.name}" + (f". {price_note}" if price_note else ""),
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        if created_image_path:
            Path(created_image_path).unlink(missing_ok=True)
        raise
    redirect_url = f"/worksheet/items/{item.id}" if confirmation_token else f"/orders/{order.id}?ai_created=1"
    if save_mode == "batch" or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "item_id": item.id, "order_id": order.id, "redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=303)


@app.get("/seasons/{season_name}", response_class=HTMLResponse)
def season_detail(season_name: str, request: Request, db: Session = Depends(get_db)):
    data = dashboard_data(db, season_name)
    if data is None:
        raise HTTPException(404, "Season not found")
    return templates.TemplateResponse(
        request,
        "season_detail.html",
        context(request, data=data, created=request.query_params.get("created")),
    )


def _order_detail(order_id: int, request: Request, db: Session):
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.brand),
            selectinload(Order.season).selectinload(Season.rules),
            selectinload(Order.items).selectinload(OrderItem.customer),
            selectinload(Order.items).selectinload(OrderItem.shipment_link).selectinload(ShipmentItem.shipment),
        )
    )
    if order is None:
        raise HTTPException(404, "Order not found")
    customers = db.scalars(select(Customer).order_by(Customer.code)).all()
    shipments = db.scalars(select(Shipment).where(Shipment.season_id == order.season_id).order_by(Shipment.name)).all()
    ready_batches = db.scalars(select(ReadyBatch).where(ReadyBatch.season_id == order.season_id).order_by(ReadyBatch.id)).all()
    return templates.TemplateResponse(
        request,
        "brand_detail.html",
        context(
            request,
            brand=order.brand,
            order=order,
            summary=order_summary(order),
            customers=customers,
            shipments=shipments,
            ready_batches=ready_batches,
        ),
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(order_id: int, request: Request, db: Session = Depends(get_db)):
    return _order_detail(order_id, request, db)


@app.get("/brands/{brand_id}", response_class=HTMLResponse)
def brand_detail(brand_id: int, request: Request, db: Session = Depends(get_db)):
    brand = db.scalar(
        select(Brand)
        .where(Brand.id == brand_id)
        .options(
            selectinload(Brand.orders).selectinload(Order.season),
            selectinload(Brand.orders).selectinload(Order.season).selectinload(Season.rules),
            selectinload(Brand.orders).selectinload(Order.items).selectinload(OrderItem.customer),
            selectinload(Brand.orders).selectinload(Order.items).selectinload(OrderItem.shipment_link).selectinload(ShipmentItem.shipment),
        )
    )
    if brand is None:
        raise HTTPException(404, "Brand not found")
    order = max(brand.orders, key=lambda item: item.season.id, default=None)
    if order is None:
        raise HTTPException(404, "Order not found")
    return _order_detail(order.id, request, db)


def required_decimal(value: str, label: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise HTTPException(422, f"Invalid {label}") from None


def optional_selling_price(value: str) -> Decimal | None:
    if not value.strip():
        return None
    price = required_decimal(value, "selling price")
    if not price.is_finite() or price < ZERO or price >= Decimal("10000000000000000"):
        raise HTTPException(422, "Selling price must be a finite, non-negative amount below 10000000000000000")
    return money(price)


def parse_optional_id(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(422, f"Invalid {label}") from None


def parse_optional_date(value: str, label: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"Invalid {label}") from None


@app.post("/orders/{order_id}/items")
def create_item(
    order_id: int,
    customer_id: str = Form(""),
    article: str = Form(""),
    name: str = Form(""),
    color: str = Form(""),
    size: str = Form(""),
    quantity: int = Form(..., ge=1),
    wholesale_unit_price: str = Form(...),
    selling_unit_price: str = Form(""),
    markup_percentage: str = Form(""),
    status: str = Form("Ordered"),
    ready_batch_id: str = Form(""),
    shipment_id: str = Form(""),
    source_user: str = Form("web user"),
    image: UploadFile | None = File(default=None),
    image_url: str = Form(""),
    image_context_url: str = Form(""),
    db: Session = Depends(get_db),
):
    order = db.scalar(select(Order).where(Order.id == order_id).options(
        selectinload(Order.brand), selectinload(Order.season).selectinload(Season.rules)
    ))
    if order is None:
        raise HTTPException(404, "Order not found")
    try:
        values = {
            "customer_id": parse_optional_id(customer_id, "customer"),
            "article": article, "name": name, "color": color, "size": size, "quantity": quantity,
            "wholesale_unit_price": item_service.parse_money(wholesale_unit_price, "Price"),
            "selling_unit_price": item_service.parse_money(selling_unit_price, "Selling price", optional=True),
            "markup_percentage": item_service.parse_percent(markup_percentage) if markup_percentage.strip() else order_rule(order).markup_percentage,
            "status": status,
            "ready_batch_id": parse_optional_id(ready_batch_id, "Ready batch"),
            "shipment_id": parse_optional_id(shipment_id, "shipment"),
        }
        item = item_service.create_item(db, order, values, actor=source_user, channel="Manual entry").item
    except item_service.ItemRuleError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    created_image_path = None
    if image is not None and image.filename:
        try:
            relative_path, created_image_path = store_manual_image(item.id, image.filename, image.file.read())
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
        item.image_path = relative_path
        item.image_source_filename = image.filename
        item.image_match_method = "manual_order_upload"
        item.image_updated_at = datetime.now()
    elif image_url.strip():
        try:
            relative_path, created_image_path = store_external_image(item.id, image_url.strip())
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
        item.image_path = relative_path
        item.image_source_filename = image_url.strip()
        item.image_source_url = image_url.strip()
        item.image_context_url = image_context_url.strip() or None
        item.image_match_method = "manual_search_confirmed"
        item.image_updated_at = datetime.now()
    try:
        db.commit()
    except Exception:
        db.rollback()
        if created_image_path:
            Path(created_image_path).unlink(missing_ok=True)
        raise
    return RedirectResponse(f"/orders/{order.id}?created={item.id}", status_code=303)


@app.post("/orders/{order_id}/readiness")
def update_order_readiness(
    order_id: int,
    factory_ready_from: str = Form(""),
    factory_ready_to: str = Form(""),
    factory_ready_note: str = Form(""),
    db: Session = Depends(get_db),
):
    order = db.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.brand)))
    if order is None:
        raise HTTPException(404, "Order not found")
    start = parse_optional_date(factory_ready_from, "factory readiness start date")
    end = parse_optional_date(factory_ready_to, "factory readiness end date")
    if start and end and end < start:
        raise HTTPException(422, "Factory readiness end date cannot be before the start date")
    changes = {
        "factory_ready_from": start,
        "factory_ready_to": end,
        "factory_ready_note": factory_ready_note.strip() or None,
    }
    for field, value in changes.items():
        previous = getattr(order, field)
        if previous != value:
            setattr(order, field, value)
            record_change(
                db, entity_type="Order", entity_id=order.id, field=field,
                previous_value=previous, new_value=value, source_user="web user",
                description=f"Updated {order.brand.name} factory readiness: {field}",
            )
    db.commit()
    return RedirectResponse(f"/orders/{order.id}?readiness=saved", status_code=303)


@app.post("/items/{item_id}/image")
def upload_item_image(
    item_id: int,
    image: UploadFile = File(...),
    source_user: str = Form("web user"),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(OrderItem)
        .where(OrderItem.id == item_id)
        .options(selectinload(OrderItem.order).selectinload(Order.brand))
    )
    if item is None:
        raise HTTPException(404, "Product not found")
    if not image.filename:
        raise HTTPException(422, "Choose an image")
    try:
        relative_path, absolute_path = store_manual_image(item.id, image.filename, image.file.read())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    previous = item.image_path
    item.image_path = relative_path
    item.image_source_filename = Path(image.filename).name
    item.image_match_method = "manual_product_upload"
    item.image_updated_at = datetime.now().astimezone()
    record_change(
        db,
        entity_type="OrderItem",
        entity_id=item.id,
        field="image_path",
        previous_value=previous,
        new_value=relative_path,
        source_user=source_user,
        description=f"Uploaded product image {item.image_source_filename} for {item.order.brand.name} {item.article or item.name or item.id}",
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        Path(absolute_path).unlink(missing_ok=True)
        raise
    if return_to == "library":
        return RedirectResponse("/library?updated=image#products", status_code=303)
    return RedirectResponse(f"/orders/{item.order.id}?image={item.id}", status_code=303)


@app.post("/items/{item_id}")
async def update_item(
    request: Request,
    item_id: int,
    customer_id: str = Form(""),
    article: str = Form(""),
    name: str = Form(""),
    color: str = Form(""),
    size: str = Form(""),
    quantity: int = Form(..., ge=1),
    wholesale_unit_price: str = Form(...),
    selling_unit_price: str | None = Form(None),
    markup_percentage: str = Form(...),
    status: str = Form(...),
    ready_batch_id: str = Form(""),
    shipment_id: str = Form(""),
    source_user: str = Form("web user"),
    revision: str = Form(""),
    correction_reason: str = Form(""),
    db: Session = Depends(get_db),
):
    item = db.scalar(
        select(OrderItem)
        .where(OrderItem.id == item_id)
        .options(
            selectinload(OrderItem.order).selectinload(Order.brand),
            selectinload(OrderItem.shipment_link),
        )
    )
    if item is None:
        raise HTTPException(404, "Item not found")
    try:
        values = {
            "customer_id": parse_optional_id(customer_id, "customer"),
            "article": article, "name": name, "color": color, "size": size, "quantity": quantity,
            "wholesale_unit_price": item_service.parse_money(wholesale_unit_price, "Price"),
            "markup_percentage": item_service.parse_percent(markup_percentage),
            "status": status,
            "ready_batch_id": parse_optional_id(ready_batch_id, "Ready batch"),
            "shipment_id": parse_optional_id(shipment_id, "shipment"),
        }
        # Old clients that omit this field must preserve an existing override.
        if "selling_unit_price" in await request.form():
            values["selling_unit_price"] = item_service.parse_money(selling_unit_price or "", "Selling price", optional=True)
        item_service.change_item(
            db, item, values, actor=source_user, channel="product editor",
            expected_revision=int(revision) if revision.strip().isdigit() else None,
            reason=correction_reason,
        )
    except item_service.ItemRuleError as exc:
        db.rollback()
        raise HTTPException(409 if isinstance(exc, item_service.StaleItemError) else 422, str(exc)) from None
    db.commit()
    return RedirectResponse(f"/orders/{item.order.id}?saved={item.id}", status_code=303)


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, season: str = "", db: Session = Depends(get_db)):
    current = selected_season(db, season)
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    customers = db.scalars(
        select(Customer).options(selectinload(Customer.order_items), selectinload(Customer.payments), selectinload(Customer.aliases)).order_by(Customer.code)
    ).all()
    statements = []
    for customer in customers:
        try:
            statements.append(customer_statement(db, customer.id, current.name if current else None))
        except LookupError:
            continue
    return templates.TemplateResponse(request, "customers.html", context(request, statements=statements, season=current, seasons=seasons))


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(customer_id: int, request: Request, season: str = "", db: Session = Depends(get_db)):
    current = selected_season(db, season)
    try:
        statement = customer_statement(db, customer_id, current.name if current else None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    return templates.TemplateResponse(request, "customer_detail.html", context(request, statement=statement, seasons=seasons, season=current))


@app.post("/customers/{customer_id}/settings")
def update_customer_settings(
    customer_id: int, name: str = Form(""), phone: str = Form(""), email: str = Form(""),
    country_code: str = Form(""), address: str = Form(""), city: str = Form(""), postal_code: str = Form(""),
    preferred_contact: str = Form(""), notes: str = Form(""), alias: str = Form(""),
    return_to: str = Form("customers"), db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(404, "Customer not found")
    country = country_code.strip().upper() or None
    if country and (len(country) != 2 or not country.isalpha()):
        raise HTTPException(422, "Country must be a two-letter code")
    values = {"country_code": country}
    if return_to == "detail":
        values.update({
            "name": name.strip() or customer.name,
            "phone": phone.strip() or None,
            "email": email.strip() or None,
            "address": address.strip() or None,
            "city": city.strip() or None,
            "postal_code": postal_code.strip() or None,
            "preferred_contact": preferred_contact.strip() or None,
            "notes": notes.strip() or None,
        })
    for field, value in values.items():
        previous = getattr(customer, field)
        if previous != value:
            setattr(customer, field, value)
            record_change(db, entity_type="Customer", entity_id=customer.id, field=field, previous_value=previous, new_value=value, source_user="web user", description=f"Updated {customer.code} {field}")
    clean_alias = alias.strip().upper()
    if clean_alias:
        existing = db.scalar(select(CustomerAlias).where(CustomerAlias.alias == clean_alias))
        if existing and existing.customer_id != customer.id:
            raise HTTPException(409, "Alias belongs to another customer")
        if existing is None:
            db.add(CustomerAlias(alias=clean_alias, customer_id=customer.id))
            record_change(db, entity_type="CustomerAlias", entity_id=clean_alias, field=None, previous_value=None, new_value=customer.code, source_user="web user", description=f"Added alias {clean_alias} for {customer.code}")
    db.commit()
    destination = f"/customers/{customer.id}?saved=1" if return_to == "detail" else "/customers"
    return RedirectResponse(destination, status_code=303)


@app.get("/ready-batches", response_class=HTMLResponse)
def ready_batches_page(request: Request, season: str = "", db: Session = Depends(get_db)):
    season = selected_season(db, season)
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    batches = db.scalars(
        select(ReadyBatch).where(ReadyBatch.season_id == season.id).options(
            selectinload(ReadyBatch.items).selectinload(OrderItem.order).selectinload(Order.season).selectinload(Season.rules),
            selectinload(ReadyBatch.payment_allocations).selectinload(PaymentAllocation.payment),
        ).order_by(ReadyBatch.id)
    ).all() if season else []
    rows = []
    for batch in batches:
        value = money(sum((item_amounts(item).final for item in batch.items if item.status != "Cancelled"), ZERO))
        due = money(sum((customer_ready_due(item) for item in batch.items), ZERO))
        customer_paid = money(sum((signed_payment_amount(allocation.amount, allocation.payment.type) for allocation in batch.payment_allocations if allocation.stage == "ready"), ZERO))
        factory_paid = money(sum(db.scalars(select(FactoryPayment.amount).where(FactoryPayment.ready_batch_id == batch.id, FactoryPayment.stage == "ready")).all(), ZERO))
        rows.append({"batch": batch, "value": value, "due": due, "customer_paid": customer_paid,
                     "factory_due": factory_ready_due(batch.items), "factory_paid": factory_paid})
    orders = db.scalars(
        select(Order).where(Order.season_id == season.id).options(selectinload(Order.brand)).order_by(Order.id)
    ).all() if season else []
    return templates.TemplateResponse(request, "ready_batches.html", context(request, rows=rows, season=season, seasons=seasons, generated=request.query_params.get("generated"), ready_result=request.query_params.get("result")))


@app.post("/ready-batches/process")
def process_ready_batch_upload(
    name: str = Form(...),
    season_name: str = Form(...),
    ready_date: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(422, "Only .xlsx Ready lists are supported")
    safe_name = re.sub(r"[^A-Za-zА-Яа-я0-9_. -]+", "_", Path(file.filename).name)
    import_dir = ROOT_DIR / "data" / "imports" / "ready" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    import_dir.mkdir(parents=True, exist_ok=True)
    destination = import_dir / safe_name
    with destination.open("wb") as stream:
        shutil.copyfileobj(file.file, stream)
    try:
        summary = process_ready_workbook(db, destination, name, parse_optional_date(ready_date, "Ready date"), season_name)
        db.flush()
        zip_name = ""
        affected = set(summary.get("affected_customer_ids", []))
        if affected:
            zip_path, _files = build_all_customer_pdfs(db, affected, f"{name}_affected_customers", season_name)
            zip_name = zip_path.name
        db.commit()
    except Exception:
        db.rollback()
        raise
    target = f"/ready-batches?season={season_name}&result={summary.get('assigned_units', 0)}"
    if zip_name:
        target += f"&generated={zip_name}"
    return RedirectResponse(target, status_code=303)


@app.post("/ready-batches")
def create_ready_batch(name: str = Form(...), season_name: str = Form(...), ready_date: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    season = selected_season(db, season_name)
    if season is None:
        raise HTTPException(422, "Import or create a season first")
    if db.scalar(select(ReadyBatch).where(ReadyBatch.season_id == season.id, ReadyBatch.name == name.strip())):
        raise HTTPException(409, "Ready batch name already exists")
    batch = ReadyBatch(season_id=season.id, name=name.strip(), ready_date=parse_optional_date(ready_date, "Ready date"), status="Open", notes=notes.strip() or None)
    db.add(batch)
    db.flush()
    record_change(db, entity_type="ReadyBatch", entity_id=batch.id, field=None, previous_value=None, new_value=batch.name, source_user="web user", description=f"Created Ready batch {batch.name}")
    db.commit()
    return RedirectResponse(f"/ready-batches?season={season.name}", status_code=303)


@app.post("/factory-payments")
def create_factory_payment(season_name: str = Form(...), order_id: str = Form(""), ready_batch_id: str = Form(""), payment_date: date = Form(...), amount: str = Form(...), stage: str = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    season = selected_season(db, season_name)
    if season is None or stage not in {"initial", "ready"}:
        raise HTTPException(422, "Invalid factory payment")
    batch_id = parse_optional_id(ready_batch_id, "Ready batch")
    selected_order_id = parse_optional_id(order_id, "factory order")
    if stage == "ready" and not batch_id:
        raise HTTPException(422, "Ready factory payments require a batch")
    selected_order = db.get(Order, selected_order_id) if selected_order_id else None
    if selected_order_id and (selected_order is None or selected_order.season_id != season.id):
        raise HTTPException(422, "Factory order does not belong to this season")
    if batch_id:
        batch = db.get(ReadyBatch, batch_id)
        if batch is None or batch.season_id != season.id:
            raise HTTPException(422, "Ready batch does not belong to this season")
        if selected_order_id and not db.scalar(select(OrderItem.id).where(OrderItem.order_id == selected_order_id, OrderItem.ready_batch_id == batch_id).limit(1)):
            raise HTTPException(422, "Selected factory order has no goods in this Ready batch")
    paid_amount = money(required_decimal(amount, "amount"))
    if paid_amount <= ZERO:
        raise HTTPException(422, "Factory payment amount must be positive")
    payment = FactoryPayment(season_id=season.id, order_id=selected_order_id, ready_batch_id=batch_id, date=payment_date, amount=paid_amount, stage=stage, note=note.strip() or None)
    db.add(payment)
    db.flush()
    record_change(db, entity_type="FactoryPayment", entity_id=payment.id, field=None, previous_value=None, new_value=payment.amount, source_user="web user", description=f"Recorded factory {stage} payment {payment.amount} for {selected_order.brand.name if selected_order else 'season-level allocation'}")
    db.commit()
    return RedirectResponse(f"/ready-batches?season={season.name}", status_code=303)


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, db: Session = Depends(get_db)):
    rules = db.scalars(select(BrandSeasonRule).options(selectinload(BrandSeasonRule.brand), selectinload(BrandSeasonRule.season)).order_by(BrandSeasonRule.id)).all()
    return templates.TemplateResponse(request, "rules.html", context(request, rules=rules))


@app.post("/rules/{rule_id}")
def update_rule(rule_id: int, markup: str = Form(...), initial: str = Form(...), ready: str = Form(...), transport: str = Form(...), special_cost: str = Form("0"), exempt_countries: str = Form("CH"), receipt_days: int = Form(14, ge=0), readiness_payment_logic: str = Form("ready_batch_only"), notes: str = Form(""), db: Session = Depends(get_db)):
    rule = db.get(BrandSeasonRule, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    values = {
        "markup_percentage": required_decimal(markup, "markup") / 100,
        "initial_prepayment_percentage": required_decimal(initial, "initial percentage") / 100,
        "ready_balance_percentage": required_decimal(ready, "Ready percentage") / 100,
        "transport_percentage": required_decimal(transport, "transport") / 100,
        "special_cost": money(required_decimal(special_cost, "special cost")),
        "transport_exempt_country_codes": ",".join(part.strip().upper() for part in exempt_countries.split(",") if part.strip()),
        "expected_receipt_days": receipt_days,
        "readiness_payment_logic": readiness_payment_logic.strip() or "ready_batch_only",
        "notes": notes.strip() or None,
    }
    for field, value in values.items():
        previous = getattr(rule, field)
        if previous != value:
            setattr(rule, field, value)
            record_change(db, entity_type="BrandSeasonRule", entity_id=rule.id, field=field, previous_value=previous, new_value=value, source_user="web user", description=f"Updated {rule.season.name} / {rule.brand.name} rule: {field}")
    db.commit()
    return RedirectResponse("/rules", status_code=303)


@app.get("/shipments", response_class=HTMLResponse)
def shipments_page(request: Request, db: Session = Depends(get_db)):
    season = active_season(db)
    shipments = db.scalars(
        select(Shipment)
        .where(Shipment.season_id == season.id if season else Shipment.id == -1)
        .options(selectinload(Shipment.items).selectinload(ShipmentItem.order_item))
        .order_by(Shipment.date, Shipment.name)
    ).all()
    summaries = [shipment_summary(shipment) for shipment in shipments]
    expenses = db.scalars(
        select(Expense).where(Expense.season_id == season.id if season else Expense.id == -1).options(
            selectinload(Expense.shipment), selectinload(Expense.order).selectinload(Order.brand),
            selectinload(Expense.customer),
        ).order_by(Expense.id.desc())
    ).all()
    customers = db.scalars(select(Customer).order_by(Customer.code)).all()
    orders = db.scalars(select(Order).where(Order.season_id == season.id if season else Order.id == -1).options(selectinload(Order.brand), selectinload(Order.season)).order_by(Order.id)).all()
    return templates.TemplateResponse(request, "shipments.html", context(request, summaries=summaries, expenses=expenses, customers=customers, orders=orders))


@app.post("/shipments")
def create_shipment(
    name: str = Form(...),
    shipment_date: str = Form(""),
    status: str = Form("Assigned"),
    transport_percentage: str = Form("0"),
    use_rule_transport: str = Form("on"),
    destination_country_code: str = Form(""),
    eligible_override: str = Form(""),
    fixed_cost: str = Form("0"),
    suitcase_cost: str = Form("0"),
    bag_cost: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    season = active_season(db)
    if season is None:
        raise HTTPException(422, "Import or create a season first")
    if db.scalar(select(Shipment).where(Shipment.name == name.strip())):
        raise HTTPException(409, "Shipment name already exists")
    shipment = Shipment(
        season_id=season.id,
        name=name.strip(),
        date=parse_optional_date(shipment_date, "shipment date"),
        status=status.strip() or "Assigned",
        transport_percentage=required_decimal(transport_percentage, "transport percentage") / Decimal("100"),
        use_rule_transport=use_rule_transport == "on",
        destination_country_code=destination_country_code.strip().upper() or None,
        transport_eligible_value_override=money(required_decimal(eligible_override, "eligible override")) if eligible_override else None,
        fixed_cost=money(required_decimal(fixed_cost, "fixed cost")),
        suitcase_cost=money(required_decimal(suitcase_cost, "suitcase cost")),
        bag_cost=money(required_decimal(bag_cost, "bag cost")),
        notes=notes.strip() or None,
    )
    db.add(shipment)
    db.flush()
    record_change(db, entity_type="Shipment", entity_id=shipment.id, field=None, previous_value=None, new_value=shipment.name, source_user="web user", description=f"Created shipment {shipment.name}")
    db.commit()
    return RedirectResponse("/shipments", status_code=303)


@app.post("/expenses")
def create_expense(
    expense_type: str = Form(...),
    amount: str = Form(...),
    shipment_id: str = Form(""),
    order_id: str = Form(""),
    customer_id: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    season = active_season(db)
    if season is None:
        raise HTTPException(422, "Import or create a season first")
    selected_shipment_id = parse_optional_id(shipment_id, "shipment")
    selected_order_id = parse_optional_id(order_id, "order")
    selected_customer_id = parse_optional_id(customer_id, "customer")
    if not any((selected_shipment_id, selected_order_id, selected_customer_id)):
        raise HTTPException(422, "An exceptional expense must be assigned to a shipment, order, or customer")
    if selected_shipment_id and db.get(Shipment, selected_shipment_id) is None:
        raise HTTPException(422, "Shipment not found")
    if selected_shipment_id and db.get(Shipment, selected_shipment_id).season_id != season.id:
        raise HTTPException(422, "Shipment does not belong to this season")
    selected_order = db.get(Order, selected_order_id) if selected_order_id else None
    if selected_order_id and (selected_order is None or selected_order.season_id != season.id):
        raise HTTPException(422, "Order does not belong to this season")
    if selected_customer_id and db.get(Customer, selected_customer_id) is None:
        raise HTTPException(422, "Customer not found")
    expense_amount = money(required_decimal(amount, "expense amount"))
    if expense_amount < ZERO:
        raise HTTPException(422, "Expense amount cannot be negative")
    clean_type = expense_type.strip().lower()
    if clean_type not in {"transport", "bag", "suitcase", "fixed logistics", "exceptional"}:
        raise HTTPException(422, "Unsupported expense type")
    expense = Expense(
        season_id=season.id, shipment_id=selected_shipment_id, order_id=selected_order_id,
        customer_id=selected_customer_id, type=clean_type, amount=expense_amount,
        note=note.strip() or None,
    )
    db.add(expense)
    db.flush()
    record_change(
        db, entity_type="Expense", entity_id=expense.id, field=None,
        previous_value=None, new_value=expense.amount, source_user="web user",
        description=f"Recorded {expense.type} expense {expense.amount}",
    )
    db.commit()
    return RedirectResponse("/shipments", status_code=303)


@app.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, db: Session = Depends(get_db)):
    season = active_season(db)
    payments = db.scalars(select(Payment).where(Payment.season_id == season.id if season else Payment.id == -1).options(selectinload(Payment.customer), selectinload(Payment.order), selectinload(Payment.allocations)).order_by(Payment.date.desc(), Payment.id.desc())).all()
    customers = db.scalars(select(Customer).order_by(Customer.code)).all()
    ready_batches = db.scalars(select(ReadyBatch).where(ReadyBatch.season_id == season.id if season else ReadyBatch.id == -1).order_by(ReadyBatch.id.desc())).all()
    orders = db.scalars(select(Order).where(Order.season_id == season.id if season else Order.id == -1).options(selectinload(Order.brand), selectinload(Order.season)).order_by(Order.id)).all()
    return templates.TemplateResponse(request, "payments.html", context(request, payments=payments, customers=customers, ready_batches=ready_batches, orders=orders))


@app.post("/payments")
def create_payment(
    customer_id: int = Form(...),
    payment_date: date = Form(...),
    amount: str = Form(...),
    type: str = Form("payment"),
    stage: str = Form("unallocated"),
    order_id: str = Form(""),
    ready_batch_id: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    season = active_season(db)
    if season is None:
        raise HTTPException(422, "Import or create a season first")
    if db.get(Customer, customer_id) is None:
        raise HTTPException(422, "Customer not found")
    if type not in {"payment", "refund"}:
        raise HTTPException(422, "Unsupported payment type")
    payment_amount = money(required_decimal(amount, "amount"))
    if payment_amount <= ZERO:
        raise HTTPException(422, "Payment amount must be positive")
    selected_order_id = parse_optional_id(order_id, "order")
    selected_order = db.get(Order, selected_order_id) if selected_order_id else None
    if selected_order_id and (selected_order is None or selected_order.season_id != season.id):
        raise HTTPException(422, "Order does not belong to this season")
    payment = Payment(season_id=season.id, customer_id=customer_id, order_id=selected_order_id, date=payment_date, amount=payment_amount, type=type, note=note.strip() or None)
    db.add(payment)
    db.flush()
    if stage not in {"unallocated", "initial", "ready"}:
        raise HTTPException(422, "Unsupported payment stage")
    batch_id = parse_optional_id(ready_batch_id, "Ready batch")
    if stage == "ready" and not batch_id:
        raise HTTPException(422, "Ready payments require a Ready batch")
    if batch_id:
        batch = db.get(ReadyBatch, batch_id)
        if batch is None or batch.season_id != season.id:
            raise HTTPException(422, "Ready batch does not belong to this season")
    if stage != "unallocated":
        db.add(PaymentAllocation(payment_id=payment.id, ready_batch_id=batch_id, order_id=selected_order_id, stage=stage, amount=payment.amount))
    record_change(db, entity_type="Payment", entity_id=payment.id, field=None, previous_value=None, new_value=payment.amount, source_user="web user", description=f"Recorded {type} {payment.amount} for customer {customer_id}")
    db.commit()
    return RedirectResponse("/payments", status_code=303)


@app.get("/validation", response_class=HTMLResponse)
def validation_page(request: Request, db: Session = Depends(get_db)):
    report = validate_system(db)
    return templates.TemplateResponse(request, "validation.html", context(request, report=report))


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, season: str = "", db: Session = Depends(get_db)):
    customers = db.scalars(select(Customer).order_by(Customer.code)).all()
    current = selected_season(db, season)
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    ready_batches = db.scalars(select(ReadyBatch).where(ReadyBatch.season_id == current.id if current else ReadyBatch.id == -1).order_by(ReadyBatch.id.desc())).all()
    return templates.TemplateResponse(request, "reports.html", context(request, customers=customers, ready_batches=ready_batches, season=current, seasons=seasons))


@app.get("/reports/customers/{customer_id}/pdf")
def customer_pdf(customer_id: int, season: str = "", db: Session = Depends(get_db)):
    try:
        path = build_customer_pdf(db, customer_id, season_name=season or None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/reports/customers-all.zip")
def all_customer_pdfs(season: str = "", db: Session = Depends(get_db)):
    path, _files = build_all_customer_pdfs(db, season_name=season or None)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/reports/customers-ready/{ready_batch_id}.zip")
def ready_customer_pdfs(ready_batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ReadyBatch, ready_batch_id)
    if batch is None:
        raise HTTPException(404, "Ready batch not found")
    season = db.get(Season, batch.season_id)
    customer_ids = set(db.scalars(select(OrderItem.customer_id).where(OrderItem.ready_batch_id == ready_batch_id, OrderItem.customer_id.is_not(None))).all())
    path, _files = build_all_customer_pdfs(db, customer_ids, f"ready_batch_{ready_batch_id}", season.name)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/reports/customers-outstanding.zip")
def outstanding_customer_pdfs(season: str = "", db: Session = Depends(get_db)):
    current = selected_season(db, season)
    customer_ids = set()
    for customer in db.scalars(select(Customer)).all():
        try:
            if customer_statement(db, customer.id, current.name if current else None).account.outstanding > ZERO:
                customer_ids.add(customer.id)
        except LookupError:
            continue
    path, _files = build_all_customer_pdfs(db, customer_ids, "outstanding_customers", current.name if current else None)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc())).all()
    reviews = db.scalars(select(ImportReview).order_by(ImportReview.id.desc()).limit(300)).all()
    allocation_reviews = db.scalars(select(AllocationReview).order_by(AllocationReview.id.desc()).limit(300)).all()
    changes = db.scalars(select(ImportChange).order_by(ImportChange.id.desc()).limit(500)).all()
    return templates.TemplateResponse(request, "import.html", context(request, batches=batches, reviews=reviews, allocation_reviews=allocation_reviews, changes=changes))


@app.post("/import")
def upload_workbook(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(422, "Only .xlsx files are supported")
    safe_name = re.sub(r"[^A-Za-zА-Яа-я0-9_. -]+", "_", Path(file.filename).name)
    import_dir = ROOT_DIR / "data" / "imports" / "master" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    import_dir.mkdir(parents=True, exist_ok=True)
    destination = import_dir / safe_name
    with destination.open("wb") as stream:
        shutil.copyfileobj(file.file, stream)
    try:
        summary = import_workbook(db, destination)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/import?result={summary.get('items', 0)}", status_code=303)


@app.post("/imports/{batch_id}/source/delete")
def delete_import_source(batch_id: int, confirmation: str = Form(...), db: Session = Depends(get_db)):
    if confirmation != "DELETE_SOURCE_FILE":
        raise HTTPException(422, "Deletion confirmation is invalid")
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import not found")
    if batch.source_deleted_at is not None:
        return RedirectResponse("/import?source_already_deleted=1", status_code=303)
    if not batch.stored_path:
        raise HTTPException(409, "This historical import has no tracked project file")

    import_root = (ROOT_DIR / "data" / "imports").resolve()
    source_path = (ROOT_DIR / batch.stored_path).resolve()
    try:
        source_path.relative_to(import_root)
    except ValueError as exc:
        raise HTTPException(409, "Stored import path is outside the managed import directory") from exc
    if not source_path.is_file():
        raise HTTPException(404, "Stored import file is already missing")

    source_path.unlink()
    parent = source_path.parent
    if parent != import_root:
        try:
            parent.rmdir()
        except OSError:
            pass
    batch.source_deleted_at = datetime.now().astimezone()
    record_change(
        db,
        entity_type="ImportBatch",
        entity_id=batch.id,
        field="stored_path",
        previous_value=batch.stored_path,
        new_value=None,
        source_user="web user",
        description=f"Deleted stored source file for import {batch.filename}; imported accounting data was retained",
    )
    db.commit()
    return RedirectResponse(f"/import?source_deleted={batch.id}", status_code=303)


def _reset_page(request: Request, db: Session, **values):
    from app import workspace_reset
    ready_imports = []
    for batch in db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc())).all():
        summary = json.loads(batch.summary_json or "{}")
        if summary.get("ready_batch_id") and not summary.get("reversed"):
            ready, blockers = workspace_reset.ready_import_reversal_blockers(db, batch)
            ready_imports.append({"batch": batch, "ready": ready, "blockers": blockers})
    return templates.TemplateResponse(request, "workspace_reset.html", context(
        request, scope=workspace_reset.reset_scope(db, ROOT_DIR), phrase=workspace_reset.RESET_PHRASE,
        ready_imports=ready_imports, **values), status_code=422 if values.get("error") else 200)


@app.get("/workspace/reset", response_class=HTMLResponse)
def workspace_reset_page(request: Request, db: Session = Depends(get_db)):
    return _reset_page(request, db, snapshot=request.query_params.get("snapshot"))


@app.post("/workspace/reset")
def reset_workspace_route(request: Request, confirmation: str = Form(...), db: Session = Depends(get_db)):
    from app import workspace_reset
    try:
        snapshot = workspace_reset.reset_workspace(db, confirmation, ROOT_DIR)
    except ValueError as exc:
        db.rollback()
        return _reset_page(request, db, error=str(exc))
    return RedirectResponse(f"/workspace/reset?snapshot={snapshot.path.name}", status_code=303)


@app.post("/imports/reset-all")
def reset_all_imported_data():
    # The old label suggested only imports were affected. Reset now has an explicit scope page.
    return RedirectResponse("/workspace/reset", status_code=303)


@app.post("/imports/{batch_id}/reverse-ready")
def reverse_ready_import_route(request: Request, batch_id: int, reason: str = Form(...), db: Session = Depends(get_db)):
    from app import workspace_reset
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import not found")
    try:
        workspace_reset.reverse_ready_import(db, batch, reason)
    except ValueError as exc:
        db.rollback()
        return _reset_page(request, db, error=str(exc))
    db.commit()
    return RedirectResponse("/workspace/reset?reversed=1", status_code=303)


@app.get("/reports/download/{filename}")
def report_download(filename: str):
    safe = Path(filename).name
    path = (ROOT_DIR / "data" / "reports" / safe).resolve()
    output_root = (ROOT_DIR / "data" / "reports").resolve()
    if path.parent != output_root or not path.exists():
        raise HTTPException(404, "Report archive not found")
    media_type = "application/zip" if path.suffix.lower() == ".zip" else "application/pdf"
    return FileResponse(path, media_type=media_type, filename=path.name)


def _excel_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/exports/business.xlsx")
def export_business_excel(db: Session = Depends(get_db)):
    return _excel_response(build_business_workbook(db), "fashion-business-data.xlsx")


@app.get("/exports/seasons/{season_id}.xlsx")
def export_season_excel(season_id: int, db: Session = Depends(get_db)):
    try:
        content = build_business_workbook(db, season_id=season_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return _excel_response(content, f"fashion-season-{season_id}.xlsx")


@app.get("/exports/customers/{customer_id}.xlsx")
def export_customer_excel(customer_id: int, season: str = "", db: Session = Depends(get_db)):
    """Customer-facing workbook (allowlisted fields only; safe to share with that customer)."""
    from app.customer_export import customer_workbook
    try:
        current = selected_season(db, season) if season else None
        content = customer_workbook(db, customer_id, current.id if current else None)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return _excel_response(content, f"statement-customer-{customer_id}.xlsx")


@app.get("/exports/customers/{customer_id}/internal.xlsx")
def export_customer_internal_excel(customer_id: int, season: str = "", db: Session = Depends(get_db)):
    """Internal business workbook filtered to one customer (contains wholesale costs; do not share)."""
    try:
        current = selected_season(db, season) if season else None
        content = build_business_workbook(db, customer_id=customer_id, season_id=current.id if current else None)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return _excel_response(content, f"internal-customer-{customer_id}.xlsx")


@app.get("/exports/collections/{collection_id}.xlsx")
def export_collection_excel(collection_id: int, db: Session = Depends(get_db)):
    try:
        content = build_business_workbook(db, collection_id=collection_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return _excel_response(content, f"fashion-collection-{collection_id}.xlsx")


@app.get("/imports/{batch_id}/download")
def download_import_source(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None or not batch.stored_path or batch.source_deleted_at:
        raise HTTPException(404, "The original import file is not available")
    import_root = (ROOT_DIR / "data" / "imports").resolve()
    path = (ROOT_DIR / batch.stored_path).resolve()
    try:
        path.relative_to(import_root)
    except ValueError as exc:
        raise HTTPException(409, "Import file path is invalid") from exc
    if not path.is_file():
        raise HTTPException(404, "The stored import file is missing")
    return FileResponse(path, filename=batch.filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/search")
def global_search(q: str = "", db: Session = Depends(get_db)):
    clean = " ".join(q.strip().split())
    if len(clean) < 2:
        return {"query": clean, "results": []}
    needle = f"%{clean.lower()}%"
    results: list[dict[str, object]] = []

    customers = db.scalars(select(Customer).where(or_(
        func.lower(Customer.code).like(needle), func.lower(func.coalesce(Customer.name, "")).like(needle),
        func.lower(func.coalesce(Customer.phone, "")).like(needle), func.lower(func.coalesce(Customer.email, "")).like(needle),
        func.lower(func.coalesce(Customer.city, "")).like(needle),
    )).order_by(Customer.code).limit(8)).all()
    results.extend({"type": "Customer", "title": customer.name or customer.code, "subtitle": " · ".join(filter(None, [customer.code, customer.phone, customer.email, customer.city])), "url": f"/customers/{customer.id}"} for customer in customers)

    brands = db.scalars(select(Brand).where(or_(func.lower(Brand.name).like(needle), func.lower(func.coalesce(Brand.notes, "")).like(needle))).order_by(Brand.name).limit(8)).all()
    results.extend({"type": "Brand", "title": brand.name, "subtitle": "Open products, rules, and reference links", "url": f"/brands/{brand.id}"} for brand in brands)

    seasons = db.scalars(select(Season).where(func.lower(Season.name).like(needle)).order_by(Season.id.desc()).limit(8)).all()
    results.extend({"type": "Season", "title": season.name, "subtitle": "Open its brand rules and tables", "url": f"/rules?season_id={season.id}"} for season in seasons)

    orders = db.scalars(select(Order).join(Order.brand).join(Order.season).options(selectinload(Order.brand), selectinload(Order.season)).where(or_(
        func.lower(Brand.name).like(needle), func.lower(Season.name).like(needle), func.lower(Order.status).like(needle),
        func.lower(func.coalesce(Order.factory_ready_note, "")).like(needle),
    )).order_by(Order.id.desc()).limit(8)).all()
    results.extend({"type": "Order", "title": f"{order.brand.name} · {order.season.name}", "subtitle": order.status, "url": f"/orders/{order.id}"} for order in orders)

    products = db.scalars(select(OrderItem).join(OrderItem.order).join(Order.brand).outerjoin(OrderItem.customer).options(
        selectinload(OrderItem.order).selectinload(Order.brand), selectinload(OrderItem.order).selectinload(Order.season), selectinload(OrderItem.customer),
    ).where(or_(
        func.lower(func.coalesce(OrderItem.article, "")).like(needle), func.lower(func.coalesce(OrderItem.name, "")).like(needle),
        func.lower(func.coalesce(OrderItem.color, "")).like(needle), func.lower(func.coalesce(OrderItem.size, "")).like(needle),
        func.lower(OrderItem.status).like(needle), func.lower(Brand.name).like(needle),
        func.lower(func.coalesce(Customer.code, "")).like(needle), func.lower(func.coalesce(Customer.name, "")).like(needle),
    )).order_by(OrderItem.id.desc()).limit(12)).all()
    results.extend({
        "type": "Product", "title": product.name or product.article or f"Product {product.id}",
        "subtitle": " · ".join(filter(None, [product.order.brand.name, product.article, product.color, product.size, product.customer.code if product.customer else None])),
        "url": f"/orders/{product.order_id}#product-{product.id}",
    } for product in products)

    collections = db.scalars(select(Collection).where(or_(func.lower(Collection.name).like(needle), func.lower(func.coalesce(Collection.description, "")).like(needle))).order_by(Collection.created_at.desc()).limit(8)).all()
    results.extend({"type": "Collection", "title": collection.name, "subtitle": collection.description or "Saved product collection", "url": f"/library#collection-{collection.id}"} for collection in collections)

    files = db.scalars(select(LibraryFile).where(or_(func.lower(LibraryFile.filename).like(needle), func.lower(LibraryFile.category).like(needle), func.lower(func.coalesce(LibraryFile.note, "")).like(needle))).order_by(LibraryFile.uploaded_at.desc()).limit(8)).all()
    results.extend({"type": "File", "title": file.filename, "subtitle": file.category.title(), "url": f"/library/files/{file.id}/download"} for file in files)
    return {"query": clean, "results": results[:48]}


def projected_profit(db: Session) -> dict[str, object] | None:
    data = dashboard_data(db)
    if data is None:
        return None
    gross_profit = money(data.totals.final - data.totals.base)
    other_costs = money(data.expenses_total + data.shipment_fees_total)
    estimated_profit = money(gross_profit - other_costs)
    margin = money((estimated_profit / data.totals.final * Decimal("100")) if data.totals.final else ZERO)
    return {
        "season": data.season.name,
        "projected_revenue": data.totals.final,
        "wholesale_cost": data.totals.base,
        "gross_profit_before_logistics": gross_profit,
        "logistics_and_extra_costs": other_costs,
        "projected_profit": estimated_profit,
        "projected_margin_percentage": margin,
        "explanation": "Projected order revenue minus wholesale product cost, shipment fees, and recorded extra costs. Payments affect cash received, not projected profit.",
    }


@app.post("/api/assistant/help")
async def assistant_help(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(422, "Ask a question first")
    profit = projected_profit(db)
    rules = db.scalars(
        select(BrandSeasonRule)
        .options(selectinload(BrandSeasonRule.brand), selectinload(BrandSeasonRule.season))
        .order_by(BrandSeasonRule.id.desc())
        .limit(30)
    ).all()
    context_data = {
        "profit": profit,
        "rules": [{
            "rule_id": rule.id, "season": rule.season.name, "brand": rule.brand.name,
            "markup_percentage": float(rule.markup_percentage * 100),
            "initial_percentage": float(rule.initial_prepayment_percentage * 100),
            "ready_percentage": float(rule.ready_balance_percentage * 100),
            "transport_percentage": float(rule.transport_percentage * 100),
            "special_cost": float(rule.special_cost),
        } for rule in rules],
        "pages": {
            "Home": "/", "Add order": "/manual-entry", "AI intake": "/ai-intake", "Orders": "/orders",
            "Customers": "/customers", "Payments": "/payments", "PDFs": "/reports", "Library": "/library",
            "Rules": "/rules", "To-do": "/operations",
            "Excel files": "/excel", "Issues & suggestions": "/validation", "Source reviews": "/source-reviews",
            "Shipment groups": "/groups", "Seller credit": "/credits", "Storage & backups": "/storage",
        },
        "exports": {"Editable order workbook (download, edit, upload)": "/excel/download", "Blank new-products template": "/excel/download?blank=true", "Full accounting reference report (not editable upload format)": "/exports/business.xlsx"},
    }
    try:
        reply = ai_intake.business_assistant(message, context_data)
    except ValueError as exc:
        return JSONResponse({"answer": str(exc), "profit": profit, "rule_proposal": None}, status_code=503)
    result = reply.model_dump()
    allowed_downloads = {"/exports/business.xlsx", "/excel/download", "/excel/download?blank=true"}
    if result.get("download_url") not in allowed_downloads:
        result["download_url"] = None
    if result.get("download_url") is None and re.search(r"\b(excel|xlsx|spreadsheet|export|download)\b", message, re.IGNORECASE):
        result["download_url"] = "/excel/download"
        result["download_label"] = "Download editable order workbook"
    proposal = result.get("rule_proposal")
    if proposal:
        valid_rule = next((rule for rule in rules if rule.id == proposal.get("rule_id")), None)
        initial = proposal.get("initial_percentage")
        ready = proposal.get("ready_percentage")
        if valid_rule is None or (initial is not None and ready is not None and money(Decimal(str(initial)) + Decimal(str(ready))) != Decimal("100.00")):
            result["rule_proposal"] = None
            result["answer"] += " I could not create a safe rule proposal; open Business rules to review it manually."
    result["profit"] = profit
    return result


@app.post("/assistant/apply-rule")
def apply_assistant_rule(
    rule_id: int = Form(...),
    markup_percentage: str = Form(""), initial_percentage: str = Form(""), ready_percentage: str = Form(""),
    transport_percentage: str = Form(""), special_cost: str = Form(""), explanation: str = Form(""),
    confirmation: str = Form(...), db: Session = Depends(get_db),
):
    if confirmation != "APPLY_AI_RULE_PROPOSAL":
        raise HTTPException(422, "Review and confirm the proposed rule change")
    rule = db.get(BrandSeasonRule, rule_id)
    if rule is None:
        raise HTTPException(404, "Business rule not found")
    updates = {}
    for field, raw in {
        "markup_percentage": markup_percentage, "initial_prepayment_percentage": initial_percentage,
        "ready_balance_percentage": ready_percentage, "transport_percentage": transport_percentage,
    }.items():
        if raw.strip():
            value = required_decimal(raw, field) / Decimal("100")
            if value < ZERO or value > Decimal("1"):
                raise HTTPException(422, f"{field} must be between 0 and 100")
            updates[field] = value
    if special_cost.strip():
        updates["special_cost"] = money(required_decimal(special_cost, "special cost"))
    initial = updates.get("initial_prepayment_percentage", rule.initial_prepayment_percentage)
    ready = updates.get("ready_balance_percentage", rule.ready_balance_percentage)
    if money(initial + ready) != Decimal("1.00"):
        raise HTTPException(422, "Initial and Ready percentages must total 100%")
    for field, value in updates.items():
        previous = getattr(rule, field)
        if previous != value:
            setattr(rule, field, value)
            record_change(db, entity_type="BrandSeasonRule", entity_id=rule.id, field=field, previous_value=previous, new_value=value, source_user="AI proposal + user confirmation", description=f"Applied reviewed AI rule proposal: {explanation[:300]}")
    db.commit()
    return RedirectResponse("/rules?ai_applied=1", status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, db: Session = Depends(get_db)):
    collections = db.scalars(select(Collection).options(
        selectinload(Collection.season),
        selectinload(Collection.items).selectinload(CollectionItem.order_item).selectinload(OrderItem.order).selectinload(Order.brand),
    ).order_by(Collection.created_at.desc())).all()
    files = db.scalars(select(LibraryFile).order_by(LibraryFile.uploaded_at.desc())).all()
    imports = db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc())).all()
    brands = db.scalars(select(Brand).order_by(Brand.name)).all()
    seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
    orders = db.scalars(select(Order).options(selectinload(Order.brand), selectinload(Order.season)).order_by(Order.id.desc())).all()
    items = db.scalars(select(OrderItem).options(
        selectinload(OrderItem.order).selectinload(Order.brand), selectinload(OrderItem.order).selectinload(Order.season)
    ).order_by(OrderItem.id.desc()).limit(500)).all()
    report_root = ROOT_DIR / "data" / "reports"
    report_files = [
        {"name": path.name, "size_bytes": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime)}
        for path in sorted(report_root.glob("*"), key=lambda candidate: candidate.stat().st_mtime, reverse=True)
        if path.is_file() and path.suffix.lower() in {".pdf", ".zip"}
    ] if report_root.exists() else []
    return templates.TemplateResponse(request, "library.html", context(
        request, collections=collections, files=files, imports=imports, report_files=report_files,
        brands=brands, seasons=seasons, orders=orders, items=items,
    ))


@app.post("/library/collections")
def create_collection(name: str = Form(...), description: str = Form(""), season_id: str = Form(""), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(422, "Collection name is required")
    if db.scalar(select(Collection).where(Collection.name == clean_name)):
        raise HTTPException(409, "A collection with this name already exists")
    selected_season = parse_optional_id(season_id, "season")
    if selected_season and db.get(Season, selected_season) is None:
        raise HTTPException(422, "Season not found")
    collection = Collection(name=clean_name, description=description.strip() or None, season_id=selected_season)
    db.add(collection)
    db.flush()
    record_change(db, entity_type="Collection", entity_id=collection.id, field=None, previous_value=None, new_value=collection.name, source_user="web user", description=f"Created collection {collection.name}")
    db.commit()
    return RedirectResponse("/library?created=collection#collections", status_code=303)


@app.post("/library/collections/{collection_id}/items")
def add_collection_item(collection_id: int, order_item_id: int = Form(...), db: Session = Depends(get_db)):
    if db.get(Collection, collection_id) is None or db.get(OrderItem, order_item_id) is None:
        raise HTTPException(404, "Collection or product not found")
    if db.get(CollectionItem, (collection_id, order_item_id)) is None:
        db.add(CollectionItem(collection_id=collection_id, order_item_id=order_item_id))
        db.commit()
    return RedirectResponse("/library#collections", status_code=303)


@app.post("/library/brands")
def create_library_brand(
    name: str = Form(...),
    default_markup: str = Form("35"),
    website_url: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(422, "Brand name is required")
    if db.scalar(select(Brand).where(func.lower(Brand.name) == clean_name.lower())):
        raise HTTPException(409, "This brand already exists")
    markup = required_decimal(default_markup, "default markup")
    if markup < 0 or markup > 1000:
        raise HTTPException(422, "Default markup must be between 0% and 1000%")
    brand = Brand(
        name=clean_name,
        default_markup_percentage=markup / Decimal("100"),
        website_url=validated_https_url(website_url),
        notes=notes.strip() or None,
    )
    db.add(brand)
    db.flush()
    record_change(
        db, entity_type="Brand", entity_id=brand.id, field=None, previous_value=None,
        new_value=brand.name, source_user="web user", description=f"Created brand {brand.name} from the business library",
    )
    db.commit()
    return RedirectResponse("/library?created=brand#brands", status_code=303)


@app.post("/library/files")
def upload_library_file(file: UploadFile = File(...), category: str = Form("other"), note: str = Form(""), db: Session = Depends(get_db)):
    content = file.file.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(422, "File is larger than 25 MB")
    original = re.sub(r"[^A-Za-zА-Яа-я0-9_. -]+", "_", Path(file.filename or "file").name)[:250]
    extension = Path(original).suffix.lower()
    if extension not in {".pdf", ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".docx", ".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(422, "Use PDF, spreadsheet, document, text, JPG, PNG, or WEBP files")
    folder = (ROOT_DIR / "data" / "library").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now():%Y%m%d-%H%M%S}-{hashlib.sha256(content).hexdigest()[:12]}{extension}"
    target = (folder / filename).resolve()
    if target.parent != folder:
        raise HTTPException(422, "Invalid library file path")
    target.write_bytes(content)
    row = LibraryFile(filename=original, stored_path=target.relative_to(ROOT_DIR).as_posix(), category=category.strip() or "other", content_type=file.content_type, size_bytes=len(content), note=note.strip() or None)
    db.add(row)
    db.commit()
    return RedirectResponse("/library?created=file#files", status_code=303)


@app.get("/library/files/{file_id}/download")
def download_library_file(file_id: int, db: Session = Depends(get_db)):
    row = db.get(LibraryFile, file_id)
    if row is None:
        raise HTTPException(404, "File not found")
    root = (ROOT_DIR / "data" / "library").resolve()
    path = (ROOT_DIR / row.stored_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(409, "Library path is invalid") from exc
    if not path.is_file():
        raise HTTPException(404, "Stored file is missing")
    return FileResponse(path, filename=row.filename, media_type=row.content_type or "application/octet-stream")


def validated_https_url(value: str) -> str | None:
    clean = value.strip()
    if not clean:
        return None
    parsed = urlparse(clean)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(422, "Links must use a complete public HTTPS address")
    return clean


@app.post("/library/links")
def save_library_link(target_type: str = Form(...), target_id: int = Form(...), url: str = Form(""), db: Session = Depends(get_db)):
    clean_url = validated_https_url(url)
    mapping = {"brand": (Brand, "website_url"), "catalog": (Order, "catalog_url"), "product": (OrderItem, "product_url")}
    if target_type not in mapping:
        raise HTTPException(422, "Unsupported link type")
    model, field = mapping[target_type]
    target = db.get(model, target_id)
    if target is None:
        raise HTTPException(404, "Link target not found")
    previous = getattr(target, field)
    setattr(target, field, clean_url)
    record_change(db, entity_type=model.__name__, entity_id=target_id, field=field, previous_value=previous, new_value=clean_url, source_user="web user", description=f"Updated {target_type} reference link")
    db.commit()
    return RedirectResponse("/library?updated=link#brands", status_code=303)


@app.get("/api/library/link-suggestions")
def library_link_suggestions(target_type: str, target_id: int, db: Session = Depends(get_db)):
    if target_type == "brand":
        target = db.get(Brand, target_id)
        query = f"{target.name} official website" if target else ""
    elif target_type == "catalog":
        target = db.scalar(select(Order).where(Order.id == target_id).options(selectinload(Order.brand), selectinload(Order.season)))
        query = f"{target.brand.name} {target.season.name} official collection lookbook catalog" if target else ""
    elif target_type == "product":
        target = db.scalar(select(OrderItem).where(OrderItem.id == target_id).options(selectinload(OrderItem.order).selectinload(Order.brand)))
        query = " ".join(filter(None, [target.order.brand.name, target.article, target.name, "official product"])) if target else ""
    else:
        raise HTTPException(422, "Unsupported link type")
    if not target:
        raise HTTPException(404, "Link target not found")
    try:
        links = ai_intake.search_reference_links(query)
    except ValueError as exc:
        return {"query": query, "links": [], "warning": str(exc)}
    return {"query": query, "links": [link.model_dump() for link in links], "warning": None if links else "No links found"}


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    logs = db.scalars(select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(500)).all()
    return templates.TemplateResponse(request, "audit.html", context(request, logs=logs))


from app.excel_exchange import install_excel_exchange
install_excel_exchange(app, templates, context)
from app.source_reviews import install_source_reviews
install_source_reviews(app, templates, context)
install_intake_sources(app, templates, context)
install_worksheet(app, templates, context)
install_storage(app, templates, context)
from app.intake_routes import install_intake
install_intake(app, templates, context)
from app.inbox_routes import install_inbox
install_inbox(app, templates, context)
from app.connections_routes import install_connections
install_connections(app, templates, context)
from app.recovery_routes import install_recovery
install_recovery(app, templates, context)
# Register background job handlers and schedulers.
from app import outbox as _outbox, sync as _sync  # noqa: E402,F401
