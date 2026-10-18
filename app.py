"""FastAPI entrypoint (Vercel auto-detects top-level `app`). Fire from a phone, close it, get pinged by ntfy."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
for _f in (".env", ".env.vercel", ".env.lambda"):
    if (ROOT / _f).exists():
        load_dotenv(ROOT / _f)

from framework import provisioner  # noqa: E402
from framework.adapters import vercel_sandbox as vs  # noqa: E402
from framework.adapters.base import Provisioned  # noqa: E402
from framework.compiler import compile_goal  # noqa: E402
from framework.spec import Spec  # noqa: E402

app = FastAPI(title="donedone", description="Goal -> verifiable evals -> self-provisioned, self-verifying agent loop")

PHASES = {"bootstrap": "running", "building": "running", "verifying": "running", "replanning": "running",
          "done": "done", "escalated": "escalated", "failed": "failed"}


class PlanReq(BaseModel):
    goal: str
    reference_image_b64: str | None = None


class RunReq(BaseModel):
    spec: Spec
    ntfy_topic: str | None = None


class RunResp(BaseModel):
    job_id: str
    sandbox_name: str
    live_url: str | None
    environment_plan: list[Provisioned]


@app.get("/")
def index():
    return {
        "name": "donedone",
        "about": "Turns a one-line goal into machine-verifiable checks, provisions a Vercel Sandbox with headless "
                 "Chromium, and runs a Nemotron agent loop until every check passes (or pings you via ntfy).",
        "endpoints": {
            "POST /plan": "{goal, reference_image_b64?} -> Spec (checks + environment)",
            "POST /run": "{spec, ntfy_topic?} -> {job_id, sandbox_name, live_url, environment_plan}",
            "GET /jobs/{job_id}": "-> {job_id, status, live_url, status_json}",
            "POST /jobs/{job_id}/stop": "stop the job's sandbox",
        },
    }


@app.post("/plan", response_model=Spec)
async def plan(req: PlanReq):
    images = []
    if req.reference_image_b64:
        b64 = req.reference_image_b64.split(",", 1)[-1]  # tolerate data: URLs
        try:
            images.append(base64.b64decode(b64))
        except Exception:
            raise HTTPException(400, "reference_image_b64 is not valid base64")
    try:
        return await run_in_threadpool(compile_goal, req.goal, images or None)
    except Exception as e:
        raise HTTPException(502, f"spec compile failed: {e}")


@app.post("/run", response_model=RunResp)
async def run(req: RunReq):
    job_id = secrets.token_hex(4)
    creds = {"CRUSOE_API_KEY": os.environ.get("CRUSOE_API_KEY", ""),
             "NTFY_TOPIC": req.ntfy_topic or os.environ.get("NTFY_TOPIC", "")}
    results = await provisioner.provision(req.spec, job_id, creds)
    live = next((p.live_url for p in results if p.live_url), None)
    return RunResp(job_id=job_id, sandbox_name=vs.sandbox_name(job_id), live_url=live, environment_plan=results)


@app.get("/jobs/{job_id}")
async def job(job_id: str):
    box = await vs._get(job_id)
    if box is None:
        return {"job_id": job_id, "status": "unknown", "live_url": None, "status_json": None}
    status_json = await vs.read_status(job_id)
    if status_json is None:
        status = "unknown" if vs._is_stopped(box) else "provisioning"
    else:
        status = PHASES.get(str(status_json.get("phase")), "running")
    return {"job_id": job_id, "status": status, "live_url": vs.public_url(box), "status_json": status_json}


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    return {"job_id": job_id, "stopped": await vs.stop(job_id)}
