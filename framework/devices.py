"""Device & credential catalog for the web credential manager, plus live probes for the "Test" button.

The server stores nothing: the browser keeps the vault and sends it with each /run. Each device type maps
its fields onto the env names the adapters already understand, so a configured device is just a bundle of
secrets with a probe attached. Probes never echo secret values.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time

import httpx

from .adapters.secrets_broker import CATALOG

EXTRA_SECRETS: dict[str, dict[str, str]] = {
    "VERCEL_TEAM_ID": {"used_for": "Vercel team scope for deploys", "hint": "team_xxx from the Vercel dashboard", "kind": "text"},
    "LAMBDA_HOST": {"used_for": "IP of an already-running GPU box", "hint": "optional when LAMBDA_API_KEY can resolve it", "kind": "text"},
    "LAMBDA_USER": {"used_for": "SSH user on the GPU box", "hint": "ubuntu on Lambda", "kind": "text"},
    "LAMBDA_API_KEY": {"used_for": "Lambda Cloud API: find or boot the GPU instance", "hint": "cloud.lambdalabs.com → API keys", "kind": "token"},
}
KEY_SECRETS = {"LAMBDA_SSH_KEY"}


def secret_catalog() -> list[dict]:
    out = []
    for name, meta in CATALOG.items():
        out.append({"name": name, "used_for": meta["used_for"], "hint": meta["hint"],
                    "kind": "key" if name in KEY_SECRETS else "token"})
    for name, meta in EXTRA_SECRETS.items():
        out.append({"name": name, **meta})
    return out


DEVICE_TYPES: list[dict] = [
    {"type": "sandbox-desktop", "title": "Linux microVM + desktop", "real": True,
     "about": "Vercel Sandbox with Xvfb, headed Chromium and noVNC. Built in; nothing to configure.",
     "fields": [], "provides": []},
    {"type": "crusoe", "title": "Crusoe inference key", "real": True,
     "about": "Nemotron 3.5 Super VL + Lightning on Crusoe Managed Inference.",
     "fields": [{"name": "api_key", "label": "API key", "kind": "token", "required": True, "env": "CRUSOE_API_KEY"}],
     "provides": ["CRUSOE_API_KEY"]},
    {"type": "ntfy", "title": "ntfy push topic", "real": True,
     "about": "Where the loop pings you when it stops or needs you.",
     "fields": [{"name": "topic", "label": "Topic", "kind": "text", "required": True, "env": "NTFY_TOPIC"}],
     "provides": ["NTFY_TOPIC"]},
    {"type": "gpu-ssh", "title": "GPU box over SSH", "real": True,
     "about": "Any Linux box with nvidia-smi. The runner SSHes in with this key.",
     "fields": [{"name": "host", "label": "Host / IP", "kind": "text", "required": True, "env": "LAMBDA_HOST"},
                {"name": "user", "label": "User", "kind": "text", "required": False, "env": "LAMBDA_USER"},
                {"name": "private_key", "label": "Private key (paste)", "kind": "key", "required": True, "env": "LAMBDA_SSH_KEY"}],
     "provides": ["LAMBDA_HOST", "LAMBDA_USER", "LAMBDA_SSH_KEY"]},
    {"type": "lambda-api", "title": "Lambda Cloud (boot on demand)", "real": True,
     "about": "Finds a running A100 or launches one with your registered SSH key. Launching is billed hourly.",
     "fields": [{"name": "api_key", "label": "API key", "kind": "token", "required": True, "env": "LAMBDA_API_KEY"},
                {"name": "ssh_key_name", "label": "SSH key name (registered on Lambda)", "kind": "text", "required": False, "env": "LAMBDA_SSH_KEY_NAME"},
                {"name": "private_key", "label": "Matching private key (paste)", "kind": "key", "required": False, "env": "LAMBDA_SSH_KEY"},
                {"name": "region", "label": "Region", "kind": "text", "required": False, "env": "LAMBDA_REGION"},
                {"name": "instance_type", "label": "Instance type", "kind": "text", "required": False, "env": "LAMBDA_INSTANCE_TYPE"},
                {"name": "allow_launch", "label": "Allow launching a new instance (billed)", "kind": "bool", "required": False, "env": "LAMBDA_ALLOW_LAUNCH"}],
     "provides": ["LAMBDA_API_KEY", "LAMBDA_SSH_KEY_NAME", "LAMBDA_SSH_KEY", "LAMBDA_REGION", "LAMBDA_INSTANCE_TYPE", "LAMBDA_ALLOW_LAUNCH"]},
    {"type": "vercel-deploy", "title": "Vercel deploy token", "real": True,
     "about": "Lets the agent deploy what it builds.",
     "fields": [{"name": "token", "label": "Token", "kind": "token", "required": True, "env": "VERCEL_TOKEN"},
                {"name": "team_id", "label": "Team id", "kind": "text", "required": False, "env": "VERCEL_TEAM_ID"}],
     "provides": ["VERCEL_TOKEN", "VERCEL_TEAM_ID"]},
    {"type": "github", "title": "GitHub token", "real": True,
     "about": "Push code or open pull requests.",
     "fields": [{"name": "token", "label": "Token", "kind": "token", "required": True, "env": "GITHUB_TOKEN"}],
     "provides": ["GITHUB_TOKEN"]},
    {"type": "macos", "title": "macOS device", "real": False,
     "about": "Simulated adapter today: returns a provisioning plan, never real hardware.",
     "fields": [{"name": "host", "label": "Host", "kind": "text", "required": False, "env": "MACOS_HOST"},
                {"name": "user", "label": "User", "kind": "text", "required": False, "env": "MACOS_USER"}],
     "provides": ["MACOS_HOST", "MACOS_USER"]},
]


def device_env(dtype: str, config: dict) -> dict[str, str]:
    """Flatten one device's config onto env names."""
    dt = next((d for d in DEVICE_TYPES if d["type"] == dtype), None)
    if not dt:
        return {}
    out = {}
    for f in dt["fields"]:
        v = config.get(f["name"])
        if f["kind"] == "bool":
            v = "1" if v in (True, "1", "true", "on") else ""
        if v:
            out[f["env"]] = str(v)
    return out


def _ssh_probe(host: str, user: str, key_text: str) -> tuple[bool, str]:
    if not key_text or "PRIVATE KEY" not in key_text:
        return False, "no private key pasted"
    fd, path = tempfile.mkstemp(prefix="dd_key_")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(key_text.replace("\r\n", "\n").rstrip("\n") + "\n")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        r = subprocess.run(["ssh", "-i", path, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                            "-o", "ConnectTimeout=10", f"{user or 'ubuntu'}@{host}",
                            "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1"],
                           capture_output=True, text=True, timeout=25)
        if r.returncode == 0 and r.stdout.strip():
            return True, f"ssh ok · {r.stdout.strip()}"
        err = (r.stderr.strip().splitlines() or ["ssh failed"])[-1]
        return False, err[:200]
    except subprocess.TimeoutExpired:
        return False, "ssh timed out"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_device(dtype: str, config: dict) -> dict:
    """Live probe for the credential manager's Test button. Never returns secret values."""
    t0 = time.monotonic()
    ok, detail = False, "unknown device type"
    try:
        if dtype == "sandbox-desktop":
            ok, detail = True, "built in: provisioned per job"
        elif dtype == "macos":
            ok, detail = False, "simulated adapter: no real macOS host is contacted"
        elif dtype == "crusoe":
            r = httpx.get("https://api.inference.crusoecloud.com/v1/models", timeout=10,
                          headers={"Authorization": f"Bearer {config.get('api_key', '')}"})
            if r.status_code == 200:
                ids = [m.get("id", "") for m in r.json().get("data", [])]
                nem = [i for i in ids if "emotron" in i]
                ok, detail = True, f"{len(ids)} models · {len(nem)} Nemotron"
            else:
                detail = f"HTTP {r.status_code}"
        elif dtype == "ntfy":
            topic = (config.get("topic") or "").strip()
            if not topic:
                detail = "topic is empty"
            else:
                r = httpx.post(f"https://ntfy.sh/{topic}", content=b"donedone: credential test",
                               headers={"Title": "donedone"}, timeout=10)
                ok, detail = r.is_success, ("test message sent" if r.is_success else f"HTTP {r.status_code}")
        elif dtype == "gpu-ssh":
            ok, detail = _ssh_probe(config.get("host", ""), config.get("user", "ubuntu"), config.get("private_key", ""))
        elif dtype == "lambda-api":
            r = httpx.get("https://cloud.lambdalabs.com/api/v1/instances", timeout=10, auth=(config.get("api_key", ""), ""))
            if r.status_code == 200:
                insts = r.json().get("data", [])
                live = [i for i in insts if i.get("status") not in ("terminated", "terminating")]
                k = httpx.get("https://cloud.lambdalabs.com/api/v1/ssh-keys", timeout=10, auth=(config.get("api_key", ""), ""))
                names = [x.get("name") for x in k.json().get("data", [])] if k.status_code == 200 else []
                want = config.get("ssh_key_name")
                ok = True
                detail = f"{len(live)} running instance(s) · {len(names)} ssh key(s)"
                if want and want not in names:
                    ok, detail = False, detail + f" · key '{want}' not registered"
            else:
                detail = f"HTTP {r.status_code}"
        elif dtype == "vercel-deploy":
            r = httpx.get("https://api.vercel.com/v2/user", timeout=10,
                          headers={"Authorization": f"Bearer {config.get('token', '')}"})
            if r.status_code == 200:
                u = r.json().get("user", {})
                ok, detail = True, f"token ok · {u.get('username') or u.get('email') or 'user'}"
                tid = config.get("team_id")
                if tid:
                    t = httpx.get(f"https://api.vercel.com/v2/teams/{tid}", timeout=10,
                                  headers={"Authorization": f"Bearer {config.get('token', '')}"})
                    detail += " · team ok" if t.status_code == 200 else f" · team HTTP {t.status_code}"
                    ok = ok and t.status_code == 200
            else:
                detail = f"HTTP {r.status_code}"
        elif dtype == "github":
            r = httpx.get("https://api.github.com/user", timeout=10,
                          headers={"Authorization": f"Bearer {config.get('token', '')}"})
            ok, detail = r.status_code == 200, (f"token ok · {r.json().get('login')}" if r.status_code == 200 else f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001 - probe must never raise to the client
        ok, detail = False, f"{type(e).__name__}"
    return {"ok": ok, "detail": detail, "latency_ms": int((time.monotonic() - t0) * 1000)}
