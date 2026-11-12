"""Verify the extended catalog and its original compatibility contract."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from shutil import copyfile

import pytest
from openpyxl import load_workbook

from backend import catalog
from backend.build_catalog import import_extra
from backend.catalog_db import DATABASE_PATH, load_games

SOURCE_DIR = DATABASE_PATH.parent.parent / "sourcedata"


def test_catalog_counts_and_imported_data() -> None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        mapping = json.loads((SOURCE_DIR / "game_ids.json").read_text(encoding="utf-8"))
        assert set(connection.execute("SELECT game_id, name FROM game_text WHERE locale = 'en'")) == {
            (entry["game_id"], entry["en"]) for entry in mapping
        }
        assert set(connection.execute("SELECT game_id, name FROM game_text WHERE locale = 'zh'")) == {
            (entry["game_id"], entry["zh"]) for entry in mapping
        }
        assert connection.execute(
            "SELECT locale, translated, count(*) FROM game_extra GROUP BY locale, translated ORDER BY locale"
        ).fetchall() == [("en", 0, 60), ("en", 1, 175), ("zh", 1, 235)]
        assert {row[0] for row in connection.execute("SELECT game_id FROM rulebooks")} == {
            "blackjack", "texas-holdem", "uno"
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Re-importing exercises every bilingual name join and category comparison.
        with closing(sqlite3.connect(":memory:")) as copy:
            connection.backup(copy)
            copy.execute("DELETE FROM game_extra")
            import_extra(copy, SOURCE_DIR)
            assert copy.execute("SELECT count(*) FROM game_extra").fetchone()[0] == 2 * len(mapping)


def test_translation_file_is_imported(tmp_path: Path) -> None:
    copyfile(SOURCE_DIR / "桌游推荐_合并版.xlsx", tmp_path / "桌游推荐_合并版.xlsx")
    translation = {
        "setup_items": "cards", "conditions": "table", "win_relation": "cooperative",
        "interaction": "discuss", "rules_version": "base", "aliases": "Alias one, Alias two",
    }
    (tmp_path / "translations.en.json").write_text(
        json.dumps({"uno": translation}), encoding="utf-8"
    )
    with closing(sqlite3.connect(":memory:")) as copy, closing(sqlite3.connect(DATABASE_PATH)) as source:
        source.backup(copy)
        copy.execute("DELETE FROM game_extra")
        import_extra(copy, tmp_path)
        result = copy.execute(
            "SELECT setup_items, aliases, translated FROM game_extra WHERE game_id='uno' AND locale='en'"
        ).fetchone()
        assert result == ("cards", '["Alias one", "Alias two"]', 1)
        assert copy.execute("SELECT count(*) FROM game_extra WHERE locale='en' AND translated=0").fetchone()[0] == len(catalog.GAMES) - 1


@pytest.mark.parametrize(("cell", "value", "message"), [
    ("K5", "Missing game", "English names"),
    ("A5", "Wrong Chinese name", "Chinese name"),
    ("F5", "Wrong category", "categories"),
])
def test_excel_mismatch_is_rejected(tmp_path: Path, cell: str, value: str, message: str) -> None:
    target = tmp_path / "桌游推荐_合并版.xlsx"
    copyfile(SOURCE_DIR / target.name, target)
    workbook = load_workbook(target)
    workbook["标签筛选"][cell] = value
    workbook.save(target)
    workbook.close()
    with closing(sqlite3.connect(":memory:")) as copy, closing(sqlite3.connect(DATABASE_PATH)) as source:
        source.backup(copy)
        copy.execute("DELETE FROM game_extra")
        with pytest.raises(ValueError, match=message):
            import_extra(copy, tmp_path)


def test_v1_snapshot_remains_readable(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    with closing(sqlite3.connect(DATABASE_PATH)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
        target.executescript("DROP TABLE game_extra; DROP TABLE rulebooks; PRAGMA user_version=1;")
    games = load_games(path)
    assert len(games) == len(catalog.GAMES)
    assert all("rulebook" not in game and "extra" not in game["text"]["zh"] for game in games)
    assert games[0]["text"]["en"]["core_rules"] == load_games()[0]["text"]["en"]["core_rules"]


@pytest.mark.parametrize(("counts", "expected"), [
    ([], ""), ([4], "4"), ([2, 3, 4, 6], "2-4,6"),
    ([6, 2, 3, 2, 4], "2-4,6"), ([1, 100], "1,100"),
])
def test_compress_counts(counts: list[int], expected: str) -> None:
    assert catalog.compress_counts(counts) == expected


@pytest.mark.parametrize(("query", "lang", "game_id"), [
    ("德州扑克", "zh", "texas-holdem"),
    ("Texas Hold’em", "en", "texas-holdem"),
    (" TEXAS  HOLD'EM ", "en", "texas-holdem"),
    ("牛头王", "zh", "6-nimmt"),
    ("Take 5!", "en", "6-nimmt"),
    ("Ark Nova", "en", "ark-nova"),
    ("方舟动物园", "zh", "ark-nova"),
])
def test_name_search(query: str, lang: str, game_id: str) -> None:
    assert game_id in {game["game_id"] for game in catalog.search_names(query, lang)}


def test_localized_views_have_contract_fields() -> None:
    game = catalog.get("uno")
    assert game is not None
    catalog_fields = {"id", "name", "players", "best", "needs_host", "special_components",
                      "tags", "tier", "blurb", "why", "win", "setup"}
    game_fields = catalog_fields | {"core_rules", "detailed_rules", "conditions", "interaction", "has_rulebook"}
    for language in ("en", "zh"):
        assert set(catalog.catalog_view(game, language)) == catalog_fields
        view = catalog.game_view(game, language)
        assert set(view) == game_fields
        assert view["has_rulebook"] is True
        assert view["tags"] == game["text"][language]["categories"]
