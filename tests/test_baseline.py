"""Regression tests for the team's catalog-mode API."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.server import app

FIXTURES = Path(__file__).parent / "fixtures" / "baseline_recommend"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("MODEL_PROVIDER", "catalog")
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.db"))
    return TestClient(app)


def assert_original_fields(actual: object, expected: object) -> None:
    """Allow new mapping fields while preserving all original fields and lists."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual
            assert_original_fields(actual[key], value)
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected, strict=True):
            assert_original_fields(actual_item, expected_item)
    else:
        assert actual == expected


def test_health_and_games(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["games_loaded"] == 175
    games = client.get("/games")
    assert games.status_code == 200
    assert len(games.json()) == 175


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_catalog_recommendations(client: TestClient, fixture: Path) -> None:
    snapshot = json.loads(fixture.read_text(encoding="utf-8"))
    response = client.post("/recommend", json=snapshot["request"])
    assert response.status_code == 200
    assert_original_fields(response.json(), snapshot["response"])


def test_raw_rules_return_core_rules(client: TestClient) -> None:
    game_id = client.get("/games").json()[0]["game_id"]
    core = client.get(f"/games/{game_id}/core-rules")
    response = client.post("/rules/explain", json={"game_id": game_id, "mode": "raw"})
    assert core.status_code == response.status_code == 200
    assert response.json()["method"] == "raw"
    assert response.json()["answer"] == core.json()["core_rules"]
    assert response.json()["source_rules"] == core.json()["core_rules"]
