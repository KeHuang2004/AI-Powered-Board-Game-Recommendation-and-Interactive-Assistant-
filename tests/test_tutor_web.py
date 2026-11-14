"""Mock-only web search, caching, provenance, and experiment isolation."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import database, setting
from backend.database import get_games_by_id
from backend.harness.prompts import render
from backend.harness.tools import RunCtx, Tool
from backend.llm import MockClient, ToolCall
from backend.server import app
from backend.tutor import plan, session, tools, web
from backend.tutor.state import SessionState

FIXTURES = Path("tests/fixtures/d9b")


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in {"MODEL_PROVIDER": "mock", "SEARCH_PROVIDER": "mock", "TUTOR": "t1",
                       "DEPLOY_MODE": "local", "PLAN_CACHE": "1", "DEBUG": "1",
                       "APP_DB_PATH": str(tmp_path / "app.db"), "TRACE_FILE": str(tmp_path / "trace.jsonl")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(setting, "_verified", True)

    def no_network(*args, **kwargs):
        raise AssertionError("Test attempted a network request")

    monkeypatch.setattr(web.httpx, "post", no_network)


def stream_events(response) -> list[tuple[str, dict]]:
    result = []
    for block in response.text.strip().split("\n\n"):
        if block.startswith("event: "):
            event, data = block.split("\n", 1)
            result.append((event[7:], json.loads(data[6:])))
    return result


def non_rulebook_game() -> str:
    return next(game_id for game_id, game in get_games_by_id().items() if not game.get("rulebook"))


def test_disabled_templates_and_submit_schema_match_pre_d9b(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    cases = json.loads((FIXTURES / "rendered_systems_v1.json").read_text())
    for case in cases:
        assert render(case["template"], **case["variables"], web_enabled=False) == case["rendered"]
    registry = tools.registry()
    registry.register(Tool(name="submit_lesson_plan", params=plan.LessonPlan, handler=plan._submit,
                           validator=plan.validate_plan, terminal=True))
    ctx = RunCtx(kind="plan", template="plan_user", lang="en")
    actual = registry.schemas(["submit_lesson_plan"], ctx,
                              {"submit_lesson_plan": plan.plan_tool_schema(False)})
    assert actual == json.loads((FIXTURES / "plan_tools_v1.json").read_text())
    enabled_schema = plan.plan_tool_schema(True)
    assert enabled_schema["$defs"]["Step"]["properties"]["src"]["enum"][-1] == "web"


def test_deepseek_request_contract_uses_only_mocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def fake_post(url, *, headers, json, timeout):
        requests.append((url, headers, json, timeout))
        body = ({"results": [{"title": "Rules", "url": "https://example.test/rules", "snippet": "setup"}]}
                if url.endswith("/search") else {"title": "Rules", "content": "a" * 9000})
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(web.httpx, "post", fake_post)
    backend = web.DeepSeekSearchBackend("fixture-secret")
    assert backend.search("game rules", 3) == [{"title": "Rules", "url": "https://example.test/rules",
                                               "snippet": "setup"}]
    page = backend.fetch("https://example.test/rules")
    assert len(page["content"]) == 8000 and page["url"] == "https://example.test/rules"
    assert requests[0] == ("https://api.deepseek.com/v1/tools/search",
                           {"Authorization": "Bearer fixture-secret", "Content-Type": "application/json"},
                           {"query": "game rules", "count": 3, "need_content": False}, 20.0)
    assert requests[1][0] == "https://api.deepseek.com/v1/tools/fetch"
    assert requests[1][2] == {"url": "https://example.test/rules"} and requests[1][3] == 20.0


def test_deepseek_timeout_does_not_expose_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("fixture-secret", request=httpx.Request("POST", "https://example.test/"))

    monkeypatch.setattr(web.httpx, "post", timeout)
    with pytest.raises(web.SearchError) as error:
        web.DeepSeekSearchBackend("fixture-secret").search("rules")
    assert "fixture-secret" not in str(error.value)


def test_web_prompts_include_rules_version_without_changing_disabled_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = get_games_by_id()["500-rummy"]
    assert game["text"]["zh"]["extra"]["rules_version"]
    enabled = plan.prepare_plan(game, "en", 4)
    assert '"rules_version"' in enabled.messages[-1]["content"]
    record = session.create_session(game["game_id"], "en", ["A"], "t0", None)
    assert '"rules_version"' in session.system_text(record)
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    disabled = plan.prepare_plan(game, "en", 4)
    assert '"rules_version"' not in disabled.messages[-1]["content"]
    assert '"rules_version"' not in session.system_text(record)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://user:password@example.test/", "https://[bad"])
def test_fetch_rejects_invalid_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ValueError):
        tools.FetchPage(url=url)


def test_plan_search_fetch_cache_and_second_session_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    game_id = non_rulebook_game()
    fixture = json.loads(Path("tests/fixtures/mock_auto/plan_user.json").read_text())
    fixture["setup"][0]["src"] = "web"
    script = [
        {"tool_calls": [{"id": "s", "name": "web_search", "arguments": '{"query":"game rules"}'}]},
        {"tool_calls": [{"id": "f", "name": "fetch_page", "arguments": '{"url":"https://example.test/rules"}'}]},
        {"tool_calls": [{"id": "p", "name": "submit_lesson_plan", "arguments": json.dumps(fixture)}]},
    ]
    monkeypatch.setattr("backend.server.make_client", lambda _profile: MockClient(script=script))
    client = TestClient(app)
    body = {"game_id": game_id, "lang": "en", "players": 4}
    first = stream_events(client.post("/api/sessions", json=body))
    first_view = next(data for name, data in first if name == "session")
    assert first_view["current"]["src"] == "web"
    with database.connect_runtime() as connection:
        row = connection.execute("SELECT game_id, url, title, text FROM web_cache").fetchone()
    assert row["game_id"] == game_id and row["url"] == "https://example.test/rules"
    assert "Set up" in row["text"]
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    plan_trace = next(trace for trace in traces if trace["kind"] == "plan")
    assert [span["name"] for span in plan_trace["spans"] if span["type"] == "tool"] == [
        "web_search", "fetch_page", "submit_lesson_plan"]
    search_span = next(span for span in plan_trace["spans"] if span.get("name") == "web_search")
    assert search_span["result"] == [{"title": "Example game rules", "url": "https://example.test/rules",
                                      "snippet": "Example setup and turn rules."}]
    assert "web_search" in [schema["function"]["name"] for schema in plan_trace["spans"][0]["tools"]]

    second = stream_events(client.post("/api/sessions", json=body))
    assert second[0] == ("status", {"stage": "cache_hit"})
    second_id = next(data["session_id"] for name, data in second if name == "session")
    message_script = [
        {"tool_calls": [{"id": "l", "name": "lookup_rulebook", "arguments": '{"query":"setup game"}'}]},
        {"content": "Set up the game. [[web]]"},
    ]
    monkeypatch.setattr("backend.server.make_client", lambda _profile: MockClient(script=message_script))
    events = stream_events(client.post(f"/api/sessions/{second_id}/message", json={"text": "How do we start?"}))
    lookup = next(data for name, data in events if name == "tool")
    assert lookup["name"] == "lookup_rulebook" and lookup["result"]["passages"][0]["source"] == "web"
    answer = next(data for name, data in events if name == "message")
    assert answer["tags"] == ["web"]
    assert answer["sources"] == [{"title": "Example game rules", "url": "https://example.test/rules"}]
    stored = client.get(f"/api/sessions/{second_id}").json()
    assert stored["history"][-1]["sources"] == answer["sources"]
    assert not any(data.get("name") in {"web_search", "fetch_page"} for name, data in events if name == "tool")


def test_plan_sources_survive_session_and_plan_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    game_id = non_rulebook_game()
    item = json.loads(Path("tests/fixtures/mock_auto/plan_user.json").read_text())
    item["setup"][0]["src"] = "web"
    script = [
        {"tool_calls": [{"id": "f", "name": "fetch_page", "arguments": '{"url":"https://example.test/rules"}'}]},
        {"tool_calls": [{"id": "p", "name": "submit_lesson_plan", "arguments": json.dumps(item)}]},
    ]
    prepared = plan.prepare_plan(get_games_by_id()[game_id], "en", 4)
    lesson = plan.resolve_plan(prepared, MockClient(script=script))
    saved = session.create_session(game_id, "en", ["A", "B", "C", "D"], "t1", lesson)
    loaded = session.load_session(saved.session_id)
    cached = plan.prepare_plan(get_games_by_id()[game_id], "en", 4).cached
    assert cached and cached._web_sources == loaded.plan._web_sources
    reply = session.message(loaded, "Explain", MockClient(script=[{"content": "Rules. [[web]]"}]))
    assert reply["message"]["sources"] == [{"title": "Example game rules", "url": "https://example.test/rules"}]


def test_existing_rulebook_plan_does_not_expose_web_tools(tmp_path: Path) -> None:
    prepared = plan.prepare_plan(get_games_by_id()["uno"], "en", 4)
    assert prepared.has_rulebook and not prepared.web_enabled
    plan.resolve_plan(prepared, MockClient())
    trace = json.loads((tmp_path / "trace.jsonl").read_text().splitlines()[-1])
    names = {schema["function"]["name"] for span in trace["spans"] for schema in span.get("tools", [])}
    assert names == {"submit_lesson_plan"}


@pytest.mark.parametrize(("retries", "opens"), [(1, False), (2, True)])
def test_lookup_limit_forces_plan_submission_and_bounds_corrections(
    monkeypatch: pytest.MonkeyPatch, retries: int, opens: bool,
) -> None:
    monkeypatch.setenv("MAX_TOOL_ITERS", "2")
    monkeypatch.setenv("MAX_SUBMIT_RETRIES", str(retries))
    fixture = (Path("tests/fixtures/mock_auto") / "plan_user.json").read_text()
    valid = json.loads(fixture)
    valid["setup"][0]["src"] = "web"
    invalid = json.loads(fixture)
    invalid["setup"][0]["src"] = "rulebook"

    def response(call_id, name, arguments):
        return {"tool_calls": [{"id": call_id, "name": name, "arguments": json.dumps(arguments)}]}

    script = [
        response("search", "web_search", {"query": "game rules"}),
        response("read", "fetch_page", {"url": "https://example.test/rules"}),
        response("extra", "fetch_page", {"url": "https://example.test/extra"}),
        response("invalid", "submit_lesson_plan", invalid),
        response("corrected", "submit_lesson_plan", valid),
    ]
    requests = []

    class RecordingClient(MockClient):
        def chat(self, messages, tools=None, tool_choice=None, **kwargs):
            submission = next(tool["function"] for tool in tools if tool["function"]["name"] == "submit_lesson_plan")
            sources = submission["parameters"]["$defs"]["Step"]["properties"]["src"]["enum"]
            requests.append((tool_choice, [tool["function"]["name"] for tool in tools], sources))
            return super().chat(messages, tools=tools, tool_choice=tool_choice, **kwargs)

    monkeypatch.setattr("backend.server.make_client", lambda _profile: RecordingClient(script=script))
    result = stream_events(TestClient(app).post("/api/sessions", json={
        "game_id": non_rulebook_game(), "lang": "en", "players": 4,
    }))
    forced = {"type": "function", "function": {"name": "submit_lesson_plan"}}
    assert requests[0][0] is requests[1][0] is None
    assert "web" not in requests[1][2]
    assert requests[2:] == [(forced, ["submit_lesson_plan"], ["dataset", "general", "web"])] * (retries + 1)
    assert any(name == "session" for name, _ in result) is opens
    assert any(name == "error" and data["code"] == "session_creation_failed" for name, data in result) is not opens
    with database.connect_runtime() as connection:
        assert [row["url"] for row in connection.execute("SELECT url FROM web_cache")] == [
            "https://example.test/rules",
        ]


def test_auto_mock_end_to_end_search_and_sources(tmp_path: Path) -> None:
    client = TestClient(app)
    game_id = non_rulebook_game()
    events = stream_events(client.post("/api/sessions", json={"game_id": game_id, "lang": "en", "players": 4}))
    assert [data["stage"] for name, data in events if name == "status"] == [
        "planning", "searching", "reading", "checking", "starting",
    ]
    view = next(data for name, data in events if name == "session")
    assert view["current"]["src"] == "web"
    reply_events = stream_events(client.post(f"/api/sessions/{view['session_id']}/message",
                                            json={"text": "How do we start?"}))
    reply = next(data for name, data in reply_events if name == "message")
    assert "[[web]]" in reply["text"] and reply["sources"][0]["url"] == "https://example.test/rules"
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    plan_trace = next(record for record in records if record["kind"] == "plan")
    assert [span["name"] for span in plan_trace["spans"] if span["type"] == "tool"] == [
        "web_search", "fetch_page", "submit_lesson_plan"]
    second = stream_events(client.post("/api/sessions", json={"game_id": game_id, "lang": "en", "players": 3}))
    assert second[0] == ("status", {"stage": "planning"})
    assert next(data for name, data in second if name == "session")["current"]["src"] == "web"
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    second_plan = [record for record in records if record["kind"] == "plan"][-1]
    assert [span["name"] for span in second_plan["spans"] if span["type"] == "tool"] == [
        "lookup_rulebook", "submit_lesson_plan"]


def test_provider_failure_is_a_tool_error_and_key_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "fixture-search-secret"
    monkeypatch.setenv("SEARCH_API_KEY", secret)
    game_id = non_rulebook_game()
    record = session.create_session(game_id, "en", ["A"], "t0", None)

    class Failing:
        def search(self, query, max_results=5):
            raise RuntimeError(secret)

    monkeypatch.setattr(web, "get_backend", lambda: Failing())
    mock = MockClient(script=[
        {"tool_calls": [{"id": "s", "name": "web_search", "arguments": '{"query":"rules"}'}]},
        {"content": f"Unavailable. {secret} [[general]]"},
    ])
    result = session.message(record, "Rules?", mock)
    assert result["events"][0]["result"]["error"] and secret not in str(result["events"])
    trace_text = (tmp_path / "trace.jsonl").read_text()
    trace = json.loads(trace_text.splitlines()[-1])
    assert trace["interventions"]["tool_error"] == 1 and secret not in trace_text


@pytest.mark.parametrize(("mode", "phase"), [("t0", "qa"), ("t1", "setup"), ("t1", "first_round"),
                                            ("t1", "ending"), ("t1", "qa")])
def test_search_exposed_in_each_play_phase(mode: str, phase: str) -> None:
    state = SessionState(mode=mode, phase=phase, players=["A"], scores={"A": 0}, lang="en")
    active = tools.exposed_tools(state, False, web_enabled=True, has_cached=True)
    assert {"web_search", "fetch_page", "lookup_rulebook"} <= set(active)
    disabled = tools.exposed_tools(state, False, web_enabled=False, has_cached=True)
    assert "lookup_rulebook" in disabled and not {"web_search", "fetch_page"} & set(disabled)


def test_rulebook_and_cached_pages_are_ranked_together(monkeypatch: pytest.MonkeyPatch) -> None:
    game_id = non_rulebook_game()
    monkeypatch.setattr(web.database, "read_rulebook", lambda _game_id: {"text": "Official turn order and scoring."})
    web.save_page(game_id, {"url": "https://example.test/rules", "title": "Web rules",
                            "content": "Setup requires the unique scarlet token."}, "en")
    passages = web.search_cached_and_rulebook(game_id, "scarlet token", 2)
    assert len(passages) == 2 and passages[0]["source"] == "web"
    assert any("Official turn order" in passage["text"] for passage in passages)


def test_cache_upserts_per_game_and_url() -> None:
    page = {"url": "https://example.test/rules", "title": "Rules", "content": "First version."}
    web.save_page("one", page, "en")
    web.save_page("one", {**page, "content": "Updated version."}, "zh")
    web.save_page("two", page, "en")
    assert len(web.cached_pages("one")) == len(web.cached_pages("two")) == 1
    assert web.cached_pages("one")[0]["text"] == "Updated version."


def test_settings_switch_changes_only_search_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setting, "ROOT", tmp_path)
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    (tmp_path / ".env").write_text("OTHER=preserved\n", encoding="utf-8")
    client = TestClient(app)
    assert client.get("/health").json()["search_enabled"] is False
    assert client.get("/api/settings").json()["search_enabled"] is False
    assert client.put("/api/settings/search", json={"search_enabled": True}).json() == {"search_enabled": True}
    assert client.get("/health").json()["search_enabled"] is True
    assert client.get("/api/settings").json()["verified"] is True
    assert "OTHER=preserved" in (tmp_path / ".env").read_text()
    assert "SEARCH_PROVIDER='deepseek'" in (tmp_path / ".env").read_text()
    assert client.put("/api/settings/search", json={"search_enabled": False}).json() == {"search_enabled": False}
    assert "SEARCH_PROVIDER='none'" in (tmp_path / ".env").read_text()


def test_offline_cache_is_available_to_product_but_isolated_from_experiments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_id = non_rulebook_game()
    web.save_page(game_id, {"url": "https://example.test/rules", "title": "Cached",
                            "content": "Some rules."}, "en")
    monkeypatch.setenv("SEARCH_PROVIDER", "none")
    assert web.get_backend() is None
    ctx = RunCtx(kind="tutor_turn", template="tutor_turn", game_id=game_id)
    outcome = tools.registry().execute(ToolCall(id="l", name="lookup_rulebook", arguments='{"query":"rules"}'), ctx)
    assert outcome.result["passages"][0]["url"] == "https://example.test/rules"
    experiment_ctx = ctx.model_copy(update={"meta": {"run_id": "experiment"}})
    outcome = tools.registry().execute(ToolCall(id="l", name="lookup_rulebook", arguments='{"query":"rules"}'),
                                       experiment_ctx)
    assert outcome.error_kind == "tool_error"
    record = session.create_session(game_id, "en", ["A"], "t0", None)
    result = session.message(record, "Rules?", MockClient())
    assert result["events"][0]["name"] == "lookup_rulebook"
    assert result["message"]["sources"][0]["url"] == "https://example.test/rules"
    assert not any(event["name"] in {"web_search", "fetch_page"} for event in result["events"])
    prepared = plan.prepare_plan(get_games_by_id()[game_id], "en", 4)
    assert prepared.web_enabled and not prepared.search_enabled
    lesson = plan.resolve_plan(prepared, MockClient())
    assert lesson.setup[0].src == "web"
    experimental = plan.prepare_plan(get_games_by_id()[game_id], "en", 4, experiment=True)
    assert not experimental.web_enabled and "rules_version" not in experimental.messages[-1]["content"]
    assert "Previously fetched web pages" not in session.system_text(record, experiment=True)
    experiment_record = session.create_session(game_id, "en", ["B"], "t0", None)
    experimental_reply = session.message(experiment_record, "Rules?", MockClient(), meta={"run_id": "experiment"})
    assert experimental_reply["events"] == [] and experimental_reply["message"]["sources"] == []
