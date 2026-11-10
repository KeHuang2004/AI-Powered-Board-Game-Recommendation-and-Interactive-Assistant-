"""Read a consistent catalog snapshot from SQLite at application startup."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "database" / "boardgames.sqlite3"


def load_games(path: Path = DATABASE_PATH) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"Catalog database missing at {path}. Run python -m backend.build_catalog.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2):
            raise ValueError("Unsupported catalog schema. Run python -m backend.build_catalog.")
        games = {}
        for row in connection.execute("SELECT * FROM games ORDER BY game_id"):
            games[row["game_id"]] = {
                "game_id": row["game_id"],
                "requires_host": bool(row["requires_host"]),
                "requires_dedicated_components": bool(row["requires_dedicated_components"]),
                "tier": row["tier"],
                "players": [],
                "recommended_players": [],
                "text": {},
            }
        for row in connection.execute("SELECT * FROM game_text"):
            games[row["game_id"]]["text"][row["locale"]] = {
                key: row[key] for key in ("name", "summary", "core_rules", "detailed_rules", "catalog_reason")
            }
            games[row["game_id"]]["text"][row["locale"]]["categories"] = []
        for row in connection.execute("SELECT * FROM game_player_counts ORDER BY game_id, players"):
            game = games[row["game_id"]]
            game["players"].append(row["players"])
            if row["recommended"]:
                game["recommended_players"].append(row["players"])
        for row in connection.execute("SELECT * FROM game_tags ORDER BY game_id, locale, position"):
            games[row["game_id"]]["text"][row["locale"]]["categories"].append(row["tag"])
        if version == 2:
            for game in games.values():
                game["rulebook"] = None
            for row in connection.execute("SELECT * FROM game_extra"):
                extra = {key: row[key] for key in (
                    "setup_items", "conditions", "win_relation", "interaction", "rules_version"
                )}
                extra["aliases"] = json.loads(row["aliases"])
                extra["translated"] = bool(row["translated"])
                games[row["game_id"]]["text"][row["locale"]]["extra"] = extra
            for row in connection.execute("SELECT game_id, filename, est_tokens FROM rulebooks"):
                games[row["game_id"]]["rulebook"] = {
                    "filename": row["filename"], "est_tokens": row["est_tokens"]
                }
    if not games or any(set(g["text"]) != {"en", "zh"} or not g["players"] for g in games.values()):
        raise ValueError("Incomplete SQLite catalog. Run python -m backend.build_catalog.")
    return list(games.values())
