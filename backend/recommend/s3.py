"""Search and detail tools exposed to the autonomous retrieval strategy."""
from __future__ import annotations

from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from ..catalog import GAMES, GAMES_BY_ID, catalog_view, game_view
from ..harness.tools import RunCtx, Tool
from ..llm import LLMClient
from ..rulebook import _tokens
from .common import RecommendRequest, RecommendResult


class SearchGames(BaseModel):
    players: int | None = Field(default=None, ge=1, le=100)
    exclude_needs_host: bool | None = None
    exclude_special_components: bool | None = None
    text: str | None = None
    limit: int = Field(default=20, ge=1, le=175)


class GetGame(BaseModel):
    game_id: str


def _index(lang: str) -> BM25Okapi:
    return BM25Okapi([_tokens(" ".join([game["text"][lang]["name"], game["text"][lang]["summary"],
                                        game["text"][lang]["catalog_reason"],
                                        *game["text"][lang]["categories"]])) for game in GAMES])


_INDEX = {language: _index(language) for language in ("en", "zh")}


def search_games(args: SearchGames, ctx: RunCtx) -> dict:
    scores = _INDEX[ctx.lang].get_scores(_tokens(args.text)) if args.text else None
    indexed = [(index, game) for index, game in enumerate(GAMES)
               if (args.players is None or args.players in game["players"])
               and (not args.exclude_needs_host or not game["requires_host"])
               and (not args.exclude_special_components or not game["requires_dedicated_components"])]
    indexed.sort(key=lambda item: (-(float(scores[item[0]]) if scores is not None else 0), item[1]["game_id"]))
    return {"games": [catalog_view(game, ctx.lang) for _, game in indexed[:args.limit]]}


def get_game(args: GetGame, ctx: RunCtx) -> dict:
    return {"game": game_view(GAMES_BY_ID[args.game_id], ctx.lang)}


def tools() -> list[Tool]:
    return [Tool(name="search_games", params=SearchGames, handler=search_games),
            Tool(name="get_game", params=GetGame, handler=get_game)]


def run(req: RecommendRequest, eligible: list[dict], client: LLMClient, ctx: RunCtx, *,
        order: str | int, language_name: str) -> RecommendResult:
    from .strategies import recommend

    return recommend(req, eligible, client, ctx, strategy="s3", order=order, language_name=language_name)
