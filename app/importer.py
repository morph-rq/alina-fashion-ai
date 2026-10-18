from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_change
from app.calculations import ZERO, money
from app.config import ROOT_DIR, settings
from app.worksheet import source_status, ensure_import_groups
from app.models import (
    AllocationReview,
    Brand,
    BrandSeasonRule,
    Customer,
    CustomerAlias,
    Expense,
    ImportBatch,
    ImportChange,
    ImportReview,
    Order,
    OrderItem,
    Payment,
    ReadyBatch,
    Season,
    Shipment,
    ShipmentItem,
    SellerCreditAccount,
)


PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEA"
    "HnOcQAAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class SheetSpec:
    sheet: str
    brand: str
    rows: tuple[int, ...]
    markup: Decimal
    name_cols: tuple[str, ...]
    article_col: str | None
    color_cols: tuple[str, ...]
    price_col: str
    total_col: str
    qty_col: str | None = None
    size_col: str | None = None
    size_columns: tuple[str, ...] = ()
    size_header_row: int | None = None
    customer_col: str | None = None
    ready_col: str | None = None
    shipment_cols: dict[str, str] = field(default_factory=dict)
    ready_when_unallocated: bool = False
    size_from_customer_note: bool = False
    reference_only: bool = False


def cols(start: str, end: str) -> tuple[str, ...]:
    return tuple(
        get_column_letter(index)
        for index in range(column_index_from_string(start), column_index_from_string(end) + 1)
    )


SPECS = [
    SheetSpec("BV ", "BV", (2,), Decimal("0.35"), ("B",), None, (), "G", "G", size_col="E", customer_col="I", ready_when_unallocated=True),
    SheetSpec("Dior main ", "Dior Main", tuple(range(3, 9)), Decimal("0.35"), ("C",), "D", ("E", "F"), "I", "J", qty_col="H", size_col="G", customer_col="K", ready_col="M", shipment_cols={"N": "Sped 10"}),
    SheetSpec("Balenciaga main", "Balenciaga Main", tuple(range(4, 7)), Decimal("0.30"), ("E",), "D", ("F",), "G", "J", qty_col="I", size_col="H", ready_col="K", shipment_cols={"L": "Sped 16"}),
    SheetSpec("The ROW main (PRE)", "The Row Main", (3, 4, 5, 6, 7, 8, 9, 13), Decimal("0.35"), ("C",), "D", ("E", "F"), "P", "Q", qty_col="O", size_columns=cols("H", "N"), size_header_row=2, ready_col="S"),
    SheetSpec("Zimmerman", "Zimmerman", tuple(range(3, 9)), Decimal("0.25"), ("B",), "C", ("D",), "J", "M", qty_col="L", size_columns=cols("E", "I"), size_header_row=2, ready_col="O"),
    SheetSpec("Kiton Women ", "Kiton Woman", tuple(range(8, 17)), Decimal("0.30"), ("C", "B"), "E", ("F",), "G", "T", qty_col="S", size_columns=cols("H", "R"), size_header_row=7, ready_col="V", shipment_cols={"W": "Sped 14"}),
    SheetSpec("KITON MEN ", "Kiton Men", tuple(range(3, 13)), Decimal("0.30"), ("D",), "E", ("G",), "H", "Y", qty_col="X", size_columns=cols("I", "W"), size_header_row=2, ready_col="AA", shipment_cols={"Z": "Sped 14"}),
    SheetSpec("Valentino", "Valentino Pre", tuple(range(3, 7)), Decimal("0.35"), ("B",), "C", ("E",), "R", "S", qty_col="Q", size_columns=cols("F", "P"), size_header_row=2, shipment_cols={"U": "Sped 1", "V": "Sped 8"}),
    SheetSpec("brunello cucinelli women", "Brunello Woman", tuple(range(4, 19)), Decimal("0.35"), ("E",), "F", ("G", "H"), "I", "L", qty_col="K", customer_col="M", shipment_cols={"N": "Sped 3", "O": "Sped 11", "P": "Sped 12"}, ready_col="Q", size_from_customer_note=True),
    SheetSpec("Brunello Cucinelli MEN ", "Brunello Men", tuple(range(3, 20)), Decimal("0.35"), ("B",), "C", ("D",), "E", "V", qty_col="U", size_columns=cols("F", "T"), size_header_row=2, shipment_cols={"W": "Sped 3", "X": "Sped 11"}),
    SheetSpec("Celine pre", "Celine Pre", tuple(range(4, 9)), Decimal("0.30"), ("C",), "D", ("E",), "G", "I", qty_col="H", size_col="F", ready_col="K", shipment_cols={"L": "Sped 12"}),
    SheetSpec("THE ROW pre", "The Row Pre", tuple(range(3, 10)), Decimal("0.35"), ("C", "K"), "D", ("E", "F"), "AD", "AK", qty_col="AF", size_columns=cols("O", "AC"), size_header_row=2, shipment_cols={"AL": "Sped 11"}),
    SheetSpec("THE ROW pre", "The Row Pre", (12, 13, 14, 15), Decimal("0.35"), ("C", "K"), "D", ("E", "F"), "Q", "Y", qty_col="S", size_col="N", customer_col="A", ready_col="AA"),
    SheetSpec("Loro Piana women ", "Loro Piana Woman", tuple(range(3, 11)), Decimal("0.35"), ("D",), "E", ("F", "G"), "T", "U", qty_col="S", size_columns=cols("H", "R"), size_header_row=2, shipment_cols={"V": "Sped 10"}, ready_col="W"),
    SheetSpec("Loro Piana women ", "Loro Piana Woman", (13, 14), Decimal("0.35"), ("D",), "E", ("F",), "T", "U", qty_col="S", size_columns=cols("J", "Q"), size_header_row=12, shipment_cols={"V": "Sped 10"}, ready_col="W"),
    SheetSpec("loro piana men ", "Loro Piana Man", tuple(range(4, 8)), Decimal("0.35"), ("D",), "E", ("F",), "G", "I", qty_col="H", size_col="J", shipment_cols={"L": "Sped 10"}, ready_col="K"),
    SheetSpec("DIOR CRUIZ SSv 26", "Dior Cruise", (4, 6, 7, 8, 9, 10), Decimal("0.35"), ("G",), "E", ("F",), "I", "L", qty_col="K", size_columns=cols("M", "S"), size_header_row=3),
    SheetSpec("Alaia pre", "Alaia Pre", tuple(range(5, 22)), Decimal("0.30"), ("C",), "D", ("E",), "F", "M", qty_col="L", size_columns=cols("G", "K"), size_header_row=4, customer_col="A", shipment_cols={"O": "Sped 1", "P": "Sped 2", "Q": "Sped 8", "R": "Sped 10"}),
    SheetSpec("YSL pre ", "YSL Main", (4, 5), Decimal("0.30"), ("C",), "D", ("E",), "H", "I", qty_col="G", size_col="F", shipment_cols={"J": "Sped 16"}),
    SheetSpec("DIOR CRUIZ SSv 26", "Dior Cruise", (5,), Decimal("0.35"), ("D",), None, (), "I", "L", reference_only=True),
]


SUMMARY_LABELS = {
    "alaia pre": "Alaia Pre",
    "dior cruise": "Dior Cruise",
    "loro piana woman": "Loro Piana Woman",
    "loro piana man": "Loro Piana Man",
    "brunello men": "Brunello Men",
    "brunello woman": "Brunello Woman",
    "kiton woman": "Kiton Woman",
    "kiton men": "Kiton Men",
    "valentino pre": "Valentino Pre",
    "ysl main": "YSL Main",
    "celine pre": "Celine Pre",
    "the row": "The Row Pre",
    "zimmerman": "Zimmerman",
    "the row main": "The Row Main",
    "balenciaga main": "Balenciaga Main",
    "bv": "BV",
    "dior main": "Dior Main",
}


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace("€", "").replace("\u00a0", "").replace(" ", "")
    if not text or text.startswith("#"):
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        match = re.search(r"-?\d+(?:[.,]\d+)?", text)
        return Decimal(match.group(0).replace(",", ".")) if match else None


def parse_int(value: Any) -> int | None:
    number = parse_decimal(value)
    if number is None:
        return None
    return max(int(number), 0)


def text_join(values: list[Any]) -> str | None:
    parts = [str(value).strip() for value in values if value not in (None, "")]
    return " / ".join(parts) or None


def sanitize_xlsx(source_path: Path, output_path: Path) -> list[str]:
    bad_media: list[str] = []
    with zipfile.ZipFile(source_path) as source, zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            try:
                payload = source.read(info)
            except zipfile.BadZipFile:
                if not info.filename.startswith("xl/media/"):
                    raise
                payload = PLACEHOLDER_PNG
                bad_media.append(info.filename)
            target.writestr(info, payload)
    return bad_media


def extract_images(workbook, import_id: int) -> dict[tuple[str, int], str]:
    root = ROOT_DIR / "data" / "images" / f"import-{import_id}"
    root.mkdir(parents=True, exist_ok=True)
    result: dict[tuple[str, int], str] = {}
    for sheet in workbook.worksheets:
        sheet_dir = root / re.sub(r"[^a-zA-Z0-9_-]+", "-", sheet.title).strip("-")
        sheet_dir.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(sheet._images, start=1):
            row = image.anchor._from.row + 1
            extension = (image.format or "png").lower()
            if extension not in {"png", "jpeg", "jpg", "gif"}:
                extension = "png"
            path = sheet_dir / f"row-{row}-{index}.{extension}"
            try:
                path.write_bytes(image._data())
                result.setdefault((sheet.title, row), str(path.relative_to(ROOT_DIR)).replace("\\", "/"))
            except Exception:
                continue
    return result


CUSTOMER_NOISE = {"READY", "LOOK", "SPED", "SIZE", "COLOR", "COL", "PCS"}
SIZE_WORDS = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"}


def parse_customer_allocations(
    description: Any,
    quantity: int,
    default_code: str,
    aliases: dict[str, str] | None = None,
) -> tuple[list[str | None], str | None]:
    """Return one customer code per unit or all-None when the split is uncertain."""
    built_in_aliases = {"A.A.": "AA", "ALINA": "AA", "ALINA A.": "AA"} if default_code.upper() == "AA" else {}
    aliases = built_in_aliases | {key.upper(): value.upper() for key, value in (aliases or {}).items()}
    text_value = str(description or "").strip()
    if quantity <= 0:
        return [], "Allocation quantity is not positive"
    if not text_value:
        return [default_code.upper()] * quantity, None
    normalized_alias_text = text_value
    for alias, canonical in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
        normalized_alias_text = re.sub(
            rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])",
            canonical,
            normalized_alias_text,
            flags=re.IGNORECASE,
        )
    matches: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?<![A-Za-z])([A-Za-z]{2,5})(?![A-Za-z])", normalized_alias_text):
        token = match.group(1).upper()
        if token in CUSTOMER_NOISE or token in SIZE_WORDS:
            continue
        matches.append((match.start(), match.end(), aliases.get(token, token)))
    if not matches:
        return [None] * quantity, f"No customer alias could be parsed from {text_value!r}"
    if len(matches) == 1:
        return [matches[0][2]] * quantity, None
    counts: list[int] = []
    for index, (_start, end, _code) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(normalized_alias_text)
        segment = re.sub(r"\([^)]*\)", "", normalized_alias_text[end:next_start])
        sizes = re.findall(r"\d+(?:[.,]\d+)?|XXS|XS|XXXL|XXL|XL|[SML]", segment, flags=re.IGNORECASE)
        counts.append(max(len(sizes), 1))
    if sum(counts) != quantity:
        codes = ", ".join(match[2] for match in matches)
        return [None] * quantity, f"Customer allocation ({codes}) implies {sum(counts)} units but source quantity is {quantity}"
    allocations: list[str] = []
    for count, (_start, _end, code) in zip(counts, matches, strict=True):
        allocations.extend([code] * count)
    return allocations, None


class WorkbookImporter:
    def __init__(self, session: Session):
        self.session = session
        self.batch: ImportBatch | None = None
        self.review_count = 0
        self.customers: dict[str, Customer] = {}
        self.aliases: dict[str, str] = {
            alias.upper(): code.upper()
            for alias, code in self.session.execute(
                select(CustomerAlias.alias, Customer.code).join(Customer)
            ).all()
        }
        self.orders: dict[str, Order] = {}
        self.shipments: dict[str, Shipment] = {}
        self.ready_batch: ReadyBatch | None = None
        self.incremental = False
        self.change_counts = {"new": 0, "changed": 0, "unchanged": 0, "review": 0, "removed": 0}

    def change(self, sheet: str, row: int | None, action: str, previous: Any, new: Any, status: str = "Applied", reason: str | None = None) -> None:
        assert self.batch is not None
        self.change_counts[action] = self.change_counts.get(action, 0) + 1
        self.session.add(
            ImportChange(
                import_batch_id=self.batch.id,
                source_file=self.batch.filename,
                sheet=sheet,
                row=row,
                action=action,
                status=status,
                previous_values=json.dumps(previous, ensure_ascii=False, default=str) if previous is not None else None,
                new_values=json.dumps(new, ensure_ascii=False, default=str) if new is not None else None,
                reason=reason,
            )
        )

    def review(self, sheet: str, row: int | None, raw: Any, reason: str) -> None:
        assert self.batch is not None
        self.review_count += 1
        self.session.add(
            ImportReview(
                import_batch_id=self.batch.id,
                sheet=sheet,
                row=row,
                raw_values=json.dumps(raw, ensure_ascii=False, default=str),
                reason=reason,
            )
        )

    def allocation_review(self, sheet: str, row: int | None, raw: Any, description: Any, reason: str) -> None:
        assert self.batch is not None
        self.review_count += 1
        self.session.add(
            AllocationReview(
                import_batch_id=self.batch.id,
                source_file=self.batch.filename,
                sheet=sheet,
                row=row,
                raw_description=str(description or "") or None,
                raw_values=json.dumps(raw, ensure_ascii=False, default=str),
                reason=reason,
            )
        )

    def get_customer(self, code: str) -> Customer:
        clean = code.strip().upper()
        if clean not in self.customers:
            customer = self.session.scalar(select(Customer).where(Customer.code == clean))
            if customer is None:
                customer = Customer(code=clean)
                self.session.add(customer)
                self.session.flush()
            if self.session.scalar(select(CustomerAlias).where(CustomerAlias.alias == clean)) is None:
                self.session.add(CustomerAlias(alias=clean, customer_id=customer.id))
            if clean == "AA":
                for built_in in ("A.A.", "ALINA", "ALINA A."):
                    if self.session.scalar(select(CustomerAlias).where(CustomerAlias.alias == built_in)) is None:
                        self.session.add(CustomerAlias(alias=built_in, customer_id=customer.id))
            self.aliases[clean] = clean
            if clean == "AA":
                self.aliases.update({"A.A.": "AA", "ALINA": "AA", "ALINA A.": "AA"})
            self.customers[clean] = customer
        return self.customers[clean]

    def get_shipment(self, name: str) -> Shipment:
        if name not in self.shipments:
            shipment = self.session.scalar(select(Shipment).where(Shipment.name == name))
            if shipment is None:
                shipment = Shipment(season_id=self.ready_batch.season_id if self.ready_batch else None, name=name, status="Assigned", transport_percentage=ZERO, use_rule_transport=False, fixed_cost=ZERO, suitcase_cost=ZERO, bag_cost=ZERO)
                self.session.add(shipment)
                self.session.flush()
            self.shipments[name] = shipment
        return self.shipments[name]

    def import_file(self, source_path: Path) -> dict[str, Any]:
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        existing = self.session.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
        if existing:
            return json.loads(existing.summary_json or "{}") | {"already_imported": True}
        self.incremental = self.session.scalar(select(Order.id).limit(1)) is not None
        try:
            stored_path = source_path.resolve().relative_to(ROOT_DIR.resolve()).as_posix()
        except ValueError:
            stored_path = None
        self.batch = ImportBatch(filename=source_path.name, sha256=digest, stored_path=stored_path)
        self.session.add(self.batch)
        self.session.flush()
        with tempfile.TemporaryDirectory(prefix="fashion-import-") as directory:
            clean_path = Path(directory) / "sanitized.xlsx"
            bad_media = sanitize_xlsx(source_path, clean_path)
            formula_book = openpyxl.load_workbook(clean_path, data_only=False)
            data_book = openpyxl.load_workbook(clean_path, data_only=True)
            images = extract_images(formula_book, self.batch.id)
            for filename in bad_media:
                self.review("Workbook package", None, {"media": filename}, "Embedded image failed CRC validation and was replaced by a placeholder")
            self._import_workbook(data_book, formula_book, images)
        ensure_import_groups(self.session)
        summary = {
            "filename": source_path.name,
            "season": settings.default_season,
            "orders": len(self.orders),
            "items": self.session.scalar(select(func.count()).select_from(OrderItem)) or 0,
            "customers": len(self.customers),
            "shipments": len(self.shipments),
            "reviews": self.review_count,
            "already_imported": False,
            "mode": "incremental" if self.incremental else "initial",
            "changes": self.change_counts,
        }
        self.batch.summary_json = json.dumps(summary, ensure_ascii=False)
        record_change(
            self.session,
            entity_type="ImportBatch",
            entity_id=self.batch.id,
            field=None,
            previous_value=None,
            new_value=summary,
            source_user="Excel importer",
            description=f"Imported {source_path.name}: {summary['orders']} orders, {summary['items']} product records",
        )
        return summary

    def _import_workbook(self, data_book, formula_book, images: dict[tuple[str, int], str]) -> None:
        season = self.session.scalar(select(Season).where(Season.name == settings.default_season))
        if season is None:
            season = Season(name=settings.default_season)
            self.session.add(season)
            self.session.flush()
        self.ready_batch = self.session.scalar(
            select(ReadyBatch).where(ReadyBatch.season_id == season.id, ReadyBatch.name == "Ready 1 (Imported)")
        )
        if self.ready_batch is None:
            self.ready_batch = ReadyBatch(
                season_id=season.id,
                name="Ready 1 (Imported)",
                status="Historical",
                notes="Workbook Ready and already shipped goods grouped here because the source does not preserve Ready-batch identity.",
            )
            self.session.add(self.ready_batch)
            self.session.flush()
        default_customer = self.get_customer(settings.default_customer_code)
        summary_sheet = data_book["SS25"]
        summary_by_brand: dict[str, dict[str, Any]] = {}
        for row in range(4, 21):
            label = normalized(summary_sheet[f"A{row}"].value)
            brand_name = SUMMARY_LABELS.get(label)
            if not brand_name:
                continue
            summary_by_brand[brand_name] = {
                "total": money(parse_decimal(summary_sheet[f"B{row}"].value) or ZERO),
                "readiness": summary_sheet[f"O{row}"].value,
            }
        for spec in SPECS:
            if spec.brand in self.orders:
                continue
            brand = self.session.scalar(select(Brand).where(Brand.name == spec.brand))
            if brand is None:
                brand = Brand(name=spec.brand, default_markup_percentage=spec.markup)
                self.session.add(brand)
                self.session.flush()
            summary = summary_by_brand.get(spec.brand, {})
            order = self.session.scalar(select(Order).where(Order.season_id == season.id, Order.brand_id == brand.id))
            if order is None:
                order = Order(
                    season_id=season.id,
                    brand_id=brand.id,
                    status="Ordered",
                    factory_ready_note=str(summary.get("readiness") or "") or None,
                    source_total=summary.get("total"),
                    source_sheet=spec.sheet,
                )
                self.session.add(order)
                self.session.flush()
            else:
                order.factory_ready_note = str(summary.get("readiness") or "") or None
                order.source_total = summary.get("total")
            self.orders[spec.brand] = order
            rule = self.session.scalar(select(BrandSeasonRule).where(BrandSeasonRule.season_id == season.id, BrandSeasonRule.brand_id == brand.id))
            if rule is None:
                self.session.add(BrandSeasonRule(
                    season_id=season.id,
                    brand_id=brand.id,
                    markup_percentage=spec.markup,
                    initial_prepayment_percentage=Decimal("0.50"),
                    ready_balance_percentage=Decimal("0.50"),
                    transport_percentage=Decimal("0.05"),
                    transport_exempt_country_codes="CH",
                    expected_receipt_days=14,
                    notes="Imported SS26 rule; change future seasons by creating a new Brand + Season rule.",
                ))
        for spec in SPECS:
            self._import_spec(spec, data_book[spec.sheet], formula_book[spec.sheet], images)
        self._import_payments_and_costs(summary_sheet, default_customer)
        for brand_name, order in self.orders.items():
            if not order.items:
                self.review(order.source_sheet or brand_name, None, {}, "No product rows were imported for this order")
        for formula_sheet in formula_book.worksheets:
            for row in formula_sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and "#REF!" in cell.value:
                        self.review(formula_sheet.title, cell.row, {cell.coordinate: cell.value}, "Broken #REF! formula preserved as an import review item")

    def _raw_row(self, sheet, row: int) -> dict[str, Any]:
        return {
            cell.coordinate: cell.value
            for cell in sheet[row]
            if cell.value not in (None, "")
        }

    def _sizes(self, spec: SheetSpec, sheet, row: int, qty: int) -> tuple[list[str | None], str | None]:
        if spec.size_from_customer_note and spec.customer_col:
            note = str(sheet[f"{spec.customer_col}{row}"].value or "")
            without_parentheses = re.sub(r"\([^)]*\)", "", note)
            remainder = re.sub(r"^\s*[A-Za-z]{2,4}\s*", "", without_parentheses).strip()
            if qty == 1 and "/" in remainder:
                return [remainder.upper()], None
            tokens = re.findall(r"\d+(?:[,.]\d+)?|XXS|XS|XXL|XL|[SML]", remainder, flags=re.IGNORECASE)
            tokens = [token.replace(",", ".").upper() for token in tokens]
            if tokens:
                if len(tokens) == qty:
                    return tokens, None
                if len(tokens) == 1:
                    return tokens * qty, None
                return tokens[:qty] + [None] * max(qty - len(tokens), 0), "Customer/size note contains more size tokens than the item quantity"
        if spec.size_col:
            size = sheet[f"{spec.size_col}{row}"].value
            return [str(size).strip() if size not in (None, "") else None] * qty, None
        if not spec.size_columns:
            return [None] * qty, None
        entries: list[tuple[str, int]] = []
        raw_sum = 0
        for col in spec.size_columns:
            raw = parse_int(sheet[f"{col}{row}"].value) or 0
            if raw <= 0:
                continue
            header = sheet[f"{col}{spec.size_header_row}"].value
            label = str(header).strip() if header not in (None, "") else col
            entries.append((label, raw))
            raw_sum += raw
        if not entries:
            return [None] * qty, None
        if raw_sum == qty:
            return [label for label, count in entries for _ in range(count)], None
        if len(entries) == qty:
            return [label for label, _count in entries], f"Size-grid counts sum to {raw_sum}, but total quantity is {qty}; treated each populated size as one unit"
        if len(entries) == 1:
            return [entries[0][0]] * qty, f"Size-grid value {entries[0][1]} does not reconcile to quantity {qty}; retained the size label"
        return [label for label, _count in entries][:qty] + [None] * max(qty - len(entries), 0), f"Size grid could not be reconciled to quantity {qty}"

    def _import_spec(self, spec: SheetSpec, sheet, formula_sheet, images: dict[tuple[str, int], str]) -> None:
        order = self.orders[spec.brand]
        for row in spec.rows:
            raw = self._raw_row(sheet, row)
            existing_items = list(self.session.scalars(
                select(OrderItem).where(
                    OrderItem.order_id == order.id,
                    OrderItem.source_sheet == spec.sheet,
                    OrderItem.source_row == row,
                )
            ).all()) if self.incremental else []
            name = text_join([sheet[f"{col}{row}"].value for col in spec.name_cols])
            article = str(sheet[f"{spec.article_col}{row}"].value).strip() if spec.article_col and sheet[f"{spec.article_col}{row}"].value not in (None, "") else None
            price = parse_decimal(sheet[f"{spec.price_col}{row}"].value)
            source_total = parse_decimal(sheet[f"{spec.total_col}{row}"].value)
            qty = parse_int(sheet[f"{spec.qty_col}{row}"].value) if spec.qty_col else None
            if qty is None and spec.size_columns:
                qty = sum(parse_int(sheet[f"{col}{row}"].value) or 0 for col in spec.size_columns)
            qty = qty or (1 if any(value is not None for value in (name, article, price)) else 0)
            if qty <= 0 or (name is None and article is None and price is None):
                if existing_items:
                    advanced = any(self._has_business_history(item) for item in existing_items)
                    if advanced:
                        self.change(spec.sheet, row, "review", [item.source_note for item in existing_items], raw, "Review", "Source row disappeared but custom pricing or operational Ready/shipment history exists; existing records were preserved")
                        self.review(spec.sheet, row, raw, "Source row disappeared but existing Ready/shipment records were preserved for manual review")
                    else:
                        for item in existing_items:
                            item.status = "Cancelled"
                        self.change(spec.sheet, row, "removed", [item.source_note for item in existing_items], raw, reason="Source row is empty in the new workbook; prior non-fulfilled records were cancelled")
                continue
            raw_json = json.dumps(raw, ensure_ascii=False, default=str)
            if existing_items:
                if all((item.source_note or "") == raw_json for item in existing_items):
                    self.change_counts["unchanged"] += 1
                    continue
                advanced = any(self._has_business_history(item) for item in existing_items)
                if advanced:
                    self.change(spec.sheet, row, "review", [item.source_note for item in existing_items], raw, "Review", "Supplier row changed after custom pricing or Ready/shipment workflow; existing records were preserved")
                    self.review(spec.sheet, row, raw, "Supplier row changed after Ready/shipment activity; compare before/after in Import Changes")
                    continue
                previous = [item.source_note for item in existing_items]
                for item in existing_items:
                    self.session.delete(item)
                self.session.flush()
                self.change(spec.sheet, row, "changed", previous, raw, reason="Source fields changed before fulfillment; row was safely replaced")
            else:
                self.change_counts["new"] += 1
            if price is None:
                self.review(spec.sheet, row, raw, "Product row has no reliably parseable wholesale price")
            allocation_description = sheet[f"{spec.customer_col}{row}"].value if spec.customer_col else None
            customer_allocations, customer_reason = parse_customer_allocations(
                allocation_description,
                qty,
                settings.default_customer_code,
                self.aliases,
            )
            if customer_reason:
                self.allocation_review(spec.sheet, row, raw, allocation_description, customer_reason)
            sizes, size_reason = self._sizes(spec, sheet, row, qty)
            if size_reason:
                self.review(spec.sheet, row, raw, size_reason)
            color = text_join([sheet[f"{col}{row}"].value for col in spec.color_cols])
            inactive = price is None or price <= ZERO or source_total is None or source_total <= ZERO
            if inactive and price is not None and price > ZERO:
                self.review(
                    spec.sheet,
                    row,
                    raw,
                    "Quantity/price exists but the workbook row total is blank, zero, or broken; imported as Cancelled to preserve the workbook order total",
                )
            assignments: list[str | None] = [None] * qty
            allocation_values: list[tuple[str, Decimal]] = []
            if spec.ready_col:
                ready_value = parse_decimal(sheet[f"{spec.ready_col}{row}"].value)
                if ready_value and ready_value > ZERO:
                    allocation_values.append(("Ready", ready_value))
            for column, shipment_name in spec.shipment_cols.items():
                value = parse_decimal(sheet[f"{column}{row}"].value)
                if value and value > ZERO:
                    allocation_values.append((shipment_name, value))
            position = 0
            if not inactive and price and price > ZERO:
                for assignment, allocation in allocation_values:
                    units_decimal = allocation / price
                    units = int(units_decimal.quantize(Decimal("1")))
                    if abs(Decimal(units) * price - allocation) > Decimal("0.01") or units <= 0:
                        self.review(spec.sheet, row, raw, f"Allocation {allocation} for {assignment} is not an exact whole-unit multiple of price {price}")
                        continue
                    for _ in range(units):
                        if position >= len(assignments):
                            self.review(spec.sheet, row, raw, "Ready/shipment allocation exceeds item quantity")
                            break
                        assignments[position] = assignment
                        position += 1
                if spec.ready_when_unallocated and not allocation_values:
                    assignments = ["Ready"] * qty
            groups: list[tuple[str | None, str | None, str | None, int]] = []
            for customer_code_value, size, assignment in zip(customer_allocations, sizes, assignments, strict=False):
                if groups and groups[-1][0] == customer_code_value and groups[-1][1] == size and groups[-1][2] == assignment:
                    previous = groups[-1]
                    groups[-1] = (previous[0], previous[1], previous[2], previous[3] + 1)
                else:
                    groups.append((customer_code_value, size, assignment, 1))
            allocation_group_id = f"{self.batch.id}:{spec.sheet}:{row}:{uuid.uuid4().hex[:8]}"
            for customer_code_value, size, assignment, count in groups:
                status = "Cancelled" if inactive else ("Ready" if assignment == "Ready" else "Assigned to shipment" if assignment else "Ordered")
                worksheet_state, fill, worksheet_note = source_status(formula_sheet.parent, formula_sheet, row,
                    (*spec.name_cols, spec.article_col, spec.price_col), bool(assignment and assignment != "Ready"))
                if spec.reference_only:
                    count = 0
                    worksheet_state = 'pending'
                    worksheet_note = 'Source reference only: COUTURE SKIRT NOT OPEN; zero ordered units and no price. Retained so the product is not lost.'
                customer_obj = self.get_customer(customer_code_value) if customer_code_value else None
                item = OrderItem(
                    order_id=order.id,
                    customer_id=customer_obj.id if customer_obj else None,
                    ready_batch_id=self.ready_batch.id if (assignment is not None and not inactive) else None,
                    article=article,
                    name=name,
                    color=color,
                    size=size,
                    quantity=count,
                    wholesale_unit_price=money(price) if price is not None else None,
                    markup_percentage=spec.markup,
                    status=status,
                    worksheet_status=worksheet_state,
                    source_fill=fill,
                    worksheet_note=worksheet_note,
                    image_path=images.get((spec.sheet, row)),
                    source_file=self.batch.filename,
                    source_sheet=spec.sheet,
                    source_row=row,
                    source_total=money((price or ZERO) * count) if source_total is not None else None,
                    source_note=raw_json,
                    source_allocation_description=str(allocation_description or "") or None,
                    allocation_status="Allocated" if customer_obj else "Review",
                    allocation_group_id=allocation_group_id,
                )
                self.session.add(item)
                self.session.flush()
                if assignment and assignment != "Ready":
                    shipment = self.get_shipment(assignment)
                    self.session.add(ShipmentItem(shipment_id=shipment.id, order_item_id=item.id))

    def _has_business_history(self, item):
        group = item.purchase_group
        return bool(item.selling_unit_price is not None or item.ready_batch_id or item.shipment_link
                    or item.status not in {"Ordered", "Confirmed", "Production", "Cancelled"}
                    or (group and (group.special_cost > ZERO or not (group.notes or '').startswith('Imported grouping.')))
                    or self.session.scalar(select(SellerCreditAccount.id).where(
                        (SellerCreditAccount.item_id == item.id) | (SellerCreditAccount.group_id == item.purchase_group_id))))

    def _import_payments_and_costs(self, sheet, default_customer: Customer) -> None:
        season_id = self.ready_batch.season_id if self.ready_batch else None
        for row in range(26, 39):
            amount = parse_decimal(sheet[f"C{row}"].value)
            date_value = sheet[f"B{row}"].value
            if not amount or amount <= ZERO or not isinstance(date_value, datetime):
                continue
            note = f"Workbook payment: {sheet[f'A{row}'].value or ''}"
            exists = self.session.scalar(select(Payment.id).where(
                Payment.season_id == season_id,
                Payment.customer_id == default_customer.id,
                Payment.date == date_value.date(),
                Payment.amount == money(amount),
                Payment.type == "payment",
                Payment.note == note,
            ))
            if exists is None:
                self.session.add(Payment(season_id=season_id, customer_id=default_customer.id, date=date_value.date(), amount=money(amount), type="payment", note=note))
        for row in (48, 49, 52, 53, 54, 56, 58):
            amount = parse_decimal(sheet[f"C{row}"].value)
            if amount is None:
                continue
            note = str(sheet[f"A{row}"].value or "Workbook expense")
            expense_type = "bag" if "bag" in note.casefold() else "transport"
            exists = self.session.scalar(select(Expense.id).where(
                Expense.season_id == season_id,
                Expense.customer_id == default_customer.id,
                Expense.type == expense_type,
                Expense.amount == money(amount),
                Expense.note == note,
            ))
            if exists is None:
                self.session.add(Expense(season_id=season_id, customer_id=default_customer.id, type=expense_type, amount=money(amount), note=note))


def import_workbook(session: Session, source_path: str | Path) -> dict[str, Any]:
    importer = WorkbookImporter(session)
    return importer.import_file(Path(source_path))
