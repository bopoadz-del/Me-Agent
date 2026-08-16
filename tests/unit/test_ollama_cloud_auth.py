"""OllamaClient must authenticate against Ollama Cloud (spec Section 8, live E2E 11.4 step 7).

Ollama Cloud (https://ollama.com) returns HTTP 401 without a Bearer token, so the
client is unusable against Cloud unless it sends one. Local Ollama takes no auth and
must keep working unchanged — sending an empty Authorization header would break it.

These tests pin the request the client actually puts on the wire: URL, and the
presence/absence of the Authorization header. They do not mock OllamaClient itself.
"""
from __future__ import annotations

import json

import httpx
import pytest

from common.models.llm_client import OllamaClient


@pytest.fixture
def captured(monkeypatch):
    """Capture the outbound request without reaching the network."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"response": json.dumps({"sum": 8})})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return seen


def test_local_ollama_sends_no_authorization_header(captured, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")

    OllamaClient().generate(prompt="a=5, b=3", system="add them")

    assert "authorization" not in captured["headers"], (
        "local Ollama takes no auth; an Authorization header must not be invented"
    )
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"


def test_cloud_api_key_is_sent_as_bearer(captured, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-cloud-key-abc123")
    monkeypatch.setenv("OLLAMA_URL", "https://ollama.com")

    OllamaClient(model="gpt-oss:120b-cloud").generate(prompt="a=5, b=3", system="add them")

    assert captured["headers"].get("authorization") == "Bearer test-cloud-key-abc123"
    assert captured["url"] == "https://ollama.com/api/generate"
    assert captured["body"]["model"] == "gpt-oss:120b-cloud"


def test_blank_api_key_is_treated_as_absent(captured, monkeypatch):
    """An empty env var must not produce `Authorization: Bearer ` against local Ollama."""
    monkeypatch.setenv("OLLAMA_API_KEY", "   ")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")

    OllamaClient().generate(prompt="a=1, b=1", system="add them")

    assert "authorization" not in captured["headers"]


def test_explicit_api_key_argument_wins_over_env(captured, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "env-key")
    monkeypatch.setenv("OLLAMA_URL", "https://ollama.com")

    OllamaClient(api_key="explicit-key").generate(prompt="a=2, b=2", system="add them")

    assert captured["headers"].get("authorization") == "Bearer explicit-key"
