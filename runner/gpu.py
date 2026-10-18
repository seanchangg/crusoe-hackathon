"""Independent GPU verification from inside the sandbox: ship site/kernel.py to the GPU box, run OUR fixed
harness (gpu_harness.py) there over SSH, and grade its JSON against the check's expected value.

The builder only ever writes kernel.py; it never sees or runs the grader. Stdlib only.

Env (set by the lambda_gpu adapter through the sandbox env):
    GPU_HOST, GPU_USER (default ubuntu), GPU_WORKDIR (absolute or ~/...), GPU_SSH_KEY_B64 (or GPU_SSH_KEY_PATH)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "gpu_harness.py"
KEY_FILE = Path(os.environ.get("GPU_KEY_FILE", "/tmp/gpu_key"))
KNOWN_HOSTS = "/tmp/gpu_known_hosts"
HARNESS_TIMEOUT_S = int(os.environ.get("GPU_HARNESS_TIMEOUT", "420"))
_state: dict = {"harness_uploaded": False, "kernel_sha": None}

# ------------------------------------------------------------ ssh plumbing ------


def gpu_available() -> bool:
    return bool(os.environ.get("GPU_HOST"))


def _key_path() -> str | None:
    if os.environ.get("GPU_SSH_KEY_PATH"):
        return os.environ["GPU_SSH_KEY_PATH"]
    b64 = os.environ.get("GPU_SSH_KEY_B64")
    if not b64:
        return None
    if not KEY_FILE.exists():
        data = base64.b64decode(b64)
        if not data.endswith(b"\n"):
            data += b"\n"
        fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(KEY_FILE, 0o600)
    return str(KEY_FILE)


def _opts() -> list[str]:
    o = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
         "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR"]
    k = _key_path()
    if k:
        o += ["-i", k]
    return o


def _target() -> str:
    return f"{os.environ.get('GPU_USER') or 'ubuntu'}@{os.environ['GPU_HOST']}"


def _workdir() -> str:
    """Remote workdir as a path usable both in ssh commands and scp targets (~/x -> x, relative to $HOME)."""
    w = os.environ.get("GPU_WORKDIR") or "donedone/job-" + os.environ.get("JOB_ID", "local")
    return w[2:] if w.startswith("~/") else w


def ensure_ssh_client() -> bool:
    """Best-effort install of an ssh client if the image lacks one."""
    if shutil.which("ssh") and shutil.which("scp"):
        return True
    for cmd in (["sudo", "apt-get", "install", "-y", "-q", "openssh-client"],
                ["sudo", "dnf", "install", "-y", "-q", "openssh-clients"]):
        if shutil.which(cmd[1]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=120)
            except Exception:  # noqa: BLE001
                pass
    return bool(shutil.which("ssh") and shutil.which("scp"))


def ssh(cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", *_opts(), _target(), cmd], capture_output=True, text=True, timeout=timeout)


def scp(local: str | Path, remote_rel: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["scp", "-q", *_opts(), str(local), f"{_target()}:{remote_rel}"],
                          capture_output=True, text=True, timeout=timeout)


def _upload_kernel(kernel: Path) -> None:
    wd = _workdir()
    if not _state["harness_uploaded"]:
        r = ssh(f"mkdir -p {shlex.quote(wd)}", timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"ssh mkdir failed: {r.stderr.strip()[:200]}")
        r = scp(HARNESS, f"{wd}/gpu_harness.py")
        if r.returncode != 0:
            raise RuntimeError(f"scp harness failed: {r.stderr.strip()[:200]}")
        _state["harness_uploaded"] = True
    sha = hashlib.sha256(kernel.read_bytes()).hexdigest()
    if sha != _state["kernel_sha"]:
        r = scp(kernel, f"{wd}/kernel.py")
        if r.returncode != 0:
            raise RuntimeError(f"scp kernel.py failed: {r.stderr.strip()[:200]}")
        _state["kernel_sha"] = sha


# ------------------------------------------------------------ grading ------

_CMP = re.compile(r"^\s*(<=|>=|==|<|>|=)?\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$")
_LOWER_IS_BETTER = {"max_abs_err", "ms"}


def compare(value, expected: str | None, metric: str | None = None) -> bool:
    """value vs expected like '< 2e-2', '>= 1.0', '>= 150', 'true'. Bare numbers: <= for err/ms, >= otherwise."""
    if expected is None or value is None:
        return False
    e = str(expected).strip().lower()
    if e in ("true", "false", "yes", "no"):
        want = e in ("true", "yes")
        if isinstance(value, str):
            return (value.strip().lower() in ("true", "yes", "1")) == want
        return bool(value) == want
    m = _CMP.match(e)
    if not m:
        return False
    op, num = m.group(1), float(m.group(2))
    if op is None:
        op = "<=" if metric in _LOWER_IS_BETTER else ">="
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if v != v:  # NaN
        return False
    return {"<": v < num, "<=": v <= num, ">": v > num, ">=": v >= num,
            "==": abs(v - num) <= 1e-9 * max(1.0, abs(num)), "=": abs(v - num) <= 1e-9 * max(1.0, abs(num))}[op]


def parse_harness_output(stdout: str) -> dict | None:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _fmt(x, nd=3) -> str:
    if x is None:
        return "-"
    if isinstance(x, bool):
        return str(x).lower()
    if isinstance(x, float):
        return f"{x:.{nd}g}"
    return str(x)


def evidence_line(check: dict, h: dict) -> str:
    metric = check.get("metric")
    parts = [f"{metric}={_fmt(h.get('value'), 4)} (expected {check.get('expected')})",
             f"kernel_ms={_fmt(h.get('kernel_ms'))}", f"torch_ms={_fmt(h.get('torch_ms'))}",
             f"tflops={_fmt(h.get('tflops'))}", f"max_abs_err={_fmt(h.get('max_abs_err'))}"]
    if h.get("device"):
        parts.append(f"on {h['device']}")
    if h.get("error"):
        parts.append(f"error: {str(h['error'])[:240]}")
    return " ".join(parts)


def _result(check: dict, passed: bool, evidence: str, harness: dict | None = None) -> dict:
    return {"id": check.get("id"), "passed": bool(passed), "score": 1.0 if passed else 0.0,
            "evidence": evidence.replace("\n", " ")[:600], "screenshot": None, "harness": harness}


def build_command(check: dict) -> str:
    shapes = json.dumps(check.get("shapes") or {}, separators=(",", ":"))
    args = ["python3", "gpu_harness.py", "--kernel", "kernel.py", "--op", check.get("op") or "attention",
            "--shapes", shapes, "--dtype", check.get("dtype") or "fp16", "--metric", check.get("metric") or "compiles"]
    return f"cd {shlex.quote(_workdir())} && timeout {HARNESS_TIMEOUT_S} " + " ".join(shlex.quote(a) for a in args)


def verify_gpu(check: dict, job_dir: str | Path = ".") -> dict:
    """Run one kind='gpu' check on the remote GPU with the fixed harness. Never raises."""
    if not gpu_available():
        return _result(check, False, "no GPU attached (GPU_HOST unset): lambda_gpu adapter did not provision")
    kernel = Path(job_dir) / "site" / "kernel.py"
    if not kernel.exists():
        return _result(check, False, f"kernel.py not found at {kernel}: builder must write site/kernel.py")
    if not ensure_ssh_client():
        return _result(check, False, "ssh/scp client not available in the sandbox")
    try:
        _upload_kernel(kernel)
        r = ssh(build_command(check), timeout=HARNESS_TIMEOUT_S + 30)
    except subprocess.TimeoutExpired:
        return _result(check, False, f"harness timed out after {HARNESS_TIMEOUT_S}s (kernel hang?)")
    except Exception as e:  # noqa: BLE001
        return _result(check, False, f"GPU transport error: {type(e).__name__}: {e}")
    h = parse_harness_output(r.stdout)
    if h is None:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return _result(check, False, f"harness produced no JSON (exit {r.returncode}): {' | '.join(tail)[:300]}")
    passed = bool(h.get("ok")) and compare(h.get("value"), check.get("expected"), check.get("metric"))
    return _result(check, passed, evidence_line(check, h), h)


def gpu_probe() -> str:
    """One-line description of the attached GPU for the builder's context."""
    if not gpu_available():
        return "no GPU attached"
    try:
        r = ssh("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | head -1; "
                "python3 -c 'import torch,triton;print(\"torch\",torch.__version__,\"triton\",triton.__version__,"
                "\"cuda\",torch.version.cuda)' 2>&1 | tail -1", timeout=40)
        out = " | ".join(line.strip() for line in r.stdout.strip().splitlines() if line.strip())
        return out or f"GPU probe failed: {r.stderr.strip()[:200]}"
    except Exception as e:  # noqa: BLE001
        return f"GPU probe failed: {type(e).__name__}: {e}"


if __name__ == "__main__":
    import sys
    print(gpu_probe())
    if len(sys.argv) > 1:  # python3 gpu.py '<check json>' [job_dir]
        print(json.dumps(verify_gpu(json.loads(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "."), indent=2))
