"""Application configuration and settings endpoints."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


class Price(BaseModel):
    input: float = Field(default=0, ge=0)
    cached_input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)


class Profile(BaseModel):
    name: str
    transport: Literal["openai_compat", "codex_cli", "mock"]
    model: str = ""
    family: str
    tier: str = "aux"
    provider: str = ""
    base_url: str = ""
    api_key_env: str = ""
    api_key_file: str = ""
    api_key_field: str = "api_key"
    reasoning_effort: str = ""
    extra_body: dict = Field(default_factory=dict)
    max_tokens: int | None = Field(default=16384, gt=0)
    timeout_s: float = Field(default=300, gt=0)
    max_concurrency: int = Field(default=4, gt=0)
    force_text_protocol: bool = False
    replay_reasoning: bool = False
    price: Price = Field(default_factory=Price)

    @model_validator(mode="after")
    def validate_connection(self) -> Profile:
        if self.transport != "mock" and not self.model:
            raise ValueError(f"Profile {self.name} requires a model")
        if self.transport == "openai_compat" and (not self.base_url or not (self.api_key_env or self.api_key_file)):
            raise ValueError(f"Profile {self.name} requires base_url and a key source")
        return self


ClientConfig = Profile


def profile_hash(profile: Profile) -> str:
    data = json.dumps(profile.model_dump(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _lookup(*keys: str, default: str = "") -> tuple[str, str]:
    for key in keys:
        if key in os.environ:
            return os.environ[key].strip(), key
    values = dotenv_values(ROOT / ".env")
    for key in keys:
        if key in values:
            return (values[key] or "").strip(), key
    return default, ""


def setting(name: str, *legacy: str, default: str = "") -> str:
    return _lookup(name, *legacy, default=default)[0]


def product_secret_redactor():
    secrets = tuple(secret for name in ("MODEL_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "RECOMMEND_API_KEY",
                                        "SEARCH_API_KEY")
                    if (secret := setting(name)))

    def redact(value: str) -> str:
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    return redact


def redact_product_value(value):
    redact = product_secret_redactor()

    def apply(item):
        if isinstance(item, dict):
            return {redact(str(key)):apply(child) for key, child in item.items()}
        if isinstance(item, list):
            return [apply(child) for child in item]
        return redact(item) if isinstance(item, str) else item

    return apply(value)


def product_profile() -> Profile | None:
    provider = setting("MODEL_PROVIDER", "RECOMMEND_PROVIDER", default="gemini").lower()
    if provider == "catalog":
        return None
    if provider == "mock":
        return Profile(name="product", transport="mock", family="mock", provider="mock", max_tokens=None)
    if provider not in ("deepseek", "gemini", "compatible"):
        return None
    old_model = "GEMINI_MODEL" if provider == "gemini" else "RECOMMEND_MODEL"
    model = setting("MODEL_NAME", old_model)
    old_key = {"gemini": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "compatible": "RECOMMEND_API_KEY"}[provider]
    key, key_source = _lookup("MODEL_API_KEY", old_key)
    base_url = {
        "deepseek": "https://api.deepseek.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "compatible": setting("MODEL_BASE_URL", "RECOMMEND_BASE_URL"),
    }[provider]
    if not model or not key or not base_url:
        return None
    extra_str = setting("MODEL_EXTRA_BODY")
    try:
        extra = json.loads(extra_str) if extra_str else {}
    except json.JSONDecodeError as exc:
        raise ValueError("MODEL_EXTRA_BODY must be a JSON object") from exc
    if not isinstance(extra, dict):
        raise TypeError("MODEL_EXTRA_BODY must be a JSON object")
    effort = setting("MODEL_REASONING_EFFORT")
    thinking = bool(effort or extra.get("enable_thinking") or (
        isinstance(extra.get("thinking"), dict) and extra["thinking"].get("type") == "enabled"
    ))
    return Profile(name="product", transport="openai_compat", model=model, family=provider,
                   provider=provider, base_url=base_url, api_key_env=key_source,
                   reasoning_effort=effort, extra_body=extra, max_tokens=16384 if thinking else None,
                   timeout_s=float(setting("MODEL_TIMEOUT_S", default="60")))


def read_api_key(profile: Profile) -> str:
    """Resolve a key only when a client is being constructed."""
    if profile.api_key_file:
        path = Path(profile.api_key_file)
        if not path.is_absolute():
            path = ROOT / path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))[profile.api_key_field]
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"Cannot read key field for profile {profile.name}") from exc
    else:
        value = setting(profile.api_key_env) if profile.api_key_env else ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing API key for profile {profile.name}")
    return value.strip()






class Settings(BaseModel):
    deploy_mode: Literal["local", "server"] = "local"
    recommender: str = "s_team"
    tutor: str = "t1"
    max_tool_iters: int = 3
    max_submit_retries: int = 2
    history_turns: int = 8
    trace_dir: str = "data/traces"
    trace_file: str = ""
    plan_cache: bool = True
    search_provider: Literal["none", "deepseek", "mock"] = "none"
    debug: bool = False
    app_db_path: str = "data/app.db"
    sse_timeout_s: float = Field(default=300, gt=0)


def get_settings() -> Settings:
    defaults = Settings()
    values = {}
    for name in Settings.model_fields:
        default = getattr(defaults, name)
        raw = setting(name.upper(), default=str(int(default)) if isinstance(default, bool) else str(default))
        values[name] = raw
    return Settings(**values)


def lang_name(lang: str) -> str:
    return {"zh": "Chinese", "en": "English", "ru": "Russian"}[lang]


_verified = product_profile() is not None


def is_verified() -> bool:
    return _verified


def set_verified(value: bool) -> None:
    global _verified
    _verified = value


def reload_product_profile() -> Profile | None:
    set_verified(False)
    return product_profile()


settings_router = APIRouter(prefix="/api")
_SETTINGS_LOCK = threading.RLock()
_KEYS = {"provider":"MODEL_PROVIDER", "base_url":"MODEL_BASE_URL",
         "api_key":"MODEL_API_KEY", "model":"MODEL_NAME"}


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
    from .server import APIError

    if get_settings().deploy_mode != expected:
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
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            if os.name == "posix":
                os.fchmod(output.fileno(), 0o600)
            output.write("".join(updated))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@settings_router.get("/settings")
def get_model_settings() -> dict:
    _mode("local")
    provider = setting("MODEL_PROVIDER", "RECOMMEND_PROVIDER", default="gemini").lower()
    profile = product_profile()
    legacy_key = {"deepseek":"DEEPSEEK_API_KEY", "gemini":"GEMINI_API_KEY", "compatible":"RECOMMEND_API_KEY"}.get(provider, "")
    key = setting("MODEL_API_KEY", legacy_key) if legacy_key else setting("MODEL_API_KEY")
    return {"provider":provider, "base_url":setting("MODEL_BASE_URL", "RECOMMEND_BASE_URL"),
            "model":profile.model if profile else setting("MODEL_NAME"), "api_key_set":bool(key),
            "configured":profile is not None, "verified":is_verified(),
            "search_enabled":get_settings().search_provider != "none"}


@settings_router.put("/settings/search")
def put_search_settings(request: SearchSettings) -> dict:
    _mode("local")
    provider = "deepseek" if request.search_enabled else "none"
    with _SETTINGS_LOCK:
        write_env({"SEARCH_PROVIDER": provider}, ROOT / ".env")
        os.environ["SEARCH_PROVIDER"] = provider
    return {"search_enabled": request.search_enabled}


@settings_router.put("/settings")
def put_model_settings(request: ModelSettings) -> dict:
    _mode("local")
    values = {_KEYS[key]:value for key, value in request.model_dump().items()}
    with _SETTINGS_LOCK:
        write_env(values, ROOT / ".env")
        os.environ.update(values)
        reload_product_profile()
        return get_model_settings()


@settings_router.post("/settings/test")
def test_model_settings() -> dict:
    from .llm import make_client, probe_capabilities
    from .server import APIError

    _mode("local")
    with _SETTINGS_LOCK:
        profile = product_profile()
        if profile is None:
            raise APIError(409, "llm_not_configured", "A model must be configured first")
        try:
            result = probe_capabilities(make_client(profile))
        except Exception as error:
            set_verified(False)
            raise APIError(502, "model_unavailable", "The configured model is unavailable") from error
        ok = bool(result.get("chat") and result.get("tool_calling") and result.get("multi_turn_tool"))
        set_verified(ok)
        return {"ok":ok, "chat":bool(result.get("chat")), "tool_calling":bool(result.get("tool_calling")),
                "latency_ms":result.get("latency_ms", 0),
                "detail":"ok" if ok else "one or more capability checks failed"}
