from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent


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
    # Backward-compatible local settings. New installations use the generic AI names above.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_fallback_models: str = "gemini-2.5-flash,gemini-3.7-flash"
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    searxng_url: str = "http://127.0.0.1:8888"
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
        return self.ai_api_key.strip() or self.gemini_api_key.strip()

    @property
    def active_ai_model(self) -> str:
        return self.ai_model.strip() or self.gemini_model.strip()

    @property
    def active_ai_fallback_models(self) -> str:
        return self.ai_fallback_models.strip() or self.gemini_fallback_models.strip()


settings = Settings()
