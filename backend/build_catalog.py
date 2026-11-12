"""Rebuild the SQLite catalog from the two source files and their stable ID map."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SOURCE_FILES = {"en": "boardgame_dataset.json", "zh": "桌游数据集_中文版.json"}
SHARED_TAGS = ("Players", "Recommended players", "Requires DM", "Requires dedicated components")
TEXT_FIELDS = ("Name", "Conclusion", "Core rules", "Detailed rules", "Recommendation reasons")
EXTRA_COLUMNS = {
    "setup_items": "开局用品",
    "conditions": "适用条件",
    "win_relation": "胜负关系",
    "interaction": "玩家互动",
    "rules_version": "规则版本",
}
RULEBOOK_FILES = {"blackjack": "blackjack.pdf", "texas-holdem": "texas_holdem.pdf", "uno": "uno.pdf"}
logger = logging.getLogger(__name__)


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


def split_values(value: object, separator: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(separator) if part.strip()]


def import_extra(connection: sqlite3.Connection, source_dir: Path) -> None:
    from openpyxl import load_workbook

    workbook = load_workbook(source_dir / "桌游推荐_合并版.xlsx", read_only=True, data_only=True)
    try:
        rows = iter(workbook["标签筛选"].iter_rows(min_row=4, values_only=True))
        headings = next(rows)
        records = [dict(zip(headings, row, strict=True)) for row in rows if row[0] is not None]
    finally:
        workbook.close()
    by_name = {row["英文名称"]: row for row in records}
    catalog_names = connection.execute(
        "SELECT en.name, zh.name, en.game_id FROM game_text AS en "
        "JOIN game_text AS zh ON en.game_id = zh.game_id "
        "WHERE en.locale = 'en' AND zh.locale = 'zh'"
    ).fetchall()
    if len(records) != len(by_name) or set(by_name) != {name for name, _, _ in catalog_names}:
        raise ValueError("Excel English names must match the catalog without duplicates")

    translation_path = source_dir / "translations.en.json"
    if translation_path.exists():
        translations = read_json(translation_path)
        if not isinstance(translations, dict):
            raise ValueError("English translations must be an object keyed by game ID")
    else:
        translations = {}
    untranslated = 0
    for name, chinese_name, game_id in catalog_names:
        row = by_name[name]
        if row["游戏名称"] != chinese_name:
            raise ValueError(f"Excel Chinese name does not match the catalog: {game_id}")
        excel_tags = {tag for column in ("类型", "机制", "场景") for tag in split_values(row[column], "、")}
        source_tags = {item[0] for item in connection.execute(
            "SELECT tag FROM game_tags WHERE game_id = ? AND locale = 'zh'", (game_id,)
        )}
        if excel_tags != source_tags:
            raise ValueError(f"Excel categories do not match Chinese categories: {game_id}")
        zh = {key: str(row[column] or "").strip() for key, column in EXTRA_COLUMNS.items()}
        zh["aliases"] = split_values(row["其他检索名"], "、")
        en_raw = translations.get(game_id)
        untranslated += en_raw is None
        if en_raw is not None and (
            not isinstance(en_raw, dict)
            or any(not isinstance(en_raw.get(key), str) for key in (*EXTRA_COLUMNS, "aliases"))
        ):
            raise ValueError(f"Invalid English translation fields: {game_id}")
        en = {key: en_raw[key].strip() if en_raw else "" for key in EXTRA_COLUMNS}
        en["aliases"] = split_values(en_raw["aliases"], ",") if en_raw else []
        for locale, values, translated in (("zh", zh, 1), ("en", en, int(en_raw is not None))):
            connection.execute(
                "INSERT INTO game_extra VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game_id, locale, *(values[key] for key in EXTRA_COLUMNS),
                 json.dumps(values["aliases"], ensure_ascii=False), translated),
            )
    if untranslated:
        logger.warning("English extra fields are unavailable for %d games", untranslated)


def import_rulebooks(connection: sqlite3.Connection, source_dir: Path) -> None:
    from pypdf import PdfReader

    from .rulebook import estimate_tokens

    ids = {row[0] for row in connection.execute("SELECT game_id FROM games")}
    if not set(RULEBOOK_FILES).issubset(ids):
        raise ValueError("A rulebook game ID is missing from the catalog")
    for game_id, filename in RULEBOOK_FILES.items():
        reader = PdfReader(source_dir / "rulebooks" / filename)
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if not text:
            raise ValueError(f"Rulebook has no extractable text: {filename}")
        connection.execute("INSERT INTO rulebooks VALUES (?, ?, ?, ?)",
                           (game_id, filename, text, estimate_tokens(text)))


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
                import_extra(connection, data_dir / "sourcedata")
                import_rulebooks(connection, data_dir / "sourcedata")
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
