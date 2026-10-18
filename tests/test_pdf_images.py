from pathlib import Path

from PIL import Image as PILImage
from reportlab.platypus import Image as ReportLabImage, Paragraph

from app import pdf_report


def _write_test_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", (80, 100), "#8b7454").save(path, format="JPEG")


def test_pdf_resolves_ai_selected_managed_image_path(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "data" / "images" / "manual" / "item-1.jpg"
    _write_test_image(image_path)
    monkeypatch.setattr(pdf_report, "ROOT_DIR", tmp_path)

    assert pdf_report.resolve_product_image_path("manual/item-1.jpg") == image_path
    assert isinstance(pdf_report.product_image("manual/item-1.jpg"), ReportLabImage)


def test_pdf_resolves_legacy_import_path_and_rejects_escape(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "data" / "images" / "import-1" / "row-4.jpg"
    _write_test_image(image_path)
    monkeypatch.setattr(pdf_report, "ROOT_DIR", tmp_path)

    assert pdf_report.resolve_product_image_path("data/images/import-1/row-4.jpg") == image_path
    assert pdf_report.resolve_product_image_path("../../outside.jpg") is None
    assert isinstance(pdf_report.product_image("../../outside.jpg"), Paragraph)
