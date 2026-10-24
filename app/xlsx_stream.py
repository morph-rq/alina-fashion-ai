"""Streaming access to .xlsx/.xlsm packages for very large supplier workbooks (up to ~1 GB).

* ``sheet_parts`` / ``image_anchors`` read drawing anchors straight from the package, so
  photos can be matched to rows without loading every image into memory, and a damaged
  image (bad CRC, seen in real SS24 files) is skipped instead of failing the import.
* ``patch_workbook`` writes a modified copy of the ORIGINAL file: only edited cells of
  edited sheets change. Images, styles, merged cells, comments, other sheets and untouched
  formulas are copied as they are. Sheet XML is processed row by row, so memory use does not
  grow with the file size. Macros in .xlsm are copied, never executed.
"""
from __future__ import annotations

import posixpath
import re
import shutil
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
import codecs
from xml.etree import ElementTree as _ElementTree
from xml.sax.saxutils import escape

from defusedxml import ElementTree  # uploaded workbooks are untrusted XML

from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string, get_column_letter

NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
R_ID = "{%s}id" % NS["r"]
R_EMBED = "{%s}embed" % NS["r"]
CHUNK = 1 << 20


def _target(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _rels(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    rel_path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    try:
        root = ElementTree.fromstring(archive.read(rel_path))
    except KeyError:
        return {}
    return {rel.get("Id"): _target(part, rel.get("Target", "")) for rel in root
            if rel.get("TargetMode") != "External"}


def sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(sheet name, part path)] in workbook order."""
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = _rels(archive, "xl/workbook.xml")
    result = []
    sheets = workbook.find("s:sheets", NS)
    for sheet in sheets if sheets is not None else []:
        target = rels.get(sheet.get(R_ID))
        if target:
            result.append((sheet.get("name"), target))
    return result


@dataclass(frozen=True)
class Anchor:
    row: int          # 1-based top-left row
    col: int          # 1-based top-left column
    to_row: int
    media: str        # part path inside the package


def image_anchors(archive: zipfile.ZipFile, sheet_part: str) -> list[Anchor]:
    anchors: list[Anchor] = []
    for drawing in (t for t in _rels(archive, sheet_part).values() if "/drawings/" in t and t.endswith(".xml")):
        try:
            root = ElementTree.fromstring(archive.read(drawing))
        except (KeyError, _ElementTree.ParseError, zipfile.BadZipFile, zlib.error, ValueError):
            continue
        media = _rels(archive, drawing)
        for anchor in list(root):
            start = anchor.find("xdr:from", NS)
            end = anchor.find("xdr:to", NS)
            if start is None:
                continue
            for blip in anchor.iter("{%s}blip" % NS["a"]):
                target = media.get(blip.get(R_EMBED))
                if not target:
                    continue
                row = int(start.findtext("xdr:row", "0", NS)) + 1
                to_row = int(end.findtext("xdr:row", str(row - 1), NS)) + 1 if end is not None else row
                anchors.append(Anchor(row, int(start.findtext("xdr:col", "0", NS)) + 1, to_row, target))
    return anchors


def read_media(archive: zipfile.ZipFile, part: str, limit: int = 40 * 1024 * 1024) -> bytes | None:
    """Image bytes, or None for a missing, oversized or damaged entry."""
    try:
        info = archive.getinfo(part)
        if info.file_size > limit:
            return None
        return archive.read(part)
    except (KeyError, zipfile.BadZipFile, zlib.error, OSError, EOFError):
        return None


# --- Patching -----------------------------------------------------------------------

CELL_RE = re.compile(r"<c\b([^>]*?)(/>|>(.*?)</c>)", re.S)
ROW_RE = re.compile(r"<row\b[^>]*?(?:/>|>.*?</row>)", re.S)
ATTR_R = re.compile(r'\br="([A-Z]+)(\d+)"')
ATTR_ROW_R = re.compile(r'\br="(\d+)"')
CALC_FOLLOWERS = ("<oleSize", "<customWorkbookViews", "<pivotCaches", "<smartTagPr", "<smartTagTypes",
                  "<webPublishing", "<fileRecoveryPr", "<webPublishObjects", "<extLst", "</workbook>")


class PatchError(ValueError):
    pass


def _cell_xml(ref: str, style: str | None, value) -> str:
    style_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style_attr} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style_attr}><v>{value!r}</v></c>' if isinstance(value, float) else f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = escape(str(value))
    space = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t{space}>{text}</t></is></c>'


def _style_of(attrs: str) -> str | None:
    match = re.search(r'\bs="(\d+)"', attrs)
    return match.group(1) if match else None


def _row_style(row_number: int, edits: dict[int, object]) -> str:
    cells = "".join(_cell_xml(f"{get_column_letter(col)}{row_number}", None, value) for col, value in sorted(edits.items()))
    return f'<row r="{row_number}">{cells}</row>'


class _SharedFormulas:
    """Expands shared formulas whose master cell is overwritten, so dependents stay valid."""

    def __init__(self, edits: dict[tuple[int, int], object]):
        self.edits = edits
        self.broken: dict[str, tuple[str, str]] = {}   # si -> (formula, origin ref)

    def check_master(self, ref: str, body: str) -> None:
        match = re.search(r'<f\b([^>]*\bt="shared"[^>]*\bref="[^"]+"[^>]*)>(.*?)</f>', body or "", re.S)
        if match:
            si = re.search(r'\bsi="(\d+)"', match.group(1))
            if si:
                self.broken[si.group(1)] = (match.group(2), ref)

    def expand(self, ref: str, attrs: str, body: str) -> str | None:
        match = re.search(r'<f\b[^>]*\bt="shared"[^>]*\bsi="(\d+)"[^>]*/>', body or "")
        if not match or match.group(1) not in self.broken:
            return None
        formula, origin = self.broken[match.group(1)]
        from xml.sax.saxutils import unescape
        translated = Translator("=" + unescape(formula), origin=origin).translate_formula(ref)[1:]
        body = body.replace(match.group(0), f"<f>{escape(translated)}</f>")
        return f"<c{attrs}>{body}</c>"


def _patch_row(row_xml: str, row_number: int, edits: dict[int, object], shared: _SharedFormulas) -> str:
    cells = []
    sequential = 0
    for match in CELL_RE.finditer(row_xml):
        attrs, body = match.group(1), match.group(3)
        ref_match = ATTR_R.search(attrs)
        col = column_index_from_string(ref_match.group(1)) if ref_match else sequential + 1
        sequential = col
        ref = f"{get_column_letter(col)}{row_number}"
        if col in edits:
            shared.check_master(ref, body)
            cells.append((col, _cell_xml(ref, _style_of(attrs), edits[col])))
        else:
            cells.append((col, shared.expand(ref, attrs, body) or match.group(0)))
    present = {col for col, _ in cells}
    for col, value in edits.items():
        if col not in present:
            cells.append((col, _cell_xml(f"{get_column_letter(col)}{row_number}", None, value)))
    cells.sort(key=lambda pair: pair[0])
    open_tag = re.match(r"<row\b[^>]*?(?=/?>)", row_xml).group(0)
    open_tag = re.sub(r'\sspans="[^"]*"', "", open_tag)
    return f"{open_tag}>{''.join(xml for _, xml in cells)}</row>"


def _rewrite_rows_only_for_shared(row_xml: str, row_number: int, shared: _SharedFormulas) -> str:
    if not shared.broken or 't="shared"' not in row_xml:
        return row_xml
    return _patch_row(row_xml, row_number, {}, shared)


def _stream_sheet(source, target, edits: dict[tuple[int, int], object]) -> None:
    by_row: dict[int, dict[int, object]] = {}
    for (row, col), value in edits.items():
        if row < 1 or col < 1 or row > 1_048_576 or col > 16_384:
            raise PatchError(f"Ячейка {row}:{col} вне листа Excel.")
        if isinstance(value, str) and value.startswith("="):
            raise PatchError("Формулы не записываются из редактора — только значения.")
        by_row.setdefault(row, {})[col] = value
    pending = sorted(by_row)
    shared = _SharedFormulas(edits)
    buffer = ""
    in_data = False
    done = False
    decoder = codecs.getincrementaldecoder("utf-8")()
    while True:
        chunk = source.read(CHUNK)
        if chunk:
            buffer += decoder.decode(chunk)
        elif not buffer:
            break
        if not in_data:
            index = buffer.find("<sheetData")
            if index < 0:
                keep = max(0, len(buffer) - 16)
                target.write(buffer[:keep].encode())
                buffer = buffer[keep:]
                if not chunk:
                    target.write(buffer.encode())
                    break
                continue
            end = buffer.find(">", index)
            if end < 0:
                if not chunk:
                    raise PatchError("Повреждённый лист: не найден sheetData.")
                continue
            tag = buffer[index:end + 1]
            if tag.endswith("/>"):
                rows = "".join(_row_style(r, by_row[r]) for r in pending)
                target.write((buffer[:index] + f"<sheetData>{rows}</sheetData>").encode())
                pending = []
                buffer = buffer[end + 1:]
                in_data, done = True, True
                continue
            target.write(buffer[:end + 1].encode())
            buffer = buffer[end + 1:]
            in_data = True
        if done:
            target.write(buffer.encode())
            buffer = ""
            if not chunk:
                break
            continue
        position = 0
        while True:
            close = buffer.find("</sheetData>", position)
            match = ROW_RE.search(buffer, position)
            if match and (close < 0 or match.start() < close):
                row_xml = match.group(0)
                number_match = ATTR_ROW_R.search(row_xml[:row_xml.find(">") + 1])
                if not number_match:
                    raise PatchError("Строка без номера в листе — такой файл нельзя изменить безопасно.")
                number = int(number_match.group(1))
                out = buffer[position:match.start()]
                while pending and pending[0] < number:
                    out += _row_style(pending[0], by_row[pending[0]])
                    pending.pop(0)
                if pending and pending[0] == number:
                    out += _patch_row(row_xml, number, by_row[number], shared)
                    pending.pop(0)
                else:
                    out += _rewrite_rows_only_for_shared(row_xml, number, shared)
                target.write(out.encode())
                position = match.end()
                continue
            if close >= 0 and (not match or close < match.start()):
                out = buffer[position:close] + "".join(_row_style(r, by_row[r]) for r in pending)
                pending = []
                target.write(out.encode())
                buffer = buffer[close:]
                done = True
                break
            buffer = buffer[position:]
            break
        if done:
            continue
        if not chunk:
            raise PatchError("Повреждённый лист: не найден конец sheetData.")


def _set_full_calc(xml: str) -> str:
    if "<calcPr" in xml:
        return re.sub(r"<calcPr\b([^>]*?)(/?)>", lambda m: "<calcPr" + re.sub(r'\sfullCalcOnLoad="[^"]*"', "", m.group(1))
                      + ' fullCalcOnLoad="1"' + m.group(2) + ">", xml, count=1)
    positions = [xml.find(tag) for tag in CALC_FOLLOWERS if xml.find(tag) >= 0]
    index = min(positions)
    return xml[:index] + '<calcPr fullCalcOnLoad="1"/>' + xml[index:]


def patch_workbook(source: Path, target: Path, edits: dict[str, dict[tuple[int, int], object]],
                   progress: Callable[[float], None] | None = None) -> dict:
    """Write ``target``: a copy of ``source`` with only the given cells changed.

    ``edits`` maps sheet name to {(row, column): value}; values are text, numbers, booleans
    or None (cleared). The calculation chain is dropped and Excel is asked to recalculate on
    open, so formulas that depend on edited cells show fresh results.
    """
    source, target = Path(source), Path(target)
    edits = {name: cells for name, cells in edits.items() if cells}
    if not edits:
        shutil.copyfile(source, target)
        return {"sheets": 0, "cells": 0}
    temp = target.with_suffix(target.suffix + ".part")
    with zipfile.ZipFile(source) as archive:
        parts = dict(sheet_parts(archive))
        missing = [name for name in edits if name not in parts]
        if missing:
            raise PatchError(f"Листы не найдены в исходном файле: {', '.join(missing)}")
        edited_parts = {parts[name]: cells for name, cells in edits.items()}
        infos = archive.infolist()
        total = sum(info.file_size for info in infos) or 1
        done = 0
        with zipfile.ZipFile(temp, "w", allowZip64=True) as out:
            for info in infos:
                name = info.filename
                if name == "xl/calcChain.xml":
                    done += info.file_size
                    continue
                new_info = zipfile.ZipInfo(name, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                if name in edited_parts:
                    with archive.open(info) as src, out.open(new_info, "w", force_zip64=True) as dst:
                        _stream_sheet(src, dst, edited_parts[name])
                elif name in ("[Content_Types].xml", "xl/_rels/workbook.xml.rels", "xl/workbook.xml"):
                    xml = archive.read(info).decode("utf-8")
                    if name == "[Content_Types].xml":
                        xml = re.sub(r'<Override[^>]*PartName="/xl/calcChain.xml"[^>]*/>', "", xml)
                    elif name.endswith(".rels"):
                        xml = re.sub(r'<Relationship[^>]*Target="[^"]*calcChain.xml"[^>]*/>', "", xml)
                    else:
                        xml = _set_full_calc(xml)
                    out.writestr(new_info, xml.encode("utf-8"))
                else:
                    # Read fully (spooled to disk when large) before writing, so a damaged entry
                    # (e.g. a corrupt photo with a bad CRC) never leaves a half-written duplicate.
                    with tempfile.SpooledTemporaryFile(max_size=16 * CHUNK) as spool:
                        try:
                            with archive.open(info) as src:
                                shutil.copyfileobj(src, spool, CHUNK)
                        except (zipfile.BadZipFile, zlib.error):
                            _copy_raw(source, info, out, new_info)
                        else:
                            spool.seek(0)
                            with out.open(new_info, "w", force_zip64=True) as dst:
                                shutil.copyfileobj(spool, dst, CHUNK)
                done += info.file_size
                if progress:
                    progress(min(done / total, 1.0))
    temp.replace(target)
    return {"sheets": len(edits), "cells": sum(len(cells) for cells in edits.values())}


def _copy_raw(source: Path, info: zipfile.ZipInfo, out: zipfile.ZipFile, new_info: zipfile.ZipInfo) -> None:
    """Copy an entry whose content fails its CRC: read without CRC check and store as is."""
    with open(source, "rb") as handle:
        handle.seek(info.header_offset)
        header = handle.read(30)
        name_length = int.from_bytes(header[26:28], "little")
        extra_length = int.from_bytes(header[28:30], "little")
        handle.seek(info.header_offset + 30 + name_length + extra_length)
        raw = handle.read(info.compress_size)
    data = zlib.decompressobj(-15).decompress(raw) if info.compress_type == zipfile.ZIP_DEFLATED else raw
    out.writestr(new_info, data)


def iter_sheet_rows(path: Path, sheet: str, *, values: bool = True) -> Iterator[tuple[int, list]]:
    """Stream (row number, values) with openpyxl's read-only reader (images are not loaded)."""
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=values, keep_links=False)
    try:
        worksheet = book[sheet]
        for row in worksheet.iter_rows():
            cells = [c for c in row if getattr(c, "row", None)]
            if not cells:
                continue
            yield cells[0].row, [(c.column, c.value) for c in cells if c.value is not None]
    finally:
        book.close()
