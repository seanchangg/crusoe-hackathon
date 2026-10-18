"""ntfy.sh push notifications (zero-auth; subscribe to the topic in the ntfy app)."""
from __future__ import annotations

import httpx


def ping(topic: str, title: str, message: str, url: str | None = None) -> bool:
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "replace").decode()}
    if url:
        headers["Click"] = url
    try:
        r = httpx.post(f"https://ntfy.sh/{topic}", content=message.encode(), headers=headers, timeout=10)
        return r.is_success
    except Exception:
        return False
