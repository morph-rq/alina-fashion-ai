"""Customer-ready Excel and PDF built from one snapshot, with a confidentiality check and versions.

The snapshot is the single source for both files, so the PDF always matches the workbook.
It contains only the customer's own active allocations, priced with the customer's own
rate (base + commission, the layout customers already receive), their deposit, confirmed
payments and balance. Never included: other customers, allocation notes from the supplier
file, factory percentages or totals, the owner's spread, internal notes, credits.

``check_confidential`` scans every XML part of the workbook (all sheets, hidden ones,
comments, defined names, document properties) and the PDF text for other customers'
codes, aliases and names, and for factory-only wording, and refuses to issue the file if
anything leaks.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calculations import ZERO, base_total, money
from app.config import ROOT_DIR
from app.factory_ledger import active_items, deposit_rate, order_lines, owner_customer, season_ledger
from app.models import Customer, CustomerAlias, GeneratedDocument, Season
from app.settlement import split_line_finals

DOCS_DIR = ROOT_DIR / "data" / "documents"
STATUS_RU = {"Ordered": "Заказано", "Confirmed": "Подтверждено", "Production": "В производстве", "Ready": "Готово",
             "Assigned to shipment": "В отгрузке", "In transit": "В пути", "Arrived": "Прибыло", "Delivered": "Доставлено"}
FACTORY_WORDS = ("фабрик", "factory", "закупочн", "себестоим", "маржа", "прибыл", "margin", "profit")


class ConfidentialityError(ValueError):
    pass


def _pct(value: Decimal) -> str:
    text = f"{Decimal(value) * 100:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def customer_snapshot(db: Session, customer: Customer, season: Season, brands: list[str] | None = None,
                      item_ids: list[int] | None = None, label: str | None = None) -> dict:
    """``item_ids`` limits the document to an exact selection (ready items, three selected rows…); its
    totals are then the selected lines only, priced exactly as inside the full order."""
    if customer.is_owner:
        raise ValueError("Это ваш собственный заказ — для него есть внутренний отчёт, а не документ клиента.")
    owner = owner_customer(db)
    owner_code = owner.code if owner else None
    wanted = {b.casefold().strip() for b in brands or []}
    chosen = {int(i) for i in item_ids} if item_ids is not None else None
    subset = False
    orders = []
    for order in sorted(season.orders, key=lambda o: o.brand.name.lower()):
        if wanted and order.brand.name.casefold().strip() not in wanted:
            continue
        mine = [i for i in active_items(order) if i.customer_id == customer.id]
        items = [i for i in mine if chosen is None or i.id in chosen]
        if not items:
            continue
        lines = [l for l in order_lines(order, owner_code) if l.customer == customer.code]
        total = money(sum((l.total for l in lines), ZERO))
        base = money(sum((l.base for l in lines), ZERO))
        rates = {l.rate for l in lines if l.manual_total is None}
        commission_style = len(rates) == 1 and all(l.manual_total is None for l in lines)
        finals = split_line_finals([(i.id, base_total(i.wholesale_unit_price, i.quantity)) for i in mine], total)
        if len(items) != len(mine):
            subset = True
            total = money(sum((finals.get(i.id, ZERO) for i in items), ZERO))
            base = money(sum((base_total(i.wholesale_unit_price, i.quantity) for i in items), ZERO))
        rows = []
        for item in sorted(items, key=lambda i: (i.source_row or 0, i.id)):
            line_base = base_total(item.wholesale_unit_price, item.quantity)
            final = finals.get(item.id, ZERO)
            rows.append({
                "image": item.image_path, "name": item.name or "", "article": item.article or "", "color": item.color or "",
                "size": item.size or "", "quantity": item.quantity,
                "unit": str(money(item.wholesale_unit_price or 0)) if commission_style else str(money(final / item.quantity) if item.quantity else final),
                "line": str(line_base) if commission_style else str(final),
                "status": STATUS_RU.get(item.status, item.status),
                "shipment": item.shipment.name if item.shipment else "",
            })
        rate = next(iter(rates)) if commission_style else None
        dep_rate = Decimal(deposit_rate(order))
        orders.append({"brand": order.brand.name, "rows": rows, "base": str(base), "total": str(total),
                       "commission_rate": str(rate) if rate is not None else None,
                       "commission": str(money(total - base)) if commission_style else None,
                       "deposit_rate": str(dep_rate), "deposit": str(money(total * dep_rate)),
                       "ready_window": " – ".join(d.strftime("%d.%m.%Y") for d in (order.factory_ready_from, order.factory_ready_to) if d)
                       or (order.factory_ready_note or "")})
    if not orders:
        raise ValueError(f"У {customer.code} нет позиций в {season.name}" + (f" ({', '.join(brands)})" if brands else "") + ".")
    ledger = season_ledger(db, season)
    c = ledger.settlement.customers.get(customer.code)
    paid = money((c.confirmed_to_factory + c.confirmed_to_owner) if c else ZERO)
    total = money(sum((Decimal(o["total"]) for o in orders), ZERO))
    deposit = money(sum((Decimal(o["deposit"]) for o in orders), ZERO))
    ready_due = ledger.customer_ready_balance.get(customer.code, ZERO) if not wanted and not subset else None
    if subset:
        # Payments belong to the whole order, not to a few lines: a selection shows only its own totals.
        return {"customer": {"code": customer.code, "name": customer.name or customer.code}, "season": season.name,
                "brands": [o["brand"] for o in orders], "orders": orders, "currency": "EUR", "subset": True,
                "label": label or "Выбранные позиции", "item_ids": sorted(chosen or []),
                "total": str(total), "deposit": str(deposit), "paid": None, "deposit_outstanding": None,
                "ready_due": None, "balance": None, "credit": None, "payments": []}
    return {
        "customer": {"code": customer.code, "name": customer.name or customer.code},
        "season": season.name, "brands": [o["brand"] for o in orders], "orders": orders, "currency": "EUR",
        "label": label, "item_ids": sorted(chosen) if chosen is not None else None,
        "total": str(total), "deposit": str(deposit), "paid": str(paid),
        "deposit_outstanding": str(max(money(deposit - paid), ZERO)),
        "ready_due": str(ready_due) if ready_due is not None else None,
        "balance": str(max(money(total - paid), ZERO)),
        "credit": str(max(money(paid - total), ZERO)),
        "payments": [{"date": p["date"].isoformat() if p.get("date") else "", "amount": str(p["amount"])}
                     for p in ledger.payments if p.get("payer") == customer.code and p.get("confirmed")
                     and (not wanted or p.get("order_id") is None or p.get("order_id") in {o.id for o in season.orders if o.brand.name.casefold().strip() in wanted})],
    }


def figures(snapshot: dict, short: bool = False) -> list[tuple[str, str]]:
    if snapshot.get("subset"):
        return [("Итого по выбранным позициям", snapshot["total"]), ("Предоплата по ним", snapshot["deposit"])]
    rows = [("Итого заказ", snapshot["total"]), ("Предоплата", snapshot["deposit"]), ("Оплачено", snapshot["paid"]),
            ("Внести по предоплате" if short else "Осталось внести по предоплате", snapshot["deposit_outstanding"])]
    if snapshot.get("ready_due") not in (None, "0.00", "0"):
        rows.append(("За готовые изделия" if short else "К оплате за готовые изделия", snapshot["ready_due"]))
    rows.append(("Остаток по заказу", snapshot["balance"]))
    if not short and snapshot.get("credit") not in (None, "0.00"):
        rows.append(("Переплата (в вашу пользу)", snapshot["credit"]))
    return rows


def fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _image_file(path: str | None, root: Path) -> Path | None:
    if not path:
        return None
    candidate = (root / path).resolve()
    images = (root / "data" / "images").resolve()
    return candidate if candidate.is_relative_to(images) and candidate.is_file() else None


def _thumb(path: Path, size=(160, 200)) -> io.BytesIO | None:
    try:
        with PILImage.open(path) as source:
            source.thumbnail(size)
            canvas = PILImage.new("RGB", source.size, "white")
            canvas.paste(source.convert("RGBA"), mask=source.convert("RGBA").getchannel("A"))
            out = io.BytesIO()
            canvas.save(out, "JPEG", quality=85)
            out.seek(0)
            return out
    except Exception:  # noqa: BLE001 - a damaged photo is shown as "no photo"
        return None


def build_excel(snapshot: dict, root: Path = ROOT_DIR) -> bytes:
    book = Workbook()
    book.properties.creator = "Alina Fashion"
    book.properties.title = f"{snapshot['customer']['name']} · {snapshot['season']}"
    book.properties.lastModifiedBy = "Alina Fashion"
    ink, gold, line = "1C2130", "AA8957", Side(style="thin", color="DADFD6")
    money_fmt = '#,##0.00 "€"'
    summary = book.active
    summary.title = "ИТОГО"
    summary["A1"] = f"Заказ {snapshot['season']} — {snapshot['customer']['name']}"
    summary["A1"].font = Font(size=16, bold=True, color=ink)
    summary["A2"] = f"Сформировано {datetime.now():%d.%m.%Y %H:%M}. Суммы в евро."
    summary["A2"].font = Font(size=9, color="6A7180")
    headers = ["Бренд", "Сумма заказа с комиссией", "Предоплата", "Сумма предоплаты", "Сроки готовности"]
    for col, label in enumerate(headers, 1):
        cell = summary.cell(4, col, label)
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=ink)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    row = 5
    for order in snapshot["orders"]:
        summary.cell(row, 1, order["brand"])
        summary.cell(row, 2, float(order["total"])).number_format = money_fmt
        summary.cell(row, 3, float(order["deposit_rate"])).number_format = "0%"
        summary.cell(row, 4, float(order["deposit"])).number_format = money_fmt
        summary.cell(row, 5, order["ready_window"] or None)
        row += 1
    row += 1
    if snapshot.get("label"):
        summary.cell(row, 1, snapshot["label"]).font = Font(italic=True, color="6A7180")
        row += 1
    for label, value in figures(snapshot):
        summary.cell(row, 1, label).font = Font(bold=True)
        cell = summary.cell(row, 2, float(value))
        cell.number_format, cell.font = money_fmt, Font(bold=True, color=gold if label.startswith("Осталось") else ink)
        row += 1
    for col, width in zip("ABCDE", (28, 24, 12, 18, 24)):
        summary.column_dimensions[col].width = width
    used_titles = {"ИТОГО"}
    for order in snapshot["orders"]:
        title = re.sub(r"[\[\]:*?/\\]", " ", order["brand"])[:28].strip() or "Бренд"
        while title in used_titles:
            title = title[:25] + "_" + str(len(used_titles))
        used_titles.add(title)
        sheet = book.create_sheet(title)
        sheet.merge_cells("A1:I1")
        sheet["A1"] = f"{order['brand']} · {snapshot['season']} · {snapshot['customer']['name']}"
        sheet["A1"].font = Font(size=14, bold=True, color=ink)
        heads = ["Фото", "Наименование", "Артикул", "Цвет", "Размер", "Кол-во", "Цена", "Сумма", "Статус"]
        for col, label in enumerate(heads, 1):
            cell = sheet.cell(3, col, label)
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=ink)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        r = 4
        for item in order["rows"]:
            values = [None, item["name"], item["article"], item["color"], item["size"], item["quantity"],
                      float(item["unit"]), float(item["line"]), item["status"] + (f" · {item['shipment']}" if item["shipment"] else "")]
            for col, value in enumerate(values, 1):
                cell = sheet.cell(r, col, value if value != "" else None)     # an empty text cell makes Excel "repair" the file
                cell.border = Border(bottom=line)
                cell.alignment = Alignment(vertical="center", wrap_text=col in (2, 9))
                if isinstance(value, str) and value:
                    cell.data_type = "s"
            sheet.cell(r, 7).number_format = sheet.cell(r, 8).number_format = money_fmt
            image_path = _image_file(item["image"], root)
            thumb = _thumb(image_path) if image_path else None
            if thumb is not None:
                picture = XLImage(thumb)
                ratio = picture.width / picture.height if picture.height else 1
                picture.height, picture.width = 70, int(70 * ratio)
                sheet.add_image(picture, f"A{r}")
                sheet.row_dimensions[r].height = 56
            r += 1
        sheet.cell(r, 7, "Итого").font = Font(bold=True)
        sheet.cell(r, 8, float(order["base"] if order["commission_rate"] is not None else order["total"])).number_format = money_fmt
        if order["commission_rate"] is not None:
            sheet.cell(r + 1, 6, float(order["commission_rate"])).number_format = "0.##%"
            sheet.cell(r + 1, 7, "Комиссия")
            sheet.cell(r + 1, 8, float(order["commission"])).number_format = money_fmt
            r += 1
        sheet.cell(r + 1, 7, "Всего").font = Font(bold=True)
        total_cell = sheet.cell(r + 1, 8, float(order["total"]))
        total_cell.number_format, total_cell.font = money_fmt, Font(bold=True)
        sheet.cell(r + 2, 7, f"Предоплата {_pct(Decimal(order['deposit_rate']))}")
        sheet.cell(r + 2, 8, float(order["deposit"])).number_format = money_fmt
        for col, width in zip("ABCDEFGHI", (13, 34, 20, 14, 10, 8, 13, 14, 18)):
            sheet.column_dimensions[col].width = width
        sheet.freeze_panes = "B4"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def build_pdf(snapshot: dict, root: Path = ROOT_DIR) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from app.pdf_report import register_fonts
    regular, bold = register_fonts()
    ink, muted, stone, gold = colors.HexColor("#1C2130"), colors.HexColor("#6A7180"), colors.HexColor("#EDEFEA"), colors.HexColor("#AA8957")
    out = io.BytesIO()
    title = f"{snapshot['customer']['name']} · {snapshot['season']}"
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=title, author="Alina Fashion", subject="Заказ", creator="Alina Fashion")
    body = ParagraphStyle("b", fontName=regular, fontSize=8.5, leading=11, textColor=ink)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, textColor=muted)
    num = ParagraphStyle("n", parent=body, alignment=TA_RIGHT)
    head = ParagraphStyle("h", fontName=bold, fontSize=18, leading=22, textColor=ink, spaceAfter=2 * mm)
    h2 = ParagraphStyle("h2", fontName=bold, fontSize=12, leading=15, textColor=ink, spaceBefore=5 * mm, spaceAfter=2 * mm)

    def eur(value) -> str:
        return f"€ {Decimal(value):,.2f}".replace(",", " ")

    story = [Paragraph(escape(f"Заказ {snapshot['season']} — {snapshot['customer']['name']}"), head),
             Paragraph(escape(f"Сформировано {datetime.now():%d.%m.%Y %H:%M} · {', '.join(snapshot['brands'])}"
                              + (f" · {snapshot['label']}" if snapshot.get("label") else "")), small),
             Spacer(1, 4 * mm)]
    shown = figures(snapshot, short=True)
    box = Table([[Paragraph(escape(l), small) for l, _ in shown], [Paragraph(f"<b>{eur(v)}</b>", body) for _, v in shown]],
                colWidths=[(A4[0] - 28 * mm) / len(shown)] * len(shown))
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), stone), ("BOX", (0, 0), (-1, -1), .5, stone),
                             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(box)
    widths = [18 * mm, 58 * mm, 30 * mm, 14 * mm, 10 * mm, 22 * mm, 24 * mm]
    for order in snapshot["orders"]:
        story.append(Paragraph(escape(order["brand"]) + (f"<font size=8 color='#6A7180'>  · готовность {escape(order['ready_window'])}</font>" if order["ready_window"] else ""), h2))
        data = [[Paragraph(f"<b>{h}</b>", small) for h in ("Фото", "Изделие", "Артикул / цвет", "Размер", "Шт.", "Цена", "Сумма")]]
        for item in order["rows"]:
            image_path = _image_file(item["image"], root)
            thumb = _thumb(image_path, (120, 150)) if image_path else None
            if thumb is not None:
                with PILImage.open(thumb) as probe:
                    ratio = probe.width / probe.height if probe.height else 1
                thumb.seek(0)
                cell = Image(thumb, width=min(16 * mm, 20 * mm * ratio), height=min(20 * mm, 16 * mm / ratio))
            else:
                cell = Paragraph("—", small)
            data.append([cell, Paragraph(escape(item["name"]) + f"<br/><font size=7 color='#6A7180'>{escape(item['status'])}</font>", body),
                         Paragraph(escape(" · ".join(x for x in (item["article"], item["color"]) if x)), small),
                         Paragraph(escape(str(item["size"])), body), Paragraph(str(item["quantity"]), num),
                         Paragraph(eur(item["unit"]), num), Paragraph(eur(item["line"]), num)])
        totals = [["", "", "", "", "", Paragraph("Итого", body), Paragraph(eur(order["base"] if order["commission_rate"] else order["total"]), num)]]
        if order["commission_rate"]:
            totals.append(["", "", "", "", "", Paragraph(f"Комиссия {_pct(Decimal(order['commission_rate']))}", body), Paragraph(eur(order["commission"]), num)])
        totals.append(["", "", "", "", "", Paragraph("<b>Всего</b>", body), Paragraph(f"<b>{eur(order['total'])}</b>", num)])
        totals.append(["", "", "", "", "", Paragraph(f"Предоплата {_pct(Decimal(order['deposit_rate']))}", body), Paragraph(eur(order["deposit"]), num)])
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), .3, stone), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("BACKGROUND", (0, 0), (-1, 0), stone)]))
        story.append(table)
        tail = Table(totals, colWidths=widths)
        tail.setStyle(TableStyle([("LINEABOVE", (5, -2), (-1, -2), .6, gold)]))
        story.append(KeepTogether(tail))
    if snapshot["payments"]:
        story.append(Paragraph("Оплаты", h2))
        pays = Table([[Paragraph(escape(p["date"]), body), Paragraph(eur(p["amount"]), num)] for p in snapshot["payments"]],
                     colWidths=[40 * mm, 30 * mm], hAlign="LEFT")
        story.append(pays)

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.setFillColor(muted)
        canvas.drawString(14 * mm, 9 * mm, title)
        canvas.drawRightString(A4[0] - 14 * mm, 9 * mm, f"Стр. {document.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return out.getvalue()


def forbidden_terms(db: Session, customer: Customer) -> list[str]:
    """Other customers' codes, names and aliases. The owner is the seller, not a confidential customer:
    her name may appear as the business; her own allocations are excluded by the snapshot itself."""
    terms = []
    for other in db.scalars(select(Customer).where(Customer.id != customer.id, Customer.is_owner.is_(False))):
        terms.append(other.code)
        if other.name and other.name.casefold() not in (customer.name or "").casefold():
            terms.append(other.name)
    for alias in db.scalars(select(CustomerAlias).where(CustomerAlias.customer_id != customer.id)):
        if not alias.customer.is_owner:
            terms.append(alias.alias)
    mine = {customer.code.casefold(), (customer.name or "").casefold()} | {
        a.alias.casefold() for a in db.scalars(select(CustomerAlias).where(CustomerAlias.customer_id == customer.id))}
    return sorted({t for t in terms if t and len(t) >= 2 and t.casefold() not in mine}, key=len, reverse=True)


def _xlsx_text(content: bytes) -> str:
    from xml.sax.saxutils import unescape
    parts = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            if info.filename.endswith((".xml", ".rels", ".vml")):
                parts.append(unescape(re.sub(r"<[^>]+>", " ", archive.read(info).decode("utf-8", "ignore"))))
    return "\n".join(parts)


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    meta = " ".join(str(v) for v in (reader.metadata or {}).values())
    return meta + "\n" + "\n".join(page.extract_text() or "" for page in reader.pages)


def check_confidential(text: str, terms: list[str], snapshot: dict) -> list[str]:
    problems = []
    product_text = " ".join(f"{r['name']} {r['article']} {r['color']}" for o in snapshot["orders"] for r in o["rows"]).casefold()
    for term in terms:
        pattern = re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.I)
        if pattern.search(text) and not pattern.search(product_text):
            problems.append(f"в документе встречается «{term}» (другой клиент)")
    lowered = text.casefold()
    for word in FACTORY_WORDS:
        if word in lowered:
            problems.append(f"в документе есть служебное слово «{word}»")
    return problems


def generate(db: Session, customer: Customer, season: Season, *, brands: list[str] | None = None,
             issue: bool = False, root: Path | None = None, actor: str = "operator", item_ids: list[int] | None = None,
             label: str | None = None, formats: tuple[str, ...] = ("xlsx", "pdf")) -> list[GeneratedDocument]:
    """Build the files from one snapshot, verify them, and store them as a new version."""
    root = root or ROOT_DIR
    snapshot = customer_snapshot(db, customer, season, brands, item_ids, label)
    builders = {"xlsx": build_excel, "pdf": build_pdf}
    content = {kind: builders[kind](snapshot, root) for kind in ("xlsx", "pdf") if kind in formats}
    terms = forbidden_terms(db, customer)
    for kind, data in content.items():
        problems = check_confidential(_xlsx_text(data) if kind == "xlsx" else _pdf_text(data), terms, snapshot)
        if problems:
            raise ConfidentialityError(f"{kind.upper()} не выпущен: " + "; ".join(problems))
    print_ = fingerprint(snapshot)
    brand_filter = ", ".join(sorted(brands)) if brands else None
    if snapshot.get("subset"):
        brand_filter = ((brand_filter + " · ") if brand_filter else "") + (label or "выборка")
        brand_filter = brand_filter[:200]
    previous = list(db.scalars(select(GeneratedDocument).where(
        GeneratedDocument.customer_id == customer.id, GeneratedDocument.season_id == season.id,
        GeneratedDocument.brand_filter.is_(None) if brand_filter is None else GeneratedDocument.brand_filter == brand_filter)))
    version = max((d.version for d in previous), default=0) + 1
    folder = root / "data" / "documents" / season.name / re.sub(r"[^\w-]+", "_", customer.code)
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    stamp = datetime.now(timezone.utc)
    for kind, data in content.items():
        name = f"{season.name}_{re.sub(r'[^A-Za-z0-9_-]+', '_', customer.code)}" + (f"_{re.sub(r'[^A-Za-z0-9]+', '_', brand_filter)}" if brand_filter else "") + f"_v{version}.{kind}"
        path = folder / name
        path.write_bytes(data)
        document = GeneratedDocument(kind="customer", file_format=kind, customer_id=customer.id, season_id=season.id,
                                     brand_filter=brand_filter, status="issued" if issue else "preview", version=version,
                                     stored_path=str(path.relative_to(root)), sha256=hashlib.sha256(data).hexdigest(),
                                     data_fingerprint=print_, snapshot_json=json.dumps(snapshot, ensure_ascii=False),
                                     issued_at=stamp if issue else None)
        db.add(document)
        made.append(document)
    from app.audit import record_change
    db.flush()
    record_change(db, entity_type="GeneratedDocument", entity_id=made[0].id, field="version", previous_value=None,
                  new_value=version, source_user=actor,
                  description=f"Документы для {customer.code} {season.name} v{version}" + (" выпущены" if issue else " (предпросмотр)"))
    db.commit()
    return made


def is_outdated(db: Session, document: GeneratedDocument) -> bool:
    customer, season = db.get(Customer, document.customer_id), db.get(Season, document.season_id)
    if customer is None or season is None:
        return True
    try:
        saved = json.loads(document.snapshot_json or "{}")
        brands = saved.get("brands") if document.brand_filter else None
        return fingerprint(customer_snapshot(db, customer, season, brands, saved.get("item_ids") if saved.get("subset") else None,
                                             saved.get("label"))) != document.data_fingerprint
    except ValueError:
        return True


def issue(db: Session, document: GeneratedDocument, actor: str = "operator") -> list[GeneratedDocument]:
    """Mark a preview pair as issued. Issued files are never rewritten; a change makes a new version."""
    pair = list(db.scalars(select(GeneratedDocument).where(GeneratedDocument.customer_id == document.customer_id,
                                                          GeneratedDocument.season_id == document.season_id,
                                                          GeneratedDocument.version == document.version,
                                                          GeneratedDocument.data_fingerprint == document.data_fingerprint)))
    if is_outdated(db, document):
        raise ValueError("Данные изменились после предпросмотра — сформируйте документы заново.")
    stamp = datetime.now(timezone.utc)
    for doc in pair:
        doc.status, doc.issued_at = "issued", stamp
    from app.audit import record_change
    record_change(db, entity_type="GeneratedDocument", entity_id=document.id, field="status", previous_value="preview",
                  new_value="issued", source_user=actor, description=f"Документы v{document.version} утверждены")
    db.commit()
    return pair
