"""REAL: a Vercel Sandbox microVM (Ubuntu, 4 vCPU / 8 GB) that hosts the runner loop + headless Chromium.

Job state lives in the sandbox itself: sandbox name = f"job-{job_id}", status at out/status.json.
"""
from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path

from vercel import sandbox
from vercel.sandbox import SandboxResources

from ..spec import Environment, Spec
from .base import Provisioned

ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
WORKDIR = "/vercel/sandbox"
PORT = 8080
NOVNC_PORT = 6080
IMAGE = "vercel/sandbox/universal"
STOPPED = {"stopped", "stopping", "failed", "aborted"}


def sandbox_name(job_id: str) -> str:
    return f"job-{job_id}"


def _ensure_vercel_ids() -> None:
    """Outside Vercel, fill VERCEL_PROJECT_ID / VERCEL_TEAM_ID from the linked project if missing."""
    pj = ROOT / ".vercel" / "project.json"
    if pj.exists():
        try:
            data = json.loads(pj.read_text())
            os.environ.setdefault("VERCEL_PROJECT_ID", data.get("projectId", ""))
            os.environ.setdefault("VERCEL_TEAM_ID", data.get("orgId", ""))
        except Exception:
            pass


def public_url(box, port: int = PORT) -> str | None:
    """Public HTTPS URL for an exposed port (the JS SDK's sandbox.domain(port)); Python SDK exposes box.routes."""
    for r in getattr(box, "routes", ()) or ():
        if r.port == port:
            url = r.url
            return url if url.startswith("http") else f"https://{url}"
    return None


def _runner_files(runner_dir: Path) -> list[tuple[str, bytes, int | None]]:
    out = []
    for p in sorted(runner_dir.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = p.relative_to(runner_dir).as_posix()
            out.append((f"{WORKDIR}/runner/{rel}", p.read_bytes(), 0o755 if p.suffix == ".sh" else None))
    return out


RESERVED = ("JOB_ID", "LIVE_URL", "NOVNC_URL", "MAX_ROUNDS", "MAX_MINUTES")


def build_env(creds: dict[str, str], job_id: str, live_url: str | None, novnc_url: str | None,
              max_rounds: str = "6", max_minutes: str = "30") -> dict[str, str]:
    """The runner's process env: EVERY key in creds (the secrets broker's output, incl. GPU_*, VERCEL_*,
    SECRETS_LEDGER) plus job metadata. Job metadata wins over a same-named cred. Values are coerced to str."""
    env = {str(k): "" if v is None else str(v) for k, v in creds.items()}
    env.setdefault("CRUSOE_API_KEY", "")
    env.setdefault("NTFY_TOPIC", "")
    env.update({"JOB_ID": job_id, "LIVE_URL": live_url or "", "NOVNC_URL": novnc_url or "",
                "MAX_ROUNDS": str(max_rounds), "MAX_MINUTES": str(max_minutes)})
    return env


class VercelSandboxAdapter:
    name = "vercel_sandbox"

    def __init__(self, runner_dir: Path | str | None = None, minutes: int = 40,
                 max_rounds: str = "6", max_minutes: str = "30"):
        self.runner_dir = Path(runner_dir or os.environ.get("RUNNER_DIR") or RUNNER_DIR)
        self.minutes = minutes
        self.max_rounds, self.max_minutes = max_rounds, max_minutes

    def wanted(self, env: Environment) -> bool:
        return True  # always the compute host (browser, runner loop, status server)

    async def provision(self, spec: Spec, job_id: str, creds: dict[str, str]) -> Provisioned:
        _ensure_vercel_ids()
        name = sandbox_name(job_id)
        t0 = time.monotonic()
        try:
            files = _runner_files(self.runner_dir)
            if not any(f[0].endswith("/runner/bootstrap.sh") for f in files):
                raise RuntimeError(f"no bootstrap.sh in {self.runner_dir}")
            box = await sandbox.create_sandbox(
                name=name, image=IMAGE, ports=[PORT, NOVNC_PORT],
                execution_time_limit=timedelta(minutes=self.minutes),
                resources=SandboxResources(vcpus=4, memory=8192), persistent=False,
                env={"JOB_ID": job_id}, tags={"app": "donedone", "job": job_id})
            live_url = public_url(box)
            if live_url is None:
                await box.refresh()
                live_url = public_url(box)
            novnc_url = public_url(box, NOVNC_PORT)
            async with box.fs.batch() as b:
                for path, data, mode in files:
                    b.write_bytes(path, data, mode=mode)
                b.write_text(f"{WORKDIR}/spec.json", spec.model_dump_json(indent=2))
            env = build_env(creds, job_id, live_url, novnc_url, self.max_rounds, self.max_minutes)
            await box.create_process("bash", ["runner/bootstrap.sh"], cwd=WORKDIR, env=env)
            return Provisioned(
                adapter=self.name, real=True, status="ready",
                detail=(f"Vercel Sandbox {name} ({IMAGE}, 4 vCPU/8 GB, {self.minutes} min); uploaded "
                        f"{len(files)} runner file(s) + spec.json; bootstrap started detached "
                        f"in {time.monotonic() - t0:.1f}s"),
                connect_info={"sandbox_name": name, "port": PORT, "workdir": WORKDIR,
                              "novnc_url": novnc_url, "env_keys": sorted(env)},
                live_url=live_url)
        except Exception as e:  # report, don't crash /run
            return Provisioned(adapter=self.name, real=True, status="failed",
                               detail=f"{type(e).__name__}: {e}", connect_info={"sandbox_name": name})


async def _get(job_id: str):
    _ensure_vercel_ids()
    try:
        return await sandbox.get_sandbox(name=sandbox_name(job_id))
    except Exception:
        return None


def _is_stopped(box) -> bool:
    st = getattr(box, "status", None)
    return st is not None and str(getattr(st, "value", st)) in STOPPED


async def read_status(job_id: str) -> dict | None:
    """out/status.json from the job's sandbox, or None if the sandbox/file is missing (never resumes a stopped box)."""
    box = await _get(job_id)
    if box is None or _is_stopped(box):
        return None
    try:
        return json.loads(await box.fs.read_text(f"{WORKDIR}/out/status.json"))
    except Exception:
        return None


async def live_url(job_id: str, port: int = PORT) -> str | None:
    box = await _get(job_id)
    return public_url(box, port) if box is not None else None


async def stop(job_id: str) -> bool:
    box = await _get(job_id)
    if box is None:
        return False
    try:
        await box.stop()
        return True
    except Exception:
        return False
