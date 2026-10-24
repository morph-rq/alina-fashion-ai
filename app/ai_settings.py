"""Settings › AI: paste a Claude API key, choose Sonnet 5.5 or Haiku 5.5, and test the connection.

The key is stored with ``secret_store`` (Windows DPAPI / AES-GCM in data/private, never in backups)
and is never sent back to the browser; only its last four characters are shown.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import Form, Request
from fastapi.responses import RedirectResponse

from app import secret_store
from app.ai_intake import ANTHROPIC_URL, ANTHROPIC_VERSION
from app.config import CLAUDE_MODELS, settings, stored_ai_settings

REF = "ai-settings"


def status() -> dict:
    stored = stored_ai_settings()
    key = settings.active_ai_api_key
    return {
        "source": "app" if stored.get("api_key") else ("env" if key else None),
        "provider": settings.active_ai_provider if key else None,
        "model": settings.active_ai_model if key else None,
        "fallback": settings.active_ai_fallback_models if key else None,
        "hint": ("…" + key[-4:]) if len(key) > 8 else None,
        "saved_at": stored.get("saved_at"),
    }


def test_key(key: str, model: str) -> str | None:
    """One tiny request; returns None when Claude answered, otherwise an operator-readable reason."""
    try:
        response = httpx.post(ANTHROPIC_URL, timeout=30, headers={
            "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"},
            json={"model": model, "max_tokens": 64, "output_config": {"effort": "low"},
                  "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
    except httpx.HTTPError:
        return "Could not reach the Claude API. Check the internet connection and try again."
    if response.status_code == 200:
        return None
    if response.status_code in {401, 403}:
        return "Claude did not accept this key. Copy it again from console.anthropic.com › API keys."
    if response.status_code == 404:
        return f"This key cannot use {model}. Choose the other model or check the key's workspace."
    if response.status_code == 429:
        return "The key works but is rate-limited or out of credit right now. Check Billing in the Claude Console."
    return f"Claude answered with HTTP {response.status_code}. Try again in a minute."


def install_ai_settings(app, templates, context):
    @app.get("/settings/ai")
    def ai_page(request: Request):
        return templates.TemplateResponse(request, "settings_ai.html", context(
            request, ai=status(), models=CLAUDE_MODELS,
            message=request.query_params.get("message"), error=request.query_params.get("error")))

    @app.post("/settings/ai")
    def ai_save(action: str = Form("save"), api_key: str = Form(""), model: str = Form("claude-sonnet-5-5")):
        def back(**params):
            return RedirectResponse("/settings/ai?" + "&".join(f"{k}={quote(v)}" for k, v in params.items()), 303)

        stored = dict(stored_ai_settings())
        if action == "remove":
            secret_store.delete(REF)
            return back(message="Claude key removed from the app. A key in .env, if any, is used again.")
        if model not in CLAUDE_MODELS:
            return back(error="Choose Claude Sonnet 5.5 or Claude Haiku 5.5.")
        key = "".join(api_key.split())
        if key and not key.startswith("sk-ant-"):
            return back(error="That does not look like a Claude API key (they start with sk-ant-).")
        key = key or stored.get("api_key", "")
        if not key:
            return back(error="Paste your Claude API key first.")
        if action == "test":
            problem = test_key(key, model)
            if problem:
                return back(error=problem)
        stored.update(api_key=key, model=model, saved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        secret_store.save(REF, stored)
        label = CLAUDE_MODELS[model].split(" —")[0]
        return back(message=f"{'Key works. ' if action == 'test' else ''}Saved: AI uses {label}.")
