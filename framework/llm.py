"""Crusoe Managed Inference client helpers (OpenAI-compatible).

Two Nemotron models, two roles:
  SUPER_VL  - 120B-A12B vision-language: spec compiler, visual judge, re-planner.
  LIGHTNING - 30B-A3B text: builder and user-simulator ("preset slave"), sub-second tool calls.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from openai import OpenAI

CRUSOE_URL = "https://api.inference.crusoecloud.com/v1"
SUPER_VL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL"
LIGHTNING = "nvidia/Nemotron-3.5-Lightning-30B-A3B"

_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


def client() -> OpenAI:
    key = os.environ.get("CRUSOE_API_KEY")
    if not key:
        raise RuntimeError("CRUSOE_API_KEY is not set")
    return OpenAI(base_url=CRUSOE_URL, api_key=key)


def image_part(png_bytes: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(png_bytes).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def user_with_images(text: str, images: list[bytes]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content += [image_part(b) for b in images]
    return {"role": "user", "content": content}


def chat_json(model: str, messages: list[dict], *, max_tokens: int = 4000,
              temperature: float = 0.6, thinking: bool = False) -> dict:
    """JSON-mode completion; retries once with a bigger budget if truncated."""
    c = client()
    extra = {} if thinking else _NO_THINK
    for attempt in range(2):
        r = c.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens,
            temperature=temperature, top_p=0.95,
            response_format={"type": "json_object"}, extra_body=extra,
        )
        ch = r.choices[0]
        if ch.finish_reason == "length" and attempt == 0:
            max_tokens *= 2
            continue
        text = ch.message.content or ""
        return _loads_lenient(text)
    raise RuntimeError("unreachable")


def chat_tools(model: str, messages: list[dict], tools: list[dict], *,
               max_tokens: int = 2000, temperature: float = 1.0):
    """One tool-calling step. Returns the assistant message (may contain tool_calls)."""
    c = client()
    r = c.chat.completions.create(
        model=model, messages=messages, tools=tools, tool_choice="auto",
        max_tokens=max_tokens, temperature=temperature, top_p=0.95, extra_body=_NO_THINK,
    )
    return r.choices[0].message


def chat_text(model: str, messages: list[dict], *, max_tokens: int = 2000,
              temperature: float = 1.0) -> str:
    c = client()
    r = c.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens,
        temperature=temperature, top_p=0.95, extra_body=_NO_THINK,
    )
    return r.choices[0].message.content or ""


def _loads_lenient(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise
