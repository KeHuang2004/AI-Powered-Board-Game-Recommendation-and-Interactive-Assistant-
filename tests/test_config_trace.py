"""Verify configuration precedence, non-disclosure, schemas, and runtime storage."""
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend import database, setting
from backend.harness.prompts import metadata
from backend.harness.tools import RunCtx, Tool, ToolRegistry
from backend.harness.trace import Trace
from backend.llm import (
    LLMResult,
    OpenAICompatClient,
    ToolCall,
    make_client,
    probe_capabilities,
)


def test_product_profile_and_legacy_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    example_model = "fixture-model"
    monkeypatch.setattr(setting, "ROOT", tmp_path)
    for key in ("MODEL_PROVIDER", "RECOMMEND_PROVIDER", "MODEL_NAME", "GEMINI_MODEL", "MODEL_API_KEY",
                "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "RECOMMEND_API_KEY", "MODEL_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MODEL_PROVIDER", "catalog")
    assert setting.product_profile() is None
    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    assert setting.product_profile() is not None
    monkeypatch.setenv("MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv("MODEL_NAME", "")
    assert setting.product_profile() is None
    monkeypatch.setenv("MODEL_NAME", example_model)
    profile = setting.product_profile()
    assert profile and profile.api_key_env == "DEEPSEEK_API_KEY"
    assert "test-secret" not in profile.model_dump_json()
    assert "test-secret" not in repr(profile)
    (tmp_path / ".env").write_text("MODEL_PROVIDER=catalog\n", encoding="utf-8")
    assert setting.product_profile() is not None
    setting.reload_product_profile()
    assert setting.is_verified() is False




def test_key_file_never_serialized_and_trace_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    
    secret = "fixture-private-value"
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps({"api_key":secret}), encoding="utf-8")
    profile = setting.Profile(name="fixture", model="fixture-model", family="fixture", transport="mock").model_copy(update={"transport":"openai_compat", "base_url":"https://example.invalid",
                                                  "api_key_file":str(key_path)})
    monkeypatch.setattr("backend.llm.OpenAI", lambda **kwargs: SimpleNamespace())
    client = OpenAICompatClient(profile)
    assert secret not in repr(profile) and secret not in profile.model_dump_json()
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("TRACE_FILE", str(trace_file))
    monkeypatch.setenv("DEBUG", "1")
    trace = Trace("probe", "probe_user", request={"headers":{"Authorization":secret}}, redact=client._redact)
    arguments = json.dumps({"headers": {"Authorization": secret}, "reasoning_content": secret})
    result = LLMResult(content="ok", tool_calls=[ToolCall(id="a", name="x", arguments=arguments)],
                       raw={"protocol":"text", "reasoning_content":secret})
    trace.add_llm([{"role":"user", "content":secret, "reasoning_content":secret}], result)
    trace.write()
    written = trace_file.read_text(encoding="utf-8")
    assert secret not in written and "reasoning_content\"" not in written and "Authorization" not in written
    data = json.loads(written)
    assert data["template"] == metadata("probe_user")
    assert data["protocol"] == "text"
    assert data["spans"][0]["messages_hash"]
    assert data["spans"][0]["usage"]["cached_tokens"] is None


def test_submit_schema_uses_template(monkeypatch: pytest.MonkeyPatch) -> None:
    class First(BaseModel):
        first: str

    class Second(BaseModel):
        second: int

    module = types.ModuleType("test_schema_module")
    module.First = First
    module.Second = Second
    monkeypatch.setitem(sys.modules, module.__name__, module)
    tool = Tool(name="submit_test", params=First, terminal=True, handler=lambda args, ctx: args.model_dump())
    registry = ToolRegistry([tool])
    registry.definitions["submit_test"] = "Test"
    registry.definitions["_params_by_template"].update(
        template_a="test_schema_module:First", template_b="test_schema_module:Second"
    )
    first = registry.schemas(["submit_test"], RunCtx(kind="probe", template="template_a"))
    second = registry.schemas(["submit_test"], RunCtx(kind="probe", template="template_b"))
    assert set(first[0]["function"]["parameters"]["properties"]) == {"first"}
    assert set(second[0]["function"]["parameters"]["properties"]) == {"second"}


def test_runtime_db_tables(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    with database.connect_runtime(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"recommendations", "plan_cache", "sessions", "web_cache"}
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["mid", "strong"])
def test_mock_probe_for_native_and_text(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRACE_FILE", str(tmp_path / "trace.jsonl"))
    profile = setting.Profile(name=name, model="fixture-model", family="fixture", transport="mock", force_text_protocol=name == "strong")
    result = probe_capabilities(make_client(profile), long_context=True)
    assert result["chat"] and result["tool_calling"] and result["multi_turn_tool"] and result["long_context"]
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    names = {asset["name"] for record in traces for asset in record["prompt_assets"]}
    assert {"probe_system", "probe_user", "probe_tool_user", "probe_long_user", "tools"} <= names
    assert any(span.get("tool_schema_sha256") for record in traces for span in record["spans"])
    if name == "strong":
        assert {"text_tool_protocol", "text_transcript"} <= names


def test_probe_checks_reasoning_replay_without_recording_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRACE_FILE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("DEBUG", "1")
    private_reasoning = "private reasoning for replay"

    class RequiresReplay:
        replay_reasoning = False
        profile = setting.Profile(name="fixture", family="fixture", transport="mock")

        def chat(self, messages: list[dict], tools: list[dict] | None = None,
                 ctx: object = None, **kwargs: object) -> LLMResult:
            value = ctx.allowed_ids[0]
            if tools and not any(message["role"] == "tool" for message in messages):
                result = LLMResult(tool_calls=[ToolCall(id="id", name="probe_echo",
                                                       arguments=json.dumps({"value":value}))])
                result._reasoning_content = private_reasoning
                return result
            if any(message["role"] == "tool" for message in messages):
                assistant = next(message for message in messages if message["role"] == "assistant")
                if assistant.get("reasoning_content") != private_reasoning:
                    raise RuntimeError("reasoning required")
            return LLMResult(content=value)

    client = RequiresReplay()
    result = probe_capabilities(client)
    assert result["multi_turn_tool"] is True
    assert result["reasoning_content_replay"] == "required"
    assert client.replay_reasoning is True
    assert private_reasoning not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
