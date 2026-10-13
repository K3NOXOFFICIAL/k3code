"""Live provider tests and model-list fetching for the wizard."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

PRESETS: dict[str, dict[str, str]] = {
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
    "local": {"kind": "openai", "base_url": "http://localhost:11434/v1", "api_key_env": "LOCAL_API_KEY"},
}
GATEWAY_MARKERS = ("omniroute", "localhost:20128")
# Ids that are not chat models (embeddings, speech, images, moderation, legacy completions): never offered or picked.
# The short words only as a whole word, so "mattsmodel" or "imagenius-chat" stay.
NOT_CHAT = re.compile(
    r"embed|whisper|\btts\b|transcribe|\baudio\b|realtime|dall-e|\bimage\b|\bsora\b|moderation|rerank|babbage|davinci"
    r"|turbo-instruct",
    re.IGNORECASE,
)
CHAT_FAMILIES = ("claude", "gpt", "gemini")  # listed first, so a menu or an unattended pick starts with one of these
MAX_MODEL_CHOICES = 15  # rows of a model menu; "(type one)" takes any other id


def models_url(entry: dict[str, Any]) -> str:
    base = entry["base_url"].rstrip("/")
    return f"{base}/v1/models" if entry["kind"] == "anthropic" else f"{base}/models"


def _headers(entry: dict[str, Any], key: str) -> dict[str, str]:
    if not key:
        return {}
    if entry["kind"] == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def rank_models(models: list[dict[str, Any]]) -> list[str]:
    """The chat model ids of a models list, best first: CHAT_FAMILIES, then the rest; each newest first, then by id."""

    def rank(m: dict[str, Any]) -> tuple[bool, float, str]:
        model_id = str(m["id"])
        created = m.get("created")
        newest = -created if isinstance(created, int | float) else 0
        return not model_id.rsplit("/", 1)[-1].lower().startswith(CHAT_FAMILIES), newest, model_id

    return [str(m["id"]) for m in sorted(models, key=rank) if not NOT_CHAT.search(str(m["id"]))]


def list_models(entry: dict[str, Any], key: str, timeout: float = 8.0) -> tuple[bool, float, list[str], str]:
    """Return (ok, latency_ms, chat model ids best first, detail) from the provider's models endpoint."""
    start = time.monotonic()
    try:
        r = httpx.get(models_url(entry), headers=_headers(entry, key), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, (time.monotonic() - start) * 1000, [], f"{type(e).__name__}"
    ms = (time.monotonic() - start) * 1000
    if r.status_code != 200:
        return False, ms, [], f"HTTP {r.status_code}"
    try:
        models = [m for m in r.json().get("data", []) if isinstance(m, dict) and "id" in m]
        ids = rank_models(models)
    except (ValueError, AttributeError, TypeError):
        models, ids = [], []
    return True, ms, ids, f"HTTP 200, {len(models)} models"


def is_gateway(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('name', '')} {entry.get('base_url', '')}".lower()
    return any(m in text for m in GATEWAY_MARKERS)


def probe_url(url: str, timeout: float = 5.0) -> tuple[bool, float, str]:
    start = time.monotonic()
    try:
        r = httpx.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, (time.monotonic() - start) * 1000, type(e).__name__
    return r.status_code < 500, (time.monotonic() - start) * 1000, f"HTTP {r.status_code}"
