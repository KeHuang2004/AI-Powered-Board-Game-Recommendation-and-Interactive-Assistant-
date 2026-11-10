"""Bounded tool-call orchestration with intervention accounting."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel, Field

from ..llm import CallCtx, CodexToolError, LLMClient, LLMResult, TextToolProtocol
from .prompts import load_yaml
from .tools import RunCtx, ToolRegistry
from .trace import Trace

INTERVENTIONS = ("no_submit", "unknown_tool", "parse_fail", "schema_fail", "tool_error",
                 "validation_fail", "loop_cap", "fallback", "truncated")


class LoopResult(BaseModel):
    submitted: dict | None = None
    final_text: str | None = None
    iterations: int = 0
    interventions: dict[str, int] = Field(default_factory=dict)
    events: list[dict] = Field(default_factory=list)
    audit_errors: list[str] = Field(default_factory=list)
    stop_reason: str = ""
    trace_id: str = ""
    results: list[LLMResult] = Field(default_factory=list)


def call_model(client: LLMClient, trace: Trace, messages: list[dict], **kwargs: object) -> LLMResult:
    try:
        result = client.chat(messages, **kwargs)
        trace.add_llm(messages, result, temperature=kwargs.get("temperature"), tools=kwargs.get("tools"))
        return result
    except Exception as error:
        trace.add_error(error)
        raise


def run_loop(client: LLMClient, messages: list[dict], registry: ToolRegistry,
             exposed: list[str] | Callable[[RunCtx], list[str]], ctx: RunCtx,
             max_iters: int = 3, max_submit_retries: int = 2,
             require_submit: bool = True, *, on_event: Callable[[dict], None] | None = None,
             schema_overrides: dict[str, dict] | None = None, temperature: float | None = None,
             trace: Trace | None = None, on_finish: Callable[[LoopResult, Trace], None] | None = None,
             fallback_on_failure: bool = True) -> LoopResult:
    templates = getattr(messages, "templates", [])
    messages = list(messages)
    trace = trace or Trace(ctx.kind, ctx.template, strategy=ctx.strategy, model_profile=ctx.model_profile,
                           meta=ctx.meta, redact=getattr(client, "_redact", None))
    for name in templates:
        trace.record_prompt(name)
    if exposed:
        trace.record_prompt("tools.yaml")
    if isinstance(client, TextToolProtocol) or getattr(client, "uses_text_protocol", False):
        trace.record_prompt("text_tool_protocol")
        trace.record_prompt("text_transcript")
    result = LoopResult(trace_id=trace.data["trace_id"])
    interventions: Counter = Counter()
    tool_rounds = submit_retries = 0
    retried_truncation = False
    nudges = load_yaml("tutor_nudges")
    texts = load_yaml("harness_messages")
    call_ctx = CallCtx(kind=ctx.kind, template=ctx.template, allowed_ids=ctx.allowed_ids, n_picks=ctx.n_picks)
    try:
        while True:
            final_round = not require_submit and tool_rounds >= max_iters
            active = exposed(ctx) if callable(exposed) else exposed
            try:
                schemas = registry.schemas(active, ctx, schema_overrides)
            except Exception as error:  # noqa: BLE001 - schema errors are traced and stop this request
                trace.add_error(error)
                result.stop_reason = "error"
                break
            try:
                response = call_model(client, trace, messages,
                                      tools=schemas,
                                      tool_choice="none" if final_round else None,
                                      temperature=temperature, ctx=call_ctx)
            except Exception as error:  # noqa: BLE001 - adapter errors are traced and stop this request
                if isinstance(error, CodexToolError):
                    interventions["tool_error"] += error.count
                result.stop_reason = "error"
                break
            result.results.append(response)
            result.iterations += 1
            interventions["tool_error"] += response.raw.get("tool_errors", 0)
            if response.finish_reason == "length":
                interventions["truncated"] += 1
                profile = getattr(client, "profile", None)
                thinking = profile and (profile.reasoning_effort or profile.extra_body.get("enable_thinking") or (
                    isinstance(profile.extra_body.get("thinking"), dict)
                    and profile.extra_body["thinking"].get("type") == "enabled"
                ))
                if thinking and not retried_truncation:
                    retried_truncation = True
                    continue
                result.stop_reason = "truncated"
                break
            if response.raw.get("protocol_error"):
                interventions["parse_fail"] += 1
                submit_retries += 1
                if submit_retries > max_submit_retries:
                    interventions["loop_cap"] += 1
                    result.stop_reason = "loop_cap"
                    break
                messages.append({"role": "user", "content": texts["protocol_reminder"]})
                continue
            if not response.tool_calls:
                if not require_submit:
                    result.final_text = response.content
                    result.stop_reason = "text"
                    break
                interventions["no_submit"] += 1
                submit_retries += 1
                if submit_retries > max_submit_retries:
                    interventions["loop_cap"] += 1
                    result.stop_reason = "loop_cap"
                    break
                name = next((name for name in active if name.startswith("submit_")), "")
                messages.append({"role": "user", "content": nudges["submit_reminder"]["en"].format(tool=name)})
                continue
            if final_round:
                interventions["loop_cap"] += 1
                result.stop_reason = "loop_cap"
                break
            assistant = {"role": "assistant", "content": response.content,
                         "tool_calls": [{"id": call.id, "type": "function", "function": {
                             "name": call.name, "arguments": call.arguments
                         }} for call in response.tool_calls]}
            if getattr(client, "replay_reasoning", False) and response._reasoning_content:
                assistant["reasoning_content"] = response._reasoning_content
            messages.append(assistant)
            ordinary = False
            had_retry_error = False
            for call in response.tool_calls:
                state_before = ctx.state.model_dump() if hasattr(ctx.state, "model_dump") else None
                active = exposed(ctx) if callable(exposed) else exposed
                outcome = registry.execute(call, ctx, active)
                state_after = ctx.state.model_dump() if hasattr(ctx.state, "model_dump") else None
                tool = registry.tools.get(call.name)
                ordinary |= bool(tool and not tool.terminal)
                if outcome.error_kind:
                    interventions[outcome.error_kind] += 1
                    had_retry_error |= not tool or tool.terminal
                if outcome.audit_errors:
                    result.audit_errors.extend(outcome.audit_errors)
                    interventions["validation_fail"] += 1
                if outcome.terminal:
                    result.submitted = outcome.submitted
                event = {"name": call.name, "args": outcome.args, "result": outcome.result}
                result.events.append(event)
                trace.add_tool(call.name, outcome.args, outcome.result, outcome.error_kind,
                               before=state_before, after=state_after)
                if on_event:
                    on_event(event)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(outcome.result, ensure_ascii=False)})
            if ordinary:
                tool_rounds += 1
            if result.submitted is not None:
                result.stop_reason = "submitted"
                break
            if had_retry_error:
                submit_retries += 1
            if tool_rounds > max_iters or submit_retries > max_submit_retries:
                interventions["loop_cap"] += 1
                result.stop_reason = "loop_cap"
                break
            if require_submit and tool_rounds >= max_iters and ordinary:
                # A final submission-only round is still allowed.
                exposed = [name for name in active if registry.tools[name].terminal]
        if require_submit and result.submitted is None and fallback_on_failure:
            interventions["fallback"] += 1
    finally:
        result.interventions = {key: interventions[key] for key in INTERVENTIONS}
        trace.data["interventions"] = result.interventions
        trace.data["audit_errors"] = result.audit_errors
        trace.data["outcome"] = {"submitted": result.submitted, "final_text": result.final_text,
                                 "stop_reason": result.stop_reason}
        trace.data["fallback"] = bool(interventions["fallback"])
        if on_finish:
            on_finish(result, trace)
        trace.write()
    return result
