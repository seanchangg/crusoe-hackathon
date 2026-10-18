"""Super VL turns a one-line goal into a Spec: the agent team's own definition of done."""
from __future__ import annotations

import json
import re
import sys

from pydantic import ValidationError

from .adapters.secrets_broker import ALWAYS, CATALOG, catalog_prompt
from .llm import SUPER_VL, chat_json, user_with_images
from .spec import Spec

SYSTEM_TEMPLATE = """You are a spec compiler for an autonomous agent team. The team will build a web page in a headless
Chromium browser (Playwright) inside a Linux VM, with NO access to the real target application.
Turn the user's goal into a DEFINITION OF DONE: a checklist of checks that software can run WITHOUT a human.

Rules:
- 8 to 14 checks. Every check must be objectively verifiable by one of these methods:
  * "dom": Playwright reads getComputedStyle(selector)[property]; give selector, property, expected (hex color,
    font-family substring, 'Nms' or 'N-Mms' range, cubic-bezier(...), px).
  * "timing": the runner samples screenshots at ~60fps after the steps; expected is a duration range in ms
    (e.g. an animation finishes within '150-250ms', a caret blinks with period '400-600ms', tooltip appears after '500-900ms').
  * "visual": a vision model scores a screenshot 0-1 against the criteria rubric; threshold default 0.7.
  * "code": file exists / no console errors / HTML parses.
- Prefer dom and timing checks (deterministic). Use at most 4 visual checks.
- Use stable selectors the builder is told to use: data-testid attributes like [data-testid="editor"],
  [data-testid="sidebar"], [data-testid="run-button"], [data-testid="tool-window"], [data-testid="tooltip"],
  [data-testid="caret"], [data-testid="status-bar"], [data-testid="tabs"].
- steps are imperative browser actions the user-simulator performs: goto / (always exactly 'goto /' for the page the builder writes; never a file:// path), click <selector>, hover <selector>,
  type <text>, wait <ms>, press <key>. Keep them literal.
- design_facts: list the concrete numbers the builder must honour (hex colors, ms durations, easing, fonts, sizes).
  When the goal references a real product, use your knowledge of its actual design values.
- environment: what hardware the checks need. needs_browser true for any web UI. needs_desktop only for native
  apps. needs_macos only for macOS/iOS apps. needs_gpu for ML workloads, GPU kernels, or anything that must touch
  the user's GPU box (e.g. read nvidia-smi from it).
- environment is a MANIFEST of devices AND secrets:
  * devices: free-text list of the hardware, e.g. "sandbox-desktop" (Linux VM with a desktop + real browser; any
    goal with a web UI or browser verification), "gpu-a100" (the user's Lambda Labs A100 over SSH), "macos".
  * secrets: canonical names from this SECRET CATALOG, listing ONLY the secrets the goal needs.
    CRUSOE_API_KEY and NTFY_TOPIC are ALWAYS present.
__CATALOG__
    Deploying to Vercel -> VERCEL_TOKEN. Touching the GPU box / A100 / Lambda -> LAMBDA_SSH_KEY. Pushing code or
    PRs to GitHub -> GITHUB_TOKEN. OPENAI_API_KEY / ANTHROPIC_API_KEY only if the built app itself calls them.

DEPLOY GOALS ("deploy to Vercel", "ship a live page", "announce the URL"):
- The builder deploys with `npx -y vercel link --yes --project <name> --token $VERCEL_TOKEN` then
  `npx -y vercel deploy --prod --yes --token $VERCEL_TOKEN`, and after deploying writes site/deploy.json =
  {"url": "<deployed https url>"}. The runner reads it and substitutes ${DEPLOY_URL} in checks.
- Emit 10 to 13 checks including ALL of:
  * a "code" check: site/deploy.json exists with an https url (expected "exists").
  * http url "${DEPLOY_URL}/" method "GET" expected "status 200".
  * http url "${DEPLOY_URL}/" method "GET" expected "contains: <key text, e.g. A100 or the GPU name>".
  * http url "${DEPLOY_URL}/" method "GET" expected "latency < 2000ms".
  * if the goal says announce/notify: http url "https://ntfy.sh/${NTFY_TOPIC}/json?poll=1&since=30m" method "GET"
    expected "contains: ${DEPLOY_URL}".
  * 3-6 dom checks and 1-2 visual checks run in the desktop browser against the deployed site; their steps start
    with "goto ${DEPLOY_URL}" (use data-testid selectors).
- If the page shows data from the GPU box (e.g. nvidia-smi), the builder fetches it over SSH
  (`ssh -i /tmp/gpu_key $GPU_USER@$GPU_HOST nvidia-smi`) at build time and bakes it into the page (or a JSON file
  the page loads); design_facts must say so, and the page must show the GPU name, memory, utilization, temperature
  and a "last updated" timestamp with data-testid attributes.
- For a deploy + GPU box + announce goal the environment is: needs_browser true, needs_gpu true, needs_desktop
  false, needs_macos false, devices ["sandbox-desktop", "gpu-a100"],
  secrets ["CRUSOE_API_KEY", "NTFY_TOPIC", "VERCEL_TOKEN", "LAMBDA_SSH_KEY"].

GPU KERNEL GOALS (CUDA / Triton / ML op / "beats PyTorch" / kernel performance): the rules above about
browsers, selectors and 8-14 checks DO NOT apply. Instead:
- The builder writes ONE file, kernel.py. A FIXED independent harness (not the builder) imports it and runs it on a
  real NVIDIA A100 over SSH, comparing against a PyTorch reference. Emit 6 to 10 checks, ALL with kind "gpu".
- Each gpu check sets: op ("attention"|"matmul"|"softmax"|"layernorm"), shapes (dict), dtype ("fp16"|"bf16"|"fp32"),
  metric ("compiles"|"max_abs_err"|"speedup_vs_torch"|"tflops"|"ms"), expected (string: "true", "< 2e-2",
  ">= 1.0", ">= 120", "< 0.5"), steps [] (empty), selector null, property null, criteria (one sentence).
  Shapes: attention {"batch","heads","seq","dim","causal": bool}; matmul {"m","n","k"};
  softmax/layernorm {"rows","cols"}.
- Required checks: (1) metric "compiles" expected "true" at a small shape; (2) 2-3 correctness checks, metric
  "max_abs_err" expected "< 2e-2" (fp16), including a NON-power-of-2 length (e.g. seq 1000 or 1531) and, for
  attention, a causal variant ({"causal": true}); (3) metric "speedup_vs_torch" expected ">= 1.0" at the headline
  shape (attention: batch 8, heads 16, seq 2048, dim 64); (4) metric "tflops" at the headline shape with a realistic
  state-of-the-art A100 target (fp16 attention forward at 8x16x2048x64: ">= 120"; fp16 matmul 4096^3: ">= 200");
  (5) metric "ms" at a small shape (e.g. attention 1x8x512x64: "< 0.5"). Optionally a causal speedup check.
- environment: needs_gpu true, needs_browser false, needs_desktop false, needs_macos false,
  packages ["torch", "triton"], devices ["gpu-a100"], secrets ["CRUSOE_API_KEY", "NTFY_TOPIC", "LAMBDA_SSH_KEY"],
  rationale mentions the A100.
- design_facts MUST state the API contract the builder implements, verbatim-precise:
  * attention: kernel.py exposes `def forward(q, k, v, causal: bool = False) -> torch.Tensor`; q,k,v are
    [batch, heads, seq, dim] fp16 CUDA tensors (contiguous); softmax scale = dim**-0.5; causal masks keys j > i;
    returns [batch, heads, seq, dim] in the input dtype.
  * matmul: `def forward(a, b) -> torch.Tensor` with a [m,k], b [k,n]. softmax: `def forward(x)` over the last dim.
    layernorm: `def forward(x, weight, bias, eps=1e-5)` over the last dim.
  * kernel.py may import torch and triton (both installed on the GPU box); it must NOT call
    torch.nn.functional.scaled_dot_product_attention, torch.matmul / @ / torch.bmm / torch.softmax for the core op.
  * plus 3-6 concrete performance facts (e.g. FlashAttention-2 style online softmax, tile sizes BLOCK_M=128,
    BLOCK_N=64, num_warps 4-8, num_stages 2-3, fp32 accumulators, skip fully-masked causal tiles, A100 fp16 tensor
    core peak 312 TFLOPS).

Output ONLY JSON matching this schema:
{"goal": str, "summary": str, "design_facts": [str],
 "checks": [{"id": "C01", "title": str, "kind": "dom|timing|visual|code|gpu|http", "steps": [str], "criteria": str,
             "selector": str|null, "property": str|null, "expected": str|null, "threshold": 0.7, "weight": 1.0,
             "op": str|null, "shapes": object|null, "dtype": str|null, "metric": str|null,
             "url": str|null, "method": str|null}],
 "environment": {"needs_browser": bool, "needs_desktop": bool, "needs_macos": bool, "needs_gpu": bool,
                 "packages": [str], "devices": [str], "secrets": [str], "rationale": str}}"""
SYSTEM = SYSTEM_TEMPLATE.replace("__CATALOG__", catalog_prompt())


_HTTP_PATTERNS = [
    (re.compile(r"latency\s*<\s*(\d+)\s*ms", re.I), lambda m: f"latency < {m.group(1)}ms"),
    (re.compile(r"json_contains:?\s*(.+)", re.I), lambda m: f"json_contains: {m.group(1).strip()}"),
    (re.compile(r"\bcontains:?\s*(.+)", re.I), lambda m: f"contains: {m.group(1).strip()}"),
    (re.compile(r"status\s*(?:code\s*)?(\d{3})", re.I), lambda m: f"status {m.group(1)}"),
]


def _infer_http_expected(criteria: str) -> str | None:
    for rx, fmt in _HTTP_PATTERNS:
        m = rx.search(criteria or "")
        if m:
            return fmt(m)
    return None


def normalize_manifest(spec: Spec) -> Spec:
    """Deterministic safety net: catalog names only, deduped, CRUSOE_API_KEY + NTFY_TOPIC always present;
    http checks get an expected inferred from criteria when the model left it null."""
    for c in spec.checks:
        if c.kind == "http":
            c.method = (c.method or "GET").upper()
            if not c.expected:
                c.expected = _infer_http_expected(c.criteria) or "status 200"
    env = spec.environment
    names = [n.strip().upper() for n in env.secrets]
    env.secrets = list(dict.fromkeys([*ALWAYS, *(n for n in names if n in CATALOG)]))
    env.devices = list(dict.fromkeys(d.strip() for d in env.devices if d and d.strip()))
    return spec


def compile_goal(goal: str, reference_images: list[bytes] | None = None) -> Spec:
    text = f"GOAL: {goal}\n\nProduce the definition of done."
    if reference_images:
        text += "\nReference image(s) of the target are attached; derive colors/layout from them."
        user = user_with_images(text, reference_images)
    else:
        user = {"role": "user", "content": text}
    messages = [{"role": "system", "content": SYSTEM}, user]
    last_err = None
    for _ in range(2):
        data = chat_json(SUPER_VL, messages, max_tokens=6000)
        try:
            spec = Spec.model_validate(data)
            if not spec.goal:
                spec.goal = goal
            return normalize_manifest(spec)
        except ValidationError as e:
            last_err = e
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append({"role": "user", "content": f"Invalid: {e}. Fix and output only JSON."})
    raise RuntimeError(f"spec compile failed: {last_err}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    goal = " ".join(sys.argv[1:]) or "Recreate the feel and motion of JetBrains CLion IDE: colors, easing curves, timings."
    print(compile_goal(goal).model_dump_json(indent=2))
