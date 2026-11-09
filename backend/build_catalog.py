"""Rebuild the SQLite catalog from the two source files and their stable ID map."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SOURCE_FILES = {"en": "boardgame_dataset.json", "zh": "桌游数据集_中文版.json"}
SHARED_TAGS = ("Players", "Recommended players", "Requires DM", "Requires dedicated components")
TEXT_FIELDS = ("Name", "Conclusion", "Core rules", "Detailed rules", "Recommendation reasons")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_by_name(path: Path) -> dict:
    rows = read_json(path)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path.name} must contain a nonempty array")
    indexed = {}
    for row in rows:
        for key in TEXT_FIELDS:
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"Invalid {key} in {path.name}")
        name = row["Name"]
        if name in indexed:
            raise ValueError(f"Duplicate name in {path.name}: {name}")
        indexed[name] = row
    return indexed


def import_games(connection: sqlite3.Connection, source_dir: Path) -> int:
    sources = {locale: source_by_name(source_dir / name) for locale, name in SOURCE_FILES.items()}
    mapping = read_json(source_dir / "game_ids.json")
    if not isinstance(mapping, list) or not mapping:
        raise ValueError("game_ids.json must contain a nonempty array")
    for locale, records in sources.items():
        names = [entry[locale] for entry in mapping]
        if len(names) != len(set(names)) or set(names) != set(records):
            raise ValueError(f"Game ID map does not match the {locale} source")

    for entry in mapping:
        game_id = entry["game_id"]
        if not isinstance(game_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", game_id):
            raise ValueError(f"Invalid game ID: {game_id}")
        records = {locale: source[entry[locale]] for locale, source in sources.items()}
        en, zh = records["en"], records["zh"]
        tags = en["Tags"]
        if any(tags[k] != zh["Tags"][k] for k in SHARED_TAGS) or en["Recommendation tier"] != zh["Recommendation tier"]:
            raise ValueError(f"Shared metadata differs across languages: {game_id}")
        players, recommended = (tags[key] for key in SHARED_TAGS[:2])
        for counts in (players, recommended):
            if (
                not isinstance(counts, list) or not counts
                or any(type(n) is not int or not 1 <= n <= 100 for n in counts)
                or len(set(counts)) != len(counts)
            ):
                raise ValueError(f"Invalid player counts: {game_id}")
        if not set(recommended).issubset(players):
            raise ValueError(f"Recommended players are outside supported counts: {game_id}")
        host, equipment = (tags[key] for key in SHARED_TAGS[2:])
        if type(host) is not bool or type(equipment) is not bool:
            raise ValueError(f"Invalid equipment or host requirement: {game_id}")
        connection.execute(
            "INSERT INTO games VALUES (?, ?, ?, ?)", (game_id, host, equipment, en["Recommendation tier"])
        )
        connection.executemany(
            "INSERT INTO game_player_counts VALUES (?, ?, ?)",
            ((game_id, n, n in recommended) for n in players),
        )
        for locale, row in records.items():
            labels = row["Tags"]["Categories"]
            if not isinstance(labels, list) or not labels or any(not isinstance(t, str) or not t.strip() for t in labels):
                raise ValueError(f"Invalid categories: {game_id} ({locale})")
            connection.execute(
                "INSERT INTO game_text VALUES (?, ?, ?, ?, ?, ?, ?)",
                (game_id, locale, *(row[key].strip() for key in TEXT_FIELDS)),
            )
            connection.executemany(
                "INSERT INTO game_tags VALUES (?, ?, ?, ?)",
                ((game_id, locale, i, tag.strip()) for i, tag in enumerate(labels)),
            )
    return len(mapping)


def build_catalog(data_dir: Path = DATA_DIR) -> int:
    db_dir = data_dir / "database"
    target = db_dir / "boardgames.sqlite3"
    temporary = db_dir / f".boardgames-{uuid4().hex}.tmp"
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript((db_dir / "schema.sql").read_text(encoding="utf-8"))
            with connection:
                count = import_games(connection, data_dir / "sourcedata")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Foreign key check failed")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("SQLite integrity check failed")
        temporary.replace(target)
        return count
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    print(f"Imported {build_catalog()} games into data/database/boardgames.sqlite3")
