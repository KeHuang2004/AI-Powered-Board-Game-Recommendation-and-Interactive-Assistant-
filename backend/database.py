"""SQLite catalog construction, game queries, rulebook retrieval, and runtime storage."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from functools import cache
from pathlib import Path
from uuid import uuid4

from rank_bm25 import BM25Okapi

from .setting import ROOT, get_settings

DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "database" / "boardgames.sqlite3"


def load_games(path: Path = DATABASE_PATH) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"Catalog database missing at {path}. Run python -m backend.database.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2):
            raise ValueError("Unsupported catalog schema. Run python -m backend.database.")
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
        raise ValueError("Incomplete SQLite catalog. Run python -m backend.database.")
    return list(games.values())


RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendations (
 rid TEXT PRIMARY KEY, created_at TEXT, request_json TEXT,
 shown_ids_json TEXT, response_json TEXT, parent_rid TEXT
);
CREATE TABLE IF NOT EXISTS plan_cache (
 game_id TEXT, lang TEXT, players INTEGER, model_profile TEXT, prompt_hash TEXT,
 plan_json TEXT, created_at TEXT, PRIMARY KEY (game_id, lang, players, model_profile, prompt_hash)
);
CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, game_id TEXT,
 lang TEXT, mode TEXT, plan_json TEXT, state_json TEXT, history_json TEXT
);
CREATE TABLE IF NOT EXISTS web_cache (
 game_id TEXT, url TEXT, title TEXT, text TEXT, fetched_at TEXT, lang TEXT,
 PRIMARY KEY (game_id, url)
);
"""


def connect_runtime(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or get_settings().app_db_path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(RUNTIME_SCHEMA)
        if connection.execute("PRAGMA user_version").fetchone()[0] < 2:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DROP TABLE IF EXISTS auth_tokens")
                connection.execute("PRAGMA user_version = 2")
    except BaseException:
        connection.close()
        raise
    return connection


@cache
def get_games() -> list[dict]:
    return load_games()


@cache
def get_games_by_id() -> dict[str, dict]:
    return {game["game_id"]: game for game in get_games()}


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().replace("’", "'").split())


def get(game_id: str) -> dict | None:
    return get_games_by_id().get(game_id)


def _names(game: dict) -> set[str]:
    names = set()
    for text in game["text"].values():
        names.add(normalize_name(text["name"]))
        names.update(normalize_name(alias) for alias in text.get("extra", {}).get("aliases", []))
    return names


@cache
def _name_index() -> dict[str, set[str]]:
    return {game["game_id"]: _names(game) for game in get_games()}


def search_names(q: str, lang: str, limit: int = 10) -> list[dict]:
    if lang not in ("en", "zh"):
        raise ValueError("Unsupported language")
    query = normalize_name(q)
    if not query or limit <= 0:
        return []
    names_by_id = _name_index()
    matches = [game for game in get_games() if any(query in name for name in names_by_id[game["game_id"]])]
    matches.sort(key=lambda game: (
        query not in names_by_id[game["game_id"]],
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


CJK_PUNCTUATION = frozenset("，。！？；：、（）《》〈〉「」『』【】〔〕［］｛｝“”‘’—…·")
CHUNK_LENGTH = 800
CHUNK_OVERLAP = 150
FULL_CONTEXT_TOKENS = 12000


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0x20000 <= code <= 0x2FA1F
        or 0x3000 <= code <= 0x303F
        or char in CJK_PUNCTUATION
    )


def estimate_tokens(text: str) -> int:
    cjk = sum(_is_cjk(char) for char in text)
    return math.ceil(cjk / 1.5 + (len(text) - cjk) / 4)


def read_rulebook(game_id: str) -> dict | None:
    with closing(sqlite3.connect(DATABASE_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT filename, text, est_tokens FROM rulebooks WHERE game_id = ?", (game_id,)
        ).fetchone()
    return {"filename": row[0], "text": row[1], "est_tokens": row[2]} if row else None


def chunk_text(text: str) -> list[str]:
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start:start + CHUNK_LENGTH]
        chunks.append(chunk)
        if start + CHUNK_LENGTH >= len(text):
            break
        start += CHUNK_LENGTH - CHUNK_OVERLAP
    return chunks


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.casefold())


def search_rulebook(game_id: str, query: str, k: int = 3) -> list[dict]:
    book = read_rulebook(game_id)
    if book is None or k <= 0:
        return []
    chunks = chunk_text(book["text"])
    if not chunks:
        return []
    corpus = [tokenize(chunk) for chunk in chunks]
    if not any(corpus):
        return []
    scores = BM25Okapi(corpus).get_scores(tokenize(query))
    indices = sorted(range(len(chunks)), key=lambda i: (-float(scores[i]), i))[:k]
    return [{"chunk": i, "text": chunks[i], "score": float(scores[i])} for i in indices]


def rulebook_context(game_id: str, k: int = 8) -> dict | None:
    book = read_rulebook(game_id)
    if book is None:
        return None
    if book["est_tokens"] <= FULL_CONTEXT_TOKENS:
        return {"text": book["text"], "is_excerpt": False, "filename": book["filename"]}
    excerpts = search_rulebook(game_id, "setup turn scoring", k)
    return {"text": "\n\n".join(item["text"] for item in excerpts),
            "is_excerpt": True, "filename": book["filename"], "chunks": excerpts}


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
