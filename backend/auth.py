"""Server account authentication and request dependency."""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime, timedelta

import bcrypt
import yaml
from fastapi import Request

from . import appdb, config, usage
from .errors import APIError


def check_credentials(username: str, password: str) -> bool:
    path = config.ROOT / "accounts.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        value = data.get("accounts", {}).get(username, "")
        return isinstance(value, str) and bcrypt.checkpw(password.encode("utf-8"), value.encode("ascii"))
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError, yaml.YAMLError):
        return False


def issue_token(account: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(appdb.connect()) as connection, connection:
        connection.execute("INSERT INTO auth_tokens VALUES (?, ?, ?)", (digest, account, expires))
    return token, expires


def account_for_token(token: str) -> str | None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(appdb.connect()) as connection:
        row = connection.execute("SELECT account, expires_at FROM auth_tokens WHERE token_hash=?", (digest,)).fetchone()
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        return row["account"] if expires.tzinfo and expires > datetime.now(UTC) else None
    except ValueError:
        return None


async def authorize_request(request: Request) -> AsyncIterator[None]:
    """Apply to all application routes; mounted static pages bypass dependencies."""
    if request.url.path in ("/", "/health", "/api/login", "/api/meta"):
        yield
        return
    settings = config.get_settings()
    account = "local"
    if settings.deploy_mode == "server":
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        account = account_for_token(token) if scheme.lower() == "bearer" and token else None
        if account is None:
            raise APIError(401, "unauthorized", "Authentication required")
    usage.begin_request(account, enforce_limits=settings.deploy_mode == "server")
    request.state.account = account
    token_context = usage.current_account.set(account)
    try:
        yield
    finally:
        usage.current_account.reset(token_context)
