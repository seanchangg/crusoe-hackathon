"""REAL: credential broker. Resolves every secret in spec.environment.secrets from the vault and injects it.

The vault is the process environment: on Vercel these are project env vars; locally python-dotenv has loaded
.env / .env.vercel / .env.lambda. Resolved values are written INTO `creds` (mutated in place); the sandbox
adapter, which runs after this one, injects every key in creds into the VM's environment.

A masked ledger [{name, status, source, masked, used_for, hint}] goes to creds["SECRETS_LEDGER"] (JSON, so the
runner sees it as an env var) and to Provisioned.connect_info["ledger"] (so the CLI can render it).
Secret values are never logged or printed: only masked forms leave this module.
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path

from ..spec import Environment, Spec
from .base import Provisioned

ROOT = Path(__file__).resolve().parents[2]

# Single source of truth for the secret catalog (the compiler prompt is generated from this too).
CATALOG: dict[str, dict[str, str]] = {
    "CRUSOE_API_KEY": {
        "used_for": "LLM inference on Crusoe (Nemotron builder/verifier); always included",
        "hint": "OpenAI-compatible base_url https://api.inference.crusoecloud.com/v1, api_key=$CRUSOE_API_KEY",
    },
    "NTFY_TOPIC": {
        "used_for": "push notifications / announcing results on ntfy.sh; always included",
        "hint": "curl -d \"<message>\" https://ntfy.sh/$NTFY_TOPIC",
    },
    "VERCEL_TOKEN": {
        "used_for": "deploy web apps to Vercel (VERCEL_TEAM_ID injected too when known)",
        "hint": ("cd site && npx -y vercel link --yes --project <name> --token $VERCEL_TOKEN "
                 "${VERCEL_TEAM_ID:+--scope $VERCEL_TEAM_ID} && "
                 "npx -y vercel deploy --prod --yes --token $VERCEL_TOKEN ${VERCEL_TEAM_ID:+--scope $VERCEL_TEAM_ID}"),
    },
    "LAMBDA_SSH_KEY": {
        "used_for": "SSH into the Lambda Labs A100 box; injected as GPU_SSH_KEY_B64 + GPU_HOST + GPU_USER",
        "hint": ("echo $GPU_SSH_KEY_B64 | base64 -d > /tmp/gpu_key && chmod 600 /tmp/gpu_key && "
                 "ssh -i /tmp/gpu_key -o StrictHostKeyChecking=accept-new $GPU_USER@$GPU_HOST nvidia-smi"),
    },
    "GITHUB_TOKEN": {
        "used_for": "push code / open pull requests on GitHub",
        "hint": "git push https://x-access-token:$GITHUB_TOKEN@github.com/<owner>/<repo>.git; gh uses $GITHUB_TOKEN",
    },
    "OPENAI_API_KEY": {
        "used_for": "OpenAI API (only if the goal itself needs it)",
        "hint": "OpenAI SDK reads $OPENAI_API_KEY",
    },
    "ANTHROPIC_API_KEY": {
        "used_for": "Anthropic API (only if the goal itself needs it)",
        "hint": "Anthropic SDK reads $ANTHROPIC_API_KEY",
    },
}
ALWAYS = ("CRUSOE_API_KEY", "NTFY_TOPIC")


def catalog_prompt() -> str:
    """Catalog rendered for the compiler's system prompt."""
    return "\n".join(f"  * {name}: {meta['used_for']}" for name, meta in CATALOG.items())


def mask(value: str, *, is_key: bool = False) -> str:
    """'vcl_…9f3a' (first 4 + last 4) for tokens; '<n> bytes' for key material; never the full value."""
    if is_key:
        return f"{len(value.encode())} bytes"
    if len(value) >= 12:
        return f"{value[:4]}…{value[-4:]}"
    return f"{value[:2]}…({len(value)} chars)" if value else ""


class SecretsBrokerAdapter:
    name = "secrets_broker"

    def __init__(self, vault: Mapping[str, str] | None = None, root: Path | None = None,
                 request_keys: set[str] | None = None):
        self.vault = vault if vault is not None else os.environ
        self.root = root or ROOT
        # names whose value came from the /run request's vault (browser credential manager) rather than env
        self.request_keys = set(request_keys or ())

    def wanted(self, env: Environment) -> bool:
        return bool(env.secrets)

    # ------------------------------------------------------------ helpers ------

    def _env_source(self, name: str | None = None) -> str:
        if name and name in self.request_keys:
            return "request"
        return "vercel-env" if self.vault.get("VERCEL") == "1" else "local-env"

    def _team_id(self) -> tuple[str, str] | None:
        tid = self.vault.get("VERCEL_TEAM_ID")
        if tid:
            return tid, self._env_source("VERCEL_TEAM_ID")
        pj = self.root / ".vercel" / "project.json"
        try:
            tid = json.loads(pj.read_text()).get("orgId") or ""
        except Exception:
            tid = ""
        return (tid, "file") if tid else None

    def _lambda_key(self) -> tuple[bytes, str] | None:
        raw = self.vault.get("LAMBDA_SSH_KEY")  # the key itself, pasted into the credential manager
        if raw and "PRIVATE KEY" in raw:
            data = raw.replace("\r\n", "\n").encode()
            return (data if data.endswith(b"\n") else data + b"\n"), self._env_source("LAMBDA_SSH_KEY")
        path = self.vault.get("LAMBDA_SSH_KEY_PATH")
        if path:
            p = Path(path).expanduser()
            if p.is_file():
                return p.read_bytes(), "file"
        b64 = self.vault.get("LAMBDA_SSH_KEY_B64")
        if b64:
            try:
                data = base64.b64decode(b64)
            except Exception:
                return None
            return (data if data.endswith(b"\n") else data + b"\n"), self._env_source("LAMBDA_SSH_KEY_B64")
        return None

    # ------------------------------------------------------------ resolve ------

    def resolve(self, names: list[str], creds: dict[str, str]) -> list[dict]:
        """Mutates creds with resolved values; returns the masked ledger."""
        ledger: list[dict] = []

        def row(name: str, value: str | None, source: str, *, is_key: bool = False, meta_name: str | None = None):
            meta = CATALOG.get(meta_name or name, {"used_for": "not in catalog", "hint": ""})
            ledger.append({"name": name, "status": "injected" if value else "missing",
                           "source": source if value else "none",
                           "masked": mask(value, is_key=is_key) if value else "",
                           "used_for": meta["used_for"], "hint": meta["hint"]})

        seen: set[str] = set()
        for name in [*ALWAYS, *names]:
            if name in seen:
                continue
            seen.add(name)
            if name == "LAMBDA_SSH_KEY":
                key = self._lambda_key()
                if key:
                    data, source = key
                    creds["GPU_SSH_KEY_B64"] = base64.b64encode(data).decode()
                    row(name, data.decode(errors="replace"), source, is_key=True)
                else:
                    row(name, None, "none")
                host = self.vault.get("LAMBDA_HOST") or creds.get("GPU_HOST")
                if host:
                    creds["GPU_HOST"] = host
                creds["GPU_USER"] = self.vault.get("LAMBDA_USER") or creds.get("GPU_USER") or "ubuntu"
                if host:
                    row("GPU_HOST", host, self._env_source("LAMBDA_HOST"), meta_name=name)
                elif self.vault.get("LAMBDA_API_KEY"):
                    # No host yet: the lambda_gpu adapter boots/resolves the box via the Lambda API next.
                    ledger.append({"name": "GPU_HOST", "status": "pending", "source": "lambda-api",
                                   "masked": "", "used_for": "resolved by the GPU adapter (boot via Lambda API)",
                                   "hint": "optional: filled in once the instance is active"})
                else:
                    row("GPU_HOST", None, "none", meta_name=name)
                continue
            preset = creds.get(name)  # e.g. ntfy_topic passed in the /run request: an explicit override wins
            value = preset or self.vault.get(name)
            if value:
                creds[name] = value
            source = "request" if preset and preset != self.vault.get(name) else self._env_source(name)
            row(name, value, source)
            if name == "VERCEL_TOKEN":
                tid = self._team_id()
                if tid:
                    creds["VERCEL_TEAM_ID"] = tid[0]
                    ledger.append({"name": "VERCEL_TEAM_ID", "status": "injected", "source": tid[1],
                                   "masked": mask(tid[0]), "used_for": "Vercel team scope for deploys",
                                   "hint": "--scope $VERCEL_TEAM_ID"})
        return ledger

    async def provision(self, spec: Spec, job_id: str, creds: dict[str, str]) -> Provisioned:
        try:
            ledger = self.resolve(list(spec.environment.secrets), creds)
        except Exception as e:  # report, never crash /run (and never echo values)
            return Provisioned(adapter=self.name, real=True, status="failed",
                               detail=f"broker error: {type(e).__name__}", connect_info={"ledger": []})
        creds["SECRETS_LEDGER"] = json.dumps(ledger)
        missing = [r["name"] for r in ledger if r["status"] == "missing"]
        injected = [r["name"] for r in ledger if r["status"] == "injected"]
        detail = (f"resolved {len(injected)}/{len(ledger)} secret(s) from the vault "
                  f"({self._env_source()}) and injected them into the sandbox env")
        if missing:
            detail += f"; MISSING: {', '.join(missing)}"
        return Provisioned(adapter=self.name, real=True, status="failed" if missing else "ready",
                           detail=detail,
                           connect_info={"ledger": ledger, "devices": list(spec.environment.devices),
                                         "injected": injected, "missing": missing},
                           live_url=None)
