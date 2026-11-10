"""Local configuration endpoints and the server login endpoint."""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from . import auth, config
from .errors import APIError
from .llm import make_client, probe_capabilities

router = APIRouter(prefix="/api")
_SETTINGS_LOCK = threading.RLock()
_KEYS = {"provider":"MODEL_PROVIDER", "base_url":"MODEL_BASE_URL",
         "api_key":"MODEL_API_KEY", "model":"MODEL_NAME"}


class LoginRequest(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(max_length=1024)


class ModelSettings(BaseModel):
    provider: Literal["deepseek", "gemini", "compatible", "catalog", "mock"]
    base_url: str = Field(max_length=2048)
    api_key: str = Field(max_length=4096)
    model: str = Field(max_length=512)

    @field_validator("base_url", "api_key", "model")
    @classmethod
    def single_line(cls, value: str) -> str:
        if any(char in value for char in "\n\r\x00"):
            raise ValueError("Configuration values must be single-line")
        return value


class SearchSettings(BaseModel):
    search_enabled: bool


def _mode(expected: str) -> None:
    if config.get_settings().deploy_mode != expected:
        raise APIError(404, "not_found", "Not found")


def _quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def write_env(values: dict[str, str], path: Path) -> None:
    """Replace only the supplied entries and preserve all other lines verbatim."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines(keepends=True)
    updated = []
    remaining = dict(values)
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        match = pattern.match(line)
        key = match.group(1) if match else None
        if key in values:
            if key in remaining:
                updated.append(f"{key}={_quoted(remaining.pop(key))}\n")
        else:
            updated.append(line)
    if remaining and updated and not updated[-1].endswith(("\n", "\r")):
        updated[-1] += "\n"
    updated.extend(f"{key}={_quoted(value)}\n" for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".model-settings-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write("".join(updated))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@router.post("/login")
def login(request: LoginRequest) -> dict:
    _mode("server")
    if not auth.check_credentials(request.username, request.password):
        raise APIError(401, "invalid_credentials", "Invalid username or password")
    token, expires = auth.issue_token(request.username)
    return {"token":token, "expires_at":expires}


@router.get("/settings")
def get_model_settings() -> dict:
    _mode("local")
    provider = config.setting("MODEL_PROVIDER", "RECOMMEND_PROVIDER", default="gemini").lower()
    profile = config.product_profile()
    legacy_key = {"deepseek":"DEEPSEEK_API_KEY", "gemini":"GEMINI_API_KEY", "compatible":"RECOMMEND_API_KEY"}.get(provider, "")
    key = config.setting("MODEL_API_KEY", legacy_key) if legacy_key else config.setting("MODEL_API_KEY")
    return {"provider":provider, "base_url":config.setting("MODEL_BASE_URL", "RECOMMEND_BASE_URL"),
            "model":profile.model if profile else config.setting("MODEL_NAME"), "api_key_set":bool(key),
            "configured":profile is not None, "verified":config.is_verified(),
            "search_enabled":config.get_settings().search_provider != "none"}


@router.put("/settings/search")
def put_search_settings(request: SearchSettings) -> dict:
    _mode("local")
    provider = "deepseek" if request.search_enabled else "none"
    with _SETTINGS_LOCK:
        write_env({"SEARCH_PROVIDER": provider}, config.ROOT / ".env")
        os.environ["SEARCH_PROVIDER"] = provider
    return {"search_enabled": request.search_enabled}


@router.put("/settings")
def put_model_settings(request: ModelSettings) -> dict:
    _mode("local")
    values = {_KEYS[key]:value for key, value in request.model_dump().items()}
    with _SETTINGS_LOCK:
        write_env(values, config.ROOT / ".env")
        os.environ.update(values)
        config.reload_product_profile()
        return get_model_settings()


@router.post("/settings/test")
def test_model_settings() -> dict:
    _mode("local")
    with _SETTINGS_LOCK:
        profile = config.product_profile()
        if profile is None:
            raise APIError(409, "llm_not_configured", "A model must be configured first")
        try:
            result = probe_capabilities(make_client(profile))
        except Exception as error:
            config.set_verified(False)
            raise APIError(502, "model_unavailable", "The configured model is unavailable") from error
        ok = bool(result.get("chat") and result.get("tool_calling") and result.get("multi_turn_tool"))
        config.set_verified(ok)
        return {"ok":ok, "chat":bool(result.get("chat")), "tool_calling":bool(result.get("tool_calling")),
                "latency_ms":result.get("latency_ms", 0),
                "detail":"ok" if ok else "one or more capability checks failed"}
