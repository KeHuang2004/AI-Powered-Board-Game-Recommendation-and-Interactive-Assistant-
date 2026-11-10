"""Per-account request and actual token accounting for product requests."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from contextvars import ContextVar
from datetime import UTC, datetime

from . import appdb
from .config import get_settings
from .errors import APIError

current_account: ContextVar[str | None] = ContextVar("product_account", default=None)


def _day() -> str:
    return datetime.now(UTC).date().isoformat()


def _attach_rate_store(connection: sqlite3.Connection) -> None:
    """Keep rolling-window events separate from the contracted app schema."""
    main_path = connection.execute("PRAGMA database_list").fetchone()[2]
    connection.execute("ATTACH DATABASE ? AS rate_limits", (main_path + ".limits",))
    connection.execute("CREATE TABLE IF NOT EXISTS rate_limits.events (account TEXT NOT NULL, requested_at REAL NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS rate_limits.events_account_time ON events(account, requested_at)")
    connection.commit()


def begin_request(account: str, *, enforce_limits: bool) -> None:
    settings = get_settings()
    now = time.time()
    day = _day()
    with closing(appdb.connect()) as connection, connection:
        if enforce_limits:
            _attach_rate_store(connection)
        connection.execute("BEGIN IMMEDIATE")
        if enforce_limits:
            connection.execute("DELETE FROM rate_limits.events WHERE requested_at <= ?", (now - 60,))
            requests = connection.execute("SELECT count(*) FROM rate_limits.events WHERE account=?", (account,)).fetchone()[0]
            if requests >= settings.rate_limit_per_min:
                raise APIError(429, "rate_limited", "Too many requests; try again later")
            row = connection.execute("SELECT tokens_in, tokens_out FROM usage WHERE account=? AND day=?",
                                     (account, day)).fetchone()
            if row and row[0] + row[1] >= settings.daily_token_budget:
                raise APIError(429, "budget_exceeded", "Daily token budget exceeded")
            connection.execute("INSERT INTO rate_limits.events VALUES (?, ?)", (account, now))
        connection.execute(
            "INSERT INTO usage VALUES (?, ?, 0, 0, 1) "
            "ON CONFLICT(account, day) DO UPDATE SET requests=requests+1", (account, day),
        )


def record_tokens(tokens_in: int, tokens_out: int) -> None:
    account = current_account.get()
    if account is None:
        return
    with closing(appdb.connect()) as connection, connection:
        connection.execute(
            "INSERT INTO usage VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(account, day) DO UPDATE SET "
            "tokens_in=tokens_in+excluded.tokens_in, tokens_out=tokens_out+excluded.tokens_out",
            (account, _day(), max(0, tokens_in), max(0, tokens_out)),
        )
