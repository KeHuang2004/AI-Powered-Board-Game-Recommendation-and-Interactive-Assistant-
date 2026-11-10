"""Request models, hard constraints, shared views, and recommendation dispatch."""
from __future__ import annotations

import json
import random
from typing import Literal

from pydantic import BaseModel, Field

from ..catalog import GAMES, GAMES_BY_ID, catalog_view
from ..config import lang_name
from ..harness.prompts import load_yaml, render
from ..harness.tools import RunCtx
from ..llm import LLMClient


class RecommendRequest(BaseModel):
    players: int = Field(ge=1, le=100, strict=True)
    has_dedicated_components: bool = Field(strict=True)
    has_host: bool = Field(strict=True)
    scene: Literal["friends", "date", "family", "new_people"]
    mood: Literal["relaxed", "lively", "thinking", "competitive"]
    language: Literal["en", "zh"] = "zh"
    free_text: str = Field(default="", max_length=600)
    exclude: list[str] = Field(default_factory=list)


class InitialRecommendRequest(RecommendRequest):
    free_text: str = Field(default="", max_length=200)


class Pick(BaseModel):
    game_id: str
    fit_reason: str
    caveat: str | None = None


class SubmitRecommendations(BaseModel):
    analysis: str
    picks: list[Pick]
    note: str | None = None


class RecommendResult(BaseModel):
    method: str
    eligible_count: int
    recommendations: list[dict]
    strategy: str
    fallback: bool = False
    shortage: bool = False
    note: str | None = None
    submitted: dict | None = None
    audit_errors: list[str] = Field(default_factory=list)
    interventions: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


def hard_filter(req: RecommendRequest) -> list[dict]:
    excluded = set(req.exclude)
    return [game for game in GAMES if game["game_id"] not in excluded and req.players in game["players"]
            and (req.has_host or not game["requires_host"])
            and (req.has_dedicated_components or not game["requires_dedicated_components"])]


def render_request(req: RecommendRequest) -> str:
    data = req.model_dump()
    data["exclude_names"] = [GAMES_BY_ID[game_id]["text"][req.language]["name"]
                             for game_id in req.exclude if game_id in GAMES_BY_ID]
    return render("request_block", r=data)


def ordered_games(games: list[dict], order: str | int = "id") -> list[dict]:
    ordered = sorted(games, key=lambda game: game["game_id"])
    if isinstance(order, int):
        random.Random(order).shuffle(ordered)
    elif order != "id":
        raise ValueError("order must be 'id' or an integer seed")
    return ordered


def catalog_lines(games: list[dict], lang: str, order: str | int = "id") -> list[str]:
    return [json.dumps(catalog_view(game, lang), ensure_ascii=False)
            for game in ordered_games(games, order)]


def public_game(game: dict, language: str) -> dict:
    return {**{key: value for key, value in game.items() if key != "text"},
            **game["text"][language], "english_name": game["text"]["en"]["name"]}


def validate_picks(params: SubmitRecommendations, ctx: RunCtx) -> list[str]:
    text = load_yaml("recommend_messages")
    errors = []
    if len(params.picks) != ctx.n_picks:
        errors.append(text["wrong_count"].format(expected=ctx.n_picks, actual=len(params.picks)))
    ids = [pick.game_id for pick in params.picks]
    errors.extend(text["invalid_id"].format(game_id=game_id) for game_id in ids if game_id not in ctx.allowed_ids)
    if len(ids) != len(set(ids)):
        errors.append(text["duplicate_id"])
    if any(not pick.fit_reason.strip() for pick in params.picks):
        errors.append(text["empty_reason"])
    return errors


def fallback(req: RecommendRequest, games: list[dict] | None = None) -> list[dict]:
    from .s_team import candidate_games

    games = hard_filter(req) if games is None else games
    candidates = candidate_games(req, games)
    return [dict(game, reason=game["catalog_reason"], caveat=None) for game in candidates[:3]]


def run_recommendation(req: RecommendRequest, strategy: str, client: LLMClient | None,
                       ctx: RunCtx | None = None, *, order_seed: int | None = None) -> RecommendResult:
    from . import s0, s1, s3, s_team

    eligible = hard_filter(req)
    context = ctx or RunCtx(kind="recommend", template="recommend_user", lang=req.language)
    context.kind = "recommend"
    context.strategy = strategy
    context.lang = req.language
    context.allowed_ids = [game["game_id"] for game in eligible]
    context.n_picks = min(3, len(eligible))
    if strategy not in ("s0", "s1", "s3", "s_team"):
        raise ValueError(f"Unknown recommendation strategy: {strategy}")
    if not client or not eligible:
        return RecommendResult(method="catalog", eligible_count=len(eligible), recommendations=fallback(req, eligible),
                               strategy=strategy, shortage=len(eligible) < 3)
    if strategy == "s_team":
        return s_team.recommend(req, eligible, client, context)
    implementation = {"s0": s0, "s1": s1, "s3": s3}[strategy]
    return implementation.run(req, eligible, client, context,
                              order="id" if order_seed is None else order_seed,
                              language_name=lang_name(req.language))
