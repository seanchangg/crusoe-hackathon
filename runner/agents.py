"""The agent team: builder (Lightning), user-sim "preset slave" (Lightning),
verifier (deterministic for dom/timing/code, Super VL for visual), re-planner (Super VL)."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import creds
import gpu
import llm
from browser import frame_settle_ms, normalize_color, normalize_value

SITE = Path(os.environ.get("SITE_DIR", "site"))
BUILDER_MAX_CALLS = 25
USERSIM_MAX_CALLS = 15
REMOTE_BUDGET = int(os.environ.get("GPU_BUILDER_REMOTE_CALLS", "6"))
BROWSER_KINDS = ("dom", "timing", "visual")
NO_BROWSER_KINDS = ("gpu", "http")
_FILE_EXISTS = re.compile(r"([\w./-]+\.(?:html|css|js|py|json|txt|md|toml|ya?ml|sh))\s+exists")
_FILE_EXISTS_PREFIX = re.compile(r"\bfile\s+exists\s*[:=]?\s*[`'\"]?([\w./-]+)")
DEPLOY_SHELL_TIMEOUT = int(os.environ.get("DEPLOY_SHELL_TIMEOUT", "180"))
DEPLOY_SHELL_CALLS = int(os.environ.get("DEPLOY_SHELL_CALLS", "8"))
HTTP_TIMEOUT = 20


def file_exists_target(check: dict) -> str | None:
    """'file exists: site/deploy.json' / 'index.html exists' in expected or criteria -> path relative to site/."""
    for text in (check.get("expected") or "", check.get("criteria") or ""):
        t = text.strip()
        m = _FILE_EXISTS_PREFIX.search(t.lower()) or _FILE_EXISTS.search(t.lower())
        if m:
            p = m.group(1).strip("`'\" ").lstrip("./").lstrip("/")
            return p[5:] if p.startswith("site/") else p
    return None


def code_needs_browser(check: dict) -> bool:
    """code checks are file-existence checks (no browser) or console-error checks (browser)."""
    return file_exists_target(check) is None


def deploy_mode(spec: dict) -> bool:
    """Job ships something to the internet (http checks / deploy secrets / ${DEPLOY_URL} in checks)."""
    env = spec.get("environment") or {}
    if any(c.get("kind") == "http" for c in spec.get("checks", [])):
        return True
    if "VERCEL_TOKEN" in (env.get("secrets") or []):
        return True
    return "${DEPLOY_URL}" in json.dumps(spec.get("checks", []))


def needs_browser(spec: dict) -> bool:
    """False when every check is gpu or a file-existence code check: skip Chromium/desktop entirely."""
    return any(c.get("kind") in BROWSER_KINDS or (c.get("kind") == "code" and code_needs_browser(c))
               for c in spec["checks"])


def gpu_mode(spec: dict) -> bool:
    """Kernel-building job: all checks are gpu (or needs_gpu with no browser-facing checks)."""
    kinds = {c.get("kind") for c in spec["checks"]}
    if not kinds or "gpu" not in kinds:
        return False
    return kinds == {"gpu"} or (bool((spec.get("environment") or {}).get("needs_gpu"))
                                and not kinds & set(BROWSER_KINDS))


def _args(tc) -> dict:
    try:
        a = json.loads(tc.function.arguments or "{}")
        return a if isinstance(a, dict) else {}
    except json.JSONDecodeError:
        return {"__error__": "arguments were not valid JSON"}


def _assistant_dict(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments or "{}"}}
                           for tc in msg.tool_calls]
    return d


def _fn(name, desc, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required if required is not None else list(props)}}}


# =============================================================== builder ==

BUILDER_TOOLS = [
    _fn("write_file", "Create or overwrite a file under the site root (e.g. index.html, style.css, app.js).",
        {"path": {"type": "string"}, "content": {"type": "string"}}),
    _fn("read_file", "Read a file under the site root.", {"path": {"type": "string"}}),
    _fn("list_files", "List files under the site root.", {}, []),
    _fn("run_shell", "Run a shell command with cwd = site root (20 s timeout; 180 s with network for deploy jobs). "
        "Output is truncated.", {"cmd": {"type": "string"}}),
]


def _safe_path(p: str) -> Path:
    p = (p or "").lstrip("/")
    if p.startswith("site/"):
        p = p[5:]
    full = (SITE / p).resolve()
    if not str(full).startswith(str(SITE.resolve())):
        raise ValueError("path escapes site root")
    return full


_SHELL_BUDGET = [3]
_SHELL_TIMEOUT = [20]
_SHELL_FLAGS: dict = {"ssh_denied": False}
_URL = re.compile(r"https://[^\s\"'<>()\[\]]+")


def deploy_url_from_output(stdout: str, stderr: str = "") -> str | None:
    """Vercel CLI: the public production alias ('Aliased: https://x.vercel.app') wins - generated deployment URLs
    sit behind Vercel Authentication on teams with Standard Protection; else the last https URL on stdout."""
    both = (stdout or "") + "\n" + (stderr or "")
    m = re.findall(r"Aliased:\s*(https://[^\s\[\]]+)", both)
    if m:
        return m[-1].rstrip("/.,")
    urls = _URL.findall(stdout or "") or [u for u in _URL.findall(stderr or "") if ".vercel.app" in u]
    urls = [u.rstrip("/.,") for u in urls if "vercel.com" not in u.split("/")[2]]
    return urls[-1] if urls else None


def _maybe_record_deploy(cmd: str, r: subprocess.CompletedProcess) -> str:
    """After a successful `vercel deploy`, write site/deploy.json ourselves if the builder forgets to."""
    if "vercel" not in cmd or "deploy" not in cmd or r.returncode != 0:
        return ""
    url = deploy_url_from_output(r.stdout, r.stderr)
    if not url:
        return ""
    try:
        (SITE / "deploy.json").write_text(json.dumps({"url": url}))
        os.environ["DEPLOY_URL"] = url
        return f"\n[runner] recorded deploy.json url={url}"
    except OSError:
        return ""


def _builder_tool(name: str, a: dict) -> str:
    if "__error__" in a:
        return "ERROR: " + a["__error__"]
    try:
        if name == "write_file":
            f = _safe_path(a.get("path", "index.html"))
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(a.get("content", ""))
            return f"wrote {f.relative_to(SITE.resolve())} ({len(a.get('content', ''))} bytes)"
        if name == "read_file":
            f = _safe_path(a.get("path", ""))
            return f.read_text()[:20000] if f.exists() else "ERROR: no such file"
        if name == "list_files":
            return "\n".join(str(p.relative_to(SITE)) for p in sorted(SITE.rglob("*")) if p.is_file()) or "(empty)"
        if name == "run_shell":
            _SHELL_BUDGET[0] -= 1
            if _SHELL_BUDGET[0] < 0:
                return "ERROR: run_shell budget for this round is used up; just write the files."
            cmd = a.get("cmd", "")
            try:
                r = subprocess.run(cmd, shell=True, cwd=SITE, capture_output=True, text=True,
                                   timeout=_SHELL_TIMEOUT[0], stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "").encode()
                return creds.redact(f"ERROR: timed out after {_SHELL_TIMEOUT[0]} s\n"
                                    + out.decode(errors="replace")[-2000:])
            note = _maybe_record_deploy(cmd, r)
            out = r.stdout + r.stderr
            if "permission denied (publickey" in out.lower():
                _SHELL_FLAGS["ssh_denied"] = True
            return creds.redact((f"exit={r.returncode}\n" + out)[-4000:] + note)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"
    return f"ERROR: unknown tool {name}"


def required_testids(spec: dict) -> list[str]:
    ids = []
    for c in spec["checks"]:
        for m in re.finditer(r"data-testid\s*=\s*[\"']?([\w-]+)", (c.get("selector") or "") + " " + " ".join(c.get("steps", []))):
            if m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def _checklist_text(spec: dict) -> str:
    lines = []
    for c in spec["checks"]:
        bits = [f"- {c['id']} [{c['kind']}] {c['title']}: {c['criteria']}"]
        if c.get("selector"):
            bits.append(f"selector `{c['selector']}`")
        if c.get("property"):
            bits.append(f"property `{c['property']}`")
        if c.get("expected"):
            bits.append(f"expected `{c['expected']}`")
        if c.get("url"):
            bits.append(f"{(c.get('method') or 'GET').upper()} {c['url']}")
        if c.get("kind") == "gpu":
            bits.append(f"op {c.get('op')} dtype {c.get('dtype')} shapes {json.dumps(c.get('shapes') or {})} "
                        f"metric {c.get('metric')}")
        elif c.get("steps"):
            bits.append("steps: " + " -> ".join(c["steps"]))
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def site_snapshot() -> str:
    """Concatenated text of the site, used for the >=90%-identical drift check."""
    out = []
    for p in sorted(SITE.rglob("*")):
        if p.is_file() and p.suffix in (".html", ".css", ".js", ".py"):
            try:
                out.append(f"=== {p.relative_to(SITE)}\n{p.read_text()}")
            except Exception:
                pass
    return "\n".join(out)


def builder(spec: dict, round_no: int, feedback: dict | None) -> dict:
    SITE.mkdir(parents=True, exist_ok=True)
    if gpu_mode(spec):
        return gpu_builder(spec, round_no, feedback)
    tids = required_testids(spec)
    dep = deploy_mode(spec)
    if dep:
        system = deploy_system_prompt(spec, tids)
    else:
        system = (
        "You are a senior front-end engineer. You build a single static web page (plain HTML/CSS/JS, "
        "no frameworks, no CDN or network requests, no external fonts) by calling tools. Files live in the "
        "site root: index.html (required), optional style.css and app.js referenced relatively.\n"
        f"GOAL: {spec['goal']}\n"
        f"SUMMARY: {spec.get('summary', '')}\n"
        "DESIGN FACTS (honour exactly):\n" + "\n".join(f"- {f}" for f in spec.get("design_facts", [])) + "\n\n"
        "ACCEPTANCE CHECKLIST - an automated verifier measures getComputedStyle() for each selector/property "
        "in headless Chromium 1280x800, measures animation timings frame by frame, checks the console for errors, "
        "and a vision model scores a screenshot. Every item must pass:\n" + _checklist_text(spec) + "\n\n"
        "REQUIRED data-testid attributes (each must exist on a visible element): "
        + ", ".join(tids) + "\n"
        "Rules: set exact values with CSS (e.g. `width:240px; flex:none; box-sizing:border-box` for fixed sizes, "
        "exact hex colors, the exact transition shorthand). Hover effects must use CSS :hover with a transition on "
        "the base rule. Delayed tooltips: keep the element in the DOM, hidden via visibility:hidden/opacity:0 and "
        "shown on hover with `transition-delay` matching the required delay (or a JS setTimeout on mouseenter). "
        "Make the page look like a real dark IDE (tool-window strip, project tree, editor tabs, code with syntax "
        "colors and line numbers, run toolbar, status bar). No console errors.\n"
        "Work efficiently: put ALL CSS in a <style> tag and ALL JS in a <script> tag inside index.html and write "
        "it with ONE write_file call containing the complete file (never reference files you have not written), "
        "then reply with a one-line summary and NO tool call when finished. Do NOT try to launch browsers or "
        "test the page yourself - an independent verifier does that; run_shell is limited to 3 calls."
        )
        sec = credentials_section(spec)
        if sec:
            system += "\n\n" + sec
    if round_no == 1 or not (SITE / "index.html").exists():
        user = ("Round 1: build the app, deploy it, write deploy.json, announce it, then stop." if dep
                else "Round 1: create the page from scratch now.")
    else:
        fb = feedback or {}
        fails = "\n".join(
            f"- {f['id']} {f['title']}: expected {f.get('expected') or f.get('criteria')}; "
            + (f"url {f.get('url')}; " if f.get("url") else f"selector {f.get('selector')}; ")
            + f"EVIDENCE: {f.get('evidence')}"
            for f in fb.get("failures", []))
        user = (f"Round {round_no}. The verifier reported these FAILURES last round:\n{fails or '(none)'}\n")
        if fb.get("brief"):
            user += f"\nRE-PLAN BRIEF from the lead (follow it):\n{fb['brief']}\n"
        user += "\nCURRENT FILES:\n" + site_snapshot()[:24000] + \
            "\n\nFix every failure (rewrite files with write_file), keep what already passes, then stop."
        if dep:
            user += ("\nIf the app changed, redeploy (same command) and rewrite deploy.json; if the deploy already "
                     f"works keep it. Current deploy.json url: {creds.read_deploy_url(SITE) or '(none)'}")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    calls, written, t0, nudges = 0, [], time.time(), 0
    _SHELL_BUDGET[0] = DEPLOY_SHELL_CALLS if dep else 3
    _SHELL_TIMEOUT[0] = DEPLOY_SHELL_TIMEOUT if dep else 20
    _SHELL_FLAGS["ssh_denied"] = False
    final_message = ""
    while calls < BUILDER_MAX_CALLS:
        try:
            msg, finish = llm.chat_tools(llm.LIGHTNING, messages, BUILDER_TOOLS, agent="builder",
                                         max_tokens=16000, temperature=0.7)
        except Exception as e:  # noqa: BLE001
            print(f"[builder] LLM error: {e}", flush=True)
            break
        messages.append(_assistant_dict(msg))
        if not msg.tool_calls:
            final_message = (msg.content or "").strip()
            # Salvage code blocks if the model answered in prose instead of calling tools.
            _salvage_code_blocks(msg.content or "")
            missing = missing_assets()
            if (missing or not (SITE / "index.html").exists()) and nudges < 2:
                nudges += 1
                messages.append({"role": "user", "content": (
                    f"index.html references files that do not exist: {missing}. " if missing else
                    "index.html does not exist yet. ") + "Write them now (or inline them) with write_file."})
                continue
            break
        for tc in msg.tool_calls:
            calls += 1
            a = _args(tc)
            res = _builder_tool(tc.function.name, a)
            if tc.function.name == "write_file" and res.startswith("wrote"):
                written.append(a.get("path"))
            label = creds.redact(a.get('path') or a.get('cmd') or '')
            print(f"[builder] {tc.function.name}({label[:100 if dep else 60]}) -> {res[:80 if not dep else 160]!r}",
                  flush=True)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
    if not (SITE / "index.html").exists():
        _write_minimal(spec, tids)
        written.append("index.html (fallback)")
    out = {"tool_calls": calls, "written": written, "seconds": round(time.time() - t0, 1),
           "final_message": creds.redact(final_message)[:1500], "ssh_denied": _SHELL_FLAGS["ssh_denied"]}
    if dep:
        out["deploy_url"] = creds.read_deploy_url(SITE)
    return out


def credentials_section(spec: dict) -> str:
    """'Credentials and devices available to you' - ledger names/status/purpose, NEVER values."""
    rows = creds.secrets_prompt_section()
    env = spec.get("environment") or {}
    devs = env.get("devices") or []
    gpu_line = ("GPU box: configured via GPU_HOST/GPU_USER/GPU_SSH_KEY_B64 (key already written to /tmp/gpu_key, "
                "mode 600)" if os.environ.get("GPU_HOST") else "GPU box: NOT configured (GPU_HOST unset)")
    if not rows and not devs and not os.environ.get("GPU_HOST"):
        return ""
    dev_lines = [f"- {d if isinstance(d, str) else (d.get('name') or d.get('kind') or json.dumps(d))}"
                 for d in devs]
    return (
        "CREDENTIALS AND DEVICES AVAILABLE TO YOU (values are in your shell environment as env vars; never print, "
        "echo, cat or write them into files - reference them as \"$NAME\" inside run_shell commands):\n"
        + (rows or "(no credential ledger)") + "\n"
        + (("Devices declared by the spec:\n" + "\n".join(dev_lines) + "\n") if dev_lines else "")
        + gpu_line + "\n"
        "If a credential you need is MISSING or a device is unreachable / refuses SSH (e.g. 'Permission denied "
        "(publickey)'), do NOT fake output or invent data: say exactly which credential/device is blocking in your "
        "final message (e.g. 'BLOCKED: missing credential VERCEL_TOKEN') - the controller escalates to a human.")


def deploy_system_prompt(spec: dict, tids: list[str]) -> str:
    jid = re.sub(r"[^a-z0-9-]", "-", os.environ.get("JOB_ID", "local").lower())[:60].strip("-") or "local"
    return (
        "You are a senior full-stack engineer who ships small web apps to production by calling tools. Your "
        "working dir (the site root, where write_file and run_shell operate) is site/. Outbound network is "
        "allowed. run_shell has a 180 s timeout and up to "
        f"{DEPLOY_SHELL_CALLS} calls per round, so batch commands with && where sensible.\n"
        f"GOAL: {spec['goal']}\nSUMMARY: {spec.get('summary', '')}\n"
        "DESIGN FACTS (honour exactly):\n" + "\n".join(f"- {f}" for f in spec.get("design_facts", [])) + "\n\n"
        "ACCEPTANCE CHECKLIST - an independent verifier (which you cannot influence) fetches the deployed URL over "
        "HTTP, opens it in a real Chromium on a desktop, and reads the ntfy topic. ${DEPLOY_URL} is the url you "
        "write to deploy.json. Every item must pass:\n" + _checklist_text(spec) + "\n"
        + ("REQUIRED data-testid attributes: " + ", ".join(tids) + "\n" if tids else "") + "\n"
        + credentials_section(spec) + "\n\n"
        "RECOMMENDED PATH (simplest thing that deploys reliably): a STATIC site with a build-time snapshot.\n"
        "1. Collect the data now, e.g. GPU status:\n"
        "   ssh -i /tmp/gpu_key -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15 "
        "\"${GPU_USER:-ubuntu}@$GPU_HOST\" nvidia-smi > /tmp/nvidia-smi.txt; echo exit=$?; head -c 3000 /tmp/nvidia-smi.txt\n"
        "   (also handy: nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu "
        "--format=csv,noheader)\n"
        "   (if /tmp/gpu_key is missing: echo \"$GPU_SSH_KEY_B64\" | base64 -d > /tmp/gpu_key && chmod 600 /tmp/gpu_key)\n"
        "2. Bake the REAL output into index.html (HTML-escape it inside a <pre>, add a 'snapshot taken at <UTC time>' "
        "line and a clear title). Optionally also write api/status.json (static JSON) with the same data. Use "
        "write_file for index.html (you may build it from nvidia-smi.txt with a tiny python3 script via "
        "run_shell from /tmp/nvidia-smi.txt). Keep it self-contained: inline CSS/JS, no CDNs. Keep site/ minimal "
        "(index.html, optionally api/status.json; no .vercelignore, no raw dumps) - everything in it is uploaded.\n"
        "   (Only if a LIVE api is truly required: a tiny Python app api/index.py exposing `app` (Flask/FastAPI) + "
        "requirements.txt + vercel.json; this is slower and riskier than static.)\n"
        "3. Deploy FROM INSIDE site/ (run_shell's cwd already is site/, so nothing else is uploaded; the CLI prints "
        "the production URL on stdout; there is NO --name flag, use --project):\n"
        "   npx -y vercel deploy --prod --yes --token \"$VERCEL_TOKEN\" ${VERCEL_TEAM_ID:+--scope \"$VERCEL_TEAM_ID\"} "
        f"--project \"donedone-job-{jid}\" 2>&1 | tail -n 25\n"
        "   Take the 'Aliased:' https URL if present (production domains are public), otherwise the last "
        "https://...vercel.app URL printed, and write deploy.json = {\"url\": \"https://...\"} with write_file (the runner also records it "
        "automatically after a successful deploy). Then check it: curl -s -o /dev/null -w '%{http_code}' <url> "
        "(401 means you used a protected deployment URL - use the production alias instead).\n"
        "4. Announce: curl -s -H \"Title: <short title>\" -H \"Click: <deploy url>\" -d \"<one-line message with the "
        "deploy url>\" https://ntfy.sh/$NTFY_TOPIC\n"
        "Never print or write secret values (no `env`, `printenv`, `echo $VERCEL_TOKEN`). Do NOT fake data: if the "
        "GPU box or a credential is unavailable, stop and say 'BLOCKED: ...' naming it. When everything is "
        "deployed and announced reply with a one-line summary (include the URL) and NO tool call.")


def missing_assets() -> list[str]:
    """Relative src/href references in index.html that don't exist under site/."""
    idx = SITE / "index.html"
    if not idx.exists():
        return []
    out = []
    for ref in re.findall(r'(?:src|href)\s*=\s*["\']([^"\'#?]+)', idx.read_text()):
        if re.match(r"^(?:[a-z]+:|//|data:)", ref, re.I) or ref in ("", "/"):
            continue
        if not (SITE / ref.lstrip("./").lstrip("/")).exists() and ref not in out:
            out.append(ref)
    return out


def _salvage_code_blocks(text: str) -> None:
    for lang, fname in (("html", "index.html"), ("css", "style.css"), ("javascript", "app.js"), ("js", "app.js")):
        m = re.search(rf"```{lang}\s*\n(.*?)```", text, re.S)
        if m and len(m.group(1)) > 40:
            (SITE / fname).write_text(m.group(1))


def _write_minimal(spec: dict, tids: list[str]) -> None:
    body = "\n".join(f'<div data-testid="{t}">{t}</div>' for t in tids)
    (SITE / "index.html").write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{spec['goal'][:60]}</title></head>"
        f"<body style='background:#1e1f22;color:#ccc'>{body}</body></html>")


# ============================================================ gpu builder ==

GPU_BUILDER_TOOLS = [
    _fn("write_file", "Create or overwrite a file under the work dir. The graded file is kernel.py.",
        {"path": {"type": "string"}, "content": {"type": "string"}}),
    _fn("read_file", "Read a file under the work dir.", {"path": {"type": "string"}}),
    _fn("list_files", "List files under the work dir.", {}, []),
    _fn("run_remote", "Upload the current kernel.py to a scratch dir on the GPU box and run a shell command "
        "there (cwd = that scratch dir, so `import kernel` works). Use it to try-compile and sanity-check "
        "correctness/speed. 120 s timeout, output truncated, limited calls per round.",
        {"cmd": {"type": "string"}}),
]
_REMOTE_LEFT = [REMOTE_BUDGET]
_GPU_PROBE: list[str] = []


def gpu_probe_cached() -> str:
    if not _GPU_PROBE:
        _GPU_PROBE.append(gpu.gpu_probe())
    return _GPU_PROBE[0]


def run_remote(cmd: str) -> str:
    """Builder's try-compile tool: scp site/kernel.py to <workdir>/scratch and run cmd there over SSH.
    The scratch dir is separate from where the fixed grader runs."""
    _REMOTE_LEFT[0] -= 1
    if _REMOTE_LEFT[0] < 0:
        return "ERROR: run_remote budget for this round is used up; finish kernel.py and stop."
    if not gpu.gpu_available():
        _REMOTE_LEFT[0] = 0
        return ("ERROR: no GPU attached (GPU_HOST unset) - you cannot test remotely this round. Write kernel.py "
                "carefully against the API contract and stop.")
    if not gpu.ensure_ssh_client():
        return "ERROR: ssh client unavailable"
    scratch = f"{gpu._workdir()}/scratch"
    try:
        r = gpu.ssh(f"mkdir -p {shlex.quote(scratch)}", timeout=30)
        if r.returncode != 0:
            return f"ERROR: ssh failed: {r.stderr.strip()[:300]}"
        k = SITE / "kernel.py"
        if k.exists():
            r = gpu.scp(k, f"{scratch}/kernel.py")
            if r.returncode != 0:
                return f"ERROR: scp kernel.py failed: {r.stderr.strip()[:300]}"
        r = gpu.ssh(f"cd {shlex.quote(scratch)} && timeout 110 bash -lc {shlex.quote(cmd)}", timeout=130)
        return (f"exit={r.returncode}\n" + (r.stdout or "") + (r.stderr or ""))[-4000:]
    except subprocess.TimeoutExpired:
        return "ERROR: remote command timed out (120 s)"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


def gpu_builder(spec: dict, round_no: int, feedback: dict | None) -> dict:
    """Kernel builder: SUPER_VL (stronger coder) writes site/kernel.py, may try-compile it on the GPU box."""
    system = (
        "You are an expert GPU kernel engineer (Triton / PyTorch). You write ONE file, kernel.py, by calling "
        "tools. An independent fixed harness (which you cannot see) imports kernel.py on a real GPU, compares "
        "it with a PyTorch reference and times it.\n"
        f"GOAL: {spec['goal']}\nSUMMARY: {spec.get('summary', '')}\n"
        "API CONTRACT / DESIGN FACTS (honour exactly):\n" + "\n".join(f"- {f}" for f in spec.get("design_facts", []))
        + f"\n\nGPU BOX: {gpu_probe_cached()}\n\n"
        "ACCEPTANCE CHECKLIST (each graded by the harness):\n" + _checklist_text(spec) + "\n\n"
        "Workflow: write the complete kernel.py with write_file, then use run_remote to try it, e.g. "
        "run_remote(\"python3 -c 'import torch,kernel; q=torch.randn(1,8,512,64,device=\\\"cuda\\\",dtype=torch.float16);"
        " o=kernel.forward(q,q,q); print(o.shape, (o-torch.nn.functional.scaled_dot_product_attention(q,q,q))"
        ".abs().max().item())'\") (the reference may be used in YOUR tests, never inside kernel.py). Fix errors, "
        f"keep it correct first, then fast. run_remote is limited to {REMOTE_BUDGET} calls per round. When "
        "finished reply with a one-line summary and NO tool call."
    )
    k = SITE / "kernel.py"
    if round_no == 1 or not k.exists():
        user = "Round 1: write kernel.py from scratch now."
    else:
        fb = feedback or {}
        fails = "\n".join(f"- {f['id']} {f['title']}: expected {f.get('expected') or f.get('criteria')}; "
                          f"EVIDENCE: {f.get('evidence')}" for f in fb.get("failures", []))
        user = f"Round {round_no}. The harness reported these FAILURES last round:\n{fails or '(none)'}\n"
        if fb.get("brief"):
            user += f"\nRE-PLAN BRIEF from the lead (follow it):\n{fb['brief']}\n"
        user += "\nCURRENT kernel.py:\n" + k.read_text()[:24000] + "\n\nFix every failure, keep what passes, then stop."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    calls, written, t0, nudges = 0, [], time.time(), 0
    _REMOTE_LEFT[0] = REMOTE_BUDGET
    while calls < BUILDER_MAX_CALLS:
        try:
            msg, _ = llm.chat_tools(llm.SUPER_VL, messages, GPU_BUILDER_TOOLS, agent="builder:gpu",
                                    max_tokens=16000, temperature=0.6)
        except Exception as e:  # noqa: BLE001
            print(f"[builder] LLM error: {e}", flush=True)
            break
        messages.append(_assistant_dict(msg))
        if not msg.tool_calls:
            m = re.search(r"```python\s*\n(.*?)```", msg.content or "", re.S)
            if m and len(m.group(1)) > 40 and ("def forward" in m.group(1) or not k.exists()):
                k.write_text(m.group(1))
                written.append("kernel.py (salvaged)")
            if not k.exists() and nudges < 2:
                nudges += 1
                messages.append({"role": "user", "content": "kernel.py does not exist yet. Write it now with write_file."})
                continue
            break
        for tc in msg.tool_calls:
            calls += 1
            a = _args(tc)
            if tc.function.name == "run_remote":
                res = "ERROR: " + a["__error__"] if "__error__" in a else run_remote(a.get("cmd", ""))
            elif tc.function.name == "run_shell":
                res = "ERROR: no local shell; use run_remote"
            else:
                res = _builder_tool(tc.function.name, a)
            if tc.function.name == "write_file" and res.startswith("wrote"):
                written.append(a.get("path"))
            print(f"[builder:gpu] {tc.function.name}({(a.get('path') or a.get('cmd') or '')[:60]}) -> {res[:100]!r}",
                  flush=True)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
    return {"tool_calls": calls, "written": written, "seconds": round(time.time() - t0, 1), "model": "superVL"}


# ============================================================== user-sim ==

USERSIM_TOOLS = [
    _fn("goto", "Load a page path, e.g. '/', or an absolute URL (https://...). Resets hover state.",
        {"path": {"type": "string"}}),
    _fn("click", "Click the element matching a CSS selector.", {"selector": {"type": "string"}}),
    _fn("hover", "Move the mouse over the element matching a CSS selector and keep it there.", {"selector": {"type": "string"}}),
    _fn("type_text", "Type text with the keyboard into the focused element.", {"text": {"type": "string"}}),
    _fn("press", "Press a key, e.g. 'Enter', 'Control+Shift+A'.", {"key": {"type": "string"}}),
    _fn("wait_ms", "Wait N milliseconds.", {"ms": {"type": "integer"}}),
    _fn("screenshot", "Take a full-viewport screenshot.", {"name": {"type": "string"}}),
    _fn("measure_css", "Read the computed CSS property of the element matching selector (colors returned as hex).",
        {"selector": {"type": "string"}, "property": {"type": "string"}}),
    _fn("record_frames", "Record a burst of screenshots of an element (or the page) for duration_ms.",
        {"selector": {"type": "string"}, "duration_ms": {"type": "integer"}, "fps": {"type": "integer"}},
        ["duration_ms"]),
]


def _run_tool(browser, name: str, a: dict, check: dict, ev: dict) -> str:
    cid = check["id"]
    try:
        if name == "goto":
            path, unresolved = creds.resolve_templates(a.get("path") or "/")
            if unresolved:
                return (f"ERROR: cannot resolve ${{{unresolved[0]}}}"
                        + (" (site/deploy.json not written yet)" if "DEPLOY_URL" in unresolved else ""))
            return browser.goto(path)
        if name == "click":
            return browser.click(a["selector"])
        if name == "hover":
            return browser.hover(a["selector"])
        if name == "type_text":
            return browser.type_text(a.get("text", ""))
        if name == "press":
            return browser.press(a.get("key", "Enter"))
        if name == "wait_ms":
            return browser.wait_ms(a.get("ms", 100))
        if name == "screenshot":
            p = browser.screenshot(f"{cid}_{len(ev['screenshots'])}")
            ev["screenshots"].append(p)
            return f"saved {p}"
        if name == "measure_css":
            v = browser.measure_css(a["selector"], a["property"])
            ev["measurements"][f"{a['selector']}::{a['property']}"] = v
            return f"{a['property']} = {v}" if v is not None else f"no element matches {a['selector']}"
        if name == "record_frames":
            frames, fps = browser.record_frames(a.get("selector") or None, int(a.get("duration_ms", 500)),
                                                int(a.get("fps", 30)), prefix=f"{cid}_f")
            ev["frames"] = [(t, p) for t, p, _ in frames]
            ev["frames_fps"] = fps
            return f"recorded {len(frames)} frames at {fps} fps"
    except KeyError as e:
        return f"ERROR: missing argument {e}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
    return f"ERROR: unknown tool {name}"


_STEP_PATTERNS = [
    (re.compile(r"^(?:goto|go to|open|navigate(?: to)?|load|visit)\s+(\S+)", re.I), "goto", "path"),
    (re.compile(r"^hover(?: over)?\s+(.+?)(?:\s+for\s+(\d+)\s*(ms|s))?$", re.I), "hover", "selector"),
    (re.compile(r"^click(?: on)?\s+(.+)$", re.I), "click", "selector"),
    (re.compile(r"^(?:wait|sleep|pause)(?: for)?\s+(\d+(?:\.\d+)?)\s*(ms|s|seconds?)?", re.I), "wait_ms", "ms"),
    (re.compile(r"^type\s+[\"']?(.+?)[\"']?$", re.I), "type_text", "text"),
    (re.compile(r"^press\s+(\S+)", re.I), "press", "key"),
    (re.compile(r"^(?:take a )?screenshot", re.I), "screenshot", None),
]


def parse_step(step: str) -> tuple[str, dict] | None:
    """Deterministic fallback: 'hover [data-testid="x"]' -> ('hover', {'selector': ...})."""
    s = step.strip().rstrip(".")
    for rx, tool, key in _STEP_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        if tool == "wait_ms":
            n = float(m.group(1))
            unit = (m.group(2) or "ms").lower()
            return tool, {"ms": int(n * 1000 if unit.startswith("s") else n)}
        if tool == "screenshot":
            return tool, {"name": "shot"}
        val = m.group(1).strip()
        if key == "selector":
            val = val.strip("'\"` ") if not val.startswith("[") else val.strip("` ")
        return tool, {key: val}
    return None


DESK_TOOLS = [
    _fn("goto", "Load a page path in the browser, e.g. '/', or an absolute URL (https://...). Resets the page.",
        {"path": {"type": "string"}}),
    _fn("locate", "Find the on-screen coordinates (center x,y and box) of the element matching a CSS selector.",
        {"selector": {"type": "string"}}),
    _fn("desk_move", "Move the real mouse pointer to screen x,y (hovering whatever is there).",
        {"x": {"type": "integer"}, "y": {"type": "integer"}}),
    _fn("desk_click", "Click the real mouse at screen x,y (button 1=left, 3=right).",
        {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "integer"}}, ["x", "y"]),
    _fn("desk_type", "Type text on the real keyboard.", {"text": {"type": "string"}}),
    _fn("desk_key", "Press a key combo on the real keyboard, e.g. 'Return', 'ctrl+shift+a'.", {"key": {"type": "string"}}),
    _fn("wait_ms", "Wait N milliseconds.", {"ms": {"type": "integer"}}),
    _fn("desk_screenshot", "Screenshot the whole desktop.", {"name": {"type": "string"}}),
    _fn("desk_record", "START a screen recording of duration_ms in the background (returns at once). Call it "
        "right BEFORE the action whose motion must be captured.", {"duration_ms": {"type": "integer"}}),
    _fn("measure_css", "Read the computed CSS property of the element matching selector (colors as hex).",
        {"selector": {"type": "string"}, "property": {"type": "string"}}),
    _fn("is_visible", "Measuring instrument: is the element matching selector visible right now?",
        {"selector": {"type": "string"}}),
]
# (no record_frames on the desktop: headed page.screenshot() bursts make :hover flicker under the real pointer;
#  motion is captured with desk_record and timing is measured by the verifier's in-page rAF sampler.)


def _desk_tool(browser, name: str, a: dict, check: dict, ev: dict) -> str:
    cid = check["id"]
    try:
        if name == "locate":
            loc = browser.locate(a["selector"])
            return json.dumps(loc) if loc else f"no element matches {a['selector']}"
        if name == "desk_move":
            return browser.desk_move(int(a["x"]), int(a["y"]))
        if name == "desk_click":
            return browser.desk_click(int(a["x"]), int(a["y"]), int(a.get("button", 1) or 1))
        if name == "desk_type":
            return browser.desk_type(a.get("text", ""))
        if name == "desk_key":
            return browser.desk_key(a.get("key", "Return"))
        if name == "desk_screenshot":
            p = browser.desk_screenshot(f"{cid}_desk{len(ev['screenshots'])}")
            ev["screenshots"].append(p)
            return f"saved {p}"
        if name == "is_visible":
            v = browser.is_visible(a["selector"])
            ev["measurements"][f"{a['selector']}::visible"] = v
            return f"visible={v}"
        if name == "desk_record":
            rec = browser.desk_record(f"{cid}_clip{len(ev.get('clips', []))}", int(a.get("duration_ms", 1500)))
            ev.setdefault("clips_pending", 0)
            ev["clips_pending"] += 1
            return "recording started" if not rec.get("error") else f"ERROR: {rec['error']}"
    except KeyError as e:
        return f"ERROR: missing argument {e}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
    return _run_tool(browser, name, a, check, ev)


def _trigger_window_ms(check: dict) -> int:
    waits = sum(p[1]["ms"] for p in (parse_step(s) for s in check.get("steps", [])) if p and p[0] == "wait_ms")
    rng = parse_range_ms(check.get("expected") or "", check.get("criteria") or "")
    return int(min(6000, max(1200, waits + 600, (rng[1] + 600) if rng else 0)))


def _deterministic_steps(check: dict, browser, ev: dict) -> None:
    desk = getattr(browser, "desktop", False)
    recorded = False
    for st in check.get("steps", []):
        p = parse_step(st)
        if not p:
            continue
        tool, args = p
        if desk and tool in ("hover", "click", "type_text", "press"):
            if check.get("kind") in ("visual", "timing") and not recorded:
                _desk_tool(browser, "desk_record", {"duration_ms": _trigger_window_ms(check)}, check, ev)
                recorded = True
            if tool in ("hover", "click"):
                loc = browser.locate(args["selector"])
                if loc is None:
                    ev["actions"].append({"tool": "locate", "args": args, "result": "ERROR: not found", "via": "parser"})
                    continue
                tool, args = ("desk_move" if tool == "hover" else "desk_click"), {"x": loc["x"], "y": loc["y"]}
            elif tool == "type_text":
                tool = "desk_type"
            else:
                tool, args = "desk_key", {"key": args["key"]}
        elif desk and tool == "screenshot":
            tool = "desk_screenshot"
        res = _desk_tool(browser, tool, args, check, ev) if desk else _run_tool(browser, tool, args, check, ev)
        ev["actions"].append({"tool": tool, "args": args, "result": res[:200], "via": "parser"})


def focus_selector(check: dict) -> str | None:
    """The element a human would watch: first hover/click target in the steps, else check.selector."""
    for st in check.get("steps", []):
        p = parse_step(st)
        if p and p[0] in ("hover", "click"):
            return p[1]["selector"]
    return check.get("selector")


def trivial_steps(check: dict) -> bool:
    """True when the steps are only page loads (nothing for an LLM to interpret)."""
    return all((parse_step(s) or ("?",))[0] == "goto" for s in check.get("steps", []))


def user_sim(check: dict, browser, use_llm: bool | str = True) -> dict:
    """The preset slave: Lightning executes check.steps through tools; returns evidence.

    On a desktop (headed Chromium on Xvfb) it acts like a person: locate -> desk_move/desk_click with the real
    pointer via xdotool, records clips with ffmpeg. Headless: Playwright tools.
    use_llm: True = always Lightning; False = deterministic parser; "auto" = Lightning only when the steps
    involve interaction (hover/click/type/...), parser for plain 'goto /' checks (saves ~40 requests/round).
    """
    if check.get("kind") in NO_BROWSER_KINDS or browser is None:
        return {"actions": [], "measurements": {}, "screenshots": [], "frames": [], "clips": [], "via": "none",
                "desktop": False}
    if use_llm == "auto":
        use_llm = not trivial_steps(check)
    desk = getattr(browser, "desktop", False)
    ev = {"actions": [], "measurements": {}, "screenshots": [], "frames": [], "clips": [], "via": "llm",
          "desktop": desk}
    ok = False
    if use_llm:
        if desk:
            sys = ("You are a precise QA tester sitting at a real Linux desktop with a browser open full-screen. "
                   "Execute the given steps EXACTLY, in order, like a person: use goto for 'goto /'; to hover or "
                   "click an element first call locate(selector) and then desk_move / desk_click at the returned "
                   "x,y; 'wait N' = wait_ms. If the check is about motion, timing or appearance after an "
                   "interaction, call desk_record(duration_ms ~ total waits + 800) right BEFORE the triggering "
                   "hover/click. After the steps, if a selector+property are given call measure_css once, then "
                   "desk_screenshot once, then reply 'done' with no tool call. Do not improvise extra steps.")
            tools = DESK_TOOLS
        else:
            sys = ("You are a precise QA user-simulator driving a headless browser through tools. Execute the given "
                   "steps EXACTLY, in order, one tool call per step (use goto for 'goto /'). After the steps, if a "
                   "selector+property are given call measure_css once, then take one screenshot, then reply 'done' "
                   "with no tool call. Do not improvise extra steps.")
            tools = USERSIM_TOOLS
        user = (f"Check {check['id']} [{check.get('kind')}]: {check['title']}\nSteps:\n" +
                "\n".join(f"{i + 1}. {creds.resolve_templates(s)[0]}" for i, s in enumerate(check.get("steps", []))) +
                f"\nselector: {check.get('selector')}\nproperty: {check.get('property')}")
        messages = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        calls = 0
        try:
            while calls < USERSIM_MAX_CALLS:
                msg, _ = llm.chat_tools(llm.LIGHTNING, messages, tools, agent=f"usersim:{check['id']}",
                                        max_tokens=1000, temperature=0.2)
                messages.append(_assistant_dict(msg))
                if not msg.tool_calls:
                    break
                for tc in msg.tool_calls:
                    calls += 1
                    a = _args(tc)
                    fn = _desk_tool if desk else _run_tool
                    res = fn(browser, tc.function.name, a, check, ev)
                    ev["actions"].append({"tool": tc.function.name, "args": a, "result": res[:200]})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
            ok = any(x["tool"] == "goto" and not x["result"].startswith("ERROR") for x in ev["actions"])
        except Exception as e:  # noqa: BLE001
            print(f"[usersim] {check['id']} LLM failed: {e}", flush=True)
    if not ok:
        if desk:
            browser.finish_recordings()
        ev["via"] = "parser"
        ev["actions"] = []
        ev["screenshots"] = []
        _deterministic_steps(check, browser, ev)
    if desk:
        focus = None
        if getattr(browser, "_recs", True):  # only locate when a clip is pending
            try:
                focus = browser.focus_box(focus_selector(check))
            except Exception:  # noqa: BLE001
                focus = None
        ev["clips"] = [c for c in browser.finish_recordings(focus=focus) if not c.get("error")]
        ev.pop("clips_pending", None)
    if not ev["screenshots"]:
        ev["screenshots"].append(browser.desk_screenshot(f"{check['id']}_final") if desk
                                 else browser.screenshot(f"{check['id']}_final"))
    return ev


# ============================================================== verifier ==

EASINGS = {"ease": (0.25, 0.1, 0.25, 1.0), "linear": (0.0, 0.0, 1.0, 1.0), "ease-in": (0.42, 0.0, 1.0, 1.0),
           "ease-out": (0.0, 0.0, 0.58, 1.0), "ease-in-out": (0.42, 0.0, 0.58, 1.0)}
_HEX = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b")
_DUR = re.compile(r"(-?\d*\.?\d+)\s*(ms|s)\b")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(ms|s)\b", re.I)


def parse_durations_ms(s: str) -> list[float]:
    return [float(n) * (1000 if u == "s" else 1) for n, u in _DUR.findall(s or "")]


def parse_beziers(s: str) -> list[tuple[float, ...]]:
    out = [tuple(float(x) for x in m) for m in
           re.findall(r"cubic-bezier\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", s or "")]
    for name, b in EASINGS.items():
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", s or ""):
            out.append(b)
    return out


def parse_range_ms(*texts: str) -> tuple[float, float] | None:
    for t in texts:
        m = _RANGE.search(t or "")
        if m:
            k = 1000 if m.group(3).lower() == "s" else 1
            return float(m.group(1)) * k, float(m.group(2)) * k
    return None


def _colors_close(a: str | None, b: str | None, tol: int = 2) -> bool:
    a, b = normalize_color(a), normalize_color(b)
    if not a or not b or not a.startswith("#") or not b.startswith("#") or len(a) != len(b):
        return a == b
    return all(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) <= tol for i in range(1, len(a), 2))


def compare(prop: str, expected: str, actual: str | None) -> tuple[bool, str]:
    """Deterministic dom comparison. Returns (passed, explanation)."""
    prop = (prop or "").lower()
    exp = (expected or "").strip()
    if actual is None:
        return False, "element not found"
    act = normalize_value(actual) or ""
    e_low = exp.lower()
    # color: exact hex after normalization
    if _HEX.fullmatch(exp) or e_low.startswith("rgb"):
        want = normalize_color(exp)
        got = normalize_color(act)
        return got == want, f"got {got}, want {want}"
    if e_low in ("visible", "hidden"):
        return act.lower() == e_low, f"got {act}"
    if "font-family" in prop:
        norm = lambda s: re.sub(r"[\"']", "", s).lower()
        first = norm(exp).split(",")[0].strip()
        return first in norm(act), f"got {act!r}"
    want_b = parse_beziers(exp)
    want_d = parse_durations_ms(exp)
    rng = parse_range_ms(exp)
    if want_b or want_d or rng:
        got_d = parse_durations_ms(act)
        got_b = parse_beziers(act)
        ok, why = True, []
        if rng:
            lo, hi = rng
            hit = any(lo <= d <= hi for d in got_d)
            ok &= hit
            why.append(f"durations {got_d} vs range {lo:g}-{hi:g}ms")
        elif want_d:
            hit = all(any(abs(g - w) <= 10 for g in got_d) for w in want_d)
            ok &= hit
            why.append(f"durations {got_d} vs {want_d}")
        if want_b:
            hit = all(any(all(abs(x - y) <= 0.1 for x, y in zip(w, g)) for g in got_b) for w in want_b)
            ok &= hit
            why.append(f"easing {got_b} vs {want_b}")
        return ok, "; ".join(why) + f" (raw {act!r})"
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*px", e_low)
    if m:
        g = re.match(r"(-?\d+(?:\.\d+)?)px", act)
        if not g:
            return False, f"got {act!r}"
        return abs(float(g.group(1)) - float(m.group(1))) <= 2, f"got {act}"
    if re.fullmatch(r"-?\d+(?:\.\d+)?", e_low):
        try:
            return abs(float(act) - float(e_low)) <= 0.05, f"got {act}"
        except ValueError:
            return False, f"got {act!r}"
    return (act.lower() == e_low or e_low in act.lower()), f"got {act!r}"


TEXT_PROPS = ("textcontent", "text", "innertext")


def compare_text(expected: str | None, actual: str | None) -> tuple[bool, str]:
    """dom property textContent: 'contains: X' (case-insensitive) | 'non-empty' | exact (case-insensitive)."""
    if actual is None:
        return False, "element not found"
    act = re.sub(r"\s+", " ", actual).strip()
    shown = repr(act[:120])
    exp, _ = creds.resolve_templates((expected or "").strip())
    low = exp.lower()
    if low in ("", "non-empty", "nonempty", "not empty", "non empty", "present"):
        return bool(act), f"got {shown} (want non-empty)"
    m = re.match(r"^contains\s*:?\s*(.+)$", exp, re.I | re.S)
    if m:
        want = m.group(1).strip().strip("`'\"")
        return want.lower() in act.lower(), f"got {shown} (want contains {want!r})"
    m = re.match(r"^(?:matches|regex)\s*:?\s*(.+)$", exp, re.I | re.S)
    if m:
        try:
            return bool(re.search(m.group(1).strip(), act, re.I)), f"got {shown} (want /{m.group(1).strip()}/)"
        except re.error:
            pass
    return act.lower() == low or low in act.lower(), f"got {shown} (want {exp!r})"


def _timing(check: dict, browser) -> dict:
    """Re-run the triggering steps with an in-page rAF sampler + screenshot burst; measure settle time."""
    sel = check.get("selector") or "body"
    prop = check.get("property")
    expected = (check.get("expected") or "").strip()
    rng = parse_range_ms(check.get("expected") or "", check.get("criteria") or "")
    steps = [p for p in (parse_step(s) for s in check.get("steps", [])) if p]
    browser.goto(creds.resolve_templates(next((a["path"] for t, a in steps if t == "goto"), "/"))[0])
    browser.park_mouse([a["selector"] for t, a in steps if "selector" in a] + [sel])
    browser.wait_ms(100)
    browser.start_sampler(sel, prop)
    browser.wait_ms(50)
    window = sum(a["ms"] for t, a in steps if t == "wait_ms")
    window = max(window, (rng[1] if rng else 500) + 400, 400)
    desk = getattr(browser, "desktop", False)
    rec = None
    if desk:  # pixel evidence from the real screen (x11grab), not page.screenshot (perturbs :hover when headed)
        rec = browser.desk_record(f"{check['id']}_timing", int(window) + 400)
        rec_t0 = time.time()
    browser.mark_trigger()
    trig_wall = time.time()
    for t, a in steps:
        if t in ("hover", "click", "press", "type_text"):
            try:
                if desk:  # real pointer/keyboard: CDP input would be overridden by the X pointer position
                    if t in ("hover", "click"):
                        loc = browser.locate(a["selector"])
                        if loc is None:
                            raise RuntimeError(f"no element matches {a['selector']}")
                        (browser.desk_move if t == "hover" else browser.desk_click)(loc["x"], loc["y"])
                    elif t == "press":
                        browser.desk_key(a["key"])
                    else:
                        browser.desk_type(a["text"])
                else:
                    getattr(browser, t)(**a)
            except Exception as e:  # noqa: BLE001
                return {"passed": False, "score": 0.0, "evidence": f"trigger {t} failed: {str(e)[:120]}"}
    clip = None
    if rec is not None and not rec.get("error"):
        browser.wait_ms(int(window))
        data = browser.read_sampler()
        try:
            focus = browser.focus_box(focus_selector(check))
        except Exception:  # noqa: BLE001
            focus = None
        clip = next((c for c in browser.finish_recordings(focus=focus)
                     if c["name"] == rec["name"] and not c.get("error")), None)
        frames, fps = [], 0
        if clip:
            try:
                frames, fps = browser.clip_frames(clip["mp4"], sel, 30, prefix=f"{check['id']}_t"), 30
                lead_ms = (trig_wall - rec_t0) * 1000  # clip starts this long before the trigger
                frames = [(t - lead_ms, p, b) for t, p, b in frames if t >= lead_ms]
            except Exception as e:  # noqa: BLE001
                print(f"[timing] clip frames failed: {e}", flush=True)
        shot = clip["sheet"] if clip and clip.get("sheet") else browser.desk_screenshot(f"{check['id']}_after")
    else:
        if rec is not None:
            browser.finish_recordings()
        frames, fps = browser.record_frames(sel, int(window), 30, prefix=f"{check['id']}_f")
        data = browser.read_sampler()
        shot = browser.screenshot(f"{check['id']}_after")
    samples = data.get("samples") or []
    trig = data.get("trig") or data.get("pre") or (samples[0][0] if samples else 0)
    want_visible = expected.lower() == "visible" or (prop or "").lower() in ("visibility", "opacity", "display") \
        and expected.lower() in ("visible", "")
    if want_visible:
        match = lambda s: bool(s[2])
        target_desc = "visible"
    else:
        target = expected or (samples[-1][1] if samples else None)
        if target and (_HEX.fullmatch(target) or target.startswith("rgb")):
            match = lambda s: _colors_close(s[1], target)
        else:
            match = lambda s: (normalize_value(s[1]) or "").strip() == (normalize_value(target) or "").strip()
        target_desc = normalize_color(target) if target else "?"
    reach = None
    for i, s in enumerate(samples):
        if s[0] < trig:
            continue
        if match(s) and all(match(x) for x in samples[i:]):
            reach = s[0] - trig
            break
    settle_px = frame_settle_ms(frames)
    final_val = normalize_value(samples[-1][1]) if samples else None
    ev = (f"rAF {len(samples)} samples; target {target_desc} reached at "
          f"{'%.0fms' % reach if reach is not None else 'never'} after trigger; final={final_val}"
          f"{' visible' if samples and samples[-1][2] else ''}; pixel-settle≈"
          f"{'%.0fms' % settle_px if settle_px is not None else '?'} ({len(frames)} frames @ {fps}fps"
          f"{', desktop clip ' + Path(clip['mp4']).name if clip else ''})")
    if reach is None:
        passed = False
    elif rng:
        # ease-out curves hit the final integer color a little before the nominal duration
        passed = rng[0] - max(20.0, 0.25 * rng[0]) <= reach <= rng[1] + 20.0
        ev += f"; want {rng[0]:g}-{rng[1]:g}ms"
    else:
        passed = True
    return {"passed": passed, "score": 1.0 if passed else 0.0, "evidence": ev, "screenshot": shot,
            "reach_ms": reach, "frames_fps": fps, "timing_clip": clip}


_MOTION_TOOLS = ("hover", "click", "type_text", "press")


def involves_motion(check: dict) -> bool:
    """True when the steps interact (hover/click/type/press) or the rubric talks about motion."""
    if any((parse_step(st) or ("?",))[0] in _MOTION_TOOLS for st in check.get("steps", [])):
        return True
    return bool(re.search(r"\b(hover|click|animat|transition|motion|fade|slide|press)", 
                          (check.get("criteria") or "") + " " + check.get("title", ""), re.I))


def visual_media(check: dict, evidence: dict, browser) -> tuple[dict, str, str]:
    """Pick exactly ONE piece of media for the judge: native video (if the endpoint accepts it) or the
    contact sheet when the check involves motion and a clip exists, else a (desktop) screenshot.
    Returns (content part, description, path of the image/video judged)."""
    clip = (evidence.get("clips") or [None])[-1] or {}
    if clip and involves_motion(check):
        mp4 = clip.get("mp4")
        # Native video is opt-in (JUDGE_VIDEO=1): Crusoe's SUPER_VL accepts video_url but spends only ~650 prompt
        # tokens on a clip (a few low-res frames) and missed a 47px button that the focus-cropped contact sheet
        # (≈2.7k tokens) judged correctly (0.00 vs 0.95 on the same clip, 2026-10-17).
        if mp4 and Path(mp4).exists() and os.environ.get("JUDGE_VIDEO", "0") == "1":
            if llm.VIDEO_OK is None and os.environ.get("VIDEO_PROBE", "1") == "1":
                llm.probe_video()  # once per process
            if llm.VIDEO_OK and clip.get("focus"):
                try:
                    from browser import crop_video
                    mp4 = crop_video(mp4, clip["focus"])
                except Exception as e:  # noqa: BLE001
                    print(f"[judge] crop_video failed: {e}", flush=True)
            if llm.VIDEO_OK and Path(mp4).stat().st_size < 8_000_000:
                return (llm.video_part(Path(mp4).read_bytes()),
                        f"screen recording ({clip.get('duration_s')}s of the real desktop while the steps ran"
                        f"{', cropped to the region around the element being used' if clip.get('focus') else ''})",
                        mp4)
        if clip.get("sheet") and Path(clip["sheet"]).exists():
            return (llm.image_part(Path(clip["sheet"]).read_bytes()),
                    f"contact sheet (3x3 frames sampled evenly across a {clip.get('duration_s')}s screen recording "
                    "of the real desktop while the steps ran"
                    f"{', each frame cropped to the region around the element being used' if clip.get('focus') else ''}"
                    "; read left-to-right, top-to-bottom; timestamps in the top-left corner of each frame)",
                    clip["sheet"])
    shot = (evidence.get("screenshots") or [None])[-1]
    if not shot or not Path(shot).exists():
        shot = browser.desk_screenshot(f"{check['id']}_visual") if getattr(browser, "desktop", False) \
            else browser.screenshot(f"{check['id']}_visual")
    return llm.image_part(Path(shot).read_bytes()), "screenshot of the page", shot


def _visual(check: dict, evidence: dict, browser, spec: dict) -> dict:
    part, what, judged = visual_media(check, evidence, browser)
    shot = (evidence.get("screenshots") or [None])[-1] or (judged if judged.endswith(".png") else None)
    prompt = (f"You are a strict visual QA judge. Goal: {spec['goal']}\n"
              f"Rubric for check {check['id']} ({check['title']}): {check['criteria']}\n"
              "Design facts: " + "; ".join(spec.get("design_facts", [])[:12]) + "\n"
              f"Steps the tester performed: {' -> '.join(check.get('steps', []))}\n"
              f"You are given ONE {what}. Judge appearance AND, if it shows time, the motion/transitions. "
              "Score it 0-1 against the rubric (1 = indistinguishable from the real thing, "
              "0.7 = clearly recognisable with minor issues, <0.5 = does not read as it). "
              'Reply JSON only: {"score": float, "pass": bool, "reason": "one or two sentences naming concrete defects"}')
    try:
        msgs = [{"role": "user", "content": [{"type": "text", "text": prompt}, part]}]
        r = llm.chat_json(llm.SUPER_VL, msgs, agent=f"judge:{check['id']}", max_tokens=600, temperature=0.2)
        score = float(r.get("score", 0))
        reason = str(r.get("reason", ""))[:400]
    except Exception as e:  # noqa: BLE001
        score, reason = 0.0, f"judge error: {e}"
    thr = float(check.get("threshold") or 0.7)
    return {"passed": score >= thr, "score": round(score, 2),
            "evidence": f"SuperVL score {score:.2f} (threshold {thr}, judged {what.split(' (')[0]}): {reason}",
            "screenshot": shot, "judged": judged}


# ------------------------------------------------------------ http checks --

_HTTP_STATUS = re.compile(r"^(?:http\s+)?status(?:\s*code)?\s*(?:==|=|:|is)?\s*([1-5](?:\d\d|xx))\s*$", re.I)
_HTTP_LAT = re.compile(r"^latency\s*(<=|<)\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$", re.I)
_HTTP_TXT = re.compile(r"^(json_contains|contains)\s*:?\s*(.+)$", re.I | re.S)


def parse_http_expected(expected: str | None) -> tuple[str, object]:
    """'status 200' -> ('status','200'); 'contains: A100' -> ('contains','A100');
    'latency < 2000ms' -> ('latency', 2000.0); 'json_contains: gpu' -> ('json_contains','gpu').
    Empty -> ('status','2xx'); a bare 3-digit code -> status; anything else -> contains."""
    e = (expected or "").strip()
    if not e:
        return "status", "2xx"
    if re.fullmatch(r"[1-5]\d\d", e):
        return "status", e
    m = _HTTP_STATUS.match(e)
    if m:
        return "status", m.group(1).lower()
    m = _HTTP_LAT.match(e)
    if m:
        return "latency", float(m.group(2)) * (1000 if (m.group(3) or "ms").lower() == "s" else 1)
    m = _HTTP_TXT.match(e)
    if m:
        return m.group(1).lower(), m.group(2).strip().strip("`'\"")
    return "contains", e.strip("`'\"")


def _status_ok(status: int | None, want: str) -> bool:
    if status is None:
        return False
    if want.endswith("xx"):
        return str(status)[0] == want[0]
    return str(status) == want


def _json_texts(body: str) -> list[str]:
    """Body as JSON (or NDJSON, e.g. ntfy /json?poll=1) re-serialised for substring search."""
    out = []
    try:
        out.append(json.dumps(json.loads(body), ensure_ascii=False))
    except (json.JSONDecodeError, ValueError):
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    out.append(json.dumps(json.loads(line), ensure_ascii=False))
                except (json.JSONDecodeError, ValueError):
                    pass
    return out


def evaluate_http(op: str, arg, r: dict) -> tuple[bool, str]:
    """Pure evaluation of one parsed expectation against a response dict {status, body, latency_ms, error}."""
    st, body, lat = r.get("status"), r.get("body") or "", r.get("latency_ms")
    if r.get("error") and st is None:
        return False, f"request failed: {r['error']}"
    if op == "status":
        return _status_ok(st, str(arg)), f"status {st} (want {arg})"
    if op == "latency":
        ok = st is not None and st < 400 and lat is not None and lat < float(arg)
        return ok, f"latency {lat:.0f}ms (want < {float(arg):g}ms, status {st})"
    if op == "contains":
        hit = str(arg).lower() in body.lower()
        return hit and st is not None and st < 400, f"{'found' if hit else 'NOT found'} {str(arg)[:80]!r} (status {st})"
    if op == "json_contains":
        texts = _json_texts(body)
        if not texts:
            return False, f"body is not JSON (status {st})"
        hit = any(str(arg).lower() in t.lower() for t in texts)
        return hit and st < 400, f"JSON {'contains' if hit else 'does NOT contain'} {str(arg)[:80]!r} (status {st})"
    return False, f"unknown expectation {op}"


def _poll_url(url: str) -> str:
    """ntfy /json and /sse stream forever; force poll mode so the request returns."""
    if re.match(r"^https?://(?:www\.)?ntfy\.sh/[^/?]+/(?:json|sse|raw)(?:\?|$)", url) and "poll=" not in url:
        url += ("&" if "?" in url else "?") + "poll=1"
    return url


def http_request(url: str, method: str = "GET", timeout: float = HTTP_TIMEOUT) -> dict:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, method=method.upper(),
                                 headers={"User-Agent": "donedone-verifier/1.0", "Accept": "*/*"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(400_000)
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400_000)
        except Exception:  # noqa: BLE001
            body = b""
        status = e.code
    except Exception as e:  # noqa: BLE001
        return {"status": None, "body": "", "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    return {"status": status, "body": body.decode("utf-8", errors="replace"),
            "latency_ms": (time.perf_counter() - t0) * 1000, "error": None}


def _display_url(url: str) -> str:
    topic = os.environ.get("NTFY_TOPIC")
    if topic and len(topic) >= 4:
        url = url.replace(topic, "<ntfy-topic>")
    return creds.redact(url)


def verify_http(check: dict) -> dict:
    """kind 'http': resolve ${DEPLOY_URL}/${NTFY_TOPIC}, request with a 20 s timeout, evaluate `expected`."""
    url_t = (check.get("url") or "").strip()
    if not url_t:  # tolerate specs that put the URL in a step or the criteria
        for st in check.get("steps", []) + [check.get("criteria") or ""]:
            m = re.search(r"(\$\{DEPLOY_URL\}\S*|https?://\S+)", st)
            if m:
                url_t = m.group(1).rstrip(".,)")
                break
    url_t = url_t or "${DEPLOY_URL}"
    url, unres = creds.resolve_templates(url_t)
    exp, unres2 = creds.resolve_templates(check.get("expected") or "")
    names = sorted(set(unres + unres2))
    if names:
        blockers = sorted({b for b in (creds.blocking_secret_for(v) for v in names) if b})
        if blockers:
            ev = (f"missing credential {', '.join(blockers)}: cannot resolve "
                  + ", ".join("${" + n + "}" for n in names) + f" in {url_t}")
        else:
            ev = ("unresolved " + ", ".join("${" + n + "}" for n in names)
                  + (": builder has not written site/deploy.json {\"url\": \"https://...\"} yet (deploy did not "
                     "happen or failed)" if "DEPLOY_URL" in names else ""))
        return {"passed": False, "score": 0.0, "evidence": ev}
    if not re.match(r"^https?://", url):
        return {"passed": False, "score": 0.0, "evidence": f"url is not absolute http(s): {_display_url(url)!r}"}
    url = _poll_url(url)
    method = (check.get("method") or "GET").upper()
    op, arg = parse_http_expected(exp)
    r = http_request(url, method)
    ok, why = evaluate_http(op, arg, r)
    if not ok and (op == "latency" or r.get("status") is None or (r.get("status") or 0) >= 500):
        r2 = http_request(url, method)  # one retry: cold starts / transient network
        ok2, why2 = evaluate_http(op, arg, r2)
        if ok2 or (r2.get("latency_ms") or 1e9) < (r.get("latency_ms") or 1e9):
            r, ok, why = r2, ok2, why2 + " (retry)"
    if not ok and op in ("contains", "json_contains") and "ntfy.sh/" in url:
        # ntfy's poll endpoint lags a just-published message by a second or two
        for i in range(3):
            time.sleep(2.0)
            r2 = http_request(url, method)
            ok2, why2 = evaluate_http(op, arg, r2)
            if ok2:
                r, ok, why = r2, ok2, why2 + f" (after {2 * (i + 1)} s ntfy lag)"
                break
    snippet = re.sub(r"\s+", " ", r.get("body") or "")[:180]
    ev = (f"{method} {_display_url(url)} -> {r.get('status') or r.get('error')} in {r['latency_ms']:.0f}ms; {why}"
          f"; body: {_display_url(snippet)!r}")
    return {"passed": bool(ok), "score": float(bool(ok)), "evidence": ev[:600], "http_status": r.get("status"),
            "latency_ms": round(r["latency_ms"], 1)}


def verify(check: dict, evidence: dict, browser, spec: dict) -> dict:
    kind = check.get("kind")
    shot = (evidence.get("screenshots") or [None])[-1]
    res: dict
    if kind == "gpu":
        res = gpu.verify_gpu(check, SITE.resolve().parent)
        return {"id": check["id"], "title": check.get("title", ""), "kind": kind, "passed": bool(res.get("passed")),
                "score": res.get("score"), "evidence": res.get("evidence", ""), "screenshot": None,
                "clip": None, "sheet": None, "harness": res.get("harness")}
    try:
        if browser is None and (kind in BROWSER_KINDS or (kind == "code" and code_needs_browser(check))):
            raise RuntimeError("browser not started for this job (spec had no browser checks)")
        if kind == "dom":
            sel, prop, exp = check.get("selector"), check.get("property") or "", check.get("expected") or ""
            if not sel:
                res = {"passed": False, "score": 0.0, "evidence": "dom check without selector"}
            elif prop.replace("-", "").replace("_", "").lower() in TEXT_PROPS:
                text = browser.text_content(sel)
                ok, why = compare_text(exp, text)
                res = {"passed": ok, "score": float(ok), "evidence": f"{sel} textContent: {why}"}
            elif prop.lower() in ("visible", "visibility") and exp.lower() == "visible":
                vis = browser.is_visible(sel)
                res = {"passed": vis, "score": float(vis), "evidence": f"{sel} visible={vis}"}
            else:
                actual = browser.measure_css(sel, prop)
                ok, why = compare(prop, exp, actual)
                res = {"passed": ok, "score": float(ok), "evidence": f"{prop}: {why}"}
        elif kind == "timing":
            res = _timing(check, browser)
            shot = res.pop("screenshot", shot)
        elif kind == "http":
            res = verify_http(check)
        elif kind == "code":
            target = file_exists_target(check)
            if target:
                f = SITE / target
                ok = f.is_file()
                note = ""
                if ok and f.name == "deploy.json":
                    url = creds.read_deploy_url(SITE)
                    note = f" url={url}" if url else " (no url field!)"
                    ok = bool(url and url.startswith("http"))
                res = {"passed": ok, "score": float(ok), "evidence": f"site/{target} exists={f.is_file()}{note}"}
            else:
                if not evidence.get("actions"):
                    browser.goto("/")
                browser.wait_ms(300)
                errs = browser.console_errors()
                res = {"passed": not errs, "score": float(not errs),
                       "evidence": "no console errors" if not errs else f"{len(errs)} console errors: " + " | ".join(errs)[:300]}
        elif kind == "visual":
            res = _visual(check, evidence, browser, spec)
            shot = res.pop("screenshot", shot)
        else:
            res = {"passed": False, "score": 0.0, "evidence": f"unknown kind {kind}"}
    except Exception as e:  # noqa: BLE001
        res = {"passed": False, "score": 0.0, "evidence": f"verifier error: {type(e).__name__}: {str(e)[:200]}"}
    clip = res.pop("timing_clip", None) or (evidence.get("clips") or [None])[-1] or {}
    if clip and kind == "timing" and Path(clip["mp4"]).name not in res.get("evidence", ""):
        res["evidence"] = res.get("evidence", "") + f"; desktop clip {Path(clip['mp4']).name}"
    return {"id": check["id"], "title": check.get("title", ""), "kind": kind, **res, "screenshot": shot,
            "clip": clip.get("mp4"), "sheet": clip.get("sheet")}


# ============================================================== replan ==

def replan(spec: dict, history: list[dict], failures: list[dict], screenshot: str | None = None) -> dict:
    """Super VL rewrites the builder brief from failure evidence; may flag unverifiable checks."""
    fl = "\n".join(f"- {f['id']} [{f['kind']}] {f['title']} | selector {f.get('selector')} | property "
                   f"{f.get('property')} | expected {f.get('expected') or f.get('criteria')} | EVIDENCE: {f.get('evidence')}"
                   for f in failures)
    hist = "\n".join(f"round {h['round']}: score {h['score']} decision {h.get('decision')} failed {h.get('failed')}"
                     for h in history)
    where = ("measured by a fixed harness running kernel.py on a real GPU over SSH" if gpu_mode(spec)
             else "computed values measured in Chromium, desktop screenshots/clips")
    prompt = (
        "You are the tech lead of an autonomous build loop. A builder model keeps failing the same automated "
        f"checks. Diagnose WHY from the evidence ({where}) and write a new, "
        "short, concrete brief for the builder: exact CSS rules/selectors/JS to add or change, one bullet per "
        "failing check. Also list a check id under 'unverifiable' ONLY if it is impossible to verify in this "
        "sandbox (Chromium on a Linux desktop serving a static page, outbound HTTPS with the injected "
        "credentials (Vercel deploys, ntfy), plus an SSH-attached GPU when the job has one), e.g. it needs a real "
        "native macOS app; "
        "failing because the builder got it wrong is NOT unverifiable.\n"
        f"GOAL: {spec['goal']}\nHISTORY:\n{hist}\nFAILING CHECKS:\n{fl}\n"
        'Reply JSON: {"diagnosis": "...", "brief": "- ...\\n- ...", "unverifiable": [{"id": "C..", "reason": "..."}]}')
    imgs = []
    if screenshot and Path(screenshot).exists():
        imgs = [Path(screenshot).read_bytes()]
    try:
        r = llm.chat_json(llm.SUPER_VL, [llm.user_with_images(prompt, imgs)], agent="replan",
                          max_tokens=2000, temperature=0.4)
    except Exception as e:  # noqa: BLE001
        return {"brief": "Fix each failing check exactly as specified:\n" + fl, "unverifiable": [],
                "diagnosis": f"replan error: {e}"}
    valid = {f["id"] for f in failures}
    unv = [u for u in (r.get("unverifiable") or []) if isinstance(u, dict) and u.get("id") in valid]
    brief = r.get("brief") or ""
    if isinstance(brief, list):
        brief = "\n".join(map(str, brief))
    return {"brief": str(brief)[:4000], "unverifiable": unv, "diagnosis": str(r.get("diagnosis", ""))[:800]}
