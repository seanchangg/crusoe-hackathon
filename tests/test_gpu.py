"""Offline tests for the GPU-kernel demo path: spec, compiler fixture, adapter order, grading, harness."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runner"))

import gpu  # noqa: E402  (runner/gpu.py)
import gpu_harness as H  # noqa: E402  (runner/gpu_harness.py)

from framework import provisioner  # noqa: E402
from framework.adapters.lambda_gpu import LambdaGPUAdapter  # noqa: E402
from framework.spec import Check, Environment, Spec  # noqa: E402

FIXTURE = ROOT / "tests" / "fixture_spec_gpu.json"
HAS_TORCH = importlib.util.find_spec("torch") is not None


@pytest.fixture(scope="module")
def spec() -> Spec:
    return Spec.model_validate_json(FIXTURE.read_text())


# ------------------------------------------------------------ spec ------

def test_gpu_check_validates():
    c = Check(id="G1", title="t", kind="gpu", steps=[], criteria="c", op="attention",
              shapes={"batch": 1, "heads": 2, "seq": 1000, "dim": 64, "causal": True},
              dtype="fp16", metric="max_abs_err", expected="< 2e-2")
    assert c.kind == "gpu" and c.shapes["causal"] is True


def test_expected_coerced_to_str():
    c = Check(id="G1", title="t", kind="gpu", steps=[], criteria="c", metric="compiles", expected=True)
    assert c.expected == "true"
    assert Check(id="G2", title="t", kind="gpu", steps=[], criteria="c", expected=1.0).expected == "1.0"


def test_bad_metric_rejected():
    with pytest.raises(Exception):
        Check(id="G1", title="t", kind="gpu", steps=[], criteria="c", metric="vibes")


def test_old_checks_still_valid():
    c = Check(id="C1", title="t", kind="dom", steps=["goto /"], criteria="c", selector="body", property="color")
    assert c.op is None and c.metric is None


def test_fixture_gpu(spec):
    gpu_checks = [c for c in spec.checks if c.kind == "gpu"]
    assert len(gpu_checks) >= 6
    assert spec.environment.needs_gpu and not spec.environment.needs_browser
    metrics = {c.metric for c in gpu_checks}
    assert {"compiles", "max_abs_err", "speedup_vs_torch", "tflops"} <= metrics
    assert any(c.shapes and c.shapes.get("causal") for c in gpu_checks)
    assert any(c.shapes and c.shapes.get("seq", 1024) & (c.shapes.get("seq", 1024) - 1) for c in gpu_checks)
    assert any("def forward(q, k, v" in f for f in spec.design_facts)


# ------------------------------------------------------------ adapter selection ------

def names(env: Environment) -> list[str]:
    return [a.name for a in provisioner.select_adapters(env)]


def test_gpu_adapter_first():
    assert names(Environment(needs_gpu=True, needs_browser=False)) == ["lambda_gpu", "vercel_sandbox"]
    assert names(Environment(needs_gpu=True, needs_browser=True, needs_macos=True)) == \
        ["lambda_gpu", "vercel_sandbox", "remote_desktop", "macos"]
    assert names(Environment(needs_browser=True)) == ["vercel_sandbox", "remote_desktop"]


def test_fixture_plan(spec):
    plan = provisioner.describe_plan(spec)
    assert [p["adapter"] for p in plan] == ["lambda_gpu", "vercel_sandbox"]
    assert all(p["real"] for p in plan)


def test_lambda_adapter_fails_cleanly_without_config():
    a = LambdaGPUAdapter(env={})
    creds: dict = {}
    p = asyncio.run(a.provision(Spec(goal="g", checks=[]), "deadbeef", creds))
    assert p.adapter == "lambda_gpu" and p.status == "failed" and p.real
    assert not any(k.startswith("GPU_") for k in creds)


def test_sandbox_env_passes_gpu_creds():
    from framework.adapters.vercel_sandbox import build_env
    env = build_env({"GPU_HOST": "h", "GPU_SSH_KEY_B64": "k"}, "j", None, None)
    assert env["GPU_HOST"] == "h" and env["GPU_SSH_KEY_B64"] == "k"


# ------------------------------------------------------------ grading ------

@pytest.mark.parametrize("value,expected,metric,ok", [
    (0.001, "< 2e-2", "max_abs_err", True),
    (0.03, "< 2e-2", "max_abs_err", False),
    (1.2, ">= 1.0", "speedup_vs_torch", True),
    (0.99, ">= 1.0", "speedup_vs_torch", False),
    (150.0, ">= 150", "tflops", True),
    (0.4, "<= 0.5", "ms", True),
    (0.4, "0.5", "ms", True),
    (130, "120", "tflops", True),
    (True, "true", "compiles", True),
    (False, "true", "compiles", False),
    (None, "< 1", "ms", False),
    (float("nan"), "< 1", "max_abs_err", False),
    (2.0, "== 2", None, True),
    (1.0, "garbage", None, False),
])
def test_compare(value, expected, metric, ok):
    assert gpu.compare(value, expected, metric) is ok


def test_parse_harness_output():
    out = 'some warning\n{"metric": "ms", "value": 0.3, "ok": true}\n'
    assert gpu.parse_harness_output(out)["value"] == 0.3
    assert gpu.parse_harness_output("no json here") is None


def test_verify_gpu_without_host(monkeypatch, tmp_path):
    monkeypatch.delenv("GPU_HOST", raising=False)
    r = gpu.verify_gpu({"id": "G1", "metric": "compiles", "expected": "true"}, str(tmp_path))
    assert r["passed"] is False and r["score"] == 0.0 and "GPU_HOST" in r["evidence"] and r["screenshot"] is None


def test_verify_gpu_missing_kernel(monkeypatch, tmp_path):
    monkeypatch.setenv("GPU_HOST", "10.0.0.1")
    r = gpu.verify_gpu({"id": "G1", "metric": "compiles", "expected": "true"}, str(tmp_path))
    assert not r["passed"] and "kernel.py" in r["evidence"]


def test_build_command_quotes_shapes(monkeypatch):
    monkeypatch.setenv("GPU_WORKDIR", "/home/ubuntu/donedone/job-x")
    cmd = gpu.build_command({"op": "attention", "shapes": {"batch": 1, "causal": True}, "dtype": "fp16",
                             "metric": "tflops"})
    assert "'{\"batch\":1,\"causal\":true}'" in cmd and "--metric tflops" in cmd and cmd.startswith("cd ")


def test_verify_gpu_grades_harness_json(monkeypatch, tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "kernel.py").write_text("def forward(q,k,v,causal=False): return q\n")
    monkeypatch.setenv("GPU_HOST", "10.0.0.1")
    monkeypatch.setattr(gpu, "ensure_ssh_client", lambda: True)
    monkeypatch.setattr(gpu, "_upload_kernel", lambda k: None)

    class R:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"metric": "speedup_vs_torch", "value": 1.31, "ok": True, "error": None,
                             "kernel_ms": 1.0, "torch_ms": 1.31, "tflops": 137.4, "max_abs_err": 0.002})
    monkeypatch.setattr(gpu, "ssh", lambda cmd, timeout=60: R)
    r = gpu.verify_gpu({"id": "C05", "metric": "speedup_vs_torch", "expected": ">= 1.0", "op": "attention",
                        "shapes": {}}, str(tmp_path))
    assert r["passed"] and r["score"] == 1.0
    assert "speedup_vs_torch=1.31" in r["evidence"] and "tflops=137" in r["evidence"]
    R.stdout = R.stdout.replace('"ok": true', '"ok": false')
    assert not gpu.verify_gpu({"id": "C05", "metric": "speedup_vs_torch", "expected": ">= 1.0"},
                              str(tmp_path))["passed"]


# ------------------------------------------------------------ harness ------

def test_harness_args():
    a = H.parse_args(["--kernel", "k.py", "--op", "attention", "--shapes", '{"seq": 1000, "causal": true}',
                      "--metric", "tflops"])
    assert a.shapes == {"seq": 1000, "causal": True} and a.dtype == "fp16"
    s = H.norm_shapes("attention", a.shapes)
    assert s["batch"] == 8 and s["seq"] == 1000 and s["causal"] is True
    with pytest.raises(SystemExit):
        H.parse_args(["--op", "conv"])


def test_harness_flops():
    s = H.norm_shapes("attention", {})
    full = H.flops("attention", s)
    assert full == 4 * 8 * 16 * 2048 * 2048 * 64
    assert H.flops("attention", {**s, "causal": True}) == full / 2
    assert H.flops("matmul", {"m": 2, "n": 3, "k": 4}) == 48
    assert abs(H.tflops_from("attention", s, 1.0) - full * 1e-9) < 1e-6


def test_harness_bad_args_prints_json(capsys):
    assert H.main(["--op", "nope"]) == 2
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["ok"] is False


@pytest.mark.skipif(not HAS_TORCH, reason="torch not installed locally")
@pytest.mark.parametrize("op,shapes", [
    ("attention", {"batch": 1, "heads": 2, "seq": 33, "dim": 16, "causal": True}),
    ("matmul", {"m": 8, "n": 5, "k": 7}),
    ("softmax", {"rows": 4, "cols": 9}),
    ("layernorm", {"rows": 4, "cols": 9}),
])
def test_harness_end_to_end_cpu(tmp_path, capsys, op, shapes):
    """A torch-backed kernel.py through the full harness on CPU (fp32): correct, guard blocks shortcuts."""
    kern = tmp_path / "kernel.py"
    kern.write_text("import gpu_harness as H\n"
                    f"def forward(*a, **k):\n    return H.reference({op!r}, a, {shapes!r}, upcast=False)\n")
    sys.path.insert(0, str(ROOT / "runner"))
    base = ["--kernel", str(kern), "--op", op, "--shapes", json.dumps(shapes), "--dtype", "fp32"]
    H.main(base + ["--metric", "max_abs_err", "--allow-torch"])
    r = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert r["ok"] and r["max_abs_err"] < 1e-4, r
    H.main(base + ["--metric", "ms", "--allow-torch", "--warmup", "1", "--iters", "2"])
    r = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert r["ok"] and r["kernel_ms"] > 0 and r["torch_ms"] > 0
    H.main(base + ["--metric", "max_abs_err"])  # guard on: calling torch's op is forbidden
    r = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert not r["ok"] and "forbidden" in r["error"]
