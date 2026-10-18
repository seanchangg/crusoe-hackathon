import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


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
