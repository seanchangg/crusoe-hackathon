"""Done-Means-Done controller. Run as `python3 runner/loop.py` with cwd containing spec.json.

Each round: builder -> (user_sim + verify) per check -> weighted score -> decide():
  stop      all checks pass, or budget exhausted with score >= 0.8 ("done with caveats")
  continue  score improved over the best so far (or first round)
  replan    same failing set 2 rounds in a row / no improvement for 2 rounds / builder output >=90% identical
  escalate  2 replans did not improve, replan judged a check unverifiable, or budget exhausted < 0.8,
            or (after ONE failing round) a failure is caused by a missing credential / SSH key refused
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import difflib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import urllib.request  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import creds  # noqa: E402  (stdlib only)

OUT = Path(os.environ.get("OUT_DIR", "out"))
JOB_ID = os.environ.get("JOB_ID", "local")
LIVE_URL = os.environ.get("LIVE_URL", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "6") or 6)
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "30") or 30)
DONE_WITH_CAVEATS = 0.8


# ------------------------------------------------------------ decision --

def weighted_score(spec: dict, results: dict[str, bool]) -> float:
    """Same semantics as framework.spec.Spec.score."""
    total = sum(float(c.get("weight", 1.0)) for c in spec["checks"]) or 1.0
    got = sum(float(c.get("weight", 1.0)) for c in spec["checks"] if results.get(c["id"]))
    return round(got / total, 3)


def decide(history: list[dict], *, budget_left: bool, similarity: float | None = None) -> tuple[str, str]:
    """Pure controller decision for the latest round (history[-1]).

    history entries: {"round", "score", "failed": [ids], "decision"} (decision of earlier rounds set).
    Returns (decision, reason).
    """
    cur = history[-1]
    prev = history[:-1]
    score, failed = cur["score"], cur["failed"]
    if not failed:
        return "stop", "all checks pass"
    if not budget_left:
        if score >= DONE_WITH_CAVEATS:
            return "stop", f"budget exhausted at score {score} >= {DONE_WITH_CAVEATS}: done with caveats"
        return "escalate", f"budget exhausted at score {score} < {DONE_WITH_CAVEATS}"
    replan_idx = [i for i, h in enumerate(prev) if h.get("decision") == "replan"]
    if len(replan_idx) >= 2:
        i = replan_idx[-2]
        best_before = max(h["score"] for h in history[:i + 1])
        best_after = max(h["score"] for h in history[i + 1:])
        if best_after <= best_before:
            return "escalate", f"2 replans did not improve on best score {best_before}"
    best_prev = max((h["score"] for h in prev), default=None)
    if best_prev is None or score > best_prev:
        return "continue", "score improved" if best_prev is not None else "first round"
    if prev and set(prev[-1]["failed"]) == set(failed):
        return "replan", "same failing set two rounds in a row"
    if len(prev) >= 2:
        best_before_two = max((h["score"] for h in history[:-2]), default=-1)
        if max(prev[-1]["score"], score) <= best_before_two:
            return "replan", "no improvement for 2 rounds"
    if similarity is not None and similarity >= 0.9:
        return "replan", f"builder output {similarity:.0%} identical to last round"
    return "continue", "no improvement yet; one more try"


# ------------------------------------------------------------ credentials --

_DENIED = ("permission denied (publickey", "host key verification failed")
_BLOCKED_MARKERS = ("missing credential", "unresolved", "not configured", "unset", "permission denied",
                    "unauthorized", "forbidden", "-> 401", "-> 403", "no gpu attached", "invalid token",
                    "not authorized", "blocked")
_FAMILIES = {"VERCEL_": ("vercel", "deploy_url", "deploy"), "GPU_": ("gpu", "nvidia", "a100", "ssh", "lambda"),
             "LAMBDA_": ("gpu", "nvidia", "a100", "ssh", "lambda"),
             "NTFY": ("ntfy",), "GITHUB_": ("github",)}


def _check_text(f: dict) -> str:
    return " ".join(str(f.get(k) or "") for k in ("id", "title", "criteria", "url", "expected", "steps")).lower()


def _related(name: str, f: dict) -> bool:
    text = _check_text(f) + " " + str(f.get("evidence") or "").lower()
    if name.lower() in text:
        return True
    return any(name.upper().startswith(p) and any(w in text for w in words) for p, words in _FAMILIES.items())


def credential_blocker(failures: list[dict], ledger: list[dict] | None = None, *, builder_message: str = "",
                       ssh_denied: bool = False, devices: list[dict] | None = None) -> str | None:
    """Pure rule: is this round's failure caused by a missing credential or a refused/unreachable device?
    Returns a precise human ask (naming the secret/device) or None. Never includes secret values."""
    if not failures:
        return None
    ledger = creds.parse_ledger() if ledger is None else ledger
    by_name = {e["name"]: e for e in ledger}
    ids = lambda fs: ", ".join(f"{f['id']} ({f.get('title', '')})" for f in fs[:4])  # noqa: E731

    def ask_secret(name: str, fs: list[dict]) -> str:
        e = by_name.get(name, {})
        bits = f"{name}" + (f" (used for {e['used_for']})" if e.get("used_for") else "")
        hint = f" Hint: {e['hint']}." if e.get("hint") else ""
        return (f"BLOCKED on missing credential {bits}. Failing because of it: {ids(fs)}.{hint} "
                f"Please add {name} to the credential broker (source: {e.get('source') or 'your secret store'}) "
                "and re-run the job - I stopped instead of burning rounds on it.")

    def ask_ssh(fs: list[dict], why: str) -> str:
        user = os.environ.get("GPU_USER") or "ubuntu"
        failing = ids(fs) or "builder could not collect GPU data"
        return (f"BLOCKED on the GPU box: SSH refused ({why}). Failing because of it: {failing}. Please add the "
                f"public half of GPU_SSH_KEY_B64 to ~{user}/.ssh/authorized_keys on the host in GPU_HOST (or inject "
                "the right GPU_SSH_KEY_B64 / GPU_USER), check the box is running, and re-run.")

    # 1. explicit evidence
    for f in failures:
        ev = str(f.get("evidence") or "")
        low = ev.lower()
        if any(d in low for d in _DENIED):
            return ask_ssh([f], "Permission denied (publickey)")
        m = re.search(r"missing credential[s]?\s*:?\s*([A-Z][A-Z0-9_]+)", ev)
        if m:
            name = m.group(1)
            return ask_secret(name, [x for x in failures if _related(name, x)] or [f])
    # 2. builder reported it (it is told to say 'BLOCKED: ...' rather than fake data)
    bm = builder_message or ""
    gpu_fails = [f for f in failures if any(w in _check_text(f) for w in _FAMILIES["GPU_"])]
    if ssh_denied or any(d in bm.lower() for d in _DENIED):
        return ask_ssh(gpu_fails or failures, "Permission denied (publickey)")
    # 3. ledger says missing + a related failure whose evidence looks blocked
    for e in ledger:
        if e.get("status") == "injected" or creds.is_optional(e):
            continue
        name = e["name"]
        rel = [f for f in failures if _related(name, f)]
        blocked = [f for f in rel if name.lower() in _check_text(f)
                   or any(k in str(f.get("evidence") or "").lower() for k in _BLOCKED_MARKERS)]
        if blocked or (rel and name in bm and re.search(r"(?i)blocked|missing", bm)):
            return ask_secret(name, blocked or rel)
    # 4. a device the controller probed is unusable and a related check failed
    for d in devices or []:
        st = str(d.get("status") or "").lower()
        if d.get("kind") == "gpu" and ("denied" in st or "unreachable" in st) and gpu_fails:
            return ask_ssh(gpu_fails, f"device {d.get('name')}: {d.get('status')}")
    return None


# ------------------------------------------------------------ devices ----

def _gpu_probe_status() -> dict:
    if not os.environ.get("GPU_HOST"):
        return {"name": "gpu", "kind": "gpu", "status": "not configured", "detail": "GPU_HOST not injected"}
    if os.environ.get("DEVICE_PROBE", "1") != "1":
        return {"name": "gpu", "kind": "gpu", "status": "configured", "detail": "probe disabled"}
    try:
        import gpu
        if not gpu.ensure_ssh_client():
            return {"name": "gpu", "kind": "gpu", "status": "unreachable", "detail": "no ssh client in sandbox"}
        r = gpu.ssh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -2", timeout=30)
        out = (r.stdout or "").strip().replace("\n", "; ")
        err = (r.stderr or "").strip()
        if r.returncode == 0 and out:
            return {"name": "gpu", "kind": "gpu", "status": "reachable", "detail": out[:160]}
        if "permission denied" in err.lower():
            return {"name": "gpu", "kind": "gpu", "status": "ssh denied", "detail": "Permission denied (publickey)"}
        return {"name": "gpu", "kind": "gpu", "status": "unreachable",
                "detail": creds.redact(err.splitlines()[-1] if err else f"exit {r.returncode}")[:160]}
    except Exception as e:  # noqa: BLE001
        return {"name": "gpu", "kind": "gpu", "status": "unreachable", "detail": f"{type(e).__name__}: {e}"[:160]}


def build_devices(spec: dict | None, desktop: str | None, gpu_status: dict | None = None) -> list[dict]:
    """spec.environment.devices + desktop + gpu status, for status.json (no hostnames/secrets)."""
    env = (spec or {}).get("environment") or {}
    out = []
    gpu_ish = lambda t: any(w in t.lower() for w in ("gpu", "a100", "h100", "nvidia", "lambda", "cuda"))  # noqa: E731
    for d in env.get("devices") or []:
        if isinstance(d, dict):
            name = str(d.get("name") or d.get("kind") or d.get("type") or "device")
            kind = str(d.get("kind") or d.get("type") or "")
        else:
            name, kind = str(d), ""
        entry = {"name": name, "kind": kind or ("gpu" if gpu_ish(name) else "desktop" if "desktop" in name.lower()
                                                else "device"), "status": "declared", "detail": ""}
        if entry["kind"] == "gpu" and gpu_status:
            entry.update(status=gpu_status["status"], detail=gpu_status.get("detail", ""))
        elif entry["kind"] in ("desktop", "browser"):
            entry["status"] = desktop or "unknown"
        out.append(entry)
    if not any(e["kind"] in ("desktop", "browser") for e in out):
        out.append({"name": "desktop", "kind": "desktop", "status": desktop or "unknown", "detail": ""})
    if gpu_status and not any(e["kind"] == "gpu" for e in out) and \
            (os.environ.get("GPU_HOST") or env.get("needs_gpu")):
        out.append(dict(gpu_status))
    return out


# ------------------------------------------------------------ status ----

def desktop_state(raw: str | None) -> str:
    """Normalise to 'ready' | 'unavailable' | 'skipped' (bootstrap may say available/ready/...)."""
    r = (raw or "").strip().lower()
    if r in ("ready", "available", "on"):
        return "ready"
    if r == "skipped":
        return "skipped"
    return "unavailable"


class Status:
    def __init__(self, spec: dict | None):
        self.t0 = time.time()
        self.d = {"job_id": JOB_ID, "phase": "bootstrap", "round": 0, "max_rounds": MAX_ROUNDS,
                  "score": 0.0, "history": [], "checks": [], "decision": None, "message": "",
                  "goal": (spec or {}).get("goal", ""), "elapsed_s": 0, "updated_at": None,
                  "live_url": LIVE_URL, "novnc_url": os.environ.get("NOVNC_URL", ""),
                  "desktop": desktop_state(os.environ.get("DESKTOP")), "usage": {},
                  "secrets": creds.parse_ledger(), "deploy_url": creds.read_deploy_url(), "devices": []}
        self.d["devices"] = build_devices(spec, self.d["desktop"])
        if spec:
            self.d["checks"] = [{"id": c["id"], "title": c["title"], "kind": c["kind"], "passed": None,
                                 "score": None, "evidence": "", "screenshot": None} for c in spec["checks"]]

    def set(self, **kw) -> None:
        self.d.update(kw)
        self.write()

    def set_check(self, res: dict) -> None:
        for i, c in enumerate(self.d["checks"]):
            if c["id"] == res["id"]:
                self.d["checks"][i] = {k: res.get(k) for k in
                                       ("id", "title", "kind", "passed", "score", "evidence", "screenshot",
                                        "clip", "sheet", "judged")}
        self.write()

    def write(self) -> None:
        try:
            import llm
            self.d["usage"] = dict(llm.USAGE)
        except Exception:
            pass
        self.d["elapsed_s"] = round(time.time() - self.t0, 1)
        self.d["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        OUT.mkdir(parents=True, exist_ok=True)
        tmp = OUT / "status.json.tmp"
        tmp.write_text(json.dumps(self.d, indent=2))
        tmp.replace(OUT / "status.json")


def rel_shot(p: str | None) -> str | None:
    """Screenshot path relative to out/ so it resolves against LIVE_URL (shots/xxx.png)."""
    if not p:
        return None
    try:
        return str(Path(p).resolve().relative_to(OUT.resolve()))
    except ValueError:
        return p


def ntfy(phase: str, score: float, message: str) -> None:
    if not NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC unset; skipping", flush=True)
        return
    headers = {"Title": f"donedone {JOB_ID}: {phase} score={score}", "Tags": "robot"}
    if LIVE_URL:
        headers["Click"] = LIVE_URL
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode()[:3500],
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"[ntfy] sent ({r.status})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[ntfy] failed: {e}", flush=True)


def write_report(spec: dict, st: Status, decisions: list[str]) -> None:
    d = st.d
    lines = [f"# donedone report: {JOB_ID}", "", f"**Goal:** {spec['goal']}", "",
             f"**Final phase:** {d['phase']}  |  **score:** {d['score']}  |  **rounds:** {d['round']}/{MAX_ROUNDS}"
             f"  |  **elapsed:** {d['elapsed_s']} s", "", f"**Message:** {d['message']}", "",
             "## Checks", "", "| id | kind | title | result | score | evidence | screenshot |",
             "|---|---|---|---|---|---|---|"]
    for c in d["checks"]:
        res = "PASS" if c["passed"] else ("FAIL" if c["passed"] is False else "-")
        ev = (c.get("evidence") or "").replace("|", "/").replace("\n", " ")
        shot = f"[png]({c['screenshot']})" if c.get("screenshot") else ""
        if c.get("clip"):
            shot += f" [mp4]({c['clip']})"
        if c.get("sheet"):
            shot += f" [frames]({c['sheet']})"
        lines.append(f"| {c['id']} | {c['kind']} | {c['title']} | {res} | {c.get('score')} | {ev} | {shot} |")
    lines += ["", "## Rounds", "", "| round | score | decision | failed |", "|---|---|---|---|"]
    for h in d["history"]:
        lines.append(f"| {h['round']} | {h['score']} | {h['decision']} | {', '.join(h['failed'])} |")
    lines += ["", "## Decision log", ""] + [f"- {x}" for x in decisions]
    u = d.get("usage") or {}
    lines += ["", f"LLM calls: {u.get('calls')}, tokens {u.get('prompt_tokens')}+{u.get('completion_tokens')}, "
              f"{round(u.get('seconds', 0), 1)} s in LLM", ""]
    (OUT / "report.md").write_text("\n".join(lines))


# ------------------------------------------------------------ main ------

def run(spec: dict, st: Status) -> None:
    import agents
    import llm

    site = agents.SITE
    site.mkdir(parents=True, exist_ok=True)
    (OUT / "shots").mkdir(parents=True, exist_ok=True)
    try:  # expose the built site under the live URL as /site/
        if not (OUT / "site").exists():
            (OUT / "site").symlink_to(site.resolve(), target_is_directory=True)
    except OSError:
        pass
    browser = None
    if agents.needs_browser(spec):
        from browser import Browser
        browser = Browser(str(site), str(OUT / "shots"))
        st.set(desktop="ready" if browser.desktop else "unavailable", desktop_note=browser.desktop_note)
        if browser.desktop and os.environ.get("JUDGE_VIDEO", "0") == "1" and os.environ.get("VIDEO_PROBE", "1") == "1":
            llm.probe_video()
        st.set(video_input=bool(llm.VIDEO_OK))
    else:
        st.set(desktop="skipped", desktop_note="no browser checks in this spec (gpu/code/http only)")
        print("[loop] no browser checks: browser/desktop not started", flush=True)
    gpu_status = _gpu_probe_status() if (os.environ.get("GPU_HOST") or _wants_gpu(spec)) else None
    if gpu_status:
        print(f"[loop] gpu device: {gpu_status['status']} {gpu_status.get('detail', '')}", flush=True)
    st.set(devices=build_devices(spec, st.d["desktop"], gpu_status), secrets=creds.parse_ledger(),
           deploy_url=creds.read_deploy_url(site))
    budget_s = MAX_MINUTES * 60
    feedback: dict | None = None
    decisions: list[str] = []
    prev_snapshot = None
    last_round_s = 0.0
    mode = os.environ.get("USER_SIM", "auto")  # auto | llm | parser
    use_llm_sim = {"llm": True, "parser": False}.get(mode, "auto")
    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            r0 = time.time()
            llm.CONTEXT["round"] = rnd
            st.set(phase="building", round=rnd, message=f"round {rnd}: builder working")
            b = agents.builder(spec, rnd, feedback)
            print(f"[round {rnd}] builder: {creds.redact(json.dumps(b, default=str))[:600]}", flush=True)
            st.set(deploy_url=creds.read_deploy_url(site))
            snap = agents.site_snapshot()
            sim = difflib.SequenceMatcher(None, prev_snapshot, snap).ratio() if prev_snapshot else None
            prev_snapshot = snap

            st.set(phase="verifying", message=f"round {rnd}: verifying {len(spec['checks'])} checks")
            results, by_id = {}, {}
            for c in spec["checks"]:
                c0 = time.time()
                try:
                    ev = agents.user_sim(c, browser, use_llm=use_llm_sim)
                except Exception as e:  # noqa: BLE001
                    ev = {"actions": [], "screenshots": [], "error": str(e)}
                    try:
                        if browser is not None:
                            browser.new_page()
                    except Exception:
                        pass
                res = agents.verify(c, ev, browser, spec)
                for k in ("screenshot", "clip", "sheet", "judged"):
                    res[k] = rel_shot(res.get(k))
                results[c["id"]] = bool(res["passed"])
                by_id[c["id"]] = res
                st.set_check(res)
                print(f"[round {rnd}] {c['id']} {c['kind']:<6} {'PASS' if res['passed'] else 'FAIL'} "
                      f"({time.time() - c0:.1f}s via {ev.get('via')}) {res['evidence'][:140]}", flush=True)
            score = weighted_score(spec, results)
            failed = [cid for cid, ok in results.items() if not ok]
            last_round_s = time.time() - r0
            elapsed = time.time() - st.t0
            budget_left = rnd < MAX_ROUNDS and elapsed + last_round_s < budget_s
            entry = {"round": rnd, "score": score, "decision": None, "failed": failed,
                     "seconds": round(last_round_s, 1), "similarity": round(sim, 3) if sim is not None else None}
            history = st.d["history"] + [entry]
            decision, why = decide(history, budget_left=budget_left, similarity=sim)
            entry["decision"] = decision
            entry["reason"] = why
            st.d["history"] = history
            decisions.append(f"round {rnd}: score {score} -> {decision} ({why})")
            print(f"[round {rnd}] score={score} failed={failed} decision={decision} ({why}) "
                  f"round_time={last_round_s:.0f}s", flush=True)
            failures = [{**by_id[cid], **{k: c.get(k) for k in ("selector", "property", "expected", "criteria",
                                                                  "url", "method", "steps")}}
                        for c in spec["checks"] for cid in [c["id"]] if cid in failed]
            blocker = None if decision == "stop" else credential_blocker(
                failures, st.d.get("secrets"), builder_message=str(b.get("final_message") or ""),
                ssh_denied=bool(b.get("ssh_denied")), devices=st.d.get("devices"))
            if blocker:
                decision, why = "escalate", "blocked on a missing credential / refused device"
                entry["decision"], entry["reason"] = decision, why
                decisions.append(f"round {rnd}: escalate ({blocker[:200]})")
                print(f"[round {rnd}] credential blocker -> escalate: {blocker}", flush=True)
            st.set(score=score, decision=decision, message=f"round {rnd}: {decision} ({why})")

            if decision == "stop":
                caveat = "" if not failed else f" Caveats: still failing {', '.join(failed)}."
                st.set(phase="done", message=f"Done after {rnd} rounds, score {score} ({why}).{caveat}")
                break
            if decision == "escalate":
                st.set(phase="escalated", message=blocker or escalation_question(failures, score, why))
                break
            brief = None
            if decision == "replan":
                st.set(phase="replanning", message=f"round {rnd}: re-planning ({why})")
                latest = next((by_id[c["id"]].get("screenshot") for c in spec["checks"]
                               if c["kind"] == "visual" and by_id[c["id"]].get("screenshot")), None)
                rp = agents.replan(spec, history, failures, str(OUT / latest) if latest else None)
                decisions.append(f"round {rnd}: replan diagnosis: {rp.get('diagnosis', '')[:300]}")
                print(f"[round {rnd}] replan: {rp.get('diagnosis', '')[:200]} | unverifiable={rp['unverifiable']}",
                      flush=True)
                if rp["unverifiable"]:
                    entry["decision"] = "escalate"
                    reasons = "; ".join(f"{u['id']}: {u.get('reason', '')}" for u in rp["unverifiable"])
                    decisions.append(f"round {rnd}: escalate (unverifiable here: {reasons})")
                    st.set(phase="escalated", decision="escalate",
                           message=f"Score {score}. These checks look unverifiable in this sandbox: "
                                   f"{reasons}. Should I drop them, or can you provide a reference/real environment?")
                    break
                brief = rp["brief"]
            feedback = {"failures": failures, "brief": brief}
        else:
            st.set(phase="done", message=f"Max rounds reached, score {st.d['score']}")
    finally:
        write_report(spec, st, decisions)
        if browser is not None:
            browser.close()


def _wants_gpu(spec: dict) -> bool:
    env = spec.get("environment") or {}
    return bool(env.get("needs_gpu")) or any(
        any(w in str(d).lower() for w in ("gpu", "a100", "nvidia", "lambda")) for d in env.get("devices") or [])


def escalation_question(failures: list[dict], score: float, why: str) -> str:
    worst = "; ".join(f"{f['id']} ({f['title']}): {str(f.get('evidence'))[:120]}" for f in failures[:4])
    return (f"Stuck at score {score} ({why}). Still failing: {worst}. "
            "Question: should I relax these criteria (are the expected values right for the goal?), "
            "or can you provide a reference screenshot/clip so I can target them precisely?")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    spec_path = Path(os.environ.get("SPEC_PATH", "spec.json"))
    st = Status(None)
    try:
        spec = json.loads(spec_path.read_text())
        st = Status(spec)
        st.set(phase="building", message="loop starting")
        run(spec, st)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        st.set(phase="failed", message=f"runner crashed: {type(e).__name__}: {str(e)[:300]}")
    d = st.d
    print(f"[loop] finished phase={d['phase']} score={d['score']} elapsed={d['elapsed_s']}s :: {d['message']}",
          flush=True)
    if d["phase"] in ("done", "escalated", "failed"):
        ntfy(d["phase"], d["score"], d["message"])
    return 0 if d["phase"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
