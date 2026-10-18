"""Credential broker + device/secret manifest (offline), plus checks on the compiled deploy fixture."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework import provisioner
from framework.adapters import vercel_sandbox as vs
from framework.adapters.secrets_broker import CATALOG, SecretsBrokerAdapter, mask
from framework.spec import Check, Environment, Spec

ROOT = Path(__file__).resolve().parents[1]
for _f in (".env", ".env.vercel", ".env.lambda"):
    load_dotenv(ROOT / _f)

DEPLOY_SECRETS = ["CRUSOE_API_KEY", "NTFY_TOPIC", "VERCEL_TOKEN", "LAMBDA_SSH_KEY"]


def spec_with(secrets: list[str], **env) -> Spec:
    return Spec(goal="g", checks=[Check(id="C01", title="t", kind="code", steps=[], criteria="c")],
                environment=Environment(secrets=secrets, **env))


def run(adapter, spec, creds):
    return asyncio.run(adapter.provision(spec, "deadbeef", creds))


# ---------------------------------------------------------------- spec contract

def test_spec_http_check_and_manifest():
    c = Check(id="C09", title="up", kind="http", steps=[], criteria="200", url="${DEPLOY_URL}/",
              method="GET", expected="status 200")
    assert c.url == "${DEPLOY_URL}/" and c.method == "GET"
    e = Environment()
    assert e.secrets == [] and e.devices == []


def test_mask():
    assert mask("vcl_abcdefghijkl9f3a") == "vcl_…9f3a"
    assert mask("x" * 400, is_key=True) == "400 bytes"
    short = mask("topic12")
    assert "topic12" not in short


# ---------------------------------------------------------------- broker

def test_ledger_masks_and_injects(tmp_path):
    token = "vcl_SECRETTOKENVALUE9f3a"
    vault = {"CRUSOE_API_KEY": "crusoe-SECRET-KEY-1234", "NTFY_TOPIC": "donedone-topic-xyz", "VERCEL_TOKEN": token,
             "VERCEL_TEAM_ID": "team_abcdefgh1234"}
    creds: dict[str, str] = {}
    p = run(SecretsBrokerAdapter(vault, root=tmp_path), spec_with(["CRUSOE_API_KEY", "NTFY_TOPIC", "VERCEL_TOKEN"]),
            creds)
    assert p.status == "ready" and p.real and p.adapter == "secrets_broker"
    assert creds["VERCEL_TOKEN"] == token and creds["VERCEL_TEAM_ID"] == "team_abcdefgh1234"
    ledger = p.connect_info["ledger"]
    assert json.loads(creds["SECRETS_LEDGER"]) == ledger
    row = next(r for r in ledger if r["name"] == "VERCEL_TOKEN")
    assert row == {"name": "VERCEL_TOKEN", "status": "injected", "source": "local-env", "masked": "vcl_…9f3a",
                   "used_for": CATALOG["VERCEL_TOKEN"]["used_for"], "hint": CATALOG["VERCEL_TOKEN"]["hint"]}
    blob = json.dumps(p.model_dump())  # no raw value anywhere in what the API returns
    for v in vault.values():
        assert v not in blob


def test_vercel_env_source(tmp_path):
    p = run(SecretsBrokerAdapter({"VERCEL": "1", "CRUSOE_API_KEY": "k" * 20, "NTFY_TOPIC": "t" * 20},
                                 root=tmp_path), spec_with(["CRUSOE_API_KEY"]), {})
    assert {r["source"] for r in p.connect_info["ledger"]} == {"vercel-env"}


def test_team_id_from_project_json(tmp_path):
    (tmp_path / ".vercel").mkdir()
    (tmp_path / ".vercel" / "project.json").write_text(json.dumps({"orgId": "team_fromfile12345"}))
    creds: dict[str, str] = {}
    p = run(SecretsBrokerAdapter({"VERCEL_TOKEN": "v" * 24}, root=tmp_path), spec_with(["VERCEL_TOKEN"]), creds)
    assert creds["VERCEL_TEAM_ID"] == "team_fromfile12345"
    assert next(r for r in p.connect_info["ledger"] if r["name"] == "VERCEL_TEAM_ID")["source"] == "file"


def test_missing_secret_fails_but_others_injected(tmp_path):
    creds: dict[str, str] = {}
    p = run(SecretsBrokerAdapter({"CRUSOE_API_KEY": "c" * 20, "NTFY_TOPIC": "n" * 20}, root=tmp_path),
            spec_with(["CRUSOE_API_KEY", "NTFY_TOPIC", "VERCEL_TOKEN"]), creds)
    assert p.status == "failed"
    assert p.connect_info["missing"] == ["VERCEL_TOKEN"]
    by = {r["name"]: r for r in p.connect_info["ledger"]}
    assert by["VERCEL_TOKEN"]["status"] == "missing" and by["VERCEL_TOKEN"]["masked"] == ""
    assert by["CRUSOE_API_KEY"]["status"] == by["NTFY_TOPIC"]["status"] == "injected"
    assert creds["CRUSOE_API_KEY"] == "c" * 20 and "VERCEL_TOKEN" not in creds
    assert "SECRETS_LEDGER" in creds


def test_always_secrets_even_if_not_listed(tmp_path):
    p = run(SecretsBrokerAdapter({}, root=tmp_path), spec_with(["GITHUB_TOKEN"]), {})
    assert [r["name"] for r in p.connect_info["ledger"]] == ["CRUSOE_API_KEY", "NTFY_TOPIC", "GITHUB_TOKEN"]


def test_request_ntfy_topic_wins(tmp_path):
    creds = {"NTFY_TOPIC": "from-request-topic"}
    p = run(SecretsBrokerAdapter({"NTFY_TOPIC": "from-env-topic-x"}, root=tmp_path), spec_with(["NTFY_TOPIC"]), creds)
    assert creds["NTFY_TOPIC"] == "from-request-topic"
    row = next(r for r in p.connect_info["ledger"] if r["name"] == "NTFY_TOPIC")
    assert row["status"] == "injected" and row["source"] == "request"


def test_lambda_key_path_roundtrip(tmp_path):
    key = b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAfake\n-----END OPENSSH PRIVATE KEY-----\n"
    kp = tmp_path / "lambda_a100"
    kp.write_bytes(key)
    creds: dict[str, str] = {}
    p = run(SecretsBrokerAdapter({"LAMBDA_SSH_KEY_PATH": str(kp), "LAMBDA_HOST": "203.0.113.7",
                                  "CRUSOE_API_KEY": "c" * 20, "NTFY_TOPIC": "n" * 20}, root=tmp_path),
            spec_with(["LAMBDA_SSH_KEY"]), creds)
    assert p.status == "ready"
    assert base64.b64decode(creds["GPU_SSH_KEY_B64"]) == key
    assert creds["GPU_HOST"] == "203.0.113.7" and creds["GPU_USER"] == "ubuntu"
    row = next(r for r in p.connect_info["ledger"] if r["name"] == "LAMBDA_SSH_KEY")
    assert row["source"] == "file" and row["masked"] == f"{len(key)} bytes"
    assert "AAAAfake" not in json.dumps(p.connect_info)


def test_lambda_key_b64(tmp_path):
    key = b"-----BEGIN KEY-----\nZZZ\n-----END KEY-----"
    creds: dict[str, str] = {}
    p = run(SecretsBrokerAdapter({"LAMBDA_SSH_KEY_B64": base64.b64encode(key).decode(), "LAMBDA_USER": "me"},
                                 root=tmp_path), spec_with(["LAMBDA_SSH_KEY"]), creds)
    assert base64.b64decode(creds["GPU_SSH_KEY_B64"]) == key + b"\n"
    assert creds["GPU_USER"] == "me" and "GPU_HOST" not in creds
    by = {r["name"]: r for r in p.connect_info["ledger"]}
    assert by["LAMBDA_SSH_KEY"]["status"] == "injected" and by["GPU_HOST"]["status"] == "missing"
    assert p.status == "failed"


# ---------------------------------------------------------------- order + env passthrough

def names(env: Environment) -> list[str]:
    return [a.name for a in provisioner.select_adapters(env)]


def test_adapter_order():
    env = Environment(secrets=DEPLOY_SECRETS, needs_gpu=True, needs_browser=True, devices=["sandbox-desktop"])
    assert names(env) == ["secrets_broker", "lambda_gpu", "vercel_sandbox", "remote_desktop"]
    assert names(Environment(secrets=["CRUSOE_API_KEY"], needs_browser=False)) == ["secrets_broker",
                                                                                   "vercel_sandbox"]
    assert names(Environment(needs_browser=False)) == ["vercel_sandbox"]  # no manifest -> no broker


def test_describe_plan_marks_broker_real():
    plan = provisioner.describe_plan(spec_with(DEPLOY_SECRETS, needs_gpu=True))
    assert plan[0]["adapter"] == "secrets_broker" and plan[0]["real"]


def test_build_env_passes_every_cred():
    creds = {"CRUSOE_API_KEY": "c", "NTFY_TOPIC": "n", "VERCEL_TOKEN": "v", "VERCEL_TEAM_ID": "t",
             "GPU_HOST": "h", "GPU_USER": "u", "GPU_SSH_KEY_B64": "k", "SECRETS_LEDGER": "[]", "JOB_ID": "spoof"}
    env = vs.build_env(creds, "job1", "https://live", None, "6", "30")
    for k in ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "GPU_HOST", "GPU_USER", "GPU_SSH_KEY_B64", "SECRETS_LEDGER"):
        assert env[k] == creds[k]
    assert env["JOB_ID"] == "job1" and env["LIVE_URL"] == "https://live" and env["NOVNC_URL"] == ""
    assert env["MAX_ROUNDS"] == "6" and env["MAX_MINUTES"] == "30"
    assert vs.build_env({}, "j", None, None)["CRUSOE_API_KEY"] == ""


def test_broker_creds_reach_sandbox_env(tmp_path):
    creds: dict[str, str] = {}
    run(SecretsBrokerAdapter({"CRUSOE_API_KEY": "c" * 20, "NTFY_TOPIC": "n" * 20, "VERCEL_TOKEN": "v" * 24},
                             root=tmp_path), spec_with(DEPLOY_SECRETS[:3]), creds)
    env = vs.build_env(creds, "j", "u", "n")
    assert env["VERCEL_TOKEN"] == "v" * 24 and json.loads(env["SECRETS_LEDGER"])


# ---------------------------------------------------------------- compiled deploy fixture

FIXTURE = ROOT / "tests" / "fixture_spec_deploy.json"


@pytest.mark.skipif(not FIXTURE.exists(), reason="compile tests/fixture_spec_deploy.json first")
def test_deploy_fixture():
    spec = Spec.model_validate_json(FIXTURE.read_text())
    env = spec.environment
    assert set(env.secrets) == set(DEPLOY_SECRETS)
    assert {"sandbox-desktop", "gpu-a100"} <= set(env.devices)
    assert env.needs_browser and env.needs_gpu
    http = [c for c in spec.checks if c.kind == "http"]
    assert len(http) >= 2 and all(c.url for c in http)
    assert any(s.startswith("goto ${DEPLOY_URL}") for c in spec.checks for s in c.steps)
    assert names(env)[:3] == ["secrets_broker", "lambda_gpu", "vercel_sandbox"]
