"""Deterministic tools for player state, dice, scores, and references."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

from .. import rulebook
from ..catalog import GAMES_BY_ID, game_view
from ..harness.prompts import load_yaml
from ..harness.tools import RunCtx, Tool, ToolInputError, ToolRegistry
from . import web
from .state import DiceRoll, SessionState


class RollDice(BaseModel):
    count: int = Field(ge=1, le=10, strict=True)
    sides: int = Field(ge=2, le=100, strict=True)


class UpdateScore(BaseModel):
    player: str
    delta: int = Field(strict=True)


class ResetScores(BaseModel):
    pass


class SetPlayers(BaseModel):
    names: list[str] = Field(min_length=1, max_length=100)

    @field_validator("names")
    @classmethod
    def names_are_unique(cls, values: list[str]) -> list[str]:
        names = [name.strip() for name in values]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError(load_yaml("tutor_errors")["invalid_players"])
        return names


class AdvanceStep(BaseModel):
    pass


class GetGame(BaseModel):
    game_id: str


class LookupRulebook(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class WebSearch(BaseModel):
    query: str = Field(min_length=1, max_length=128)
    max_results: int = Field(default=5, ge=1, le=50, strict=True)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(load_yaml("tutor_errors")["invalid_web_query"])
        return value


class FetchPage(BaseModel):
    url: str = Field(min_length=1, max_length=8192)

    @field_validator("url")
    @classmethod
    def allowed_url(cls, value: str) -> str:
        if not web.valid_url(value):
            raise ValueError(load_yaml("tutor_errors")["invalid_web_url"])
        return value


def _state(ctx: RunCtx) -> SessionState:
    if not isinstance(ctx.state, SessionState):
        raise TypeError("A session state is required")
    return ctx.state


def roll_dice(args: RollDice, ctx: RunCtx) -> dict:
    state = _state(ctx)
    values = [secrets.randbelow(args.sides) + 1 for _ in range(args.count)]
    roll = DiceRoll(spec=f"{args.count}d{args.sides}", values=values, ts=datetime.now(UTC).isoformat())
    state.dice_log.append(roll)
    return roll.model_dump()


def update_score(args: UpdateScore, ctx: RunCtx) -> dict:
    state = _state(ctx)
    if args.player not in state.scores:
        raise ToolInputError(load_yaml("tutor_errors")["unknown_player"].format(player=args.player))
    state.scores[args.player] += args.delta
    return {"scores": dict(state.scores)}


def reset_scores(_args: ResetScores, ctx: RunCtx) -> dict:
    state = _state(ctx)
    state.scores = {name: 0 for name in state.players}
    return {"scores": dict(state.scores)}


def set_players(args: SetPlayers, ctx: RunCtx) -> dict:
    state = _state(ctx)
    state.scores = {name: state.scores.get(name, 0) for name in args.names}
    state.players = list(args.names)
    return {"players": state.players, "scores": dict(state.scores)}


def advance_step(args: AdvanceStep, ctx: RunCtx) -> dict:
    state = _state(ctx)
    state.advance()
    return {"phase": state.phase, "current": state.current()}


def get_game(args: GetGame, ctx: RunCtx) -> dict:
    if args.game_id != ctx.game_id:
        raise ToolInputError(load_yaml("tutor_errors")["wrong_game"])
    return {"game": game_view(GAMES_BY_ID[args.game_id], ctx.lang)}


def lookup_rulebook(args: LookupRulebook, ctx: RunCtx) -> dict:
    if not ctx.game_id:
        raise ToolInputError(load_yaml("tutor_errors")["missing_rulebook"])
    has_book = bool(GAMES_BY_ID[ctx.game_id].get("rulebook"))
    pages = [] if ctx.meta.get("run_id") else web.cached_pages(ctx.game_id)
    if pages:
        return {"passages": web.search_cached_and_rulebook(ctx.game_id, args.query, 3)}
    if not has_book:
        raise ToolInputError(load_yaml("tutor_errors")["missing_rulebook"])
    return {"passages": rulebook.search(ctx.game_id, args.query, 3)}


def web_search(args: WebSearch, ctx: RunCtx) -> list[dict]:
    backend = web.get_backend()
    if backend is None:
        raise ToolInputError(load_yaml("tutor_errors")["search_unavailable"])
    return backend.search(args.query, args.max_results)


def fetch_page(args: FetchPage, ctx: RunCtx) -> dict:
    backend = web.get_backend()
    if backend is None or not ctx.game_id:
        raise ToolInputError(load_yaml("tutor_errors")["search_unavailable"])
    page = backend.fetch(args.url)
    page["content"] = page["content"][:8000]
    web.save_page(ctx.game_id, page, ctx.lang)
    return page


def registry(*, web_context: bool | None = None) -> ToolRegistry:
    registry = ToolRegistry([
        Tool(name="roll_dice", params=RollDice, handler=roll_dice),
        Tool(name="update_score", params=UpdateScore, handler=update_score),
        Tool(name="reset_scores", params=ResetScores, handler=reset_scores,
             description="Set every player's score to zero."),
        Tool(name="set_players", params=SetPlayers, handler=set_players),
        Tool(name="advance_step", params=AdvanceStep, handler=advance_step),
        Tool(name="get_game", params=GetGame, handler=get_game),
        Tool(name="lookup_rulebook", params=LookupRulebook, handler=lookup_rulebook),
        Tool(name="web_search", params=WebSearch, handler=web_search),
        Tool(name="fetch_page", params=FetchPage, handler=fetch_page),
    ])
    if web_context is None:
        web_context = web.enabled()
    if web_context:
        registry.definitions["lookup_rulebook"] = registry.definitions["_lookup_rulebook_with_web"]
    return registry


def exposed_tools(state: SessionState, has_rulebook: bool, *, web_enabled: bool = False,
                  has_cached: bool = False) -> list[str]:
    if state.mode == "t0":
        names = ["roll_dice", "update_score", "set_players", "get_game"]
    elif state.phase == "setup":
        names = ["advance_step", "set_players", "get_game"]
    elif state.phase in ("first_round", "ending"):
        names = ["advance_step", "roll_dice", "update_score", "set_players", "get_game"]
    else:
        names = ["roll_dice", "update_score", "set_players", "get_game"]
    if has_rulebook or has_cached:
        names.append("lookup_rulebook")
    if web_enabled:
        names.extend(["web_search", "fetch_page"])
    return names
