"""Credential-broker plumbing for the sandbox runner (stdlib only).

The provisioner injects secrets as env vars plus SECRETS_LEDGER, a JSON list of
{name, status: "injected"|"missing", source, masked, used_for, hint}. Nothing here ever returns or logs a
secret VALUE: the ledger is metadata only, and redact() scrubs values out of tool output before it reaches an
LLM, a log line or the public status page.

Also: ${VAR} template resolution for check urls/steps (DEPLOY_URL comes from site/deploy.json).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SITE = Path(os.environ.get("SITE_DIR", "site"))
# Only these are substituted into urls/steps/expected values; arbitrary env (tokens!) never is.
TEMPLATE_VARS = ("DEPLOY_URL", "NTFY_TOPIC", "JOB_ID", "LIVE_URL", "NOVNC_URL")
_TEMPLATE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_LEDGER_FIELDS = ("name", "status", "source", "masked", "used_for", "hint")


def parse_ledger(raw: str | None = None) -> list[dict]:
    """SECRETS_LEDGER -> list of metadata dicts (unknown keys, especially anything like 'value', dropped)."""
    raw = os.environ.get("SECRETS_LEDGER", "") if raw is None else raw
    try:
        data = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):  # tolerate {"NAME": {...}} shape
        data = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        d = {k: (str(e[k]) if e.get(k) is not None else "") for k in _LEDGER_FIELDS if k in e}
        d["name"] = str(e["name"])
        d["status"] = str(e.get("status") or ("injected" if os.environ.get(d["name"]) else "missing"))
        out.append(d)
    return out


def missing_names(ledger: list[dict] | None = None) -> list[str]:
    return [e["name"] for e in (parse_ledger() if ledger is None else ledger) if e.get("status") != "injected"]


def is_optional(entry: dict) -> bool:
    return "optional" in ((entry.get("hint") or "") + " " + (entry.get("used_for") or "")).lower()


def _secret_values(ledger: list[dict] | None = None) -> list[str]:
    names = {e["name"] for e in (parse_ledger() if ledger is None else ledger)}
    names |= {"CRUSOE_API_KEY", "VERCEL_TOKEN", "GITHUB_TOKEN", "GPU_SSH_KEY_B64"}
    vals = [os.environ.get(n, "") for n in names]
    return sorted({v for v in vals if len(v) >= 6}, key=len, reverse=True)


def redact(text: str, ledger: list[dict] | None = None) -> str:
    """Replace any injected secret value appearing in text with ***."""
    if not text:
        return text
    for v in _secret_values(ledger):
        if v in text:
            text = text.replace(v, "***")
    return text


# ------------------------------------------------------------ deploy url / templates --

def read_deploy_url(site: Path | None = None) -> str | None:
    """URL from site/deploy.json ({"url": "https://..."}); exported into os.environ['DEPLOY_URL'] when found."""
    p = Path(site or SITE) / "deploy.json"
    try:
        d = json.loads(p.read_text())
        url = str(d.get("url") or "").strip() if isinstance(d, dict) else ""
    except Exception:  # noqa: BLE001
        url = ""
    if url and not re.match(r"^https?://", url):
        url = "https://" + url.lstrip("/")
    if url:
        os.environ["DEPLOY_URL"] = url.rstrip("/")
        return url.rstrip("/")
    return os.environ.get("DEPLOY_URL") or None


def resolve_templates(s: str | None, site: Path | None = None) -> tuple[str, list[str]]:
    """'${DEPLOY_URL}/api' -> ('https://x.vercel.app/api', []); unresolved names are returned (text kept)."""
    if not s or "${" not in s:
        return s or "", []
    if "${DEPLOY_URL}" in s:
        read_deploy_url(site)
    unresolved: list[str] = []

    def sub(m: re.Match) -> str:
        name = m.group(1)
        val = os.environ.get(name, "") if name in TEMPLATE_VARS else ""
        if not val:
            unresolved.append(name)
            return m.group(0)
        return val.rstrip("/") if name.endswith("_URL") else val
    out = _TEMPLATE.sub(sub, s)
    # '${DEPLOY_URL}/' + '/api' style double slashes after substitution
    out = re.sub(r"(?<!:)//+", "/", out.replace("://", "\x00")).replace("\x00", "://")
    return out, unresolved


def blocking_secret_for(var: str, ledger: list[dict] | None = None) -> str | None:
    """Which missing credential explains an unresolved template var (DEPLOY_URL <- VERCEL_TOKEN)."""
    ledger = parse_ledger() if ledger is None else ledger
    miss = {e["name"] for e in ledger if e.get("status") != "injected" and not is_optional(e)}
    if var in miss:
        return var
    if var == "DEPLOY_URL":
        for n in ("VERCEL_TOKEN",):
            if n in miss:
                return n
    if var in TEMPLATE_VARS and var != "DEPLOY_URL" and not os.environ.get(var):
        return var
    return None


def secrets_prompt_section(ledger: list[dict] | None = None) -> str:
    """Builder prompt block: names/status/purpose only - never values."""
    ledger = parse_ledger() if ledger is None else ledger
    if not ledger:
        return ""
    rows = []
    for e in ledger:
        bits = [f"- {e['name']}: {e.get('status', '?').upper()}"]
        if e.get("used_for"):
            bits.append(f"used for: {e['used_for']}")
        if e.get("hint"):
            bits.append(f"hint: {e['hint']}")
        rows.append(" | ".join(bits))
    return "\n".join(rows)
