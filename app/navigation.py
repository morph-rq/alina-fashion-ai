"""Navigation helpers shared by templates and routes.

The interface has four primary areas (Overview, Workspace, Inbox, Customers) plus Settings.
Every existing URL keeps working; these helpers only decide which area and Workspace view
a URL belongs to, and validate same-site "return here" links.
"""
from __future__ import annotations

from urllib.parse import urlsplit

AREAS = (
    ("overview", "Overview", "/"),
    ("workspace", "Workspace", "/worksheet"),
    ("inbox", "Inbox", "/inbox"),
    ("customers", "Customers", "/customers"),
)

_INBOX = ("/inbox", "/intake", "/ai-intake", "/source-reviews", "/intake-sources", "/sources/")
_CUSTOMERS = ("/customers", "/reports")
_SETTINGS = ("/settings", "/rules", "/connections", "/recovery", "/workspace/reset", "/audit", "/library", "/storage")
_OVERVIEW = ("/operations", "/validation")

WORKSPACE_VIEWS = (
    ("products", "Products", "/worksheet"),
    ("brands", "Brands", "/brands"),
    ("logistics", "Ready & shipping", "/logistics"),
    ("money", "Money", "/money"),
)
_VIEW_PREFIXES = {
    "products": ("/worksheet", "/excel", "/import", "/imports", "/product-images"),
    "brands": ("/brands", "/seasons", "/orders", "/manual-entry"),
    "logistics": ("/logistics", "/ready-batches", "/shipments", "/groups"),
    "money": ("/money", "/payments", "/credits"),
}


def nav_area(path: str) -> str:
    if path == "/" or path.startswith(_OVERVIEW):
        return "overview"
    if path.startswith(_INBOX):
        return "inbox"
    if path.startswith(_CUSTOMERS):
        return "customers"
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
