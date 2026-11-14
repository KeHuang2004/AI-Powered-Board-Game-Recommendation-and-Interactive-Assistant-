"""Provider adapters and the text-tool protocol."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import httpx
import yaml
from openai import BadRequestError, OpenAI
from pydantic import BaseModel, Field, PrivateAttr
from ulid import ULID

from .setting import ROOT, Profile, read_api_key
from .harness.prompts import load_yaml, render


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


class LLMResult(BaseModel):
    _reasoning_content: str | None = PrivateAttr(default=None)
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
    model_returned: str = ""
    latency_ms: int = 0
    raw: dict = Field(default_factory=dict)


class StreamEvent(BaseModel):
    text: str = ""
    result: LLMResult | None = None


class CallCtx(BaseModel):
    kind: str
    template: str
    allowed_ids: list[str] = Field(default_factory=list)
    n_picks: int = 0


class LLMClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, max_tokens: int | None = None,
             temperature: float | None = None, response_format: dict | None = None,
             ctx: CallCtx | None = None) -> LLMResult: ...

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               tool_choice: str | dict | None = None, max_tokens: int | None = None,
               temperature: float | None = None, ctx: CallCtx | None = None) -> Iterator[StreamEvent]: ...


def _usage(value: dict | None) -> Usage:
    value = value or {}
    prompt_details = value.get("prompt_tokens_details") or {}
    completion_details = value.get("completion_tokens_details") or {}
    cached = prompt_details.get("cached_tokens")
    if cached is None:
        cached = value.get("prompt_cache_hit_tokens", value.get("cached_input_tokens"))
    return Usage(input_tokens=value.get("prompt_tokens", value.get("input_tokens", 0)) or 0,
                 output_tokens=value.get("completion_tokens", value.get("output_tokens", 0)) or 0,
                 cached_tokens=cached, reasoning_tokens=completion_details.get("reasoning_tokens"))


class OpenAICompatClient:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.replay_reasoning = profile.replay_reasoning
        secret = read_api_key(profile)
        self._redact = lambda text: text.replace(secret, "[REDACTED]")
        self._client = OpenAI(base_url=profile.base_url, api_key=secret, timeout=profile.timeout_s, max_retries=0)

    def _params(self, messages: list[dict], tools: list[dict] | None,
                tool_choice: str | dict | None, max_tokens: int | None,
                temperature: float | None, response_format: dict | None) -> dict:
        params = {"model": self.profile.model, "messages": messages}
        for key, value in (("tools", tools or None), ("tool_choice", tool_choice if tools else None),
                           ("max_tokens", max_tokens if max_tokens is not None else self.profile.max_tokens),
                           ("temperature", temperature), ("response_format", response_format),
                           ("reasoning_effort", self.profile.reasoning_effort or None),
                           ("extra_body", self.profile.extra_body or None)):
            if value is not None:
                params[key] = value
        return params

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, max_tokens: int | None = None,
             temperature: float | None = None, response_format: dict | None = None,
             ctx: CallCtx | None = None) -> LLMResult:
        start = time.monotonic()
        params = self._params(messages, tools, tool_choice, max_tokens, temperature, response_format)
        temperature_unsupported = False
        try:
            response = self._client.chat.completions.create(**params)
        except BadRequestError as error:
            if temperature is None or "temperature" not in str(error).lower():
                raise
            params.pop("temperature", None)
            response = self._client.chat.completions.create(**params)
            temperature_unsupported = True
        data = response.model_dump() if hasattr(response, "model_dump") else response
        choice = data["choices"][0]
        message = choice["message"]
        calls = [ToolCall(id=call["id"], name=call["function"]["name"],
                          arguments=call["function"]["arguments"]) for call in message.get("tool_calls") or []]
        result = LLMResult(content=message.get("content"), tool_calls=calls,
                           finish_reason=choice.get("finish_reason") or "stop", usage=_usage(data.get("usage")),
                           model_returned=data.get("model") or self.profile.model,
                           latency_ms=int((time.monotonic() - start) * 1000),
                           raw={"protocol": "native", "reasoning_content_chars": len(message.get("reasoning_content") or ""),
                                "temperature_unsupported": temperature_unsupported})
        result._reasoning_content = message.get("reasoning_content")
        return result

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               tool_choice: str | dict | None = None, max_tokens: int | None = None,
               temperature: float | None = None, ctx: CallCtx | None = None) -> Iterator[StreamEvent]:
        if tools:
            result = self.chat(messages, tools, tool_choice, max_tokens, temperature, ctx=ctx)
            if result.content:
                yield StreamEvent(text=result.content)
            yield StreamEvent(result=result)
            return
        start = time.monotonic()
        params = self._params(messages, None, tool_choice, max_tokens, temperature, None)
        stream = self._client.chat.completions.create(**params, stream=True)
        pieces = []
        usage = Usage()
        finish = "stop"
        model = self.profile.model
        reasoning_chars = 0
        for item in stream:
            data = item.model_dump() if hasattr(item, "model_dump") else item
            model = data.get("model") or model
            if data.get("usage"):
                usage = _usage(data["usage"])
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("tool_calls"):
                    result = self.chat(messages, tools, tool_choice, max_tokens, temperature, ctx=ctx)
                    if result.content:
                        yield StreamEvent(text=result.content)
                    yield StreamEvent(result=result)
                    return
                reasoning_chars += len(delta.get("reasoning_content") or "")
                if delta.get("content"):
                    pieces.append(delta["content"])
                    yield StreamEvent(text=delta["content"])
                finish = choice.get("finish_reason") or finish
        yield StreamEvent(result=LLMResult(content="".join(pieces), finish_reason=finish, usage=usage,
                          model_returned=model, latency_ms=int((time.monotonic() - start) * 1000),
                          raw={"protocol": "native", "reasoning_content_chars": reasoning_chars}))


CLI_CONFIG = {
    "skills.include_instructions": False, "skills.bundled.enabled": False, "project_doc_max_bytes": 0,
    "features.skip_host_skill_discovery": True, "features.shell_tool": False, "features.apps": False,
    "features.memories": False, "features.view_image": False, "features.image_generation": False,
    "features.multi_agent": False, "features.multi_agent_v2": False, "features.skill_search": False,
    "features.code_mode": False, "web_search": "disabled",
}


class CodexToolError(RuntimeError):
    def __init__(self, count: int) -> None:
        super().__init__("CLI invoked unauthorized tools")
        self.count = count


class CodexCLIClient:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, max_tokens: int | None = None,
             temperature: float | None = None, response_format: dict | None = None,
             ctx: CallCtx | None = None) -> LLMResult:
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="boardgame-cli-") as directory:
            root = Path(directory)
            system = "\n\n".join(m.get("content") or "" for m in messages if m["role"] == "system")
            user = "\n\n".join(m.get("content") or "" for m in messages if m["role"] != "system")
            instructions = root / "system.txt"
            instructions.write_text(system, encoding="utf-8")
            cmd = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--model", self.profile.model, "--json"]
            if self.profile.reasoning_effort:
                cmd.extend(["-c", f"model_reasoning_effort={json.dumps(self.profile.reasoning_effort)}"])
            cmd.extend(["-c", f"model_instructions_file={json.dumps(str(instructions))}"])
            for key, value in CLI_CONFIG.items():
                cmd.extend(["-c", f"{key}={json.dumps(value)}"])
            cmd.append("-")
            tool_errors = 0
            for attempt in range(2):
                try:
                    process = subprocess.run(cmd, cwd=root, input=user, text=True, capture_output=True,
                                             timeout=self.profile.timeout_s, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError("Codex inference exceeded time limit") from exc
                events = []
                for line in process.stdout.splitlines():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                invalid = [event for event in events if event.get("type") in ("item.started", "item.completed")
                           and event.get("item", {}).get("type") not in ("agent_message", "reasoning", "error")]
                if invalid:
                    tool_errors += 1
                    if attempt == 0:
                        continue
                    raise CodexToolError(tool_errors)
                finished = [event for event in events if event.get("type") == "turn.completed"]
                outputs = [event["item"].get("text", "") for event in events
                           if event.get("type") == "item.completed"
                           and event.get("item", {}).get("type") == "agent_message"]
                if process.returncode != 0 or not finished or not outputs:
                    raise RuntimeError("Codex CLI did not complete successfully")
                return LLMResult(content=outputs[-1], usage=_usage(finished[-1].get("usage")),
                                 model_returned=self.profile.model, latency_ms=int((time.monotonic() - start) * 1000),
                                 raw={"protocol": "text", "tool_errors": tool_errors,
                                      "temperature_unsupported": temperature is not None,
                                      "model_identity": "requested model; CLI does not expose backend identifier"})
        raise RuntimeError("Codex CLI did not return a result")

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               tool_choice: str | dict | None = None, max_tokens: int | None = None,
               temperature: float | None = None, ctx: CallCtx | None = None) -> Iterator[StreamEvent]:
        result = self.chat(messages, tools, tool_choice, max_tokens, temperature, ctx=ctx)
        if result.content:
            yield StreamEvent(text=result.content)
        yield StreamEvent(result=result)


class MockClient:
    def __init__(self, profile: Profile | None = None, script: list[dict] | str | Path | None = None) -> None:
        self.profile = profile
        if isinstance(script, (str, Path)):
            self.script = yaml.safe_load(Path(script).read_text(encoding="utf-8"))
        else:
            self.script = list(script) if script is not None else None

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, max_tokens: int | None = None,
             temperature: float | None = None, response_format: dict | None = None,
             ctx: CallCtx | None = None) -> LLMResult:
        if self.script is not None:
            if not self.script:
                raise RuntimeError("Mock script exhausted")
            return LLMResult.model_validate(self.script.pop(0))
        return self._auto(messages, tools, tool_choice, response_format, ctx)

    def _auto(self, messages: list[dict], tools: list[dict] | None,
              tool_choice: str | dict | None, response_format: dict | None, ctx: CallCtx | None) -> LLMResult:
        if ctx is None:
            raise ValueError("Mock auto mode requires CallCtx")
        fixture_dir = ROOT / "tests" / "fixtures" / "mock_auto"
        names = [tool["function"]["name"] for tool in tools or []]
        web_context = "web_search" in names
        if not web_context and ctx.kind in {"plan", "tutor_turn"} and "lookup_rulebook" in names:
            web_description = load_yaml("tools")["_lookup_rulebook_with_web"]
            web_context = any(tool["function"]["name"] == "lookup_rulebook"
                              and tool["function"]["description"] == web_description for tool in tools or [])
        model = self.profile.model if self.profile else ""
        usage = Usage(input_tokens=max(1, sum(len(str(m.get("content") or "")) for m in messages) // 4),
                      output_tokens=20)
        if ctx.kind == "probe":
            value = ctx.allowed_ids[0] if ctx.allowed_ids else ""
            if "probe_echo" in names and tool_choice != "none" and not any(m.get("role") == "tool" for m in messages):
                return LLMResult(tool_calls=[ToolCall(id=str(ULID()), name="probe_echo",
                                  arguments=json.dumps({"value": value}))], usage=usage, model_returned=model)
            return LLMResult(content=value, usage=usage, model_returned=model)
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if ctx.kind == "plan" and web_context and tool_choice != "none":
            if not tool_messages:
                name = "lookup_rulebook" if "lookup_rulebook" in names else "web_search"
                fixture = "web_lookup.json" if name == "lookup_rulebook" else "web_search.json"
                args = (fixture_dir / fixture).read_text(encoding="utf-8")
                return LLMResult(tool_calls=[ToolCall(id=str(ULID()), name=name, arguments=args)],
                                 usage=usage, model_returned=model)
            if len(tool_messages) == 1:
                results = json.loads(tool_messages[0]["content"])
                if isinstance(results, list) and results:
                    args = json.dumps({"url": results[0]["url"]})
                    return LLMResult(tool_calls=[ToolCall(id=str(ULID()), name="fetch_page", arguments=args)],
                                     usage=usage, model_returned=model)
        submit = next((name for name in names if name.startswith("submit_")), None)
        if submit and tool_choice != "none":
            data = json.loads((fixture_dir / f"{ctx.template}.json").read_text(encoding="utf-8"))
            if ctx.kind == "plan" and web_context and tool_messages:
                result = json.loads(tool_messages[-1]["content"])
                if isinstance(result, dict) and (result.get("content") or any(
                    row.get("source") == "web" for row in result.get("passages", [])
                )):
                    data["setup"][0]["src"] = "web"
            if submit == "submit_recommendations":
                data["picks"] = [{"game_id": game_id, "fit_reason": data["picks"][0]["fit_reason"],
                                  "caveat": None} for game_id in ctx.allowed_ids[:ctx.n_picks]]
            call = ToolCall(id=str(ULID()), name=submit, arguments=json.dumps(data, ensure_ascii=False))
            return LLMResult(tool_calls=[call], usage=usage, model_returned=model)
        if ctx.kind == "recommend" and response_format:
            fixture = json.loads((fixture_dir / "recommend_user.json").read_text(encoding="utf-8"))
            reason = fixture["picks"][0]["fit_reason"]
            data = {"recommendations": [{"game_id": game_id, "reason": reason}
                                        for game_id in ctx.allowed_ids[:ctx.n_picks]]}
            return LLMResult(content=json.dumps(data), usage=usage, model_returned=model)
        if ctx.kind == "tutor_turn" and "roll_dice" in names and tool_choice != "none":
            user_text = " ".join(str(message.get("content") or "") for message in messages if message["role"] == "user")
            if ("骰" in user_text or "roll" in user_text.lower()) and not any(
                message.get("role") == "tool" for message in messages
            ):
                return LLMResult(tool_calls=[ToolCall(id=str(ULID()), name="roll_dice",
                                  arguments=json.dumps({"count": 1, "sides": 6}))],
                                 usage=usage, model_returned=model)
        if ctx.kind == "tutor_turn" and web_context and "lookup_rulebook" in names:
            if not tool_messages and tool_choice != "none":
                args = (fixture_dir / "web_lookup.json").read_text(encoding="utf-8")
                return LLMResult(tool_calls=[ToolCall(id=str(ULID()), name="lookup_rulebook", arguments=args)],
                                 usage=usage, model_returned=model)
            if tool_messages:
                result = json.loads(tool_messages[-1]["content"])
                if isinstance(result, dict) and any(row.get("source") == "web" for row in result.get("passages", [])):
                    return LLMResult(content=(fixture_dir / "tutor_web_reply.txt").read_text(encoding="utf-8").strip(),
                                     usage=usage, model_returned=model)
        fixture = "legacy_explain.txt" if ctx.kind == "explain" else "tutor_reply.txt"
        return LLMResult(content=(fixture_dir / fixture).read_text(encoding="utf-8").strip(),
                         usage=usage, model_returned=model)

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               tool_choice: str | dict | None = None, max_tokens: int | None = None,
               temperature: float | None = None, ctx: CallCtx | None = None) -> Iterator[StreamEvent]:
        result = self.chat(messages, tools, tool_choice, max_tokens, temperature, ctx=ctx)
        if result.content:
            yield StreamEvent(text=result.content)
        yield StreamEvent(result=result)


class TextToolProtocol:
    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.profile = getattr(client, "profile", None)
        self._redact = getattr(client, "_redact", None)

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, max_tokens: int | None = None,
             temperature: float | None = None, response_format: dict | None = None,
             ctx: CallCtx | None = None) -> LLMResult:
        descriptions = [{"name": item["function"]["name"],
                         "description": item["function"].get("description", ""),
                         "schema_json": json.dumps(item["function"]["parameters"], ensure_ascii=False)}
                        for item in tools or []]
        system = "\n\n".join(m.get("content") or "" for m in messages if m["role"] == "system")
        system += "\n\n" + render("text_tool_protocol", tools=descriptions)
        call_names = {}
        transcript = []
        for message in messages:
            if message["role"] == "system":
                continue
            calls = []
            for call in message.get("tool_calls") or []:
                name = call["function"]["name"]
                call_names[call["id"]] = name
                try:
                    args = json.loads(call["function"]["arguments"])
                except json.JSONDecodeError:
                    args = call["function"]["arguments"]
                calls.append({"name": name, "arguments": args})
            transcript.append({"role": message["role"], "content": message.get("content") or "",
                               "tool_calls_json": json.dumps(calls, ensure_ascii=False) if calls else "",
                               "tool_name": message.get("name", call_names.get(message.get("tool_call_id"), ""))})
        user = render("text_transcript", messages=transcript, force_no_tool=tool_choice == "none")
        rendered = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if isinstance(self.client, MockClient) and self.client.script is None:
            result = self.client._auto(messages, tools, tool_choice, response_format, ctx)
            if result.tool_calls:
                encoded = [{"name": call.name, "arguments": json.loads(call.arguments)} for call in result.tool_calls]
                result.content = "```tool_calls\n" + json.dumps(encoded, ensure_ascii=False) + "\n```"
                result.tool_calls = []
        else:
            result = self.client.chat(rendered, max_tokens=max_tokens, temperature=temperature, ctx=ctx)
        result.raw["protocol"] = "text"
        match = re.search(r"```tool_calls\s*\n(.*?)```", result.content or "", re.DOTALL)
        if not match:
            return result
        try:
            data = json.loads(match.group(1))
            if tool_choice == "none" or not isinstance(data, list) or not data:
                raise ValueError("Unexpected tool-call block")
            calls = []
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("arguments"), dict):
                    raise TypeError("Invalid tool-call item")
                calls.append(ToolCall(id=str(ULID()), name=item["name"],
                                      arguments=json.dumps(item["arguments"], ensure_ascii=False)))
            result.tool_calls = calls
            result.content = None
        except (ValueError, TypeError) as error:
            result.tool_calls = []
            result.raw["protocol_error"] = type(error).__name__
        return result

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               tool_choice: str | dict | None = None, max_tokens: int | None = None,
               temperature: float | None = None, ctx: CallCtx | None = None) -> Iterator[StreamEvent]:
        result = self.chat(messages, tools, tool_choice, max_tokens, temperature, ctx=ctx)
        if result.content:
            yield StreamEvent(text=result.content)
        yield StreamEvent(result=result)


def make_client(profile: Profile) -> LLMClient:
    if profile.transport == "mock":
        client: LLMClient = MockClient(profile)
    elif profile.transport == "codex_cli":
        client = CodexCLIClient(profile)
    else:
        client = OpenAICompatClient(profile)
    if profile.transport == "codex_cli" or profile.force_text_protocol:
        return TextToolProtocol(client)
    return client


def probe_capabilities(client: LLMClient, long_context: bool = False) -> dict:
    from .harness.probe import probe_capabilities as probe

    return probe(client, long_context)


Provider = Literal["deepseek", "gemini", "compatible"]


@dataclass(frozen=True)
class ModelSettings:
    provider: Provider
    model: str
    api_key: str = field(repr=False)
    base_url: str = ""


def setting(name: str, legacy: str = "", default: str = "") -> str:
    return os.getenv(name, os.getenv(legacy, default) if legacy else default).strip()


def model_settings() -> ModelSettings | None:
    provider = setting("MODEL_PROVIDER", "RECOMMEND_PROVIDER", "gemini").lower()
    if provider == "catalog":
        return None
    if provider == "gemini":
        key = setting("MODEL_API_KEY", "GEMINI_API_KEY")
        model = setting("MODEL_NAME", "GEMINI_MODEL", "gemini-2.5-flash")
        return ModelSettings(provider, model, key) if key and model else None
    if provider == "deepseek":
        key = setting("MODEL_API_KEY", "DEEPSEEK_API_KEY")
        model = setting("MODEL_NAME", "RECOMMEND_MODEL") or "deepseek-flash"
        return ModelSettings(provider, model, key, "https://api.deepseek.com") if key else None
    if provider == "compatible":
        key = setting("MODEL_API_KEY", "RECOMMEND_API_KEY")
        model = setting("MODEL_NAME", "RECOMMEND_MODEL")
        url = setting("MODEL_BASE_URL", "RECOMMEND_BASE_URL")
        return ModelSettings(provider, model, key, url) if key and model and url else None
    return None


def generate_text(
    settings: ModelSettings,
    *,
    instructions: str,
    data: str,
    max_tokens: int,
    schema: type[BaseModel] | None = None,
) -> str:
    if settings.provider == "gemini":
        from google import genai

        config = {
            "system_instruction": instructions,
            "temperature": 0.2,
            "max_output_tokens": max_tokens,
        }
        if schema is not None:
            config.update(response_mime_type="application/json", response_schema=schema)
        with genai.Client(api_key=settings.api_key, http_options={"timeout": 12000}) as client:
            response = client.models.generate_content(model=settings.model, contents=data, config=config)
            content = response.text
    else:
        payload = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": data},
            ],
            "max_tokens": max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        if settings.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        response = httpx.post(
            settings.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json=payload,
            timeout=httpx.Timeout(18.0, connect=4.0),
        )
        response.raise_for_status()
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Model response was truncated")
        content = choice["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Model returned no text")
    return content.strip()
