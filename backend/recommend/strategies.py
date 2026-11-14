"""Shared implementation for the three harness recommendation conditions."""
from __future__ import annotations

from ..database import get_games, get_games_by_id
from ..setting import get_settings
from ..harness.context import build_messages
from ..harness.loop import run_loop
from ..harness.tools import RunCtx, Tool, ToolRegistry
from ..harness.trace import Trace
from ..llm import LLMClient
from . import s3
from .common import (
    RecommendRequest,
    RecommendResult,
    SubmitRecommendations,
    catalog_lines,
    fallback,
    ordered_games,
    public_game,
    render_request,
    validate_picks,
)


def submit(params: SubmitRecommendations, ctx: RunCtx) -> dict:
    return {"accepted": True}


def recommend(req: RecommendRequest, eligible: list[dict], client: LLMClient, ctx: RunCtx, *,
              strategy: str, order: str | int, language_name: str) -> RecommendResult:
    ctx.template = "recommend_user_s3" if strategy == "s3" else "recommend_user"
    ctx.audit = strategy == "s0"
    ordered_eligible = ordered_games(eligible, order)
    ctx.allowed_ids = [game["game_id"] for game in ordered_eligible]
    items = get_games() if strategy == "s0" else eligible
    variables = {"lang_name": language_name, "request_block": render_request(req), "n_picks": ctx.n_picks,
                 "catalog_lines": "\n".join(catalog_lines(items, req.language, order)), "total": len(get_games())}
    messages = build_messages("recommend_system", ctx.template, variables)
    terminal = Tool(name="submit_recommendations", params=SubmitRecommendations, handler=submit,
                    terminal=True, validator=validate_picks)
    registry = ToolRegistry([terminal, *(s3.tools() if strategy == "s3" else [])])
    exposed = ["search_games", "get_game", "submit_recommendations"] if strategy == "s3" else ["submit_recommendations"]
    overrides = None
    if strategy == "s1":
        schema = SubmitRecommendations.model_json_schema()
        schema["$defs"]["Pick"]["properties"]["game_id"]["enum"] = ctx.allowed_ids
        overrides = {"submit_recommendations": schema}
    trace = Trace("recommend", ctx.template, strategy=strategy, model_profile=ctx.model_profile,
                  meta=ctx.meta, request=req.model_dump(), redact=getattr(client, "_redact", None))
    trace.record_prompt("request_block")
    trace.record_prompt("recommend_messages.yaml")
    settings = get_settings()
    loop = run_loop(client, messages, registry, exposed, ctx, settings.max_tool_iters,
                    settings.max_submit_retries, True, schema_overrides=overrides, trace=trace)
    if loop.submitted is None:
        return RecommendResult(method="catalog", eligible_count=len(eligible), recommendations=fallback(req, eligible),
                               strategy=strategy, fallback=True, shortage=len(eligible) < 3,
                               interventions=loop.interventions, error="model_error" if loop.stop_reason == "error" else None)
    submission = SubmitRecommendations.model_validate(loop.submitted)
    selected = []
    for pick in submission.picks:
        game = get_games_by_id().get(pick.game_id)
        if game is None:
            selected.append({"game_id": pick.game_id, "reason": pick.fit_reason, "caveat": pick.caveat,
                             "unresolved": True})
        else:
            selected.append(dict(public_game(game, req.language), reason=pick.fit_reason, caveat=pick.caveat))
    profile = getattr(client, "profile", None)
    method = (profile.provider or ("mock" if profile.transport == "mock" else profile.name)) if profile else "mock"
    return RecommendResult(method=method, eligible_count=len(eligible), recommendations=selected, strategy=strategy,
                           shortage=len(eligible) < 3, note=submission.note, submitted=loop.submitted,
                           audit_errors=loop.audit_errors, interventions=loop.interventions)
