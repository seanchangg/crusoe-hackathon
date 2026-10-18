"""Adapter selection (offline) + a LIVE=1 end-to-end Vercel Sandbox test (creates and always stops a sandbox)."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import provisioner
from framework.adapters import vercel_sandbox as vs
from framework.spec import Environment, Spec

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.vercel")
FIXTURE = Spec.model_validate_json((ROOT / "tests" / "fixture_spec.json").read_text())


def names(env: Environment) -> list[str]:
    return [a.name for a in provisioner.select_adapters(env)]


def test_browser_only():
    assert names(Environment(needs_browser=True)) == ["vercel_sandbox", "remote_desktop"]


def test_sandbox_is_always_compute_host():
    assert names(Environment(needs_browser=False)) == ["vercel_sandbox"]


def test_macos_and_desktop():
    assert names(Environment(needs_macos=True, needs_desktop=True)) == ["vercel_sandbox", "remote_desktop", "macos"]


def test_mocks_are_simulated():
    spec = FIXTURE.model_copy(update={"environment": Environment(needs_macos=True, needs_desktop=True)})
    adapters = [a for a in provisioner.select_adapters(spec.environment) if a.name == "macos"]
    out = asyncio.run(provisioner.provision(spec, "deadbeef", {}, adapters=adapters))
    assert [p.status for p in out] == ["simulated"]
    assert all(not p.real and p.detail.startswith("would ") for p in out)


def test_fixture_plan():
    plan = provisioner.describe_plan(FIXTURE)
    assert [p["adapter"] for p in plan] == ["vercel_sandbox", "remote_desktop"]
    assert all(p["real"] for p in plan)


PLACEHOLDER = """#!/usr/bin/env bash
set -e
mkdir -p out
python3 - <<'PY'
import json, os
json.dump({"phase": "done", "job_id": os.environ.get("JOB_ID"), "live_url": os.environ.get("LIVE_URL"),
           "has_key": bool(os.environ.get("CRUSOE_API_KEY")), "max_rounds": os.environ.get("MAX_ROUNDS")},
          open("out/status.json", "w"))
PY
exec python3 -m http.server 8080 --directory out
"""


@pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to create a real Vercel Sandbox")
def test_live_sandbox(tmp_path):
    import httpx

    (tmp_path / "bootstrap.sh").write_text(PLACEHOLDER)
    job_id = secrets.token_hex(4)
    adapter = vs.VercelSandboxAdapter(runner_dir=tmp_path, minutes=5)
    creds = {"CRUSOE_API_KEY": os.environ.get("CRUSOE_API_KEY", ""), "NTFY_TOPIC": os.environ.get("NTFY_TOPIC", "")}

    async def run():
        t0 = time.monotonic()
        p = await adapter.provision(FIXTURE, job_id, creds)
        latency = time.monotonic() - t0
        print(f"\nprovision latency: {latency:.1f}s  status={p.status}  live_url={p.live_url}\n{p.detail}")
        try:
            assert p.status == "ready", p.detail
            assert p.live_url and p.live_url.startswith("https://")
            assert latency < 60
            body = None
            async with httpx.AsyncClient(timeout=10) as c:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        r = await c.get(f"{p.live_url}/status.json")
                        if r.status_code == 200:
                            body = r.json()
                            break
                    except (httpx.HTTPError, json.JSONDecodeError):
                        pass
                    await asyncio.sleep(2)
            print(f"served after {time.monotonic() - t0:.1f}s: {body}")
            assert body and body["phase"] == "done" and body["job_id"] == job_id and body["has_key"]
            assert body["live_url"] == p.live_url
            st = await vs.read_status(job_id)
            assert st and st["phase"] == "done"
        finally:
            assert await vs.stop(job_id)

    asyncio.run(run())
