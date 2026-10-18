from __future__ import annotations

import io
import ipaddress
import json
from difflib import SequenceMatcher
import re
import secrets
import shutil
import socket
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from app.config import ROOT_DIR


STAGING_ROOT = ROOT_DIR / "data" / "staged-images"
PERMANENT_ROOT = ROOT_DIR / "data" / "images"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_COUNT = 200
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


@dataclass(frozen=True)
class StagedImage:
    original_filename: str
    stored_filename: str
    article_key: str
    extension: str
    width: int
    height: int
    size_bytes: int


@dataclass
class StageResult:
    token: str | None
    images: list[StagedImage]
    errors: list[str]


@dataclass
class ImageMatchResult:
    by_source_row: dict[int, StagedImage]
    images: list[StagedImage]
    errors: list[str]
    warnings: list[str]
    suggestions_by_image: dict[str, list[dict[str, object]]]
    assignments: dict[str, int]
    match_methods: dict[int, str]


def article_key(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _token_path(token: str) -> Path:
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid staged-image token")
    root = STAGING_ROOT.resolve()
    path = (root / token).resolve()
    if path.parent != root:
        raise ValueError("Invalid staged-image path")
    return path


def _manifest_path(token: str) -> Path:
    return _token_path(token) / "manifest.json"


def load_stage(token: str | None) -> list[StagedImage]:
    if not token:
        return []
    try:
        manifest = _manifest_path(token)
    except ValueError:
        return []
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return [StagedImage(**item) for item in data.get("images", [])]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _write_manifest(token: str, images: list[StagedImage]) -> None:
    folder = _token_path(token)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "images": [asdict(item) for item in images],
    }
    _manifest_path(token).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _inspect_image(filename: str, content: bytes) -> tuple[str, int, int]:
    if not content:
        raise ValueError(f"{filename}: image file is empty")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"{filename}: image is larger than 12 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
                detected_format = image.format
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError(f"{filename}: unsupported or damaged image") from None
    if detected_format not in FORMAT_EXTENSIONS:
        raise ValueError(f"{filename}: use JPG, PNG, or WEBP")
    if width < 20 or height < 20:
        raise ValueError(f"{filename}: image is too small")
    return FORMAT_EXTENSIONS[detected_format], width, height


def stage_images(
    uploads: Iterable[tuple[str, bytes]],
    *,
    existing_token: str | None = None,
) -> StageResult:
    cleanup_expired_stages()
    uploads = list(uploads)
    existing = load_stage(existing_token)
    if not uploads:
        return StageResult(existing_token if existing else None, existing, [])
    if len(existing) + len(uploads) > MAX_IMAGE_COUNT:
        return StageResult(existing_token, existing, [f"Upload no more than {MAX_IMAGE_COUNT} product images at once"])

    token = existing_token if existing and existing_token else secrets.token_urlsafe(24)
    folder = _token_path(token)
    folder.mkdir(parents=True, exist_ok=True)
    staged = list(existing)
    errors: list[str] = []
    for upload_index, (original_filename, content) in enumerate(uploads, start=1):
        safe_original = Path(original_filename or f"image-{upload_index}").name
        key = article_key(Path(safe_original).stem)
        if not key:
            errors.append(f"{safe_original}: filename must contain the product article/SKU")
            continue
        try:
            extension, width, height = _inspect_image(safe_original, content)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        stored_filename = f"{key}-{secrets.token_hex(5)}{extension}"
        (folder / stored_filename).write_bytes(content)
        item = StagedImage(
            original_filename=safe_original,
            stored_filename=stored_filename,
            article_key=key,
            extension=extension,
            width=width,
            height=height,
            size_bytes=len(content),
        )
        staged.append(item)
    _write_manifest(token, staged)
    return StageResult(token, staged, errors)


def _normalized_words(value: str | None) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9]+", value or "")).casefold()


def _product_label(product: object) -> str:
    article = getattr(product, "article", None) or "No article"
    name = getattr(product, "name", None) or "Unnamed product"
    brand = getattr(product, "brand_name", None) or ""
    color = getattr(product, "color", None) or ""
    size = getattr(product, "size", None) or ""
    return f"{brand} · {article} · {name} · {color} · {size}".strip(" ·")


def _suggestion_score(image: StagedImage, product: object) -> float:
    filename = _normalized_words(Path(image.original_filename).stem)
    if not filename:
        return 0.0
    candidates = [
        getattr(product, "article", None),
        getattr(product, "name", None),
        f"{getattr(product, 'name', '')} {getattr(product, 'color', '')}",
        f"{getattr(product, 'brand_name', '')} {getattr(product, 'name', '')}",
    ]
    scores = []
    filename_tokens = set(filename.split())
    for candidate in candidates:
        normalized = _normalized_words(str(candidate or ""))
        if not normalized:
            continue
        ratio = SequenceMatcher(None, filename, normalized).ratio()
        candidate_tokens = set(normalized.split())
        overlap = len(filename_tokens & candidate_tokens) / max(len(filename_tokens | candidate_tokens), 1)
        scores.append(max(ratio, overlap))
    return max(scores, default=0.0)


def decode_assignments(raw: str | None) -> tuple[dict[str, int], list[str]]:
    if not raw:
        return {}, []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}, ["Image assignments could not be read; choose the products again"]
    if not isinstance(payload, dict):
        return {}, ["Image assignments are invalid"]
    assignments: dict[str, int] = {}
    for key, value in payload.items():
        if value in {None, ""}:
            continue
        try:
            assignments[Path(str(key)).name] = int(value)
        except (TypeError, ValueError):
            return {}, ["An image points to an invalid product row"]
    return assignments, []


def match_images(
    products: Iterable[object],
    token: str | None,
    assignments: dict[str, int] | None = None,
) -> ImageMatchResult:
    images = load_stage(token)
    products = list(products)
    assignments = assignments or {}
    products_by_row = {int(getattr(product, "source_row")): product for product in products}
    article_rows: dict[str, list[object]] = {}
    article_brands: dict[str, set[str]] = {}
    for product in products:
        key = article_key(getattr(product, "article", None))
        if not key:
            continue
        article_rows.setdefault(key, []).append(product)
        article_brands.setdefault(key, set()).add(str(getattr(product, "brand_name", "")).casefold())

    errors: list[str] = []
    warnings_list: list[str] = []
    by_source_row: dict[int, StagedImage] = {}
    match_methods: dict[int, str] = {}
    suggestions_by_image: dict[str, list[dict[str, object]]] = {}
    normalized_assignments: dict[str, int] = {}
    assigned_roots: dict[int, str] = {}
    for image in images:
        suggestions = sorted(
            (
                {
                    "source_row": int(getattr(product, "source_row")),
                    "label": _product_label(product),
                    "score": round(_suggestion_score(image, product), 3),
                }
                for product in products
            ),
            key=lambda item: (-float(item["score"]), int(item["source_row"])),
        )
        suggestions_by_image[image.stored_filename] = suggestions
        explicit_row = assignments.get(image.stored_filename)
        if explicit_row is not None:
            selected = products_by_row.get(explicit_row)
            if selected is None:
                errors.append(f"Image {image.original_filename} points to a product row that no longer exists")
                continue
            target_rows = [selected]
            selected_article = article_key(getattr(selected, "article", None))
            selected_brand = str(getattr(selected, "brand_name", "")).casefold()
            if selected_article:
                target_rows = [
                    product for product in products
                    if article_key(getattr(product, "article", None)) == selected_article
                    and str(getattr(product, "brand_name", "")).casefold() == selected_brand
                ]
            root_row = min(int(getattr(product, "source_row")) for product in target_rows)
            if root_row in assigned_roots:
                errors.append(f"Images {assigned_roots[root_row]} and {image.original_filename} both target the same product")
                continue
            assigned_roots[root_row] = image.original_filename
            normalized_assignments[image.stored_filename] = explicit_row
            for product in target_rows:
                source_row = int(getattr(product, "source_row"))
                by_source_row[source_row] = image
                match_methods[source_row] = "user_selected_product"
            continue
        matching_rows = article_rows.get(image.article_key, [])
        if not matching_rows:
            errors.append(f"Choose a product for image {image.original_filename}; its filename did not exactly match an article/SKU")
            continue
        if len(article_brands.get(image.article_key, set())) > 1:
            errors.append(f"Image {image.original_filename} is ambiguous because article/SKU {image.article_key} occurs under multiple brands")
            continue
        root_row = min(int(getattr(product, "source_row")) for product in matching_rows)
        if root_row in assigned_roots:
            errors.append(f"Images {assigned_roots[root_row]} and {image.original_filename} both target the same product")
            continue
        assigned_roots[root_row] = image.original_filename
        for product in matching_rows:
            source_row = int(getattr(product, "source_row"))
            by_source_row[source_row] = image
            match_methods[source_row] = "exact_article_filename"
    unmatched_products = sum(1 for product in products if int(getattr(product, "source_row")) not in by_source_row)
    if images and unmatched_products:
        warnings_list.append(f"{unmatched_products} product rows have no image; images can be added later from the brand table")
    return ImageMatchResult(
        by_source_row,
        images,
        errors,
        warnings_list,
        suggestions_by_image,
        normalized_assignments,
        match_methods,
    )


def staged_image_path(token: str, stored_filename: str) -> Path | None:
    images = load_stage(token)
    if stored_filename not in {item.stored_filename for item in images}:
        return None
    path = (_token_path(token) / Path(stored_filename).name).resolve()
    if path.parent != _token_path(token) or not path.is_file():
        return None
    return path


def persist_matches(
    token: str,
    season_name: str,
    by_source_row: dict[int, StagedImage],
    items_by_source_row: dict[int, object],
    match_methods: dict[int, str] | None = None,
) -> list[Path]:
    season_folder_name = re.sub(r"[^A-Za-z0-9_-]", "-", season_name).strip("-").lower() or "season"
    destination = (PERMANENT_ROOT / season_folder_name).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    created: dict[str, tuple[Path, str]] = {}
    for source_row, staged in by_source_row.items():
        item = items_by_source_row.get(source_row)
        if item is None:
            continue
        if staged.stored_filename not in created:
            source = staged_image_path(token, staged.stored_filename)
            if source is None:
                raise ValueError(f"Staged image {staged.original_filename} is no longer available")
            filename = f"{staged.article_key}-{secrets.token_hex(5)}{staged.extension}"
            target = (destination / filename).resolve()
            if target.parent != destination:
                raise ValueError("Invalid product image destination")
            shutil.copy2(source, target)
            relative = target.relative_to(PERMANENT_ROOT.resolve()).as_posix()
            created[staged.stored_filename] = (target, relative)
        _target, relative = created[staged.stored_filename]
        item.image_path = relative
        item.image_source_filename = staged.original_filename
        item.image_match_method = (match_methods or {}).get(source_row, "exact_article_filename")
        item.image_updated_at = datetime.now(timezone.utc)
    return [value[0] for value in created.values()]


def store_manual_image(item_id: int, original_filename: str, content: bytes) -> tuple[str, str]:
    extension, _width, _height = _inspect_image(original_filename, content)
    folder = (PERMANENT_ROOT / "manual").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"item-{item_id}-{secrets.token_hex(8)}{extension}"
    target = (folder / filename).resolve()
    if target.parent != folder:
        raise ValueError("Invalid product image destination")
    target.write_bytes(content)
    return target.relative_to(PERMANENT_ROOT.resolve()).as_posix(), target.as_posix()


def store_external_image(item_id: int, image_url: str) -> tuple[str, str]:
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Selected product image must use a public HTTPS address")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Selected product image host could not be resolved") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Selected product image address is not public")
    try:
        response = httpx.get(image_url, timeout=20, follow_redirects=False, headers={"user-agent": "FashionOrderImageFetcher/1.0"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError(f"Selected product image could not be downloaded: {exc}") from exc
    if len(response.content) > MAX_IMAGE_BYTES:
        raise ValueError("Selected product image is larger than 12 MB")
    original = Path(parsed.path).name or f"product-{item_id}.jpg"
    return store_manual_image(item_id, original, response.content)


def discard_stage(token: str | None) -> None:
    if not token:
        return
    try:
        folder = _token_path(token)
    except ValueError:
        return
    if folder.is_dir():
        shutil.rmtree(folder)


def cleanup_expired_stages(max_age_hours: int = 24) -> None:
    if not STAGING_ROOT.is_dir():
        return
    cutoff = time.time() - (max_age_hours * 60 * 60)
    for candidate in STAGING_ROOT.iterdir():
        if not candidate.is_dir() or not TOKEN_RE.fullmatch(candidate.name):
            continue
        try:
            modified = candidate.stat().st_mtime
        except OSError:
            continue
        if modified < cutoff:
            discard_stage(candidate.name)
