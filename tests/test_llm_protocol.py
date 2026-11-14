"""Verify text protocol, provider responses, and CLI isolation without network calls."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from backend.setting import Profile
from backend.llm import (
    CallCtx,
    CodexCLIClient,
    CodexToolError,
    LLMResult,
    OpenAICompatClient,
    TextToolProtocol,
)


class CaptureClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[dict] = []
        self.kwargs: dict = {}

    def chat(self, messages: list[dict], **kwargs: object) -> LLMResult:
        self.messages = messages
        self.kwargs = kwargs
        return LLMResult(content=self.content)


def schemas() -> list[dict]:
    return [{"type":"function", "function":{"name":"echo", "description":"Echo value",
                                                "parameters":{"type":"object"}}}]


def test_text_protocol_multiple_calls_and_no_native_tools() -> None:
    raw = '```tool_calls\n[{"name":"echo","arguments":{"value":1}},{"name":"echo","arguments":{"value":2}}]\n```'
    inner = CaptureClient(raw)
    result = TextToolProtocol(inner).chat([{"role":"system","content":"s"}, {"role":"user","content":"u"}],
                                          tools=schemas())
    assert len(result.tool_calls) == 2
    assert json.loads(result.tool_calls[1].arguments) == {"value": 2}
    assert "tools" not in inner.kwargs
    assert result.raw["protocol"] == "text"


@pytest.mark.parametrize(("content", "is_error"), [
    ('```tool_calls\n[{broken]\n```', True),
    ('plain text reply', False),
    ('```tool_calls\n[{"name":"echo","arguments":{}}]\n```', False),
])
def test_text_protocol_parsing(content: str, is_error: bool) -> None:
    result = TextToolProtocol(CaptureClient(content)).chat([{"role":"user","content":"u"}], tools=schemas())
    assert bool(result.raw.get("protocol_error")) == is_error
    if content == "plain text reply":
        assert not result.tool_calls and result.content == content
    elif not is_error:
        assert len(result.tool_calls) == 1


def test_text_protocol_forced_no_tool_appends_instruction() -> None:
    inner = CaptureClient("final")
    result = TextToolProtocol(inner).chat([{"role":"user","content":"u"}], tools=schemas(), tool_choice="none")
    assert result.content == "final"
    assert "Do not call any tools" in inner.messages[1]["content"]
    assert "tool_choice" not in inner.kwargs
    invalid = CaptureClient('```tool_calls\n[{"name":"echo","arguments":{}}]\n```')
    rejected = TextToolProtocol(invalid).chat([{"role":"user","content":"u"}], tools=schemas(),
                                             tool_choice="none")
    assert not rejected.tool_calls and rejected.raw.get("protocol_error")


def profile(transport: str) -> Profile:
    config = Profile(name="fixture", model="fixture-model", family="fixture", transport="mock")
    return config.model_copy(update={"transport": transport, "base_url": "https://example.invalid/v1",
                                     "api_key_env": "TEST_SECRET"})


def completed(*, tool: bool = False) -> str:
    events = []
    if tool:
        events.append({"type":"item.completed", "item":{"type":"command_execution"}})
    events.extend([{"type":"item.completed", "item":{"type":"agent_message", "text":"ok"}},
                   {"type":"turn.completed", "usage":{"input_tokens":4,"output_tokens":2}}])
    return "\n".join(json.dumps(event) for event in events)


def test_codex_cli_retries_internal_tool_and_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def fake_run(cmd: list, **kwargs: object) -> subprocess.CompletedProcess:
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=completed(tool=len(seen) == 1), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CodexCLIClient(profile("codex_cli")).chat([{"role":"system","content":"s"},
                                                        {"role":"user","content":"u"}])
    assert len(seen) == 2 and result.raw["tool_errors"] == 1
    assert result.content == "ok" and result.usage.input_tokens == 4
    assert "--ignore-user-config" in seen[0][0]
    assert sum(arg == "-c" for arg in seen[0][0]) == 15


def test_codex_cli_rejects_repeated_internal_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, stdout=completed(tool=True), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexToolError):
        CodexCLIClient(profile("codex_cli")).chat([])


def test_codex_cli_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list, **kwargs: object) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TimeoutError):
        CodexCLIClient(profile("codex_cli")).chat([])


def test_openai_maps_usage_reasoning_and_request_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SECRET", "private-value-for-test")
    requests = []

    def create(**kwargs: object) -> dict:
        requests.append(kwargs)
        return {"model":"returned", "choices":[{"finish_reason":"length", "message":{
            "content":"answer", "reasoning_content":"private reasoning", "tool_calls":[]}}],
            "usage":{"prompt_tokens":10,"completion_tokens":5,"prompt_cache_hit_tokens":4,
                     "completion_tokens_details":{"reasoning_tokens":3}}}

    monkeypatch.setattr("backend.llm.OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    config = profile("openai_compat").model_copy(update={"reasoning_effort":"high", "extra_body":{"option":True}})
    client = OpenAICompatClient(config)
    result = client.chat([{"role":"user","content":"u"}], max_tokens=123, ctx=CallCtx(kind="probe", template="probe_user"))
    assert result.content == "answer" and result.finish_reason == "length"
    assert result.usage.cached_tokens == 4 and result.usage.reasoning_tokens == 3
    assert result.raw["reasoning_content_chars"] == len("private reasoning")
    assert "private reasoning" not in result.model_dump_json()
    assert requests[0]["max_tokens"] == 123 and requests[0]["extra_body"] == {"option":True}
    assert "private-value-for-test" not in config.model_dump_json()


def test_openai_stream_returns_deltas_and_complete_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SECRET", "stream-secret")
    requests = []

    def create(**kwargs: object) -> list[dict]:
        requests.append(kwargs)
        return [
            {"choices":[{"delta":{"content":"first "},"finish_reason":None}]},
            {"choices":[{"delta":{"content":"second"},"finish_reason":"stop"}],
             "usage":{"prompt_tokens":4,"completion_tokens":2}},
        ]

    monkeypatch.setattr("backend.llm.OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    client = OpenAICompatClient(profile("openai_compat"))
    events = list(client.stream([{"role":"user","content":"u"}]))
    assert [event.text for event in events[:-1]] == ["first ", "second"]
    assert events[-1].result and events[-1].result.content == "first second"
    assert events[-1].result.usage.input_tokens == 4
    assert requests[0]["stream"] is True


def test_unsupported_temperature_retries_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SECRET", "temperature-secret")
    requests = []

    class UnsupportedTemperature(Exception):
        pass

    def create(**kwargs: object) -> dict:
        requests.append(kwargs)
        if len(requests) == 1:
            raise UnsupportedTemperature("temperature is unsupported")
        return {"choices":[{"finish_reason":"stop", "message":{"content":"ok"}}]}

    monkeypatch.setattr("backend.llm.BadRequestError", UnsupportedTemperature)
    monkeypatch.setattr("backend.llm.OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result = OpenAICompatClient(profile("openai_compat")).chat([{"role":"user","content":"u"}], temperature=0)
    assert requests[0]["temperature"] == 0 and "temperature" not in requests[1]
    assert result.raw["temperature_unsupported"] is True
