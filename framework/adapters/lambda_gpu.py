"""REAL: the user's Lambda Labs A100 box as the test hardware for GPU-kernel goals.

The builder agent (in the Vercel Sandbox) writes kernel.py; verification ships it here over SSH and runs OUR fixed
harness (runner/gpu_harness.py), so the agent cannot grade its own homework.

Config (env):
  LAMBDA_HOST                      IP/hostname of the box (optional if the Lambda API can resolve it)
  LAMBDA_USER                      default "ubuntu"
  LAMBDA_SSH_KEY_PATH              private key file, or
  LAMBDA_SSH_KEY_B64               base64 of the private key (Vercel: no files)
  LAMBDA_API_KEY                   optional "boot up": resolve/wait for the instance via the Lambda Cloud API
  LAMBDA_INSTANCE_ID               which instance (else the first active A100 one)
  LAMBDA_ALLOW_LAUNCH=1            permit launching a NEW instance if none exists (costs money; off by default)
  LAMBDA_REGION / LAMBDA_INSTANCE_TYPE / LAMBDA_SSH_KEY_NAME   launch parameters (fallbacks)
  LAMBDA_BOOT_WAIT_S               how long to wait for 'active' (default 90; the /run function has ~120 s)

On success it MUTATES creds with GPU_HOST / GPU_USER / GPU_WORKDIR / GPU_SSH_KEY_B64 so the sandbox adapter
(which runs after this one) passes them into the runner's environment.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from ..spec import Environment, Spec
from .base import Provisioned

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "runner" / "gpu_harness.py"
LAMBDA_API = os.environ.get("LAMBDA_API_URL", "https://cloud.lambdalabs.com/api/v1")
PROBE = ('nvidia-smi --query-gpu=name,memory.total --format=csv,noheader && '
         'python3 -c "import torch,triton;print(torch.__version__,triton.__version__)"')


class LambdaGPUAdapter:
    name = "lambda_gpu"

    def __init__(self, env: dict[str, str] | None = None):
        self.env = env if env is not None else os.environ
        self.log: list[str] = []

    def wanted(self, env: Environment) -> bool:
        return env.needs_gpu

    def _say(self, msg: str) -> None:
        self.log.append(msg)
        print(f"[lambda_gpu] {msg}", flush=True)

    # ------------------------------------------------------------ key + ssh ------

    def _key_material(self, creds: dict[str, str] | None = None) -> tuple[str, str]:
        """(path to 0600 key file, base64 of key). Writes a temp file when only B64 is given.
        Falls back to the secrets broker's GPU_SSH_KEY_B64 in creds."""
        path = self.env.get("LAMBDA_SSH_KEY_PATH")
        b64 = self.env.get("LAMBDA_SSH_KEY_B64") or (creds or {}).get("GPU_SSH_KEY_B64")
        if path:
            p = Path(path).expanduser()
            data = p.read_bytes()
            return str(p), base64.b64encode(data).decode()
        if b64:
            data = base64.b64decode(b64)
            if not data.endswith(b"\n"):
                data += b"\n"
            fd, tmp = tempfile.mkstemp(prefix="lambda_key_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            self._tmp_key = tmp
            return tmp, base64.b64encode(data).decode()
        raise RuntimeError("no SSH key: set LAMBDA_SSH_KEY_PATH or LAMBDA_SSH_KEY_B64")

    def _opts(self, key: str) -> list[str]:
        kh = os.path.join(tempfile.gettempdir(), "donedone_known_hosts")
        return ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={kh}",
                "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "-i", key]

    def _ssh(self, key: str, target: str, cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", *self._opts(key), target, cmd], capture_output=True, text=True,
                              timeout=timeout)

    def _scp(self, key: str, local: Path, remote: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["scp", "-q", *self._opts(key), str(local), remote], capture_output=True, text=True,
                              timeout=timeout)

    @staticmethod
    def _port_open(host: str, port: int = 22, timeout: float = 5.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------ Lambda Cloud API ------

    def _api(self, method: str, path: str, json_body: dict | None = None) -> dict:
        import httpx
        r = httpx.request(method, f"{LAMBDA_API}{path}", auth=(self.env["LAMBDA_API_KEY"], ""),
                          json=json_body, timeout=20)
        r.raise_for_status()
        return r.json()

    def _find_instance(self) -> dict | None:
        want = self.env.get("LAMBDA_INSTANCE_ID")
        insts = self._api("GET", "/instances").get("data", [])
        self._say(f"Lambda API: {len(insts)} instance(s) on the account")
        insts = [i for i in insts if i.get("status") not in ("terminated", "terminating")]
        if want:
            return next((i for i in insts if i.get("id") == want), None)
        a100 = [i for i in insts if "a100" in str((i.get("instance_type") or {}).get("name", "")).lower()]
        pool = a100 or insts
        return next((i for i in pool if i.get("status") == "active"), pool[0] if pool else None)

    def _boot_up(self) -> str | None:
        """Best-effort: find (or, if allowed, launch) the instance, wait for 'active', return its IP."""
        if not self.env.get("LAMBDA_API_KEY"):
            return None
        wait_s = float(self.env.get("LAMBDA_BOOT_WAIT_S", "90"))
        try:
            inst = self._find_instance()
            if inst is None:
                if self.env.get("LAMBDA_ALLOW_LAUNCH") != "1":
                    self._say("no matching instance and LAMBDA_ALLOW_LAUNCH!=1: not launching (would cost money)")
                    return None
                body = {"region_name": self.env.get("LAMBDA_REGION", "us-east-1"),
                        "instance_type_name": self.env.get("LAMBDA_INSTANCE_TYPE", "gpu_1x_a100_sxm4"),
                        "ssh_key_names": [k for k in [self.env.get("LAMBDA_SSH_KEY_NAME")] if k],
                        "quantity": 1, "name": "donedone-a100"}
                self._say(f"launching {body['instance_type_name']} in {body['region_name']}")
                ids = self._api("POST", "/instance-operations/launch", body).get("data", {}).get("instance_ids", [])
                if not ids:
                    self._say("launch returned no instance id")
                    return None
                inst = {"id": ids[0], "status": "booting"}
            iid = inst["id"]
            self._say(f"instance {iid} status={inst.get('status')} ip={inst.get('ip')}")
            deadline = time.monotonic() + wait_s
            while inst.get("status") != "active" or not inst.get("ip"):
                if inst.get("status") in ("terminated", "terminating"):
                    self._say(f"instance {iid} is {inst['status']}; Lambda has no restart, launch a new one")
                    return None
                if time.monotonic() > deadline:
                    self._say(f"instance {iid} not active after {wait_s:.0f}s (status={inst.get('status')})")
                    return None
                time.sleep(5)
                inst = self._api("GET", f"/instances/{iid}").get("data", inst)
            self._say(f"instance {iid} active at {inst['ip']}")
            return inst["ip"]
        except Exception as e:  # noqa: BLE001 - best-effort
            self._say(f"Lambda API boot-up failed: {type(e).__name__}: {e}")
            return None

    # ------------------------------------------------------------ provision ------

    def _provision_sync(self, job_id: str, creds: dict[str, str], needs_torch: bool = True) -> Provisioned:
        t0 = time.monotonic()
        user = self.env.get("LAMBDA_USER") or creds.get("GPU_USER") or "ubuntu"
        host = self.env.get("LAMBDA_HOST") or creds.get("GPU_HOST")
        key, key_b64 = self._key_material(creds)
        if not host or not self._port_open(host):
            self._say(f"host {'unset' if not host else 'unreachable on :22'}; trying Lambda Cloud API boot-up")
            host = self._boot_up() or host
            if not host:
                raise RuntimeError("no GPU host: set LAMBDA_HOST (or LAMBDA_API_KEY to resolve it)")
            for _ in range(12):  # freshly booted boxes take a moment to start sshd
                if self._port_open(host):
                    break
                time.sleep(5)
        target = f"{user}@{host}"
        r = self._ssh(key, target, PROBE, timeout=60)
        lines = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
        if r.returncode != 0 and not lines:
            raise RuntimeError(f"ssh {target} failed (exit {r.returncode}): {r.stderr.strip()[:300]}")
        gpu = lines[0] if lines else "unknown GPU"
        if "nvidia-smi" in r.stderr or "command not found" in gpu:
            raise RuntimeError(f"nvidia-smi failed on {host}: {r.stderr.strip()[:200]}")
        versions = lines[1] if r.returncode == 0 and len(lines) > 1 else None
        if versions is None and needs_torch:
            self._say("torch/triton missing on the box; pip install --user torch triton (best-effort, <=120 s)")
            try:
                self._ssh(key, target, "python3 -m pip install --user -q torch triton", timeout=120)
            except subprocess.TimeoutExpired:
                self._say("pip install still running after 120 s; continuing")
            r2 = self._ssh(key, target, PROBE, timeout=60)
            l2 = [ln.strip() for ln in r2.stdout.strip().splitlines() if ln.strip()]
            versions = l2[1] if r2.returncode == 0 and len(l2) > 1 else None
        tv = versions.split() if versions else []
        torch_v, triton_v = (tv + ["missing", "missing"])[:2]

        rel = f"donedone/job-{job_id}"
        r = self._ssh(key, target, f"mkdir -p ~/{rel} && cd ~/{rel} && pwd", timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"mkdir ~/{rel} failed: {r.stderr.strip()[:200]}")
        workdir = r.stdout.strip().splitlines()[-1]
        r = self._scp(key, HARNESS, f"{target}:{shlex.quote(workdir)}/gpu_harness.py")
        if r.returncode != 0:
            raise RuntimeError(f"scp harness failed: {r.stderr.strip()[:200]}")

        creds.update({"GPU_HOST": host, "GPU_USER": user, "GPU_WORKDIR": workdir, "GPU_SSH_KEY_B64": key_b64})
        ready = versions is not None or not needs_torch  # non-kernel goals (e.g. nvidia-smi status) need no torch
        return Provisioned(
            adapter=self.name, real=True, status="ready" if ready else "failed",
            detail=(f"{gpu} at {host}; torch {torch_v} triton {triton_v}; harness uploaded to {workdir} "
                    f"in {time.monotonic() - t0:.1f}s" + ("" if ready else " (torch/triton import failed)")),
            connect_info={"host": host, "user": user, "workdir": workdir, "gpu": gpu,
                          "torch": torch_v, "triton": triton_v, "log": self.log[-6:]},
            live_url=None)

    async def provision(self, spec: Spec, job_id: str, creds: dict[str, str]) -> Provisioned:
        self._tmp_key = None
        try:
            needs_torch = any(c.kind == "gpu" for c in spec.checks)
            return await asyncio.to_thread(self._provision_sync, job_id, creds, needs_torch)
        except Exception as e:  # report, don't crash /run
            return Provisioned(adapter=self.name, real=True, status="failed", detail=f"{type(e).__name__}: {e}",
                               connect_info={"log": self.log[-6:]})
        finally:
            if self._tmp_key:
                try:
                    os.unlink(self._tmp_key)
                except OSError:
                    pass
