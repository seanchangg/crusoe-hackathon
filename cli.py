"""donedone CLI: goal -> spec -> run -> watch, against the donedone HTTP server.

Server base URL comes from DONEDONE_URL (default http://localhost:8787).

  uv run python cli.py plan "<goal>" [--out spec.json] [--image ref.png] [--offline]
  uv run python cli.py run spec.json [--ntfy topic] [--watch]
  uv run python cli.py status <job_id>
  uv run python cli.py stop <job_id>
  uv run python cli.py go "<goal>" --yes [--ntfy topic]

To change the definition of done, edit spec.json by hand before `run`.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

console = Console()
TERMINAL = {"done", "escalated", "failed"}
POLL_S = 5


def base_url() -> str:
    return os.environ.get("DONEDONE_URL", "http://localhost:8787").rstrip("/")


def api(method: str, path: str, body: dict | None = None, timeout: float = 180.0) -> dict:
    url = base_url() + path
    try:
        r = httpx.request(method, url, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        console.print(f"[red]request to {url} failed:[/] {e}")
        sys.exit(2)
    if r.status_code >= 400:
        console.print(f"[red]{method} {path} -> HTTP {r.status_code}[/]\n{r.text[:2000]}")
        sys.exit(1)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


# ---------------------------------------------------------------- rendering

def render_spec(spec: dict) -> None:
    console.print(f"[bold]Goal:[/] {spec.get('goal', '')}")
    if spec.get("summary"):
        console.print(f"[dim]{spec['summary']}[/]")
    t = Table(title=f"Definition of done ({len(spec.get('checks', []))} checks)", show_lines=False)
    for col in ("id", "kind", "title", "expected"):
        t.add_column(col, overflow="fold")
    for c in spec.get("checks", []):
        exp = c.get("expected")
        if not exp and c.get("kind") == "visual":
            exp = f"score >= {c.get('threshold', 0.7)}"
        t.add_row(c.get("id", ""), c.get("kind", ""), c.get("title", ""), exp or "-")
    console.print(t)
    env = spec.get("environment", {}) or {}
    needs = [k.removeprefix("needs_") for k in ("needs_browser", "needs_desktop", "needs_macos", "needs_gpu")
             if env.get(k)]
    console.print(f"[bold]Environment needs:[/] {', '.join(needs) or 'none'}"
                  + (f"  packages: {', '.join(env['packages'])}" if env.get("packages") else ""))
    if env.get("devices"):
        console.print(f"[bold]Devices:[/] {', '.join(env['devices'])}")
    if env.get("secrets"):
        console.print(f"[bold]Secrets requested:[/] {', '.join(env['secrets'])}")
    if env.get("rationale"):
        console.print(f"[dim]{env['rationale']}[/]")


def render_run(res: dict) -> None:
    t = Table(title=f"Environment plan for job {res.get('job_id')}")
    for col in ("adapter", "mode", "status", "detail"):
        t.add_column(col, overflow="fold")
    for p in res.get("environment_plan", []):
        mode = "[green]REAL[/]" if p.get("real") else "[yellow]SIMULATED[/]"
        st = p.get("status", "")
        color = {"ready": "green", "simulated": "yellow", "failed": "red"}.get(st, "white")
        t.add_row(p.get("adapter", ""), mode, f"[{color}]{st}[/]", p.get("detail", ""))
    console.print(t)
    broker = next((p for p in res.get("environment_plan", []) if p.get("adapter") == "secrets_broker"), None)
    if broker:
        ci = broker.get("connect_info") or {}
        if ci.get("devices"):
            console.print(f"[bold]Devices:[/] {', '.join(ci['devices'])}")
        render_ledger(ci.get("ledger") or [])
    console.print(f"[bold]sandbox:[/] {res.get('sandbox_name')}")
    console.print(f"[bold]live_url:[/] {res.get('live_url')}")
    console.print(f"[bold]job_id:[/] {res.get('job_id')}   (cli.py status {res.get('job_id')})")


def render_ledger(ledger: list[dict]) -> None:
    """Masked credential ledger from the secrets broker (values are already masked server-side)."""
    if not ledger:
        return
    t = Table(title="Credentials injected")
    for col in ("name", "status", "source", "masked", "used_for"):
        t.add_column(col, overflow="fold")
    for r in ledger:
        st = r.get("status", "")
        color = "green" if st == "injected" else "red"
        t.add_row(r.get("name", ""), f"[{color}]{st}[/]", r.get("source", ""), r.get("masked", "") or "-",
                  r.get("used_for", ""))
    console.print(t)


def render_job(job: dict) -> None:
    st = job.get("status_json") or {}
    status = job.get("status", "unknown")
    color = {"done": "green", "escalated": "yellow", "failed": "red"}.get(status, "cyan")
    score = st.get("score")
    head = (f"[{color}]{status}[/]  phase={st.get('phase', '-')}  "
            f"round={st.get('round', '-')}/{st.get('max_rounds', '-')}  "
            f"score={score if score is not None else '-'}  "
            f"elapsed={st.get('elapsed_s', '-')}s")
    console.rule(f"job {job.get('job_id')}")
    console.print(head)
    if st.get("decision") or st.get("message"):
        console.print(f"decision: [bold]{st.get('decision', '-')}[/]  {st.get('message', '')}")
    hist = st.get("history") or []
    if hist:
        console.print("history: " + "  ".join(
            f"r{h.get('round')}:{h.get('score')}({h.get('decision')})" for h in hist))
    checks = st.get("checks") or []
    if checks:
        t = Table(show_header=True)
        for col in ("id", "kind", "result", "title", "evidence"):
            t.add_column(col, overflow="fold")
        for c in checks:
            p = c.get("passed")
            res = "[green]PASS[/]" if p else ("[red]FAIL[/]" if p is False else "[dim]...[/]")
            t.add_row(str(c.get("id", "")), str(c.get("kind", "")), res, str(c.get("title", "")),
                      str(c.get("evidence", ""))[:160])
        console.print(t)
    if job.get("live_url"):
        console.print(f"live: {job['live_url']}")


def watch(job_id: str) -> dict:
    while True:
        job = api("GET", f"/jobs/{job_id}", timeout=60)
        render_job(job)
        if job.get("status") in TERMINAL:
            return job
        time.sleep(POLL_S)


# ---------------------------------------------------------------- commands

def do_plan(goal: str, image: str | None, offline: bool) -> dict:
    img_bytes = Path(image).read_bytes() if image else None
    with console.status("Super VL is writing the definition of done..."):
        if offline:
            from dotenv import load_dotenv
            load_dotenv()
            from framework.compiler import compile_goal
            spec = compile_goal(goal, [img_bytes] if img_bytes else None).model_dump()
        else:
            body: dict = {"goal": goal}
            if img_bytes:
                body["reference_image_b64"] = base64.b64encode(img_bytes).decode()
            spec = api("POST", "/plan", body, timeout=300)
    return spec


def cmd_plan(a) -> None:
    spec = do_plan(a.goal, a.image, a.offline)
    render_spec(spec)
    Path(a.out).write_text(json.dumps(spec, indent=2))
    console.print(f"saved [bold]{a.out}[/]. Edit it if you want, then: uv run python cli.py run {a.out} --watch")


def start_run(spec: dict, ntfy: str | None) -> dict:
    body: dict = {"spec": spec}
    topic = ntfy or os.environ.get("NTFY_TOPIC")
    if topic:
        body["ntfy_topic"] = topic
    with console.status("provisioning environment..."):
        res = api("POST", "/run", body, timeout=300)
    render_run(res)
    if topic:
        console.print(f"push notifications -> https://ntfy.sh/{topic}")
    return res


def cmd_run(a) -> None:
    spec = json.loads(Path(a.spec).read_text())
    res = start_run(spec, a.ntfy)
    if a.watch:
        watch(res["job_id"])


def cmd_status(a) -> None:
    render_job(api("GET", f"/jobs/{a.job_id}", timeout=60))


def cmd_stop(a) -> None:
    res = api("POST", f"/jobs/{a.job_id}/stop", timeout=60)
    console.print(json.dumps(res, indent=2))


def cmd_go(a) -> None:
    spec = do_plan(a.goal, a.image, False)
    render_spec(spec)
    Path(a.out).write_text(json.dumps(spec, indent=2))
    if not a.yes:
        if input("Run this spec? [y/N] ").strip().lower() not in ("y", "yes"):
            console.print(f"saved {a.out}; not running.")
            return
    res = start_run(spec, a.ntfy)
    if a.no_watch:
        return
    watch(res["job_id"])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cli.py", description=(
        "donedone: goal -> self-written definition of done -> self-provisioned sandbox -> "
        "self-verifying agent loop. Server: $DONEDONE_URL (default http://localhost:8787)."))
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="compile a goal into a spec (checklist + environment)")
    sp.add_argument("goal")
    sp.add_argument("--out", default="spec.json")
    sp.add_argument("--image", help="optional reference screenshot (PNG)")
    sp.add_argument("--offline", action="store_true",
                    help="call framework.compiler directly (needs CRUSOE_API_KEY) instead of the server")
    sp.set_defaults(fn=cmd_plan)

    sr = sub.add_parser("run", help="provision the environment and start the loop for a spec")
    sr.add_argument("spec")
    sr.add_argument("--ntfy", help="ntfy.sh topic for push notifications (default $NTFY_TOPIC)")
    sr.add_argument("--watch", action="store_true", help=f"poll job status every {POLL_S}s until finished")
    sr.set_defaults(fn=cmd_run)

    ss = sub.add_parser("status", help="show job status")
    ss.add_argument("job_id")
    ss.set_defaults(fn=cmd_status)

    st = sub.add_parser("stop", help="stop a job and its sandbox")
    st.add_argument("job_id")
    st.set_defaults(fn=cmd_stop)

    sg = sub.add_parser("go", help="plan + run + watch in one shot")
    sg.add_argument("goal")
    sg.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    sg.add_argument("--ntfy")
    sg.add_argument("--image")
    sg.add_argument("--out", default="spec.json")
    sg.add_argument("--no-watch", action="store_true", help="return right after /run")
    sg.set_defaults(fn=cmd_go)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted (the job keeps running in its sandbox)[/]")


if __name__ == "__main__":
    main()
