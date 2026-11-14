"""FastAPI endpoints for catalog rules and model-generated explanations."""
from __future__ import annotations

import json
import logging
import unicodedata
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..database import get_games, get_games_by_id
from ..setting import product_profile
from ..harness.context import build_messages
from ..harness.loop import call_model
from ..harness.prompts import load_yaml
from ..harness.trace import Trace
from ..llm import CallCtx, make_client

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


GAMES_BY_NAME = {normalize_name(game["text"]["en"]["name"]): game for game in get_games()}


class GameSummary(BaseModel):
    game_id: str
    name: str


class CoreRulesResponse(GameSummary):
    core_rules: str
    source_field: str = "core rules"


class ExplainRequest(BaseModel):
    game_id: str | None = Field(default=None, max_length=160)
    name: str | None = Field(default=None, max_length=200)
    language: Literal["en", "ru", "zh"] = "en"
    style: Literal["quick", "beginner", "step_by_step"] = "beginner"
    mode: Literal["llm", "raw"] = "llm"
    include_detailed: bool = False

    @model_validator(mode="after")
    def exactly_one_game_reference(self):
        has_id = bool(self.game_id and self.game_id.strip())
        has_name = bool(self.name and self.name.strip())
        if has_id == has_name:
            raise ValueError("Provide exactly one of 'game_id' or 'name'")
        return self


class ExplainResponse(GameSummary):
    answer: str
    source_field: Literal["core rules", "core and detailed rules"] = "core rules"
    source_rules: str
    method: Literal["deepseek", "gemini", "compatible", "mock", "raw"]
    model: str | None = None
    note: str = (
        "This explanation is grounded in the provided rules from the dataset. "
        "Missing details cannot be inferred reliably."
    )


def resolve_game(game_id: str | None = None, name: str | None = None) -> dict:
    game = (
        get_games_by_id().get(game_id.strip())
        if game_id and game_id.strip()
        else GAMES_BY_NAME.get(normalize_name(name or ""))
    )
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found in the dataset")
    return game


router = APIRouter()


@router.get("/rules")
def home():
    return {"service": "Board Game Rules API", "docs": "/docs"}


@router.get("/games", response_model=list[GameSummary])
def list_games():
    return [{"game_id": g["game_id"], "name": g["text"]["en"]["name"]} for g in get_games()]


@router.get("/games/{game_id}/core-rules", response_model=CoreRulesResponse)
def get_core_rules(game_id: str):
    game = resolve_game(game_id=game_id)
    return {
        "game_id": game["game_id"],
        "name": game["text"]["en"]["name"],
        "core_rules": game["text"]["en"]["core_rules"],
    }


@router.post("/rules/explain", response_model=ExplainResponse)
def explain_rules(request: ExplainRequest):
    game = resolve_game(game_id=request.game_id, name=request.name)
    core = game["text"]["en"]["core_rules"]
    answer = source_rules = core
    source_field = "core rules"
    method = "raw"
    model = None
    if request.mode == "llm":
        settings = product_profile()
        if settings is None:
            raise HTTPException(status_code=503, detail="No model is configured")
        languages = {"en": "English", "ru": "Russian", "zh": "Simplified Chinese"}
        localized = game["text"]["zh" if request.language == "zh" else "en"]
        source = {"game_name": localized["name"], "core_rules": localized["core_rules"]}
        source_rules = source["core_rules"]
        if request.include_detailed:
            source["detailed_rules"] = localized["detailed_rules"]
            source_rules += "\n\n" + source["detailed_rules"]
            source_field = "core and detailed rules"
        styles = load_yaml("legacy_styles")
        messages = build_messages("legacy_explain", "legacy_user", {
            "language_name": languages[request.language], "style_instruction": styles[request.style],
            "source_json": json.dumps(source, ensure_ascii=False),
        })
        trace = Trace("explain", "legacy_user", request={"game_id":game["game_id"],
                      "language":request.language, "style":request.style, "include_detailed":request.include_detailed})
        trace.record_prompt("legacy_explain")
        trace.record_prompt("legacy_styles.yaml")
        try:
            try:
                client = make_client(settings)
            except Exception as error:
                trace.add_error(error)
                raise
            trace._redact = getattr(client, "_redact", None)
            thinking = bool(settings.reasoning_effort or settings.extra_body.get("enable_thinking") or (
                isinstance(settings.extra_body.get("thinking"), dict)
                and settings.extra_body["thinking"].get("type") == "enabled"
            ))
            for attempt in range(2):
                response = call_model(client, trace, messages, max_tokens=settings.max_tokens or 1000,
                                      ctx=CallCtx(kind="explain", template="legacy_user"))
                if response.finish_reason == "length":
                    trace.data["interventions"]["truncated"] = trace.data["interventions"].get("truncated", 0) + 1
                    if thinking and attempt == 0:
                        continue
                    raise ValueError("Model response was truncated")
                if not response.content or not response.content.strip():
                    raise ValueError("Model returned no text")
                answer = response.content.strip()
                break
            trace.data["outcome"] = {"answer":answer}
        except Exception as error:
            trace.data["outcome"] = {"error": type(error).__name__}
            logger.warning("Rule explanation model failed (%s)", type(error).__name__)
            raise HTTPException(status_code=502, detail="Model request failed") from error
        finally:
            trace.write()
        method = settings.provider
        model = settings.model or None
    return {
        "game_id": game["game_id"],
        "name": game["text"]["en"]["name"],
        "answer": answer,
        "source_field": source_field,
        "source_rules": source_rules,
        "method": method,
        "model": model,
    }
