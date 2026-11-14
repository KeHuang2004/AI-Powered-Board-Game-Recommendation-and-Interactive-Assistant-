"""Capability checks through the same traced harness used by requests."""
from __future__ import annotations

import json
import secrets
import time

from pydantic import BaseModel

from ..database import get_games, catalog_view
from ..llm import LLMClient
from ..database import estimate_tokens
from .context import build_messages
from .loop import LoopResult, run_loop
from .tools import RunCtx, Tool, ToolRegistry


class EchoParams(BaseModel):
    value: str


def _echo(params: EchoParams, ctx: RunCtx) -> dict:
    return {"value": params.value}


def _run(client: LLMClient, template: str, value: str, *, tools: bool = False,
         lines: list[str] | None = None) -> LoopResult:
    profile = getattr(client, "profile", None)
    messages = build_messages("probe_system", template, {"value": value, "lines": lines or []})
    ctx = RunCtx(kind="probe", template=template, allowed_ids=[value],
                 model_profile=profile.name if profile else "product")
    registry = ToolRegistry([Tool(name="probe_echo", params=EchoParams, handler=_echo)])
    return run_loop(client, messages, registry, ["probe_echo"] if tools else [], ctx,
                    max_iters=1 if tools else 0, require_submit=False)


def probe_capabilities(client: LLMClient, long_context: bool = False) -> dict:
    start = time.monotonic()
    nonce = secrets.token_hex(8)
    chat = _run(client, "probe_user", nonce)
    chat_ok = bool(chat.final_text and nonce in chat.final_text)
    first_tool = _run(client, "probe_tool_user", nonce, tools=True)
    tool_ok = any(event["name"] == "probe_echo" and event["args"].get("value") == nonce
                  for event in first_tool.events)
    multi = _run(client, "probe_tool_user", nonce, tools=True)
    multi_ok = bool(any(event["name"] == "probe_echo" for event in multi.events)
                    and multi.final_text and nonce in multi.final_text)
    saw_reasoning = any(item._reasoning_content for item in multi.results)
    reasoning_result = "not_observed"
    if saw_reasoning and multi_ok:
        reasoning_result = "not_required"
    elif saw_reasoning and hasattr(client, "replay_reasoning"):
        client.replay_reasoning = True
        retried = _run(client, "probe_tool_user", nonce, tools=True)
        replay_ok = bool(any(event["name"] == "probe_echo" for event in retried.events)
                         and retried.final_text and nonce in retried.final_text)
        reasoning_result = "required" if replay_ok else "unresolved"
        multi_ok = replay_ok
        if not replay_ok:
            client.replay_reasoning = False
    results = chat.results + first_tool.results + multi.results
    report = {"chat": chat_ok, "tool_calling": tool_ok, "multi_turn_tool": multi_ok,
              "reasoning_content_replay": reasoning_result,
              "latency_ms": int((time.monotonic() - start) * 1000),
              "model_returned": results[-1].model_returned if results else "",
              "detail": "ok" if chat_ok and tool_ok and multi_ok else "one or more capability checks failed"}
    if long_context:
        lines = [json.dumps(catalog_view(game, "en"), ensure_ascii=False) for game in get_games()]
        long_result = _run(client, "probe_long_user", nonce, lines=lines)
        estimate = estimate_tokens(nonce + "\n" + "\n".join(lines))
        reported = long_result.results[0].usage.input_tokens if long_result.results else 0
        report["long_context"] = bool(long_result.final_text and nonce in long_result.final_text
                                      and reported >= estimate * 0.4)
        report["long_context_usage"] = {"estimated": estimate, "reported": reported}
    report["latency_ms"] = int((time.monotonic() - start) * 1000)
    return report
