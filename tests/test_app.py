import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def test_index_serves_web_app():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>donedone</title>" in r.text


def test_api_description_is_json():
    r = client.get("/api")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "donedone"
    assert "POST /plan" in body["endpoints"]


# ------------------------------------------------------------ credential manager ------
def test_catalog_shape():
    r = client.get("/catalog")
    assert r.status_code == 200
    data = r.json()
    names = {s["name"] for s in data["secrets"]}
    assert {"CRUSOE_API_KEY", "NTFY_TOPIC", "VERCEL_TOKEN", "LAMBDA_SSH_KEY", "LAMBDA_API_KEY"} <= names
    types = {d["type"] for d in data["devices"]}
    assert {"sandbox-desktop", "gpu-ssh", "lambda-api", "vercel-deploy", "ntfy", "crusoe", "macos"} <= types
    for d in data["devices"]:
        for f in d["fields"]:
            assert f["env"] and f["kind"] in ("text", "token", "key", "bool")


def test_resolve_marks_request_source_and_never_echoes_values():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123"
    r = client.post("/secrets/resolve", json={"vault": {"GITHUB_TOKEN": secret}, "secrets": ["GITHUB_TOKEN"]})
    assert r.status_code == 200
    rows = {x["name"]: x for x in r.json()["ledger"]}
    assert rows["GITHUB_TOKEN"]["status"] == "injected"
    assert rows["GITHUB_TOKEN"]["source"] == "request"
    assert secret not in r.text
    assert rows["GITHUB_TOKEN"]["masked"] == "ghp_…0123"


def test_device_test_offline_types():
    assert client.post("/devices/test", json={"type": "macos", "config": {}}).json()["ok"] is False
    assert client.post("/devices/test", json={"type": "sandbox-desktop", "config": {}}).json()["ok"] is True
    assert client.post("/devices/test", json={"type": "nope", "config": {}}).json()["ok"] is False


def test_run_passes_vault_as_overrides_without_leaking(monkeypatch):
    import app as app_module
    from framework import provisioner
    captured = {}

    async def fake_provision(spec, job_id, creds, adapters=None, overrides=None):
        captured["overrides"] = dict(overrides or {})
        captured["env_untouched"] = "GITHUB_TOKEN" not in __import__("os").environ
        return []

    monkeypatch.setattr(provisioner, "provision", fake_provision)
    spec = json.loads((ROOT / "tests" / "fixture_spec.json").read_text())
    secret = "ghp_zzzzzzzzzzzzzzzzzzzzzzzz9999"
    r = client.post("/run", json={"spec": spec, "vault": {"GITHUB_TOKEN": secret, "EMPTY": ""}})
    assert r.status_code == 200, r.text
    assert captured["overrides"] == {"GITHUB_TOKEN": secret}
    assert captured["env_untouched"]
    assert secret not in r.text
