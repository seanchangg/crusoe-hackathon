"""Offline tests for the sandbox runner: controller decisions + deterministic verifier helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))

import pytest  # noqa: E402

from loop import decide, weighted_score  # noqa: E402


def h(rnd, score, failed, decision=None):
    return {"round": rnd, "score": score, "failed": list(failed), "decision": decision}


def test_all_pass_stops():
    assert decide([h(1, 1.0, [])], budget_left=True)[0] == "stop"


def test_first_round_continues():
    assert decide([h(1, 0.4, ["C1"])], budget_left=True)[0] == "continue"


def test_improvement_continues():
    hist = [h(1, 0.4, ["C1", "C2"], "continue"), h(2, 0.6, ["C2"])]
    assert decide(hist, budget_left=True)[0] == "continue"


def test_same_failing_set_replans():
    hist = [h(1, 0.6, ["C2", "C3"], "continue"), h(2, 0.6, ["C3", "C2"])]
    d, why = decide(hist, budget_left=True)
    assert d == "replan" and "same failing set" in why


def test_no_improvement_two_rounds_replans():
    hist = [h(1, 0.7, ["A"], "continue"), h(2, 0.6, ["B"], "continue"), h(3, 0.65, ["C"])]
    d, why = decide(hist, budget_left=True)
    assert d == "replan" and "2 rounds" in why


def test_single_regression_gets_one_more_try():
    hist = [h(1, 0.7, ["A"], "continue"), h(2, 0.6, ["B"])]
    assert decide(hist, budget_left=True)[0] == "continue"


def test_identical_builder_output_replans():
    hist = [h(1, 0.7, ["A"], "continue"), h(2, 0.6, ["B"])]
    d, why = decide(hist, budget_left=True, similarity=0.95)
    assert d == "replan" and "identical" in why


def test_identical_output_ignored_when_improving():
    hist = [h(1, 0.5, ["A", "B"], "continue"), h(2, 0.7, ["B"])]
    assert decide(hist, budget_left=True, similarity=0.99)[0] == "continue"


def test_budget_exhausted_high_score_stops_with_caveats():
    hist = [h(1, 0.85, ["C9"])]
    d, why = decide(hist, budget_left=False)
    assert d == "stop" and "caveats" in why


def test_budget_exhausted_low_score_escalates():
    assert decide([h(1, 0.5, ["C9"])], budget_left=False)[0] == "escalate"


def test_two_failed_replans_escalate():
    hist = [h(1, 0.6, ["A"], "continue"), h(2, 0.6, ["A"], "replan"),
            h(3, 0.6, ["A"], "replan"), h(4, 0.6, ["A"])]
    d, why = decide(hist, budget_left=True)
    assert d == "escalate" and "2 replans" in why


def test_replans_that_helped_do_not_escalate():
    hist = [h(1, 0.5, ["A", "B"], "continue"), h(2, 0.5, ["A", "B"], "replan"),
            h(3, 0.5, ["A", "B"], "replan"), h(4, 0.8, ["B"])]
    assert decide(hist, budget_left=True)[0] == "continue"


def test_weighted_score_matches_spec_semantics():
    spec = {"checks": [{"id": "a", "weight": 1.0}, {"id": "b", "weight": 1.5}, {"id": "c"}]}
    assert weighted_score(spec, {"a": True, "b": True}) == round(2.5 / 3.5, 3)
    assert weighted_score(spec, {}) == 0.0


# ---- deterministic verifier helpers (need `openai` + `playwright` importable) ----
agents = pytest.importorskip("agents")
browser = pytest.importorskip("browser")


@pytest.mark.parametrize("prop,expected,actual,ok", [
    ("background-color", "#1b1b1e", "rgb(27, 27, 30)", True),
    ("background-color", "#1B1B1E", "rgb(27, 27, 31)", False),
    ("width", "240px", "241.5px", True),
    ("width", "240px", "250px", False),
    ("font-family", "JetBrains Mono", '"JetBrains Mono", monospace', True),
    ("font-family", "JetBrains Mono", "monospace", False),
    ("transition", "150ms cubic-bezier(0.2, 0, 0, 1)", "background-color 0.15s cubic-bezier(0.2, 0, 0, 1)", True),
    ("transition", "150ms cubic-bezier(0.2, 0, 0, 1)", "background-color 0.15s ease", False),
    ("transition", "150ms cubic-bezier(0.2, 0, 0, 1)", "all 0.3s cubic-bezier(0.2, 0, 0, 1)", False),
    ("transition-duration", "150-250ms", "0.2s", True),
    ("transition-duration", "150-250ms", "0.3s", False),
    ("transition-timing-function", "ease-out", "cubic-bezier(0.05, 0, 0.6, 1)", True),
])
def test_compare(prop, expected, actual, ok):
    assert agents.compare(prop, expected, actual)[0] is ok


def test_normalize_color():
    n = browser.normalize_color
    assert n("rgb(0, 122, 204)") == "#007acc"
    assert n("rgba(0, 0, 0, 0)") == "transparent"
    assert n("#ABC") == "#aabbcc"
    assert browser.normalize_value("1px solid rgb(69, 69, 69)") == "1px solid #454545"


def test_parse_step():
    assert agents.parse_step('hover [data-testid="run-button"]') == ("hover", {"selector": '[data-testid="run-button"]'})
    assert agents.parse_step("wait 600") == ("wait_ms", {"ms": 600})
    assert agents.parse_step("wait 1.5 s") == ("wait_ms", {"ms": 1500})
    assert agents.parse_step("goto /") == ("goto", {"path": "/"})
    assert agents.parse_range_ms("Tooltip becomes visible after 500-900ms of hovering") == (500, 900)


# ---- desktop user-sim, single-media visual judge, gpu dispatch, gpu builder ----
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import loop  # noqa: E402

FIX = Path(__file__).parent


class FakeBrowser:
    """Records calls; behaves like browser.Browser on a desktop (xdotool) or headless."""

    def __init__(self, tmp, desktop=True):
        self.desktop = desktop
        self.calls = []
        self.tmp = Path(tmp)

    def _png(self, name):
        p = self.tmp / f"{name}.png"
        p.write_bytes(b"\x89PNG fake")
        return str(p)

    def goto(self, path="/"):
        self.calls.append(("goto", path))
        return f"loaded {path}"

    def locate(self, selector):
        self.calls.append(("locate", selector))
        return {"x": 100, "y": 50, "left": 90, "top": 40, "width": 20, "height": 20}

    def desk_move(self, x, y):
        self.calls.append(("desk_move", x, y))
        return "moved"

    def desk_click(self, x, y, button=1):
        self.calls.append(("desk_click", x, y))
        return "clicked"

    def desk_type(self, text):
        self.calls.append(("desk_type", text))
        return "typed"

    def desk_key(self, key):
        self.calls.append(("desk_key", key))
        return "pressed"

    def wait_ms(self, ms):
        self.calls.append(("wait_ms", ms))
        return "waited"

    def desk_screenshot(self, name="desk"):
        self.calls.append(("desk_screenshot", name))
        return self._png(name)

    def screenshot(self, name="shot"):
        self.calls.append(("screenshot", name))
        return self._png(name)

    def desk_record(self, name, duration_ms):
        self.calls.append(("desk_record", duration_ms))
        return {"error": None}

    def focus_box(self, selector):
        self.calls.append(("focus_box", selector))
        return {"x": 0, "y": 0, "w": 640, "h": 400} if selector else None

    def finish_recordings(self, focus=None):
        if not any(c[0] == "desk_record" for c in self.calls):
            return []
        mp4 = self.tmp / "clip.mp4"
        mp4.write_bytes(b"fake mp4")
        return [{"name": "clip", "mp4": str(mp4), "sheet": self._png("clip_sheet"), "error": None,
                 "duration_s": 1.2, "focus": focus}]

    def hover(self, selector):
        self.calls.append(("hover", selector))
        return "hovering"

    def text_content(self, selector):
        self.calls.append(("text_content", selector))
        return getattr(self, "texts", {}).get(selector)

    def console_errors(self):
        return []


HOVER_CHECK = {"id": "C09", "kind": "visual", "title": "hover", "criteria": "button lightens on hover",
               "steps": ["goto /", 'hover [data-testid="run-button"]', "wait 200"]}


def test_needs_browser_and_gpu_mode():
    web = json.loads((FIX / "fixture_spec.json").read_text())
    g = json.loads((FIX / "fixture_spec_gpu.json").read_text())
    assert agents.needs_browser(web) and not agents.gpu_mode(web)
    assert not agents.needs_browser(g) and agents.gpu_mode(g)
    files_only = {"checks": [{"id": "a", "kind": "code", "criteria": "kernel.py exists"}]}
    assert not agents.needs_browser(files_only)
    assert agents.needs_browser({"checks": [{"id": "a", "kind": "code", "criteria": "no console errors"}]})


def test_desktop_state_normalised():
    assert loop.desktop_state("available") == "ready"
    assert loop.desktop_state("ready") == "ready"
    assert loop.desktop_state("skipped") == "skipped"
    assert loop.desktop_state(None) == "unavailable"


def test_usersim_parser_drives_real_desktop(tmp_path):
    b = FakeBrowser(tmp_path)
    ev = agents.user_sim(HOVER_CHECK, b, use_llm=False)
    names = [c[0] for c in b.calls]
    assert ev["via"] == "parser"
    assert names.index("desk_record") < names.index("locate") < names.index("desk_move")
    assert ("desk_move", 100, 50) in b.calls and "hover" not in names
    assert ev["clips"] and ev["clips"][0]["sheet"].endswith("clip_sheet.png")
    assert ("focus_box", '[data-testid="run-button"]') in b.calls and ev["clips"][0]["focus"]
    assert ev["screenshots"]  # final desktop screenshot


def test_usersim_click_and_keys_map_to_xdotool(tmp_path):
    b = FakeBrowser(tmp_path)
    chk = {"id": "C1", "kind": "dom", "title": "t", "criteria": "",
           "steps": ["goto /", "click #tab2", "type hello", "press Enter"]}
    agents.user_sim(chk, b, use_llm=False)
    names = [c[0] for c in b.calls]
    assert {"desk_click", "desk_type", "desk_key"} <= set(names)
    assert "desk_record" not in names  # dom checks do not need a clip


def test_usersim_tools_expose_desktop_and_measure():
    names = {t["function"]["name"] for t in agents.DESK_TOOLS}
    assert {"locate", "desk_move", "desk_click", "desk_type", "desk_key", "desk_screenshot", "desk_record",
            "wait_ms", "measure_css", "is_visible"} <= names
    assert "record_frames" not in names  # headed page.screenshot bursts break :hover


def test_usersim_skips_gpu_and_missing_browser():
    assert agents.user_sim({"id": "G", "kind": "gpu", "steps": []}, None)["via"] == "none"


def _judge_capture(monkeypatch):
    seen = {}

    def fake_json(model, messages, **kw):
        seen["model"] = model
        seen["parts"] = messages[0]["content"]
        return {"score": 0.9, "pass": True, "reason": "ok"}
    monkeypatch.setattr(agents.llm, "chat_json", fake_json)
    return seen


def test_visual_motion_sends_one_contact_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.llm, "VIDEO_OK", False)
    seen = _judge_capture(monkeypatch)
    b = FakeBrowser(tmp_path)
    ev = agents.user_sim(HOVER_CHECK, b, use_llm=False)
    res = agents.verify(HOVER_CHECK, ev, b, {"goal": "g", "design_facts": []})
    media = [p for p in seen["parts"] if p["type"] != "text"]
    assert len(media) == 1 and media[0]["type"] == "image_url"
    assert res["passed"] and res["sheet"].endswith("clip_sheet.png") and res["clip"].endswith(".mp4")
    assert res["judged"].endswith("clip_sheet.png") and "contact sheet" in res["evidence"]
    assert seen["model"] == agents.llm.SUPER_VL


def test_visual_video_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.llm, "VIDEO_OK", True)
    monkeypatch.delenv("JUDGE_VIDEO", raising=False)
    seen = _judge_capture(monkeypatch)
    b = FakeBrowser(tmp_path)
    ev = agents.user_sim(HOVER_CHECK, b, use_llm=False)
    agents.verify(HOVER_CHECK, ev, b, {"goal": "g", "design_facts": []})
    assert [p["type"] for p in seen["parts"] if p["type"] != "text"] == ["image_url"]


def test_visual_uses_native_video_when_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.llm, "VIDEO_OK", True)
    monkeypatch.setenv("JUDGE_VIDEO", "1")
    import browser as browser_mod
    monkeypatch.setattr(browser_mod, "crop_video", lambda mp4, crop: mp4)
    seen = _judge_capture(monkeypatch)
    b = FakeBrowser(tmp_path)
    ev = agents.user_sim(HOVER_CHECK, b, use_llm=False)
    agents.verify(HOVER_CHECK, ev, b, {"goal": "g", "design_facts": []})
    media = [p for p in seen["parts"] if p["type"] != "text"]
    assert len(media) == 1 and media[0]["type"] == "video_url"
    assert media[0]["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_visual_static_sends_one_screenshot(tmp_path, monkeypatch):
    seen = _judge_capture(monkeypatch)
    b = FakeBrowser(tmp_path)
    chk = {"id": "C13", "kind": "visual", "title": "looks like an IDE", "criteria": "dark IDE layout",
           "steps": ["goto /"]}
    ev = agents.user_sim(chk, b, use_llm=False)
    res = agents.verify(chk, ev, b, {"goal": "g", "design_facts": []})
    media = [p for p in seen["parts"] if p["type"] != "text"]
    assert len(media) == 1 and media[0]["type"] == "image_url"
    assert res["screenshot"] and res["clip"] is None


def test_verify_gpu_dispatches_to_gpu_module(monkeypatch):
    called = {}

    def fake_verify_gpu(check, job_dir):
        called["args"] = (check["id"], Path(job_dir))
        return {"id": check["id"], "passed": True, "score": 1.0, "evidence": "tflops=150", "screenshot": None,
                "harness": {"ok": True}}
    monkeypatch.setattr(agents.gpu, "verify_gpu", fake_verify_gpu)
    res = agents.verify({"id": "G1", "kind": "gpu", "title": "fast"}, {}, None, {"goal": "g"})
    assert res["passed"] and res["evidence"] == "tflops=150" and called["args"][0] == "G1"
    assert called["args"][1] == agents.SITE.resolve().parent


def test_verify_browser_check_without_browser_fails_cleanly():
    res = agents.verify({"id": "D", "kind": "dom", "title": "t", "selector": "body", "property": "color",
                         "expected": "#000"}, {}, None, {"goal": "g"})
    assert res["passed"] is False and "browser not started" in res["evidence"]


def test_gpu_builder_uses_supervl_and_run_remote(tmp_path, monkeypatch):
    spec = json.loads((FIX / "fixture_spec_gpu.json").read_text())
    monkeypatch.setattr(agents, "SITE", tmp_path)
    monkeypatch.setattr(agents, "_GPU_PROBE", ["A100 80GB | torch 2.x"])
    remote = []
    monkeypatch.setattr(agents, "run_remote", lambda cmd: remote.append(cmd) or "exit=0\nok")

    class TC:
        def __init__(self, i, name, args):
            self.id, self.function = f"t{i}", type("F", (), {"name": name, "arguments": json.dumps(args)})

    class Msg:
        def __init__(self, tcs, content=""):
            self.tool_calls, self.content = tcs, content
    script = [Msg([TC(1, "write_file", {"path": "kernel.py", "content": "def forward(q, k, v, causal=False):\n    pass\n"})]),
              Msg([TC(2, "run_remote", {"cmd": "python3 -c 'import kernel'"})]), Msg([], "done")]
    seen = {}

    def fake_tools(model, messages, tools, **kw):
        seen.setdefault("model", model)
        seen.setdefault("system", messages[0]["content"])
        seen.setdefault("tools", {t["function"]["name"] for t in tools})
        return script.pop(0), "stop"
    monkeypatch.setattr(agents.llm, "chat_tools", fake_tools)
    out = agents.builder(spec, 1, None)
    assert seen["model"] == agents.llm.SUPER_VL and "run_remote" in seen["tools"]
    assert "A100 80GB" in seen["system"] and "def forward(q, k, v" in seen["system"]
    assert (tmp_path / "kernel.py").exists() and remote == ["python3 -c 'import kernel'"]
    assert out["written"] == ["kernel.py"]


def test_run_remote_without_gpu(monkeypatch):
    monkeypatch.delenv("GPU_HOST", raising=False)
    assert agents.run_remote("nvidia-smi").startswith("ERROR: no GPU")


# ---- credentials / deploy / http checks ----
import http.server  # noqa: E402
import threading  # noqa: E402

import creds  # noqa: E402

LEDGER = [
    {"name": "VERCEL_TOKEN", "status": "injected", "source": "env", "masked": "vc_****abcd",
     "used_for": "deploy the app to Vercel", "hint": ""},
    {"name": "GPU_SSH_KEY_B64", "status": "missing", "source": "none", "masked": "",
     "used_for": "ssh to the Lambda A100 for nvidia-smi", "hint": "add the Lambda key to the broker"},
    {"name": "VERCEL_TEAM_ID", "status": "missing", "source": "none", "masked": "", "used_for": "team scope",
     "hint": "optional for personal accounts"},
]
SECRET_VALUE = "vc_supersecret_token_value_123456"


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api"):
            body, ctype, code = b'{"gpu": {"name": "NVIDIA A100-SXM4-80GB", "util": "3 %"}}', "application/json", 200
        elif self.path.startswith("/missing"):
            body, ctype, code = b"nope", "text/plain", 404
        else:
            body = (b"<html><body><pre data-testid='gpu-name'>NVIDIA A100-SXM4-80GB</pre>"
                    b"<span data-testid='mem'>1234MiB / 81920MiB</span></body></html>")
            ctype, code = "text/html", 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.parametrize("exp,parsed", [
    ("status 200", ("status", "200")),
    ("Status: 2xx", ("status", "2xx")),
    ("200", ("status", "200")),
    ("", ("status", "2xx")),
    ("contains: A100", ("contains", "A100")),
    ("contains: 'MiB'", ("contains", "MiB")),
    ("latency < 2000ms", ("latency", 2000.0)),
    ("latency < 2s", ("latency", 2000.0)),
    ("json_contains: A100", ("json_contains", "A100")),
])
def test_parse_http_expected(exp, parsed):
    assert agents.parse_http_expected(exp) == parsed


def test_evaluate_http_pure():
    r = {"status": 200, "body": '{"a": "NVIDIA A100"}', "latency_ms": 120.0, "error": None}
    assert agents.evaluate_http("status", "200", r)[0]
    assert agents.evaluate_http("contains", "a100", r)[0]  # case-insensitive
    assert agents.evaluate_http("json_contains", "A100", r)[0]
    assert agents.evaluate_http("latency", 2000.0, r)[0]
    assert not agents.evaluate_http("latency", 100.0, r)[0]
    assert not agents.evaluate_http("contains", "A100", {**r, "status": 500})[0]
    assert not agents.evaluate_http("json_contains", "x", {**r, "body": "<html>"})[0]
    assert not agents.evaluate_http("status", "200", {"status": None, "body": "", "latency_ms": 1, "error": "boom"})[0]
    ndjson = '{"event":"open"}\n{"event":"message","message":"live at https://x.vercel.app"}\n'
    assert agents.evaluate_http("json_contains", "x.vercel.app", {**r, "body": ndjson})[0]


def test_resolve_templates(tmp_path, monkeypatch):
    monkeypatch.delenv("DEPLOY_URL", raising=False)
    monkeypatch.setenv("NTFY_TOPIC", "topic123")
    monkeypatch.setenv("VERCEL_TOKEN", SECRET_VALUE)
    out, unres = creds.resolve_templates("${DEPLOY_URL}/api", tmp_path)
    assert unres == ["DEPLOY_URL"] and out == "${DEPLOY_URL}/api"
    (tmp_path / "deploy.json").write_text(json.dumps({"url": "https://app-x.vercel.app/"}))
    out, unres = creds.resolve_templates("${DEPLOY_URL}/api", tmp_path)
    assert out == "https://app-x.vercel.app/api" and not unres
    assert os.environ["DEPLOY_URL"] == "https://app-x.vercel.app"
    assert creds.resolve_templates("https://ntfy.sh/${NTFY_TOPIC}/json?poll=1")[0] == \
        "https://ntfy.sh/topic123/json?poll=1"
    # arbitrary env (tokens) is never substituted
    assert creds.resolve_templates("x ${VERCEL_TOKEN}")[1] == ["VERCEL_TOKEN"]


def test_verify_http_against_local_server(local_site, tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "SITE", tmp_path)
    monkeypatch.setattr(creds, "SITE", tmp_path)
    monkeypatch.delenv("DEPLOY_URL", raising=False)
    (tmp_path / "deploy.json").write_text(json.dumps({"url": local_site}))
    spec = {"goal": "g"}
    cases = [("${DEPLOY_URL}/", "status 200", True), ("${DEPLOY_URL}/", "contains: A100", True),
             ("${DEPLOY_URL}/", "latency < 2000ms", True), ("${DEPLOY_URL}/api", "json_contains: A100-SXM4", True),
             ("${DEPLOY_URL}/missing", "status 200", False), ("${DEPLOY_URL}/", "contains: H100", False)]
    for url, exp, want in cases:
        res = agents.verify({"id": "H", "kind": "http", "title": "t", "url": url, "method": "get",
                             "expected": exp}, {}, None, spec)
        assert res["passed"] is want, (url, exp, res["evidence"])
        assert "ms" in res["evidence"]
    code = agents.verify({"id": "C", "kind": "code", "title": "t", "criteria": "site/deploy.json exists and has url",
                          "expected": "exists"}, {}, None, spec)
    assert code["passed"] and local_site in code["evidence"]


def test_verify_http_missing_credential_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "SITE", tmp_path)
    monkeypatch.setattr(creds, "SITE", tmp_path)
    monkeypatch.delenv("DEPLOY_URL", raising=False)
    led = [dict(LEDGER[0], status="missing")]
    monkeypatch.setenv("SECRETS_LEDGER", json.dumps(led))
    res = agents.verify({"id": "H", "kind": "http", "title": "t", "url": "${DEPLOY_URL}/", "expected": "status 200"},
                        {}, None, {"goal": "g"})
    assert not res["passed"] and "missing credential VERCEL_TOKEN" in res["evidence"]
    monkeypatch.setenv("SECRETS_LEDGER", json.dumps(LEDGER))  # token injected -> just not deployed yet
    res = agents.verify({"id": "H", "kind": "http", "title": "t", "url": "${DEPLOY_URL}/", "expected": "status 200"},
                        {}, None, {"goal": "g"})
    assert "deploy.json" in res["evidence"] and "missing credential" not in res["evidence"]


def test_file_exists_target_variants():
    assert agents.file_exists_target({"expected": "file exists: site/deploy.json"}) == "deploy.json"
    assert agents.file_exists_target({"expected": "exists", "criteria": "site/deploy.json exists"}) == "deploy.json"
    assert agents.file_exists_target({"criteria": "index.html exists"}) == "index.html"
    assert agents.file_exists_target({"criteria": "no console errors", "expected": "no console errors"}) is None
    assert not agents.needs_browser({"checks": [{"id": "a", "kind": "http", "url": "${DEPLOY_URL}"},
                                                {"id": "b", "kind": "code", "expected": "file exists: site/deploy.json"}]})


def test_dom_textcontent(tmp_path):
    b = FakeBrowser(tmp_path)
    b.texts = {"#n": "  NVIDIA a100 80GB ", "#e": "   "}
    mk = lambda sel, exp: {"id": "D", "kind": "dom", "title": "t", "selector": sel, "property": "textContent",
                           "expected": exp}
    assert agents.verify(mk("#n", "contains: A100"), {}, b, {"goal": "g"})["passed"]
    assert not agents.verify(mk("#n", "contains: H100"), {}, b, {"goal": "g"})["passed"]
    assert agents.verify(mk("#n", "non-empty"), {}, b, {"goal": "g"})["passed"]
    assert not agents.verify(mk("#e", "non-empty"), {}, b, {"goal": "g"})["passed"]
    assert not agents.verify(mk("#zz", "non-empty"), {}, b, {"goal": "g"})["passed"]


def test_usersim_goto_deploy_url_on_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "SITE", tmp_path)
    monkeypatch.setenv("DEPLOY_URL", "https://app-x.vercel.app")
    b = FakeBrowser(tmp_path)
    chk = {"id": "C05", "kind": "dom", "title": "t", "steps": ["goto ${DEPLOY_URL}"], "selector": "#n",
           "property": "textContent", "expected": "non-empty"}
    agents.user_sim(chk, b, use_llm=False)
    assert ("goto", "https://app-x.vercel.app") in b.calls
    monkeypatch.delenv("DEPLOY_URL")
    b2 = FakeBrowser(tmp_path)
    ev = agents.user_sim(chk, b2, use_llm=False)
    assert not any(c[0] == "goto" for c in b2.calls)
    assert ev["actions"][0]["result"].startswith("ERROR: cannot resolve ${DEPLOY_URL}")


def test_http_check_skips_browser_usersim(tmp_path):
    b = FakeBrowser(tmp_path)
    assert agents.user_sim({"id": "H", "kind": "http", "steps": ["goto ${DEPLOY_URL}"]}, b)["via"] == "none"
    assert b.calls == []


def F(cid, title, evidence, **kw):
    return {"id": cid, "title": title, "evidence": evidence, **kw}


def test_escalate_on_missing_credential_rule():
    cb = loop.credential_blocker
    # explicit evidence from verify_http
    ask = cb([F("C02", "site 200", "missing credential VERCEL_TOKEN: cannot resolve ${DEPLOY_URL}",
                url="${DEPLOY_URL}/")], LEDGER)
    assert ask and "VERCEL_TOKEN" in ask and "BLOCKED" in ask
    # ssh refused anywhere -> names the GPU key
    ask = cb([F("C05", "gpu name", "got '' (want contains 'A100')")], LEDGER,
             builder_message="BLOCKED: ssh says Permission denied (publickey).")
    assert ask and "GPU_SSH_KEY_B64" in ask and "authorized_keys" in ask
    assert "GPU_SSH_KEY_B64" in cb([F("C05", "gpu name", "x")], LEDGER, ssh_denied=True)
    # ledger says missing + related failing check that looks blocked
    ask = cb([F("C05", "GPU name visible", "unresolved ${DEPLOY_URL}", criteria="shows A100 nvidia-smi")], LEDGER)
    assert ask and "GPU_SSH_KEY_B64" in ask
    # ordinary builder mistakes do not escalate; optional secrets never do
    assert cb([F("C05", "GPU name visible", "got 'loading' (want contains 'A100')", criteria="A100")],
              [LEDGER[0], LEDGER[2]]) is None
    assert cb([], LEDGER) is None
    # device probe said ssh denied
    ask = cb([F("C05", "GPU name visible", "got ''", criteria="A100")], [LEDGER[0]],
             devices=[{"name": "gpu-a100", "kind": "gpu", "status": "ssh denied"}])
    assert ask and "SSH refused" in ask


def test_controller_escalates_after_one_round(tmp_path, monkeypatch):
    """Whole loop.run with a missing VERCEL_TOKEN: one round, then escalate (no replans)."""
    monkeypatch.setattr(loop, "OUT", tmp_path / "out")
    monkeypatch.setattr(agents, "SITE", tmp_path / "site")
    monkeypatch.setattr(creds, "SITE", tmp_path / "site")
    monkeypatch.delenv("DEPLOY_URL", raising=False)
    monkeypatch.delenv("GPU_HOST", raising=False)
    monkeypatch.setenv("SECRETS_LEDGER", json.dumps([dict(LEDGER[0], status="missing")]))
    monkeypatch.setattr(agents, "builder", lambda spec, rnd, fb: {"tool_calls": 0, "written": [],
                                                                   "final_message": "BLOCKED: missing VERCEL_TOKEN"})
    spec = {"goal": "g", "checks": [{"id": "C02", "kind": "http", "title": "200", "url": "${DEPLOY_URL}/",
                                     "expected": "status 200", "steps": [], "criteria": ""}]}
    st = loop.Status(spec)
    loop.run(spec, st)
    assert st.d["phase"] == "escalated" and len(st.d["history"]) == 1
    assert "VERCEL_TOKEN" in st.d["message"] and st.d["secrets"][0]["name"] == "VERCEL_TOKEN"


def test_builder_prompt_has_ledger_names_never_values(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "SITE", tmp_path)
    monkeypatch.setenv("SECRETS_LEDGER", json.dumps(LEDGER))
    monkeypatch.setenv("VERCEL_TOKEN", SECRET_VALUE)
    monkeypatch.setenv("GPU_SSH_KEY_B64", "c2VjcmV0LWtleS1tYXRlcmlhbC1zaG91bGQtbmV2ZXItbGVhaw==")
    monkeypatch.setenv("JOB_ID", "abc123")
    spec = json.loads((FIX / "fixture_spec_deploy.json").read_text())
    seen = {}

    def fake_tools(model, messages, tools, **kw):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        msg = type("M", (), {"tool_calls": None, "content": "done"})()
        return msg, "stop"
    monkeypatch.setattr(agents.llm, "chat_tools", fake_tools)
    out = agents.builder(spec, 1, None)
    sysmsg = seen["system"]
    for e in LEDGER:
        assert e["name"] in sysmsg
    assert "MISSING" in sysmsg and "deploy the app to Vercel" in sysmsg
    assert SECRET_VALUE not in sysmsg + seen["user"] and "c2VjcmV0" not in sysmsg
    assert "--project \"donedone-job-abc123\"" in sysmsg and "--name" not in sysmsg.replace("NO --name", "")
    assert "ntfy.sh/$NTFY_TOPIC" in sysmsg and "/tmp/gpu_key" in sysmsg
    assert agents._SHELL_TIMEOUT[0] == 180 and "final_message" in out
    assert creds.redact(f"token={SECRET_VALUE}") == "token=***"


def test_run_shell_records_deploy_url(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "SITE", tmp_path)
    monkeypatch.delenv("DEPLOY_URL", raising=False)
    agents._SHELL_BUDGET[0] = 3
    out = "Vercel CLI 64\nhttps://vercel.com/team/p/abc\nhttps://donedone-job-x-abc123.vercel.app\n"
    res = agents._builder_tool("run_shell", {"cmd": f"printf '{out}' # vercel deploy --prod"})
    assert "recorded deploy.json" in res
    assert json.loads((tmp_path / "deploy.json").read_text())["url"] == "https://donedone-job-x-abc123.vercel.app"
    assert agents.deploy_url_from_output("x", "Aliased: https://donedone-job-x.vercel.app [2s]") == \
        "https://donedone-job-x.vercel.app"
