"""Vision models for picture -> blueprint.

``ClaudeVision`` sends the picture to Claude with ``image.VISION_PROMPT`` and a strict JSON schema (structured
outputs), so the reply always parses into the brief fields the engine consumes. It is optional: install the
``vision`` extra (``anthropic``) and provide credentials (``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile).
Without it the engine still works offline (outline + drawn front-panel parts + a "what should it do?" question).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from hwbp.image import decode, downsample, encode_png_gray

DEFAULT_MODEL = "claude-opus-5-5"
SUPPORTED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "prompt": {"type": "string"},
        "summary": {"type": "string"},
        "components": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "features": {"type": "array", "items": {"type": "string"}},
        "approxSizeMm": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"w": {"type": "number"}, "d": {"type": "number"}, "h": {"type": "number"}},
                    "required": ["w", "d", "h"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "materials": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": [
        "name", "prompt", "summary", "components", "uncertainties", "features", "approxSizeMm", "materials", "confidence"
    ],
    "additionalProperties": False,
}  # fmt: skip


class VisionError(RuntimeError):
    """The vision model could not describe the picture (refusal, truncated reply, unsupported input)."""


def _as_supported(image: bytes, mime: str) -> tuple[bytes, str]:
    """Claude reads PNG/JPEG/WebP/GIF; other formats we decode (PGM/PPM) are re-encoded as grayscale PNG."""
    if mime in SUPPORTED_MIME:
        return image, mime
    img = downsample(decode(image), 1024)
    return encode_png_gray(img.gray), "image/png"


class ClaudeVision:
    """``VisionModel`` backed by the Claude Messages API (vision + structured outputs).

    ``client`` defaults to ``anthropic.Anthropic()`` (credentials from the environment). Refusals are routed to
    Anthropic's recommended fallback model server-side (``fallbacks: "default"``); if the whole chain declines,
    ``VisionError`` is raised so the caller can fall back to the offline path.
    """

    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL, effort: str = "medium", max_tokens: int = 16000):
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - depends on the optional extra
                raise VisionError("install the 'vision' extra (anthropic) to use ClaudeVision") from e
            client = anthropic.Anthropic()
        self.client, self.model, self.effort, self.max_tokens = client, model, effort, max_tokens

    def describe(self, image: bytes, mime: str, instruction: str) -> dict:
        data, media_type = _as_supported(image, mime)
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(data).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": instruction},
                    ],
                }
            ],
        )
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise VisionError(f"the vision model declined the picture ({getattr(details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise VisionError("the vision model's reply was cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise VisionError("the vision model returned invalid JSON") from e


def vision_from_env(kind: str = "auto") -> ClaudeVision | None:
    """``none`` → offline; ``claude`` → ClaudeVision (errors if unavailable); ``auto`` → Claude when the
    ``anthropic`` package is installed and ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` is set."""
    if kind == "none":
        return None
    if kind == "claude":
        return ClaudeVision(model=os.environ.get("HWBP_VISION_MODEL", DEFAULT_MODEL))
    if kind != "auto":
        raise ValueError("vision must be auto | claude | none")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return None
    return ClaudeVision(model=os.environ.get("HWBP_VISION_MODEL", DEFAULT_MODEL))
