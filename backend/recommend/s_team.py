"""The team's original tag-based ordering and model-ranking control."""
from __future__ import annotations

import json
from collections import Counter

from pydantic import BaseModel, Field, ValidationError

from ..harness.loop import call_model
from ..harness.prompts import render
from ..harness.tools import RunCtx
from ..harness.trace import Trace
from ..llm import CallCtx, LLMClient
from .common import RecommendRequest, RecommendResult, fallback, public_game

SCENES = {
    "friends": {"朋友聚会", "固定团队"},
    "date": {"双人对局"},
    "family": {"亲子家庭"},
    "new_people": {"破冰", "派对"},
}
MOODS = {
    "relaxed": {"对话", "倾听与沟通", "合作"},
    "lively": {"派对", "破冰", "表演", "反应速度"},
    "thinking": {"策略", "逻辑排除", "安静解题", "资源管理"},
    "competitive": {"团队对抗", "直接互动", "诈唬", "隐藏身份"},
}
ENGLISH_TAGS = {
    "朋友聚会": "Social gathering", "固定团队": "Regular group",
    "双人对局": "Two-player night", "亲子家庭": "Family",
    "破冰": "Icebreaker", "派对": "Party", "对话": "Conversation",
    "倾听与沟通": "Listening and communication", "合作": "Cooperation",
    "表演": "Acting game", "反应速度": "Speed and reflexes",
    "策略": "Strategy", "逻辑排除": "Logical deduction", "安静解题": "Quiet puzzle night",
    "资源管理": "Resource management", "团队对抗": "Team competition",
    "直接互动": "Direct interaction", "诈唬": "Bluffing", "隐藏身份": "Hidden roles",
}
TIERS = {"T0": 0, "T1": 1, "T2": 2}

class ModelPick(BaseModel):
    game_id: str
    reason: str = Field(min_length=1)


class ModelRanking(BaseModel):
    recommendations: list[ModelPick] = Field(min_length=1, max_length=3)


def candidate_games(req: RecommendRequest, games: list[dict]) -> list[dict]:
    def ranking(game: dict) -> tuple:
        tags = set(game["text"]["zh"]["categories"])
        return (
            -len(tags & MOODS[req.mood]),
            -len(tags & SCENES[req.scene]),
            req.players not in game["recommended_players"],
            TIERS.get(game["tier"], 3),
            game["game_id"],
        )
    return [public_game(game, req.language) for game in sorted(games, key=ranking)]


def ranking_input(req: RecommendRequest, candidates: list[dict]) -> str:
    fields = ("game_id", "name", "recommended_players", "categories", "summary", "catalog_reason")
    translations = ENGLISH_TAGS if req.language == "en" else {}
    preferences = req.model_dump(include={"players", "has_dedicated_components", "has_host", "scene", "mood", "language"})
    data = {
        "preferences": preferences,
        "scene_tags": sorted(translations.get(tag, tag) for tag in SCENES[req.scene]),
        "mood_tags": sorted(translations.get(tag, tag) for tag in MOODS[req.mood]),
        "candidates": [{key: game[key] for key in fields} for game in candidates],
    }
    if req.free_text:
        data["free_text"] = req.free_text
    return json.dumps(data, ensure_ascii=False)


def prompt_variables(req: RecommendRequest, count: int, retry: bool) -> dict:
    example = render("s_team_example", count=count)
    return {"count": count, "language_name": "English" if req.language == "en" else "Simplified Chinese",
            "example": example, "retry": retry}


def _valid_ranking(content: str, candidates: list[dict], count: int) -> list[dict]:
    data = json.loads(content)
    picks = ModelRanking.model_validate(data).recommendations
    by_id = {game["game_id"]: game for game in candidates}
    ids = [pick.game_id.strip() for pick in picks]
    if len(ids) != count or len(set(ids)) != count or any(game_id not in by_id for game_id in ids):
        raise ValueError("Invalid candidate IDs or count")
    reasons = [" ".join(pick.reason.split()) for pick in picks]
    if not all(reasons):
        raise ValueError("Empty recommendation reason")
    return [dict(by_id[game_id], reason=reason if len(reason) <= 250 else reason[:249].rstrip() + "…",
                 caveat=None) for game_id, reason in zip(ids, reasons, strict=True)]


def recommend(req: RecommendRequest, eligible: list[dict], client: LLMClient, ctx: RunCtx) -> RecommendResult:
    candidates = candidate_games(req, eligible)[:12]
    count = min(3, len(candidates))
    ids = [game["game_id"] for game in candidates]
    trace = Trace("recommend", "s_team_user", strategy="s_team", model_profile=ctx.model_profile,
                  meta=ctx.meta, request=req.model_dump(), redact=getattr(client, "_redact", None))
    trace.record_prompt("s_team_system")
    trace.record_prompt("s_team_example")
    counters: Counter = Counter()
    selected = None
    model_error = False
    profile = getattr(client, "profile", None)
    call_ctx = CallCtx(kind="recommend", template="s_team_user", allowed_ids=ids, n_picks=count)
    try:
        for attempt in range(2):
            system = render("s_team_system", **prompt_variables(req, count, bool(attempt)))
            user = render("s_team_user", data_json=ranking_input(req, candidates))
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            try:
                response = call_model(client, trace, messages, response_format={"type": "json_object"},
                                      max_tokens=profile.max_tokens if profile and profile.max_tokens else 800,
                                      ctx=call_ctx)
            except Exception:  # noqa: BLE001 - provider failure is traced; catalog fallback is returned
                model_error = True
                break
            if response.finish_reason == "length":
                counters["truncated"] += 1
            try:
                selected = _valid_ranking(response.content or "", candidates, count)
                break
            except json.JSONDecodeError:
                counters["parse_fail"] += 1
            except (ValueError, ValidationError):
                counters["validation_fail"] += 1
        if selected is None:
            counters["fallback"] += 1
            selected = fallback(req, eligible)
        method = "catalog" if counters["fallback"] else (
            (profile.provider or ("mock" if profile.transport == "mock" else profile.name)) if profile else "mock"
        )
        result = RecommendResult(method=method, eligible_count=len(eligible), recommendations=selected,
                                 strategy="s_team", fallback=bool(counters["fallback"]), shortage=len(eligible) < 3,
                                 interventions=dict(counters), error="model_error" if model_error else None)
        trace.data["interventions"] = result.interventions
        trace.data["fallback"] = result.fallback
        trace.data["outcome"] = {"recommendations": selected}
        return result
    finally:
        trace.write()
