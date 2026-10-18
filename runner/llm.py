"""Self-contained copy of framework/llm.py for the sandbox (stdlib + openai only).

Every call is logged briefly to stdout: agent, model, tokens, elapsed.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

CRUSOE_URL = "https://api.inference.crusoecloud.com/v1"
SUPER_VL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL"
LIGHTNING = "nvidia/Nemotron-3.5-Lightning-30B-A3B"

_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
_client: OpenAI | None = None

# Running totals, read by loop.py for the status page.
USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0, "retries": 0}
CONTEXT = {"round": 0}


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("CRUSOE_API_KEY")
        if not key:
            raise RuntimeError("CRUSOE_API_KEY is not set")
        _client = OpenAI(base_url=CRUSOE_URL, api_key=key, timeout=180, max_retries=0)
    return _client


_BACKOFF = [2, 4, 8, 15, 25, 40]
_MIN_GAP = float(os.environ.get("LLM_MIN_GAP_S", "0.25"))
_last_call = [0.0]


def _create(agent: str, **kw):
    """chat.completions.create with backoff on 429 / 5xx / network errors (Crusoe returns
    429 'max_requests_limit (orchestrator)' under bursty load)."""
    for i, wait in enumerate(_BACKOFF + [None]):
        gap = _MIN_GAP - (time.time() - _last_call[0])
        if gap > 0:
            time.sleep(gap)
        _last_call[0] = time.time()
        try:
            return client().chat.completions.create(**kw)
        except (RateLimitError, APIConnectionError, APITimeoutError, APIStatusError) as e:
            status = getattr(e, "status_code", None)
            retryable = isinstance(e, (RateLimitError, APIConnectionError, APITimeoutError)) or (status or 0) >= 500
            if not retryable or wait is None:
                raise
            USAGE["retries"] = USAGE.get("retries", 0) + 1
            print(f"[llm] agent={agent} {type(e).__name__} ({status}); retry {i + 1} in {wait}s", flush=True)
            time.sleep(wait)


def _log(agent: str, model: str, r: Any, t0: float) -> None:
    dt = time.time() - t0
    u = getattr(r, "usage", None)
    pt = getattr(u, "prompt_tokens", 0) or 0
    ct = getattr(u, "completion_tokens", 0) or 0
    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += pt
    USAGE["completion_tokens"] += ct
    USAGE["seconds"] += dt
    short = "superVL" if model == SUPER_VL else "lightning" if model == LIGHTNING else model
    print(f"[llm] round={CONTEXT['round']} agent={agent} model={short} "
          f"tok={pt}+{ct} {dt:.1f}s", flush=True)


def image_part(png_bytes: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(png_bytes).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def user_with_images(text: str, images: list[bytes]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content += [image_part(b) for b in images]
    return {"role": "user", "content": content}


def chat_json(model: str, messages: list[dict], *, agent: str = "?", max_tokens: int = 4000,
              temperature: float = 0.6) -> dict:
    """JSON-mode completion; retries once with a bigger budget if truncated."""
    for attempt in range(2):
        t0 = time.time()
        r = _create(agent,
            model=model, messages=messages, max_tokens=max_tokens,
            temperature=temperature, top_p=0.95,
            response_format={"type": "json_object"}, extra_body=_NO_THINK,
        )
        _log(agent, model, r, t0)
        ch = r.choices[0]
        if ch.finish_reason == "length" and attempt == 0:
            max_tokens *= 2
            continue
        return loads_lenient(ch.message.content or "")
    raise RuntimeError("unreachable")


def chat_tools(model: str, messages: list[dict], tools: list[dict], *, agent: str = "?",
               max_tokens: int = 4000, temperature: float = 1.0):
    """One tool-calling step. Returns (assistant message, finish_reason)."""
    t0 = time.time()
    r = _create(agent,
        model=model, messages=messages, tools=tools, tool_choice="auto",
        max_tokens=max_tokens, temperature=temperature, top_p=0.95, extra_body=_NO_THINK,
    )
    _log(agent, model, r, t0)
    return r.choices[0].message, r.choices[0].finish_reason


def chat_text(model: str, messages: list[dict], *, agent: str = "?", max_tokens: int = 2000,
              temperature: float = 1.0) -> str:
    t0 = time.time()
    r = _create(agent,
        model=model, messages=messages, max_tokens=max_tokens,
        temperature=temperature, top_p=0.95, extra_body=_NO_THINK,
    )
    _log(agent, model, r, t0)
    return r.choices[0].message.content or ""


def loads_lenient(text: str) -> dict:
    text = (text or "").strip()
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


# ---- video support probe ---------------------------------------------------
VIDEO_OK: bool | None = None  # None = not probed yet


def video_part(mp4_bytes: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(mp4_bytes).decode()
    return {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}


def probe_video(sample_mp4: str | None = None) -> bool:
    """Try one native-video request against SUPER_VL; remember the answer in VIDEO_OK.

    Generates a 1 s ffmpeg testsrc clip when no sample is given. Any error -> False (use contact sheets).
    """
    global VIDEO_OK
    import shutil
    import subprocess
    import tempfile
    try:
        if not sample_mp4:
            if not shutil.which("ffmpeg"):
                VIDEO_OK = False
                return False
            sample_mp4 = os.path.join(tempfile.mkdtemp(), "probe.mp4")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc=size=320x240:rate=10:duration=1", "-pix_fmt", "yuv420p", sample_mp4],
                           check=True, timeout=30)
        with open(sample_mp4, "rb") as f:
            data = f.read()
        msg = {"role": "user", "content": [{"type": "text", "text": "Describe this video in five words."},
                                           video_part(data)]}
        t0 = time.time()
        r = client().chat.completions.create(model=SUPER_VL, messages=[msg], max_tokens=40, temperature=0.2,
                                             extra_body=_NO_THINK)
        _log("video-probe", SUPER_VL, r, t0)
        VIDEO_OK = bool((r.choices[0].message.content or "").strip())
    except Exception as e:  # noqa: BLE001
        print(f"[llm] video probe: not accepted ({type(e).__name__}: {str(e)[:120]})", flush=True)
        VIDEO_OK = False
    print(f"[llm] SUPER_VL native video input: {'yes' if VIDEO_OK else 'no -> contact sheets'}", flush=True)
    return VIDEO_OK
