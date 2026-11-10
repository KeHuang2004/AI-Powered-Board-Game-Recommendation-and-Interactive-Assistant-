"""Guided-play state, tool execution, cache, and SSE contract tests."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import appdb, config
from backend.harness.tools import RunCtx
from backend.llm import MockClient, ToolCall
from backend.server import app
from backend.tutor import session, tools
from backend.tutor.state import SessionState


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "_verified", True)
    for key, value in {"MODEL_PROVIDER":"mock", "TUTOR":"t1", "DEBUG":"1", "PLAN_CACHE":"1",
                       "APP_DB_PATH":str(tmp_path / "app.db"), "TRACE_FILE":str(tmp_path / "trace.jsonl")}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def events(response) -> list[tuple[str, dict]]:
    parsed = []
    for block in response.text.strip().split("\n\n"):
        if block.startswith("event: "):
            event, data = block.split("\n", 1)
            parsed.append((event[7:], json.loads(data[6:])))
    return parsed


def create(client: TestClient, **changes: object) -> tuple[str, list[tuple[str, dict]]]:
    body = {"game_id":"uno", "lang":"en", "players":4, **changes}
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 200
    result = events(response)
    assert result[-1][0] == "done"
    view = next(data for name, data in result if name == "session")
    return view["session_id"], result


def test_plan_cache_and_player_names_are_not_in_prompt(client: TestClient, tmp_path: Path) -> None:
    _, first = create(client, player_names=["Alice", "Bob", "Cathy", "Dan"])
    _, second = create(client, player_names=["Eve", "Fred", "Grace", "Hank"])
    assert first[0] == ("status", {"stage":"planning"})
    assert second[0] == ("status", {"stage":"cache_hit"})
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    plans = [item for item in traces if item["kind"] == "plan"]
    assert len(plans) == 1
    assert "Alice" not in json.dumps(plans[0], ensure_ascii=False)


def test_disabled_plan_cache_still_writes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAN_CACHE", "0")
    _, first = create(client)
    _, second = create(client)
    assert first[0][1]["stage"] == second[0][1]["stage"] == "planning"
    with appdb.connect() as connection:
        assert connection.execute("SELECT count(*) FROM plan_cache").fetchone()[0] == 1


def test_next_and_jump_do_not_invoke_model(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid, _ = create(client)
    monkeypatch.setattr("backend.api.make_client", lambda profile: pytest.fail("Unexpected model call"))
    jump = client.post(f"/api/sessions/{sid}/jump", json={"phase":"first_round"})
    assert jump.status_code == 200 and jump.json()["state"]["phase"] == "first_round"
    total = jump.json()["state"]["total_steps"]
    for _ in range(total + 1):
        last = client.post(f"/api/sessions/{sid}/next")
    assert last.status_code == 200 and last.json()["state"]["phase"] == "qa"
    assert last.json()["current"] is None and last.json()["state"]["index"] == total
    back = client.post(f"/api/sessions/{sid}/jump", json={"phase":"setup"})
    assert back.json()["state"]["phase"] == "setup" and back.json()["state"]["index"] == 0


def test_mock_roll_uses_code_and_emits_tool_event(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid, _ = create(client)
    client.post(f"/api/sessions/{sid}/jump", json={"phase":"first_round"})
    monkeypatch.setattr("backend.tutor.tools.secrets.randbelow", lambda sides: sides - 1)
    response = client.post(f"/api/sessions/{sid}/message", json={"text":"Please roll a die"})
    result = events(response)
    tool = next(data for name, data in result if name == "tool")
    assert tool["name"] == "roll_dice" and tool["result"]["values"] == [6]
    assert any(name == "state" and data["dice_log"][-1]["values"] == [6] for name, data in result)
    assert result[-1][0] == "done"


def test_unknown_player_is_a_tool_observation(client: TestClient) -> None:
    sid, _ = create(client)
    record = session.load_session(sid)
    ctx = RunCtx(kind="tutor_turn", template="tutor_turn", state=record.state,
                 game_id=record.game_id, lang=record.lang)
    outcome = tools.registry().execute(ToolCall(id="x", name="update_score",
                                                arguments=json.dumps({"player":"Missing", "delta":1})), ctx)
    assert outcome.error_kind == "tool_error" and "Missing" in outcome.result["error"]
    response = client.post(f"/api/sessions/{sid}/tools/update_score", json={"player":"Missing", "delta":1})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_tool_call"


def test_set_players_preserves_scores_and_initial_system(client: TestClient) -> None:
    sid, _ = create(client)
    record = session.load_session(sid)
    before = session.system_text(record)
    client.post(f"/api/sessions/{sid}/tools/update_score", json={"player":"P1", "delta":3})
    updated = client.post(f"/api/sessions/{sid}/tools/set_players", json={"names":["P1", "New"]})
    assert updated.json()["state"]["scores"] == {"P1":3, "New":0}
    after = session.load_session(sid)
    assert session.system_text(after) == before
    messages = session.build_turn_messages(after, "Continue")
    assert "players: P1, New" in messages[-1]["content"]
    assert "set_players" in messages[-1]["content"]


def test_set_players_supports_up_to_one_hundred(client: TestClient) -> None:
    sid, _ = create(client)
    names = [f"P{index}" for index in range(1, 101)]
    response = client.post(f"/api/sessions/{sid}/tools/set_players", json={"names":names})
    assert response.status_code == 200 and len(response.json()["state"]["players"]) == 100
    assert client.post(f"/api/sessions/{sid}/tools/set_players", json={"names":[*names, "P101"]}).status_code == 400


def test_t0_has_no_plan_state_or_advance(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUTOR", "t0")
    sid, start = create(client)
    view = next(data for name, data in start if name == "session")
    assert start[0][1]["stage"] == "ready"
    assert view["plan"] is None and view["state"]["phase"] == "qa"
    record = session.load_session(sid)
    assert "<plan>" not in session.system_text(record)
    assert "<state>" not in session.build_turn_messages(record, "hello")[-1]["content"]
    assert "advance_step" not in tools.exposed_tools(record.state, True)
    assert client.post(f"/api/sessions/{sid}/next").json()["error"]["code"] == "not_available_in_t0"
    assert client.post(f"/api/sessions/{sid}/jump", json={"phase":"qa"}).status_code == 409


def test_system_differences_are_only_mode_branch_and_plan(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid1, _ = create(client)
    monkeypatch.setenv("TUTOR", "t0")
    sid0, _ = create(client)
    t0 = session.system_text(session.load_session(sid0))
    t1 = session.system_text(session.load_session(sid1))
    template = Path("prompts/runtime/tutor_system.j2").read_text(encoding="utf-8")
    t1_line = next(line for line in template.splitlines() if line.startswith("- Follow the lesson plan"))
    t0_line = next(line for line in template.splitlines() if line.startswith("- Guide them through"))
    t0 = t0.replace(t0_line, "")
    t1 = re.sub(r"\n<plan>\n.*?\n</plan>", "", t1, flags=re.DOTALL).replace(t1_line, "")
    assert t0.strip() == t1.strip()


@pytest.mark.parametrize(("mode", "phase", "expected"), [
    ("t1", "setup", {"advance_step", "set_players", "get_game"}),
    ("t1", "first_round", {"advance_step", "roll_dice", "update_score", "set_players", "get_game"}),
    ("t1", "ending", {"advance_step", "roll_dice", "update_score", "set_players", "get_game"}),
    ("t1", "qa", {"roll_dice", "update_score", "set_players", "get_game"}),
    ("t0", "qa", {"roll_dice", "update_score", "set_players", "get_game"}),
])
def test_phase_tool_sets(mode: str, phase: str, expected: set[str]) -> None:
    state = SessionState(mode=mode, phase=phase, players=["A"], scores={"A":0}, lang="en")
    assert set(tools.exposed_tools(state, False)) == expected
    assert set(tools.exposed_tools(state, True)) == expected | {"lookup_rulebook"}


def test_source_tags_and_debug_trace(client: TestClient, tmp_path: Path) -> None:
    sid, _ = create(client)
    record = session.load_session(sid)
    script = [
        {"tool_calls":[{"id":"a", "name":"set_players", "arguments":'{"names":["P1","New"]}'}]},
        {"content":"Rule [[dataset]], knowledge [[general]], bad [[other]]."},
    ]
    result = session.message(record, "Update players", MockClient(script=script))
    assert result["message"]["tags"] == ["dataset", "general"]
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    trace = next(item for item in traces if item["kind"] == "tutor_turn")
    assert trace["invalid_source_tags"] == ["other"]
    llm = next(span for span in trace["spans"] if span["type"] == "llm")
    assert llm["messages"] and llm["tools"]
    tool = next(span for span in trace["spans"] if span["type"] == "tool")
    assert tool["state_before"]["players"] != tool["state_after"]["players"]


def test_catalog_mode_blocks_llm_but_not_metadata(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "catalog")
    response = client.post("/api/sessions", json={"game_id":"uno","lang":"en","players":4})
    assert response.status_code == 409 and response.json()["error"]["code"] == "llm_not_configured"
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/games/uno?lang=en").status_code == 200


def test_session_conflict_does_not_overwrite_newer_state(client: TestClient) -> None:
    sid, _ = create(client)
    first = session.load_session(sid)
    stale = session.load_session(sid)
    session.next_step(first)
    with pytest.raises(session.SessionConflict):
        session.next_step(stale)
    assert session.load_session(sid).state.index == 1


def test_tools_follow_phase_changes_within_turn(client: TestClient) -> None:
    sid, _ = create(client)
    record = session.load_session(sid)
    session.jump(record, "ending")
    scripted = [
        {"tool_calls":[
            {"id":"a", "name":"advance_step", "arguments":"{}"},
            {"id":"b", "name":"advance_step", "arguments":"{}"},
        ]},
        {"content":"Finished."},
    ]

    class Capture(MockClient):
        def __init__(self) -> None:
            super().__init__(script=scripted)
            self.names: list[list[str]] = []

        def chat(self, *args: object, **kwargs: object):
            self.names.append([item["function"]["name"] for item in kwargs.get("tools", [])])
            return super().chat(*args, **kwargs)

    mock = Capture()
    result = session.message(record, "Proceed", mock)
    assert result["state"]["phase"] == "qa"
    assert "advance_step" in mock.names[0] and "advance_step" not in mock.names[1]
    assert "error" in result["events"][1]["result"]


def test_plan_validator_rejects_missing_setup_blank_steps_and_excess() -> None:
    from backend.tutor.plan import LessonPlan, Step, validate_plan

    data = json.loads((Path("tests/fixtures/mock_auto") / "plan_user.json").read_text())
    lesson = LessonPlan.model_validate(data)
    ctx = RunCtx(kind="plan", template="plan_user", lang="en")
    assert validate_plan(lesson, ctx) == []
    short = lesson.model_copy(update={"setup":lesson.setup[:1]})
    assert validate_plan(short, ctx)
    blank = lesson.model_copy(update={"setup":[lesson.setup[0].model_copy(update={"text":" "}), lesson.setup[1]]})
    assert validate_plan(blank, ctx)
    excess = lesson.model_copy(update={"pitfalls":[Step(id=str(index), text="x", src="dataset") for index in range(31)]})
    assert validate_plan(excess, ctx)


def test_failed_plan_does_not_claim_a_fallback(tmp_path: Path) -> None:
    from backend.catalog import GAMES_BY_ID
    from backend.tutor.plan import prepare_plan, resolve_plan

    prepared = prepare_plan(GAMES_BY_ID["uno"], "en", 4)
    with pytest.raises(RuntimeError, match="Lesson plan"):
        resolve_plan(prepared, MockClient(script=[{"content":"no"}] * 3))
    trace = json.loads((tmp_path / "trace.jsonl").read_text().splitlines()[-1])
    assert trace["fallback"] is False and trace["interventions"]["no_submit"] == 3
