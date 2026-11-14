"""Write sanitized, append-only request traces."""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from ..setting import ROOT, get_settings, product_secret_redactor
from .prompts import metadata

FORBIDDEN_KEYS = {"authorization", "headers", "request_headers", "api_key", "apikey", "search_api_key",
                  "reasoning_content"}
_WRITE_LOCK = threading.Lock()


def sanitize(value: Any, redact: Callable[[str], str] | None = None) -> Any:
    if isinstance(value, dict):
        return {key: sanitize(item, redact) for key, item in value.items()
                if str(key).lower() not in FORBIDDEN_KEYS}
    if isinstance(value, (tuple, list)):
        return [sanitize(item, redact) for item in value]
    if isinstance(value, str):
        value = redact(value) if redact else value
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return value
            if isinstance(parsed, (dict, list)):
                return json.dumps(sanitize(parsed, redact), ensure_ascii=False)
        return value
    return value


def messages_hash(messages: list[dict]) -> str:
    data = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class Trace:
    def __init__(self, kind: str, template: str, *, strategy: str | None = None,
                 model_profile: str = "product", meta: dict | None = None,
                 request: dict | None = None, redact: Callable[[str], str] | None = None) -> None:
        try:
            template_info = metadata(template)
        except FileNotFoundError:
            template_info = {"name": template, "version": "none", "sha256": ""}
        self.data = {
            "trace_id": str(ULID()), "ts": datetime.now(UTC).isoformat(),
            "kind": kind, "strategy": strategy, "model_profile": model_profile,
            "model_returned": None, "template": template_info, "protocol": None,
            "prompt_assets": [template_info],
            "spans": [], "interventions": {}, "audit_errors": [], "outcome": {},
            "fallback": False, "request": request or {},
            "meta": {"run_id": None, "item_id": None, "repeat": None, "seed": None, **(meta or {})},
        }
        self._redact = redact

    def record_prompt(self, name: str) -> None:
        info = metadata(name)
        if info not in self.data["prompt_assets"]:
            self.data["prompt_assets"].append(info)

    def add_llm(self, messages: list[dict], result: Any, *, temperature: float | None = None,
                tools: list[dict] | None = None) -> None:
        span = {"type": "llm", "messages_hash": messages_hash(messages),
                "usage": result.usage.model_dump(), "latency_ms": result.latency_ms,
                "tool_calls": [call.model_dump() for call in result.tool_calls],
                "finish_reason": result.finish_reason,
                "temperature": None if result.raw.get("temperature_unsupported") else temperature,
                "protocol": result.raw.get("protocol", "native"),
                "reasoning_content_chars": result.raw.get("reasoning_content_chars", 0)}
        if tools:
            encoded = json.dumps(tools, ensure_ascii=False, sort_keys=True).encode("utf-8")
            span["tool_schema_sha256"] = hashlib.sha256(encoded).hexdigest()
        if result.raw.get("protocol_error"):
            span["protocol_error"] = result.raw["protocol_error"]
        if result.raw.get("model_identity"):
            span["model_identity"] = result.raw["model_identity"]
        if result.raw.get("temperature_unsupported"):
            span["temperature_requested"] = temperature
            span["temperature_unsupported"] = True
        if result.raw.get("retry_statuses"):
            span["retry_statuses"] = result.raw["retry_statuses"]
        if self.data["meta"].get("run_id") or get_settings().debug:
            span["messages"] = deepcopy(messages)
            span["tools"] = deepcopy(tools or [])
        self.data["spans"].append(span)
        self.data["model_returned"] = result.model_returned
        self.data["protocol"] = span["protocol"]

    def add_error(self, error: Exception) -> None:
        span = {"type": "llm", "error": type(error).__name__}
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            span["status_code"] = status
        if getattr(error, "retry_statuses", None):
            span["retry_statuses"] = error.retry_statuses
        self.data["spans"].append(span)

    def add_tool(self, name: str, args: object, result: object, error: object = None,
                 *, before: object = None, after: object = None) -> None:
        span = {"type": "tool", "name": name, "args": args, "result": result, "error": error}
        if self.data["meta"].get("run_id") or get_settings().debug:
            span.update(state_before=before, state_after=after)
        self.data["spans"].append(span)

    def write(self) -> Path:
        settings = get_settings()
        target = Path(settings.trace_file) if settings.trace_file else Path(settings.trace_dir) / (
            datetime.now(UTC).date().isoformat() + ".jsonl"
        )
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        redact_product = product_secret_redactor()

        def redact(text: str) -> str:
            return redact_product(self._redact(text) if self._redact else text)

        with _WRITE_LOCK, target.open("a", encoding="utf-8") as output:
            output.write(json.dumps(sanitize(self.data, redact), ensure_ascii=False, default=str) + "\n")
        return target
