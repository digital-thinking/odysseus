"""AI tagging of gallery photos with a vision model.

Shared by the per-image ``/api/gallery/{id}/ai-tag`` route and the library
indexing job. The model is ``gallery_tag_model`` when set, else the Settings →
Vision model, so an OCR-only vision model (e.g. glm-ocr) need not be used for
tagging.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TAG_PROMPT = (
    "Analyze this photo. Return ONLY a comma-separated list of tags. "
    "Include: objects, people (describe by appearance — age range, gender), "
    "scene/setting, activities, mood/atmosphere, colors, location type, "
    "time of day, weather if visible, any text/signs visible. "
    "Be specific but concise. 10-25 tags. No explanation, just tags."
)
_MAX_TOKENS = 300
_TIMEOUT = 120


class TaggingUnavailable(RuntimeError):
    """No usable tagging model is configured (user-facing message)."""


class TaggingFailed(RuntimeError):
    """The vision model request failed (user-facing message)."""


def resolve_tag_model(owner: Optional[str]) -> tuple[str, str, dict]:
    """Return ``(chat_url, model, headers)`` for tagging."""
    from src.document_processor import _load_vl_settings, _resolve_vl_model
    from src.settings import get_setting

    vl_settings = _load_vl_settings()
    if not vl_settings.get("vision_enabled", True):
        raise TaggingUnavailable("Vision is disabled — enable it in Settings → Vision")
    configured = (get_setting("gallery_tag_model", "") or "").strip() or vl_settings.get("vision_model", "")
    try:
        chat_url, model_name, headers = _resolve_vl_model(configured, owner=owner)
    except ValueError:
        raise TaggingUnavailable("No tagging model configured — pick one in Gallery → Settings → AI Tagging")
    if not chat_url:
        raise TaggingUnavailable("No vision-capable endpoint configured")
    return chat_url, model_name, headers or {}


def _build_payload(provider: str, model: str, mime: str, b64: str) -> dict:
    from src.llm_core import _restricts_temperature, _uses_max_completion_tokens

    if provider == "anthropic":
        return {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": TAG_PROMPT},
            ]}],
        }
    tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": TAG_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
        tok_key: _MAX_TOKENS,
        "temperature": 0.3,
    }
    # Reasoning models (o1/o3/o4/gpt-5) reject an explicit temperature.
    if _restricts_temperature(model):
        payload.pop("temperature", None)
    return payload


def _thinking_off(payload: dict, url: str, model: str) -> bool:
    """Ask local thinking models to answer directly.

    Otherwise a model such as qwen3.5 spends the whole token budget reasoning
    and returns no tags. Ollama honours ``reasoning_effort: none``; vLLM-style
    servers use ``chat_template_kwargs.enable_thinking``. Returns whether the
    payload was changed (so a server rejecting the fields can be retried).
    """
    from src.llm_core import _apply_local_qwen_thinking_mode, _supports_thinking, is_local_endpoint

    if not (is_local_endpoint(url) and _supports_thinking(model)):
        return False
    payload["reasoning_effort"] = "none"
    _apply_local_qwen_thinking_mode(payload, url, model, "off")
    return True


def _response_text(provider: str, data: dict) -> str:
    if provider == "anthropic":
        return (data.get("content") or [{}])[0].get("text", "") or ""
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def clean_tags(content: str) -> str:
    tags = [t.strip().strip(".").lower() for t in (content or "").replace("\n", ",").split(",")]
    return ", ".join([t for t in tags if t][:30])


def request_tags(image_bytes: bytes, mime: str, owner: Optional[str]) -> str:
    """Tag one image. Returns a comma-separated tag string (may be empty).

    :raises TaggingUnavailable: no model configured.
    :raises TaggingFailed: the model call failed.
    """
    from src.llm_core import _detect_provider

    chat_url, model, extra_headers = resolve_tag_model(owner)
    provider = _detect_provider(chat_url)
    b64 = base64.b64encode(image_bytes).decode()
    payload = _build_payload(provider, model, mime, b64)
    adjusted = provider != "anthropic" and _thinking_off(payload, chat_url, model)
    headers = {"Content-Type": "application/json", **extra_headers}

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(chat_url, json=payload, headers=headers)
        if adjusted and 400 <= resp.status_code < 500:
            # A server that doesn't know the thinking switches: retry the plain request.
            plain = _build_payload(provider, model, mime, b64)
            resp = client.post(chat_url, json=plain, headers=headers)
        if resp.status_code != 200:
            logger.error("AI tagging: %s returned %s: %s", model, resp.status_code, resp.text[:500])
            raise TaggingFailed("Vision model request failed")
        return clean_tags(_response_text(provider, resp.json()))
