from __future__ import annotations

import re
from io import BytesIO
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from PIL import Image as PILImage

from app.calculations import PAYMENT_TYPE_LABELS, item_amounts, signed_payment_amount
from app.config import ROOT_DIR, settings
from app.models import Customer, Shipment
from app.services import CustomerStatement, customer_statement


INK = colors.HexColor("#171714")
MUTED = colors.HexColor("#6D6A63")
STONE = colors.HexColor("#EEEAE2")
CREAM = colors.HexColor("#F8F6F1")
GOLD = colors.HexColor("#AA8957")
WHITE = colors.white


# Fonts with Cyrillic glyphs (Helvetica has none, so Russian names would print as boxes).
FONT_CANDIDATES = (
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
)


def register_fonts() -> tuple[str, str]:
    for regular_path, bold_path in FONT_CANDIDATES:
        regular, bold = Path(regular_path), Path(bold_path)
        if regular.exists() and bold.exists():
            if "FashionSans" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("FashionSans", regular))
                pdfmetrics.registerFont(TTFont("FashionSans-Bold", bold))
            return "FashionSans", "FashionSans-Bold"
    return "Helvetica", "Helvetica-Bold"


def fmt(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "customer"


def page_decoration(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(INK)
    canvas.rect(0, height - 9 * mm, width, 9 * mm, stroke=0, fill=1)
    canvas.setStrokeColor(STONE)
    canvas.line(18 * mm, 14 * mm, width - 18 * mm, 14 * mm)
    canvas.setFont(doc.regular_font, 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm, "Customer order statement")
    canvas.drawRightString(width - 18 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def resolve_product_image_path(path: str | None) -> Path | None:
    """Resolve both legacy root-relative and managed-image-relative database paths."""
    if not path:
        return None
    normalized = Path(path.replace("\\", "/"))
    if normalized.is_absolute():
        return None
    parts = normalized.parts
    relative = Path(*parts[2:]) if len(parts) >= 2 and parts[:2] == ("data", "images") else normalized
    image_root = (ROOT_DIR / "data" / "images").resolve()
    absolute = (image_root / relative).resolve()
    if not absolute.is_relative_to(image_root) or not absolute.is_file():
        return None
    return absolute


def product_image(path: str | None) -> object:
    absolute = resolve_product_image_path(path)
    if absolute is None or absolute.stat().st_size < 80:
        return Paragraph("<font color='#AAA69D'>NO IMAGE</font>", ParagraphStyle("noimage", fontSize=6, alignment=TA_CENTER))
    try:
        with PILImage.open(absolute) as source:
            source.thumbnail((220, 260))
            if source.mode != "RGB":
                canvas = PILImage.new("RGB", source.size, "white")
                if "A" in source.getbands():
                    canvas.paste(source, mask=source.getchannel("A"))
                else:
                    canvas.paste(source)
                source = canvas
            buffer = BytesIO()
            source.save(buffer, format="JPEG", quality=82, optimize=True)
            buffer.seek(0)
            ratio = source.width / source.height if source.height else 1
        max_width, max_height = 16 * mm, 20 * mm
        width = min(max_width, max_height * ratio)
        height = min(max_height, max_width / ratio) if ratio else max_height
        image = Image(buffer, width=width, height=height)
        image.hAlign = "CENTER"
        return image
    except Exception:
        return Paragraph("<font color='#AAA69D'>IMAGE ERROR</font>", ParagraphStyle("noimage", fontSize=6, alignment=TA_CENTER))


def build_customer_pdf(
    session: Session,
    customer_id: int,
    output_dir: Path | None = None,
    season_name: str | None = None,
) -> Path:
    statement = customer_statement(session, customer_id, season_name)
    output_dir = output_dir or (ROOT_DIR / "data" / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_filename(statement.customer.code)}_{safe_filename(statement.season.name)}.pdf"
    regular_font, bold_font = register_fonts()
    document = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=19 * mm,
        title=f"{statement.season.name} - {statement.customer.name or statement.customer.code}",
        author=settings.app_name,
    )
    document.regular_font = regular_font
    document.bold_font = bold_font
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], fontName=bold_font, fontSize=24, leading=28, textColor=INK, alignment=TA_LEFT, spaceAfter=3 * mm)
    eyebrow = ParagraphStyle("eyebrow", fontName=bold_font, fontSize=7.5, leading=10, textColor=GOLD, tracking=1.4, spaceAfter=2 * mm)
    heading = ParagraphStyle("heading", fontName=bold_font, fontSize=12, leading=15, textColor=INK, spaceBefore=5 * mm, spaceAfter=3 * mm)
    body = ParagraphStyle("body", fontName=regular_font, fontSize=8, leading=10.5, textColor=INK)
    small = ParagraphStyle("small", fontName=regular_font, fontSize=7, leading=9, textColor=MUTED)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)
    story: list[object] = [
        Paragraph("YOUR ORDER STATEMENT", eyebrow),
        Paragraph(f"{escape(statement.customer.name or 'Customer')} - {escape(statement.season.name)}", title),
        Paragraph(f"Customer reference: {escape(statement.customer.code)}", small),
        Spacer(1, 7 * mm),
    ]
    receipt_values = list(statement.expected_receipts.values())
    expected_receipt = (
        "pending payment" if receipt_values and not all(receipt_values)
        else max(receipt_values).isoformat() if receipt_values
        else "-"
    )
    summary_data = [
        ["ORDER VALUE", "PAYMENTS RECEIVED", "AMOUNT DUE", "EXPECTED DELIVERY"],
        [fmt(statement.ordered), fmt(statement.account.total_paid), fmt(statement.account.outstanding), expected_receipt],
        ["READY GOODS", "SHIPPED GOODS", "DELIVERY CHARGES", "BALANCE DUE"],
        [fmt(statement.ready), fmt(statement.shipped), fmt(statement.allocated_costs), fmt(statement.account.outstanding)],
    ]
    summary = Table(summary_data, colWidths=[43.5 * mm] * 4, hAlign="LEFT")
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("BACKGROUND", (0, 2), (-1, 2), INK),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("TEXTCOLOR", (0, 2), (-1, 2), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), bold_font),
                ("FONTNAME", (0, 2), (-1, 2), bold_font),
                ("FONTSIZE", (0, 0), (-1, 0), 6.3),
                ("FONTSIZE", (0, 2), (-1, 2), 6.3),
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 1), (-1, 1), bold_font),
                ("FONTNAME", (0, 3), (-1, 3), bold_font),
                ("FONTSIZE", (0, 1), (-1, 1), 8.5),
                ("FONTSIZE", (0, 3), (-1, 3), 8.5),
                ("BACKGROUND", (0, 1), (-1, 1), CREAM),
                ("BACKGROUND", (0, 3), (-1, 3), CREAM),
                ("BOX", (0, 0), (-1, -1), 0.6, INK),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, STONE),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    story.extend([summary, Paragraph(f"Amounts in {settings.currency}. Delivery estimates may update as payments and shipment status change.", small)])
    story.append(Paragraph("Products", heading))
    product_rows = [["", "BRAND / PRODUCT", "ARTICLE", "DETAILS", "FINAL", "STATUS / SHIPMENT"]]
    for item in statement.items:
        shipment_name = item.shipment.name if item.shipment else "-"
        product_rows.append(
            [
                product_image(item.image_path),
                Paragraph(f"<b>{item.order.brand.name}</b><br/>{item.name or '-'}", body),
                Paragraph(item.article or "-", small),
                Paragraph(f"{item.color or '-'}<br/>Size {item.size or '-'} / Qty {item.quantity}", small),
                Paragraph(fmt(item_amounts(item).final), right),
                Paragraph(f"{escape(item.status)}<br/><font color='#6D6A63'>{escape(shipment_name)}</font>", small),
            ]
        )
    products = Table(product_rows, colWidths=[19 * mm, 44 * mm, 27 * mm, 35 * mm, 22 * mm, 29 * mm], repeatRows=1, hAlign="LEFT")
    products.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), STONE),
                ("FONTNAME", (0, 0), (-1, 0), bold_font),
                ("FONTSIZE", (0, 0), (-1, 0), 6.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (4, 1), (4, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.35, STONE),
                ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
            ]
        )
    )
    story.append(products)
    story.append(Paragraph("Payments", heading))
    payment_rows = [["DATE", "PAYMENT", "AMOUNT"]]
    if statement.payments:
        for payment in statement.payments:
            payment_rows.append([
                payment.date.isoformat(),
                PAYMENT_TYPE_LABELS.get(payment.type, payment.type),
                fmt(signed_payment_amount(payment.amount, payment.type)),
            ])
        payment_rows.append(["", "Net payments received", fmt(statement.account.total_paid)])
    else:
        payment_rows.append(["-", "No payments recorded", "0.00"])
    payments_table = Table(payment_rows, colWidths=[36 * mm, 108 * mm, 32 * mm], repeatRows=1)
    payments_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), STONE),
                ("FONTNAME", (0, 0), (-1, 0), bold_font),
                ("FONTNAME", (0, 1), (-1, -1), regular_font),
                ("FONTSIZE", (0, 0), (-1, -1), 7.2),
                ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.35, STONE),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ] + ([("FONTNAME", (0, -1), (-1, -1), bold_font), ("LINEABOVE", (0, -1), (-1, -1), 0.7, INK)] if statement.payments else [])
        )
    )
    story.append(payments_table)
    story.append(Paragraph("Shipments", heading))
    shipment_rows = [["SHIPMENT", "DATE / EXPECTED", "PRODUCTS INCLUDED", "DELIVERY CHARGE", "STATUS"]]
    for shipment_id, items in sorted(statement.shipments.items(), key=lambda pair: pair[0]):
        shipment = session.get(Shipment, shipment_id)
        shipment_rows.append(
            [
                shipment.name if shipment else str(shipment_id),
                f"{shipment.date.isoformat() if shipment and shipment.date else '-'} / {statement.shipment_expected_receipts.get(shipment_id).isoformat() if statement.shipment_expected_receipts.get(shipment_id) else 'pending payment'}",
                Paragraph(", ".join((item.article or item.name or f"Item {item.id}") for item in items), small),
                fmt(statement.shipment_costs.get(shipment_id, Decimal("0"))),
                shipment.status if shipment else "-",
            ]
        )
    if len(shipment_rows) == 1:
        shipment_rows.append(["-", "-", "No shipped products", "0.00", "-"])
    shipment_table = Table(shipment_rows, colWidths=[24 * mm, 34 * mm, 64 * mm, 26 * mm, 26 * mm], repeatRows=1)
    shipment_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), STONE), ("FONTNAME", (0, 0), (-1, 0), bold_font), ("FONTNAME", (0, 1), (-1, -1), regular_font), ("FONTSIZE", (0, 0), (-1, -1), 7.2), ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.35, STONE), ("TOPPADDING", (0, 0), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm)]))
    story.append(shipment_table)
    story.extend(
        [
            Spacer(1, 7 * mm),
            KeepTogether(
                Table(
                    [
                        ["CURRENT CHARGES", fmt(statement.account.total_charged)],
                        ["PAYMENTS RECEIVED", fmt(statement.account.total_paid)],
                        ["BALANCE DUE", fmt(statement.account.outstanding)],
                        ["ACCOUNT CREDIT", fmt(statement.account.overpayment)],
                    ],
                    colWidths=[55 * mm, 40 * mm],
                    hAlign="RIGHT",
                    style=TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), bold_font),
                            ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                            ("LINEABOVE", (0, 0), (-1, 0), 1, INK),
                            ("LINEABOVE", (0, 2), (-1, 2), 0.7, GOLD),
                            ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                        ]
                    ),
                )
            ),
        ]
    )
    document.build(story, onFirstPage=page_decoration, onLaterPages=page_decoration)
    return output_path


def build_all_customer_pdfs(
    session: Session,
    customer_ids: set[int] | None = None,
    suffix: str = "customer_statements",
    season_name: str | None = None,
) -> tuple[Path, list[Path]]:
    query = select(Customer).order_by(Customer.code)
    if customer_ids is not None:
        query = query.where(Customer.id.in_(customer_ids))
    customers = session.scalars(query).all()
    output_dir = ROOT_DIR / "data" / "reports"
    paths = []
    for customer in customers:
        statement = customer_statement(session, customer.id, season_name)
        if statement.items or statement.payments:
            paths.append(build_customer_pdf(session, customer.id, output_dir, season_name))
    selected_season = season_name or (customer_statement(session, customers[0].id).season.name if customers else settings.default_season)
    zip_path = output_dir / f"{safe_filename(selected_season)}_{safe_filename(suffix)}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)
    return zip_path, paths
