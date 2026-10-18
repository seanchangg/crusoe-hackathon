"""environment{} -> ordered adapter list -> provision each (real where cheap, simulated elsewhere)."""
from __future__ import annotations

import json
import sys

from .adapters import (LambdaGPUAdapter, MacOSAdapter, RemoteDesktopAdapter, SecretsBrokerAdapter,
                       VercelSandboxAdapter)
from .adapters.base import Provisioned
from .spec import Environment, Spec


def select_adapters(env: Environment) -> list:
    """Vercel Sandbox is always the compute host (and the browser); mocks join when the env demands them.

    secrets_broker goes FIRST: it resolves every secret in env.secrets from the vault and writes it into creds.
    lambda_gpu next: it resolves the GPU box (reusing the broker's GPU_* if present) and writes GPU_* into creds.
    The sandbox adapter then injects every key in creds into the runner's environment."""
    adapters: list = [SecretsBrokerAdapter()] if env.secrets else []
    if env.needs_gpu:
        adapters.append(LambdaGPUAdapter())
    adapters.append(VercelSandboxAdapter())
    if env.needs_browser or env.needs_desktop:
        adapters.append(RemoteDesktopAdapter())  # real: desktop stack lives inside the sandbox
    if env.needs_macos:
        adapters.append(MacOSAdapter())
    return adapters


async def provision(spec: Spec, job_id: str, creds: dict[str, str], adapters: list | None = None) -> list[Provisioned]:
    adapters = adapters if adapters is not None else select_adapters(spec.environment)
    # Sequential on purpose: the broker fills creds first, lambda_gpu fills GPU_* creds before the sandbox starts the runner, and the desktop
    # adapter reads the sandbox the sandbox adapter just created.
    return [await a.provision(spec, job_id, creds) for a in adapters]


def describe_plan(spec: Spec) -> list[dict]:
    real = {"secrets_broker", "vercel_sandbox", "remote_desktop", "lambda_gpu"}
    secrets = ", ".join(spec.environment.secrets) or "none"
    why = {"secrets_broker": f"credential broker: resolve {secrets} from the vault, inject into the VM, masked ledger","vercel_sandbox": "compute host: Linux microVM, runner loop, status server on :8080",
           "lambda_gpu": "test hardware: your Lambda Labs A100 over SSH; a fixed harness grades kernel.py vs PyTorch",
           "remote_desktop": "Xvfb desktop + headed Chromium + noVNC on :6080 so the agent (and you) can see motion"}
    return [{"adapter": a.name, "real": a.name in real,
             "why": why.get(a.name, f"environment requires {a.name}")}
            for a in select_adapters(spec.environment)]


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "--dry-run":
        sys.exit("usage: python -m framework.provisioner --dry-run <spec.json>")
    spec = Spec.model_validate_json(open(args[1]).read())
    print(json.dumps({"goal": spec.goal, "environment": spec.environment.model_dump(),
                      "manifest": {"devices": spec.environment.devices, "secrets": spec.environment.secrets},
                      "plan": describe_plan(spec)}, indent=2))
