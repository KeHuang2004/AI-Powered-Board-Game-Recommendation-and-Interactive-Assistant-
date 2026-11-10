"""The shared catalog snapshot, localized views, and name lookup."""
from __future__ import annotations

import unicodedata

from .catalog_db import load_games

GAMES = load_games()
GAMES_BY_ID = {game["game_id"]: game for game in GAMES}


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().replace("’", "'").split())


def get(game_id: str) -> dict | None:
    return GAMES_BY_ID.get(game_id)


def _names(game: dict) -> set[str]:
    names = set()
    for text in game["text"].values():
        names.add(normalize_name(text["name"]))
        names.update(normalize_name(alias) for alias in text.get("extra", {}).get("aliases", []))
    return names


_NAME_INDEX = {game["game_id"]: _names(game) for game in GAMES}


def search_names(q: str, lang: str, limit: int = 10) -> list[dict]:
    if lang not in ("en", "zh"):
        raise ValueError("Unsupported language")
    query = normalize_name(q)
    if not query or limit <= 0:
        return []
    matches = [game for game in GAMES if any(query in name for name in _NAME_INDEX[game["game_id"]])]
    matches.sort(key=lambda game: (
        query not in _NAME_INDEX[game["game_id"]],
        normalize_name(game["text"][lang]["name"]),
        game["game_id"],
    ))
    return matches[:limit]


def compress_counts(counts: list[int]) -> str:
    values = sorted(set(counts))
    if not values:
        return ""
    groups = []
    start = end = values[0]
    for number in values[1:]:
        if number == end + 1:
            end = number
        else:
            groups.append(str(start) if start == end else f"{start}-{end}")
            start = end = number
    groups.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(groups)


def catalog_view(game: dict, lang: str) -> dict:
    text = game["text"][lang]
    extra = text.get("extra", {})
    return {
        "id": game["game_id"],
        "name": text["name"],
        "players": compress_counts(game["players"]),
        "best": compress_counts(game["recommended_players"]),
        "needs_host": game["requires_host"],
        "special_components": game["requires_dedicated_components"],
        "tags": text["categories"],
        "tier": game["tier"],
        "blurb": text["summary"],
        "why": text["catalog_reason"],
        "win": extra.get("win_relation", ""),
        "setup": extra.get("setup_items", ""),
    }


def game_view(game: dict, lang: str) -> dict:
    text = game["text"][lang]
    extra = text.get("extra", {})
    return {
        **catalog_view(game, lang),
        "core_rules": text["core_rules"],
        "detailed_rules": text["detailed_rules"],
        "conditions": extra.get("conditions", ""),
        "interaction": extra.get("interaction", ""),
        "has_rulebook": game.get("rulebook") is not None,
    }
