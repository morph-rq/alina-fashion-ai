"""Navigation helpers shared by templates and routes.

Primary areas follow the owner's workflow (Обзор, Заказы, Фабрики, Клиенты, Документы, Ассистент)
plus Settings. Inbox (incoming sources) is part of Документы.
Every existing URL keeps working; these helpers only decide which area and Workspace view
a URL belongs to, and validate same-site "return here" links.
"""
from __future__ import annotations

from urllib.parse import urlsplit

AREAS = (
    ("overview", "Обзор", "/"),
    ("workspace", "Заказы", "/board"),
    ("factories", "Фабрики", "/factories"),
    ("customers", "Клиенты", "/pricing"),
    ("inbox", "Документы", "/documents"),
    ("assistant", "Ассистент", "/assistant"),
)

_INBOX = ("/inbox", "/intake", "/ai-intake", "/source-reviews", "/intake-sources", "/sources/", "/documents",
          "/factory-import", "/originals")
_CUSTOMERS = ("/customers", "/reports", "/pricing")
_FACTORIES = ("/factories",)
_ASSISTANT = ("/assistant",)
_SETTINGS = ("/settings", "/rules", "/connections", "/recovery", "/workspace/reset", "/audit", "/library", "/storage", "/history")
_OVERVIEW = ("/operations", "/validation")

WORKSPACE_VIEWS = (
    ("board", "Заказы по фабрикам", "/board"),
    ("products", "Products", "/worksheet"),
    ("brands", "Brands", "/brands"),
    ("logistics", "Ready & shipping", "/logistics"),
    ("money", "Money", "/money"),
    ("tables", "Tables", "/tables"),
)
_VIEW_PREFIXES = {
    "board": ("/board",),
    "products": ("/worksheet", "/excel", "/import", "/imports", "/product-images"),
    "brands": ("/brands", "/seasons", "/orders", "/manual-entry"),
    "logistics": ("/logistics", "/ready-batches", "/shipments", "/groups"),
    "money": ("/money", "/payments", "/credits"),
    "tables": ("/tables",),
}


def nav_area(path: str) -> str:
    if path == "/" or path.startswith(_OVERVIEW):
        return "overview"
    if path.startswith(_INBOX):
        return "inbox"
    if path.startswith(_CUSTOMERS):
        return "customers"
    if path.startswith(_FACTORIES):
        return "factories"
    if path.startswith(_ASSISTANT):
        return "assistant"
    if path.startswith(_SETTINGS):
        return "settings"
    return "workspace"


def workspace_view(path: str) -> str | None:
    for view, prefixes in _VIEW_PREFIXES.items():
        if path.startswith(prefixes):
            return view
    return None


def safe_next(value: str | None, default: str) -> str:
    """Return a same-site path to go back to after a form, or ``default``.

    Only relative paths are accepted, so a crafted link can never redirect off the app.
    """
    candidate = (value or "").strip()
    if (not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate
            or any(char in candidate for char in "\r\n\t") or len(candidate) > 2000):
        return default
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return default
    return candidate
