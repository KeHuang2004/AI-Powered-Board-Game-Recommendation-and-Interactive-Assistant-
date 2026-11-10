"""Authentication, limits, configuration changes, and safe streaming."""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import appdb, config, usage
from backend.errors import APIError
from backend.llm import MockClient
from backend.server import app

PREFS = {"players":4, "has_host":False, "has_dedicated_components":False,
         "scene":"friends", "mood":"relaxed", "language":"en"}
SESSION = {"game_id":"uno", "lang":"en", "players":4}
QUESTION = {"game_id":"uno", "lang":"en", "question":"How do we play?"}


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "_verified", True)
    for key, value in {"DEPLOY_MODE":"local", "MODEL_PROVIDER":"mock", "MODEL_NAME":"",
                       "MODEL_BASE_URL":"", "MODEL_API_KEY":"", "APP_DB_PATH":str(tmp_path / "app.db"),
                       "TRACE_FILE":str(tmp_path / "traces.jsonl"), "RATE_LIMIT_PER_MIN":"100",
                       "DAILY_TOKEN_BUDGET":"2000000", "PLAN_CACHE":"0", "DEBUG":"1"}.items():
        monkeypatch.setenv(key, value)
    return tmp_path




def session_id(response) -> str:
    for block in response.text.split("\n\n"):
        if block.startswith("event: session\n"):
            return json.loads(block.split("data: ", 1)[1])["session_id"]
    raise AssertionError(response.text)


def test_startup_verification_uses_existing_configuration(tmp_path: Path) -> None:
    env = {**os.environ, "MODEL_PROVIDER":"mock", "APP_DB_PATH":str(tmp_path / "app.db")}
    result = subprocess.run([sys.executable, "-c", "from backend.config import is_verified; print(is_verified())"],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"
    env["MODEL_PROVIDER"] = "catalog"
    result = subprocess.run([sys.executable, "-c", "from backend.config import is_verified; print(is_verified())"],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"






def test_rate_limit_is_atomic_for_concurrent_requests(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "2")

    def attempt() -> str:
        try:
            usage.begin_request("alice", enforce_limits=True)
            return "ok"
        except APIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(lambda _: attempt(), range(12)))
    assert outcomes.count("ok") == 2 and outcomes.count("rate_limited") == 10




def test_local_settings_verification_matrix_and_secret_redaction(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (isolated / ".env").write_text("# keep this line\nCUSTOM=value\nMODEL_NAME=old\nOTHER='unchanged'\n", encoding="utf-8")
    client = TestClient(app)
    assert client.get("/health").json()["verified"] is True
    sid = session_id(client.post("/api/sessions", json=SESSION))
    secret = "fixture-very-private-key"
    responses = [client.put("/api/settings", json={"provider":"mock", "base_url":"", "model":"", "api_key":secret})]
    assert responses[-1].status_code == 200 and responses[-1].json()["verified"] is False
    assert (isolated / ".env").stat().st_mode & 0o777 == 0o600
    env_text = (isolated / ".env").read_text()
    assert "# keep this line\nCUSTOM=value\n" in env_text and "OTHER='unchanged'\n" in env_text
    responses += [client.get("/api/settings"), client.get("/health"), client.get("/api/meta"),
                  client.post("/recommend", json=PREFS), client.post("/rules/explain", json={"game_id":"uno"}),
                  client.post(f"/api/sessions/{sid}/next"), client.post("/api/sessions", json=SESSION),
                  client.post(f"/api/sessions/{sid}/message", json={"text":"Hello"}),
                  client.post("/api/qa", json=QUESTION)]
    assert [response.status_code for response in responses[-9:]] == [200, 200, 200, 200, 200, 200, 409, 409, 409]
    assert all(response.json()["error"]["code"] == "llm_not_configured" for response in responses[-3:])
    responses.append(client.post("/api/settings/test"))
    assert responses[-1].status_code == 200 and responses[-1].json()["ok"] is True
    assert client.get("/health").json()["verified"] is True
    responses.extend([client.post("/api/qa", json=QUESTION),
                      client.post(f"/api/sessions/{sid}/message", json={"text":secret}),
                      client.get(f"/api/sessions/{sid}")])
    assert all(response.status_code == 200 for response in responses[-3:])
    assert "[REDACTED]" in responses[-1].text
    assert all(secret not in response.text for response in responses)
    assert secret not in caplog.text and secret not in (isolated / "traces.jsonl").read_text()
    assert client.post("/api/login", json={"username":"a", "password":"b"}).status_code == 404
    cors = client.options("/games", headers={"Origin":"http://localhost:3000", "Access-Control-Request-Method":"GET"})
    assert "access-control-allow-origin" not in cors.headers


def test_failed_test_and_bad_requests_never_return_secret(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "another-fixture-secret"
    client = TestClient(app)
    assert client.put("/api/settings", json={"provider":"mock", "base_url":"", "model":"", "api_key":secret}).status_code == 200

    def fail(_profile):
        raise RuntimeError(secret)

    monkeypatch.setattr("backend.settings_api.make_client", fail)
    failed = client.post("/api/settings/test")
    invalid = client.put("/api/settings", json={"provider":"codex_cli", "base_url":"", "model":"x", "api_key":secret})
    missing = client.get("/api/nonexistent")
    assert failed.status_code == 502 and invalid.status_code == 422 and missing.status_code == 404
    assert all("error" in response.json() and secret not in response.text for response in (failed, invalid, missing))
    assert not client.get("/health").json()["verified"] and secret not in caplog.text


def test_openai_compatible_settings_apply_without_restart(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    secret = "fixture'key\\with-special-chars"
    response = client.put("/api/settings", json={"provider":"compatible", "base_url":"https://example.invalid/v1",
                                                  "model":"fixture-model", "api_key":secret})
    assert response.status_code == 200 and secret not in response.text
    profile = config.product_profile()
    assert profile and profile.transport == "openai_compat" and profile.model == "fixture-model"
    monkeypatch.delenv("MODEL_API_KEY")
    assert config.read_api_key(profile) == secret
    monkeypatch.setattr("backend.settings_api.make_client", lambda _profile: MockClient())
    assert client.post("/api/settings/test").json()["ok"] is True
    assert client.get("/api/settings").json()["verified"] is True


def test_model_echo_and_unexpected_errors_do_not_disclose_secrets(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = TestClient(app)
    sid = session_id(client.post("/api/sessions", json=SESSION))
    secret = "fixture-echoed-secret"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    monkeypatch.setattr("backend.api.make_client", lambda _profile: MockClient(script=[{"content":secret}]))
    response = client.post(f"/api/sessions/{sid}/message", json={"text":"Hello"})
    assert response.status_code == 200 and secret not in response.text and "[REDACTED]" in response.text

    def fail(*_args):
        raise RuntimeError(secret)

    monkeypatch.setattr("backend.api.session.game_card", fail)
    error = client.get("/api/games/uno")
    assert error.status_code == 500 and error.json()["error"]["code"] == "internal_error"
    assert secret not in error.text and secret not in caplog.text and secret not in (isolated / "traces.jsonl").read_text()


def test_stream_timeout_finishes_and_does_not_save_session(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.tutor import plan

    original = plan.resolve_plan

    def slow(*args, **kwargs):
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setenv("SSE_TIMEOUT_S", "0.02")
    monkeypatch.setattr(plan, "resolve_plan", slow)
    response = TestClient(app).post("/api/sessions", json=SESSION)
    assert "event: error" in response.text and '"code": "stream_timeout"' in response.text
    assert response.text.rstrip().endswith("event: done\ndata: {}")
    time.sleep(0.15)
    with appdb.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
