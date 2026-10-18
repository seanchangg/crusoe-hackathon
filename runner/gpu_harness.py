#!/usr/bin/env python3
"""FIXED, independent GPU verification harness. Runs ON the GPU box, never written by the builder agent.

    python3 gpu_harness.py --kernel kernel.py --op attention \
        --shapes '{"batch":8,"heads":16,"seq":2048,"dim":64,"causal":false}' --dtype fp16 --metric max_abs_err

Prints exactly ONE JSON line:
    {"metric", "value", "ok", "error", "torch_ms", "kernel_ms", "tflops", "max_abs_err", "speedup_vs_torch", ...}

The builder cannot grade its own homework: inputs are generated here, the reference is PyTorch, timing uses CUDA
events, and while kernel.py runs the forbidden torch shortcuts (SDPA, matmul, softmax, ...) raise.
Perf metrics only count if the output is also correct ("ok" false otherwise).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import math
import os
import sys
import time
import traceback

OPS = ("attention", "matmul", "softmax", "layernorm")
METRICS = ("max_abs_err", "speedup_vs_torch", "tflops", "ms", "compiles")
TOL = {"fp16": 2e-2, "bf16": 6e-2, "fp32": 1e-3}
WARMUP, ITERS = 20, 100


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--kernel", default="kernel.py")
    p.add_argument("--op", required=True, choices=OPS)
    p.add_argument("--shapes", default="{}", help="JSON dict")
    p.add_argument("--dtype", default="fp16", choices=tuple(TOL))
    p.add_argument("--metric", default="max_abs_err", choices=METRICS)
    p.add_argument("--warmup", type=int, default=WARMUP)
    p.add_argument("--iters", type=int, default=ITERS)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--allow-torch", action="store_true",
                   help="do not block torch shortcuts inside kernel.py (harness self-test only)")
    a = p.parse_args(argv)
    a.shapes = json.loads(a.shapes) if isinstance(a.shapes, str) else dict(a.shapes)
    if not isinstance(a.shapes, dict):
        p.error("--shapes must be a JSON object")
    return a


def norm_shapes(op: str, s: dict) -> dict:
    """Fill defaults so partial shape dicts from the spec still run."""
    if op == "attention":
        d = {"batch": 8, "heads": 16, "seq": 2048, "dim": 64, "causal": False}
    elif op == "matmul":
        d = {"m": 4096, "n": 4096, "k": 4096}
    else:
        d = {"rows": 4096, "cols": 4096}
    d.update(s or {})
    if op == "attention" and "seq_len" in d:
        d["seq"] = d.pop("seq_len")
    if op == "attention" and "head_dim" in d:
        d["dim"] = d.pop("head_dim")
    d["causal"] = bool(d.get("causal", False)) if op == "attention" else d.get("causal", False)
    return d


def torch_dtype(name: str):
    import torch
    return {"fp16": torch.float16, "bf16": torch.bfloat16, "fp32": torch.float32}[name]


def make_inputs(op: str, s: dict, dtype: str, device: str, seed: int = 0) -> tuple:
    import torch
    g = torch.Generator(device="cpu").manual_seed(seed)
    dt = torch_dtype(dtype)

    def rnd(*shape, scale=1.0):
        return (torch.randn(*shape, generator=g) * scale).to(device=device, dtype=dt).contiguous()

    if op == "attention":
        b, h, n, d = int(s["batch"]), int(s["heads"]), int(s["seq"]), int(s["dim"])
        return rnd(b, h, n, d), rnd(b, h, n, d), rnd(b, h, n, d)
    if op == "matmul":
        m, n, k = int(s["m"]), int(s["n"]), int(s["k"])
        return rnd(m, k, scale=k ** -0.5), rnd(k, n)
    r, c = int(s["rows"]), int(s["cols"])
    if op == "softmax":
        return (rnd(r, c),)
    return rnd(r, c), rnd(c, scale=0.1) + 1.0, rnd(c, scale=0.1)  # layernorm: x, weight, bias


def reference(op: str, inputs: tuple, s: dict, upcast: bool = True):
    """PyTorch reference. upcast=True computes in fp32 (accuracy ground truth); False = native-dtype baseline."""
    import torch
    import torch.nn.functional as F
    xs = tuple(t.float() for t in inputs) if upcast else inputs
    if op == "attention":
        q, k, v = xs
        return F.scaled_dot_product_attention(q, k, v, is_causal=bool(s.get("causal")))
    if op == "matmul":
        return torch.matmul(*xs)
    if op == "softmax":
        return torch.softmax(xs[0], dim=-1)
    x, w, b = xs
    return F.layer_norm(x, (x.shape[-1],), w, b, eps=float(s.get("eps", 1e-5)))


def call_kernel(mod, op: str, inputs: tuple, s: dict):
    if op == "attention":
        return mod.forward(*inputs, causal=bool(s.get("causal")))
    if op == "layernorm":
        return mod.forward(*inputs, eps=float(s.get("eps", 1e-5)))
    return mod.forward(*inputs)


def flops(op: str, s: dict) -> float:
    if op == "attention":
        f = 4.0 * s["batch"] * s["heads"] * s["seq"] * s["seq"] * s["dim"]
        return f / 2 if s.get("causal") else f
    if op == "matmul":
        return 2.0 * s["m"] * s["n"] * s["k"]
    return (5.0 if op == "softmax" else 8.0) * s["rows"] * s["cols"]  # memory-bound ops: nominal


def tflops_from(op: str, s: dict, ms: float | None) -> float | None:
    if not ms or ms <= 0:
        return None
    return flops(op, s) / ms * 1e-9  # flops / (ms*1e-3) / 1e12


def time_ms(fn, warmup: int = WARMUP, iters: int = ITERS) -> float:
    """Mean ms per call: CUDA events on GPU, perf_counter on CPU."""
    import torch
    for _ in range(warmup):
        fn()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(iters):
            fn()
        end.record()
        torch.cuda.synchronize()
        return start.elapsed_time(end) / iters
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    return (time.perf_counter() - t0) * 1000 / iters


class Forbidden(RuntimeError):
    pass


@contextlib.contextmanager
def guard_torch(op: str, enabled: bool = True):
    """While kernel.py runs, the torch shortcuts for the core op raise Forbidden."""
    if not enabled:
        yield
        return
    import torch
    import torch.nn.functional as F
    targets = {"attention": [(F, "scaled_dot_product_attention"), (torch, "matmul"), (torch, "bmm"),
                             (torch, "baddbmm"), (torch, "einsum"), (torch, "softmax"), (F, "softmax"),
                             (torch.Tensor, "__matmul__"), (torch.Tensor, "softmax")],
               "matmul": [(torch, "matmul"), (torch, "mm"), (torch, "bmm"), (torch, "addmm"), (torch, "einsum"),
                          (torch.Tensor, "__matmul__"), (torch.Tensor, "matmul"), (torch.Tensor, "mm")],
               "softmax": [(torch, "softmax"), (F, "softmax"), (torch.Tensor, "softmax"), (F, "log_softmax")],
               "layernorm": [(F, "layer_norm"), (torch, "layer_norm"), (torch, "native_layer_norm")]}[op]
    saved = []
    for obj, name in targets:
        if hasattr(obj, name):
            orig = getattr(obj, name)

            def blocked(*a, __n=name, **k):
                raise Forbidden(f"kernel.py called forbidden torch shortcut '{__n}' for op '{op}'")
            try:
                setattr(obj, name, blocked)
                saved.append((obj, name, orig))
            except (AttributeError, TypeError):
                pass
    try:
        yield
    finally:
        for obj, name, orig in saved:
            setattr(obj, name, orig)


def load_kernel(path: str):
    path = os.path.abspath(path)
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location("kernel_under_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "forward", None)):
        raise AttributeError("kernel.py must define forward(...)")
    return mod


def run(a: argparse.Namespace) -> dict:
    out = {"metric": a.metric, "value": None, "ok": False, "error": None, "op": a.op, "dtype": a.dtype,
           "shapes": None, "torch_ms": None, "kernel_ms": None, "tflops": None, "torch_tflops": None,
           "max_abs_err": None, "speedup_vs_torch": None, "device": None}
    try:
        import torch
        s = norm_shapes(a.op, a.shapes)
        out["shapes"] = s
        device = "cuda" if torch.cuda.is_available() else "cpu"
        out["device"] = torch.cuda.get_device_name(0) if device == "cuda" else "cpu"
        guard = not a.allow_torch
        with guard_torch(a.op, guard):
            mod = load_kernel(a.kernel)
        inputs = make_inputs(a.op, s, a.dtype, device, a.seed)
        with guard_torch(a.op, guard):
            y = call_kernel(mod, a.op, inputs, s)
        if device == "cuda":
            torch.cuda.synchronize()
        if a.metric == "compiles":
            out.update(value=True, ok=True)
            return out

        ref = reference(a.op, inputs, s, upcast=True)
        if not isinstance(y, torch.Tensor) or tuple(y.shape) != tuple(ref.shape):
            raise ValueError(f"output shape {getattr(y, 'shape', type(y))} != expected {tuple(ref.shape)}")
        err = (y.float() - ref).abs().max().item()
        if math.isnan(err):
            err = float("inf")
        out["max_abs_err"] = err
        correct = err < TOL[a.dtype]
        if a.metric == "max_abs_err":
            out.update(value=err, ok=True)
            return out

        def kern():
            return call_kernel(mod, a.op, inputs, s)

        with guard_torch(a.op, guard):
            kms = time_ms(kern, a.warmup, a.iters)
        tms = time_ms(lambda: reference(a.op, inputs, s, upcast=False), a.warmup, a.iters)
        out.update(kernel_ms=kms, torch_ms=tms, tflops=tflops_from(a.op, s, kms),
                   torch_tflops=tflops_from(a.op, s, tms), speedup_vs_torch=tms / kms if kms > 0 else None)
        out["value"] = {"speedup_vs_torch": out["speedup_vs_torch"], "tflops": out["tflops"], "ms": kms}[a.metric]
        out["ok"] = bool(correct)
        if not correct:
            out["error"] = f"incorrect output: max_abs_err {err:.3g} >= tol {TOL[a.dtype]} (perf does not count)"
    except Exception as e:  # noqa: BLE001 - everything is evidence
        tb = traceback.format_exc().strip().splitlines()
        out["error"] = f"{type(e).__name__}: {e}"[:400] + (f" | {tb[-2].strip()[:200]}" if len(tb) > 2 else "")
        out["ok"] = False
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        a = parse_args(argv)
    except SystemExit as e:
        print(json.dumps({"metric": None, "value": None, "ok": False, "error": f"bad arguments (exit {e.code})",
                          "torch_ms": None, "kernel_ms": None, "tflops": None, "max_abs_err": None}))
        return 2
    res = run(a)
    sys.stdout.flush()
    print(json.dumps(res, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
