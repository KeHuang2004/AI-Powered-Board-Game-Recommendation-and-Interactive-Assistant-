"""Runtime storage, separate from the read-only game catalog."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import ROOT, get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendations (
 rid TEXT PRIMARY KEY, created_at TEXT, account TEXT, request_json TEXT,
 shown_ids_json TEXT, response_json TEXT, parent_rid TEXT
);
CREATE TABLE IF NOT EXISTS plan_cache (
 game_id TEXT, lang TEXT, players INTEGER, model_profile TEXT, prompt_hash TEXT,
 plan_json TEXT, created_at TEXT, PRIMARY KEY (game_id, lang, players, model_profile, prompt_hash)
);
CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, account TEXT, game_id TEXT,
 lang TEXT, mode TEXT, plan_json TEXT, state_json TEXT, history_json TEXT
);
CREATE TABLE IF NOT EXISTS usage (
 account TEXT, day TEXT, tokens_in INTEGER, tokens_out INTEGER, requests INTEGER,
 PRIMARY KEY (account, day)
);
CREATE TABLE IF NOT EXISTS web_cache (
 game_id TEXT, url TEXT, title TEXT, text TEXT, fetched_at TEXT, lang TEXT,
 PRIMARY KEY (game_id, url)
);
CREATE TABLE IF NOT EXISTS auth_tokens (
 token_hash TEXT PRIMARY KEY, account TEXT, expires_at TEXT
);
PRAGMA user_version = 1;
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or get_settings().app_db_path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection
