from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent
# Claude models offered in Settings › AI. The other one is used automatically as the fallback.
CLAUDE_MODELS = {
    "claude-sonnet-5-5": "Claude Sonnet 5.5 — best quality (recommended)",
    "claude-haiku-5-5": "Claude Haiku 5.5 — fastest and lowest cost",
}
_stored = {"stamp": None, "value": {}}


def stored_ai_settings() -> dict:
    """AI key and model saved from Settings (encrypted in data/private); {} when none or unreadable."""
    try:
        from app import secret_store
        path = secret_store._path("ai-settings")
        stamp = (str(path), path.stat().st_mtime_ns) if path.exists() else (str(path), None)
        if stamp != _stored["stamp"]:
            _stored["value"] = secret_store.load("ai-settings") if stamp[1] else {}
            _stored["stamp"] = stamp
        return _stored["value"]
    except Exception:  # A damaged or foreign secret file must never stop the app; .env still works.
        return {}


class Settings(BaseSettings):
    app_name: str = "app"
    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'fashion_orders.db').as_posix()}"
    default_season: str = "SS26"
    default_customer_code: str = "AA"
    prepayment_percentage: Decimal = Decimal("0.50")
    supported_markup_percentages: str = "0.25,0.30,0.35"
    currency: str = "EUR"
    app_username: str = ""
    app_password: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_fallback_models: str = ""
    # "anthropic" (Claude API) or "gemini". Empty = detect from the key (sk-ant-... means Claude).
    ai_provider: str = ""
    anthropic_api_key: str = ""
    # Backward-compatible local settings. New installations use the generic AI names above.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_fallback_models: str = "gemini-2.5-flash,gemini-3.7-flash"
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    searxng_url: str = "http://127.0.0.1:8888"
    # Durable background jobs (analysis, sync, backups). Disable only for tests or one-off scripts.
    background_worker: bool = True
    # Address this app is opened at; the Google OAuth redirect URI is derived from it.
    public_base_url: str = "http://127.0.0.1:8001"
    # Extra host names this app may be addressed by (comma separated), besides loopback and public_base_url.
    allowed_hosts: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_api_key: str = ""   # Browser key for the Google Picker (restricted to this origin in Google Cloud).
    google_app_id: str = ""    # Google Cloud project number, required by the Picker.
    # Recovery: snapshot interval and how many verified local snapshots to keep (remote archives are never auto-deleted).
    recovery_interval_hours: int = 6
    recovery_keep_local: int = 30
    model_config = SettingsConfigDict(env_prefix="FASHION_", env_file=".env")

    @property
    def supported_markups(self) -> set[Decimal]:
        return {
            Decimal(item.strip())
            for item in self.supported_markup_percentages.split(",")
            if item.strip()
        }

    @property
    def active_ai_api_key(self) -> str:
        return (stored_ai_settings().get("api_key") or self.ai_api_key.strip() or self.anthropic_api_key.strip()
                or self.gemini_api_key.strip())

    @property
    def active_ai_provider(self) -> str:
        if stored_ai_settings().get("api_key"):
            return "anthropic"
        explicit = self.ai_provider.strip().casefold()
        if explicit in {"anthropic", "claude"}:
            return "anthropic"
        if explicit == "gemini":
            return "gemini"
        only_anthropic = self.anthropic_api_key.strip() and not self.ai_api_key.strip() and not self.gemini_api_key.strip()
        return "anthropic" if self.active_ai_api_key.startswith("sk-ant-") or only_anthropic else "gemini"

    @property
    def active_ai_model(self) -> str:
        stored = stored_ai_settings()
        if stored.get("api_key") and stored.get("model") in CLAUDE_MODELS:
            return stored["model"]
        if self.ai_model.strip():
            return self.ai_model.strip()
        return "claude-sonnet-5-5" if self.active_ai_provider == "anthropic" else self.gemini_model.strip()

    @property
    def active_ai_fallback_models(self) -> str:
        if stored_ai_settings().get("api_key"):
            return ",".join(m for m in CLAUDE_MODELS if m != self.active_ai_model)
        if self.ai_fallback_models.strip():
            return self.ai_fallback_models.strip()
        if self.active_ai_provider == "anthropic":
            return ",".join(m for m in CLAUDE_MODELS if m != self.active_ai_model)
        return self.gemini_fallback_models.strip()


settings = Settings()
