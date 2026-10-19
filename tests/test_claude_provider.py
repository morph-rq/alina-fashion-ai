import json

import httpx
import pytest

from app import ai_intake
from app.config import settings


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://x"), response=httpx.Response(self.status_code))


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setattr(settings, "ai_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "ai_provider", "")
    monkeypatch.setattr(settings, "ai_model", "")
    monkeypatch.setattr(settings, "ai_fallback_models", "")
    calls = []
    monkeypatch.setattr(ai_intake.time, "sleep", lambda s: None)
    return calls


def test_key_prefix_selects_claude_defaults(claude):
    assert settings.active_ai_provider == "anthropic"
    assert settings.active_ai_model == "claude-opus-5-5"


def test_structured_call_sends_claude_request(claude, monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers)
        return _Resp(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"recommended_index": null, "confidence": 0.1, "reason": "none"}'}]})
    monkeypatch.setattr(ai_intake.httpx, "post", post)
    result = ai_intake._generate(ai_intake.ImageApproval, "sys", [{"text": "hi"}, {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}])
    assert result.reason == "none"
    assert seen["url"].startswith("https://api.anthropic.com/")
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["body"]["model"] == "claude-opus-5-5"
    assert seen["body"]["messages"][0]["content"][1]["type"] == "image"
    assert seen["body"]["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_400_retries_without_schema_then_falls_back_on_5xx(claude, monkeypatch):
    modes = []

    def post(url, json=None, headers=None, timeout=None):
        modes.append((json["model"], "format" in json["output_config"]))
        if len(modes) == 1:
            return _Resp(400, {})
        if len(modes) == 2:
            return _Resp(529, {})
        return _Resp(200, {"content": [{"type": "text", "text": '```json\n{"recommended_index": 1, "confidence": 0.9, "reason": "r"}\n```'}]})
    monkeypatch.setattr(ai_intake.httpx, "post", post)
    result = ai_intake._generate(ai_intake.ImageApproval, "sys", [{"text": "hi"}])
    assert result.recommended_index == 1
    assert modes == [("claude-opus-5-5", True), ("claude-opus-5-5", False), ("claude-sonnet-5-5", True)]


def test_auth_failure_and_audio_are_clear(claude, monkeypatch):
    monkeypatch.setattr(ai_intake.httpx, "post", lambda *a, **k: _Resp(401, {}))
    with pytest.raises(ValueError, match="authenticate"):
        ai_intake._generate(ai_intake.ImageApproval, "s", [{"text": "x"}])
    with pytest.raises(ValueError, match="audio"):
        ai_intake._generate(ai_intake.ImageApproval, "s", [{"inlineData": {"mimeType": "audio/mpeg", "data": "A"}}])
