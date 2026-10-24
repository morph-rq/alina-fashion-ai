"""SYNTHETIC factory workbook for tests — not a real supplier document.

Its structure copies what real SS24/SS27 supplier files contain (summary sheet, merged
title, unlabeled allocation column with initials, size column, line formulas, a factory
percentage row, a grand total, photos anchored to rows, a comment, a green "ready" fill
and one ambiguous allocation). All products, codes and prices are invented.
"""
from __future__ import annotations

import io
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import PatternFill
from PIL import Image

ROWS = [
    # name, article, size, qty, price, allocation, colour
    ("Coat test", "TEST-COAT-01", 40, 1, 2000, "IDS", "Black"),
    ("Suit test", "TEST-SUIT-02", "38, 40", 2, 1500, "AA 40, Alina 38", "Navy"),
    ("Trousers test", "TEST-TRS-03", "36, 40", 2, 500, "AA 40, alina 36", "Navy"),
    ("Dress test", "TEST-DRS-04", 38, 1, 1000, "SS", "White"),
    ("Scarf test", "TEST-SCF-05", "UNI", 2, 250, "IDS, AA", "Red"),          # two customers, one size -> one each
    ("Shirt test", "TEST-SHT-06", "38, 40", 2, 300, "IDS, Alina", "Blue"),   # ambiguous: who has which size?
]


def png(color: tuple[int, int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(stream, "PNG")
    return stream.getvalue()


def build(path: Path, rate: float = 0.30) -> Path:
    book = Workbook()
    summary = book.active
    summary.title = "SS27"
    summary["A1"] = "ЗАКАЗЫ SS27"
    summary["A2"], summary["B2"], summary["C2"] = "Brand", "Сумма заказа с комиссией", "Предоплата 50%"
    summary["A4"], summary["B4"], summary["C4"] = "Dior cruise", "='Dior cruise'!H11", "=B4*0.5"
    summary["A17"] = "ОПЛАТЫ SS27"
    summary["A18"], summary["C18"], summary["D18"] = "Test transfer", 1000, "AA"
    summary["A20"] = "ВСЕГО ОПЛАЧЕНО ЗА SS27"
    sheet = book.create_sheet("Dior cruise")
    sheet["B2"] = "Dior cruise SS27 TEST FIXTURE"
    sheet.merge_cells("B2:H2")
    for col, label in zip("CDEFGH", ["Name", "Article", "Size", "Price", "Q ty", "Total"]):
        sheet[f"{col}3"] = label
    green = PatternFill("solid", fgColor="C6E0B4")
    for offset, (name, article, size, qty, price, alloc, colour) in enumerate(ROWS):
        row = 4 + offset
        sheet[f"C{row}"], sheet[f"D{row}"], sheet[f"E{row}"] = name, article, size
        sheet[f"F{row}"], sheet[f"G{row}"], sheet[f"H{row}"], sheet[f"I{row}"] = price, qty, f"=F{row}*G{row}", alloc
        sheet[f"J{row}"] = colour
        image = XLImage(io.BytesIO(png((40 * offset % 255, 90, 160))))
        image.width = image.height = 30
        sheet.add_image(image, f"B{row}")
    sheet["C4"].fill = green
    sheet["D4"].fill = green
    sheet["I5"].comment = Comment("Test note: customer confirmed sizes", "fixture")
    last = 3 + len(ROWS)
    sheet[f"G{last + 1}"], sheet[f"H{last + 1}"] = "Total", f"=SUM(H4:H{last})"
    sheet[f"G{last + 2}"], sheet[f"H{last + 2}"] = rate, f"=H{last + 1}*G{last + 2}"
    sheet[f"H{last + 3}"] = f"=SUM(H{last + 1}:H{last + 2})"
    book.save(path)
    return path


BASE_TOTAL = sum(qty * price for _, _, _, qty, price, _, _ in ROWS)   # 8700
