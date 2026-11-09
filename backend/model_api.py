from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

import httpx
from pydantic import BaseModel

Provider = Literal["deepseek", "gemini", "compatible"]


@dataclass(frozen=True)
class ModelSettings:
    provider: Provider
    model: str
    api_key: str = field(repr=False)
    base_url: str = ""


def setting(name: str, legacy: str = "", default: str = "") -> str:
    return os.getenv(name, os.getenv(legacy, default) if legacy else default).strip()


def model_settings() -> ModelSettings | None:
    provider = setting("MODEL_PROVIDER", "RECOMMEND_PROVIDER", "gemini").lower()
    if provider == "catalog":
        return None
    if provider == "gemini":
        key = setting("MODEL_API_KEY", "GEMINI_API_KEY")
        model = setting("MODEL_NAME", "GEMINI_MODEL", "gemini-2.5-flash")
        return ModelSettings(provider, model, key) if key and model else None
    if provider == "deepseek":
        key = setting("MODEL_API_KEY", "DEEPSEEK_API_KEY")
        model = setting("MODEL_NAME", "RECOMMEND_MODEL") or "deepseek-flash"
        return ModelSettings(provider, model, key, "https://api.deepseek.com") if key else None
    if provider == "compatible":
        key = setting("MODEL_API_KEY", "RECOMMEND_API_KEY")
        model = setting("MODEL_NAME", "RECOMMEND_MODEL")
        url = setting("MODEL_BASE_URL", "RECOMMEND_BASE_URL")
        return ModelSettings(provider, model, key, url) if key and model and url else None
    return None


def generate_text(
    settings: ModelSettings,
    *,
    instructions: str,
    data: str,
    max_tokens: int,
    schema: type[BaseModel] | None = None,
) -> str:
    if settings.provider == "gemini":
        from google import genai

        config = {
            "system_instruction": instructions,
            "temperature": 0.2,
            "max_output_tokens": max_tokens,
        }
        if schema is not None:
            config.update(response_mime_type="application/json", response_schema=schema)
        with genai.Client(api_key=settings.api_key, http_options={"timeout": 12000}) as client:
            response = client.models.generate_content(model=settings.model, contents=data, config=config)
            content = response.text
    else:
        payload = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": data},
            ],
            "max_tokens": max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        if settings.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        response = httpx.post(
            settings.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json=payload,
            timeout=httpx.Timeout(18.0, connect=4.0),
        )
        response.raise_for_status()
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Model response was truncated")
        content = choice["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Model returned no text")
    return content.strip()
