"""Structured question answering with three source policies."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from ..catalog import GAMES_BY_ID, game_view
from ..config import get_settings, lang_name, product_profile
from ..harness.context import build_messages
from ..harness.loop import run_loop
from ..harness.policies import check_input
from ..harness.prompts import load_yaml
from ..harness.tools import RunCtx, Tool, ToolRegistry
from ..harness.trace import Trace
from ..llm import LLMClient, make_client
from ..tutor.plan import rulebook_text

Grounding = Literal["g1", "g2", "g3"]


class Claim(BaseModel):
    text: str
    src: Literal["rulebook", "dataset", "general"]


class QAAnswer(BaseModel):
    analysis: str
    answer: str
    answerable_from_sources: bool
    claims: list[Claim]


def validate_answer(answer: QAAnswer, ctx: RunCtx) -> list[str]:
    messages = load_yaml("qa_errors")
    errors = []
    if ctx.strategy == "g1" and any(claim.src != "dataset" for claim in answer.claims):
        errors.append(messages["unsupported_g1"])
    if answer.answerable_from_sources and not answer.claims:
        errors.append(messages["missing_claims"])
    return errors


def _submit(answer: QAAnswer, ctx: RunCtx) -> dict:
    return {"accepted": True}


def answer(game_id: str, lang: Literal["en", "zh"], question: str, grounding: Grounding,
           client: LLMClient | None = None, *, model_profile: str = "product", meta: dict | None = None) -> QAAnswer:
    check_input(question)
    if grounding not in ("g1", "g2", "g3"):
        raise ValueError("Unknown grounding strategy")
    game = GAMES_BY_ID[game_id]
    view = game_view(game, lang)
    has_rulebook = grounding == "g3" and bool(game.get("rulebook"))
    book = rulebook_text(game_id) if has_rulebook else None
    variables = {"grounding":grounding, "game_name":view["name"], "lang_name":lang_name(lang),
                 "has_rulebook":has_rulebook, "game_view":json.dumps(view, ensure_ascii=False),
                 "rulebook":book, "question":question}
    messages = build_messages("qa_system", "qa_user", variables)
    if client is None:
        profile = product_profile()
        if profile is None:
            raise ValueError("Model is not configured")
        client = make_client(profile)
    ctx = RunCtx(kind="qa", template="qa_user", strategy=grounding, lang=lang, game_id=game_id,
                 model_profile=model_profile, meta=meta or {})
    registry = ToolRegistry([Tool(name="submit_answer", params=QAAnswer, handler=_submit,
                                  terminal=True, validator=validate_answer)])
    trace = Trace("qa", "qa_user", strategy=grounding, model_profile=model_profile, meta=ctx.meta,
                  request={"game_id":game_id, "lang":lang, "question":question},
                  redact=getattr(client, "_redact", None))
    trace.record_prompt("qa_errors.yaml")
    if book is not None:
        trace.record_prompt("rulebook_context")
    settings = get_settings()
    result = run_loop(client, messages, registry, ["submit_answer"], ctx,
                      settings.max_tool_iters, settings.max_submit_retries, True,
                      trace=trace, fallback_on_failure=False)
    if result.submitted is None:
        raise RuntimeError("No valid answer was submitted")
    return QAAnswer.model_validate(result.submitted)
