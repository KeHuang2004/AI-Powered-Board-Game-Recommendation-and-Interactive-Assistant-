"""Generate and cache a structured first-game lesson plan."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, PrivateAttr

from .. import appdb, rulebook
from ..catalog import game_view
from ..config import get_settings, lang_name
from ..harness.context import build_messages
from ..harness.loop import run_loop
from ..harness.prompts import load_yaml, metadata, render
from ..harness.tools import RunCtx, Tool
from ..harness.trace import Trace
from ..llm import LLMClient
from . import web


class Step(BaseModel):
    id: str
    text: str
    src: Literal["rulebook", "dataset", "general", "web"]
    tool_hint: str | None = None


class LessonPlan(BaseModel):
    _web_sources: list[dict] = PrivateAttr(default_factory=list)
    analysis: str
    goal: str
    setup: list[Step]
    turn_structure: list[Step]
    first_round: list[Step]
    ending: list[Step]
    pitfalls: list[Step]
    unknowns: list[str]


def validate_plan(plan: LessonPlan, ctx: RunCtx) -> list[str]:
    messages = load_yaml("tutor_errors")
    groups = (plan.setup, plan.turn_structure, plan.first_round, plan.ending, plan.pitfalls)
    steps = [step for group in groups for step in group]
    errors = []
    if len(plan.setup) < 2:
        errors.append(messages["setup_short"])
    if any(not step.text.strip() for step in steps):
        errors.append(messages["step_empty"])
    if not 4 <= len(steps) <= 30:
        errors.append(messages["step_count"])
    if any(step.src == "rulebook" for step in steps) and not ctx.meta.get("has_rulebook"):
        errors.append(messages["rulebook_source_unavailable"])
    if any(step.src == "web" for step in steps):
        if not ctx.meta.get("web_enabled"):
            errors.append(messages["web_disabled_source"])
        elif not ctx.meta.get("has_web_evidence"):
            errors.append(messages["web_source_unread"])
    return errors


def _submit(plan: LessonPlan, ctx: RunCtx) -> dict:
    return {"accepted": True}


def rulebook_text(game_id: str) -> str | None:
    context = rulebook.context_for(game_id)
    if context is None:
        return None
    return render("rulebook_context", is_excerpt=context["is_excerpt"], text=context["text"])


def game_context(game: dict, lang: str, *, web_enabled: bool) -> dict:
    view = game_view(game, lang)
    if web_enabled:
        local = game["text"][lang].get("extra", {})
        chinese = game["text"]["zh"].get("extra", {})
        view["rules_version"] = local.get("rules_version") or chinese.get("rules_version", "")
    return view


def plan_to_storage(plan: LessonPlan) -> str:
    if not plan._web_sources:
        return plan.model_dump_json()
    return json.dumps({**plan.model_dump(), "_web_sources": plan._web_sources}, ensure_ascii=False)


def plan_from_storage(raw: str) -> LessonPlan:
    data = json.loads(raw)
    sources = data.pop("_web_sources", [])
    plan = LessonPlan.model_validate(data)
    plan._web_sources = sources
    return plan


def plan_tool_schema(web_enabled: bool, *, has_rulebook: bool = True) -> dict:
    schema = LessonPlan.model_json_schema()
    sources = schema["$defs"]["Step"]["properties"]["src"]["enum"]
    if not web_enabled:
        sources.remove("web")
    if not has_rulebook:
        sources.remove("rulebook")
    return schema


@dataclass
class PreparedPlan:
    game_id: str
    lang: str
    players: int
    model_profile: str
    prompt_hash: str
    messages: list[dict]
    cached: LessonPlan | None
    has_rulebook: bool
    web_enabled: bool
    search_enabled: bool


def prepare_plan(game: dict, lang: str, players: int, model_profile: str = "product", *,
                 experiment: bool = False) -> PreparedPlan:
    book = rulebook_text(game["game_id"]) if game.get("rulebook") else None
    search_enabled = not experiment and web.enabled() and book is None
    has_cached = not experiment and book is None and bool(web.cached_pages(game["game_id"]))
    web_enabled = search_enabled or has_cached
    view = game_context(game, lang, web_enabled=web_enabled)
    variables = {"game_name": view["name"], "players": players, "lang_name": lang_name(lang),
                 "game_view": json.dumps(view, ensure_ascii=False), "rulebook": book,
                 "web_enabled": web_enabled, "search_enabled": search_enabled}
    messages = build_messages("plan_system", "plan_user", variables)
    hashes = [metadata(name)["sha256"] for name in ("plan_system", "plan_user", "tutor_errors.yaml")]
    if book is not None:
        hashes.append(metadata("rulebook_context")["sha256"])
    digest = hashlib.sha256(json.dumps([hashes, messages], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached = None
    if get_settings().plan_cache:
        with closing(appdb.connect()) as connection:
            row = connection.execute(
                "SELECT plan_json FROM plan_cache WHERE game_id=? AND lang=? AND players=? "
                "AND model_profile=? AND prompt_hash=?", (game["game_id"], lang, players, model_profile, digest)
            ).fetchone()
        if row:
            cached = plan_from_storage(row[0])
    return PreparedPlan(game["game_id"], lang, players, model_profile, digest, messages, cached,
                        book is not None, web_enabled, search_enabled)


def resolve_plan(prepared: PreparedPlan, client: LLMClient, *, meta: dict | None = None,
                 on_tool_start: Callable[[str], None] | None = None) -> LessonPlan:
    from . import tools

    if prepared.cached is not None:
        return prepared.cached
    context_meta = dict(meta or {})
    context_meta.update(web_enabled=prepared.web_enabled, has_rulebook=prepared.has_rulebook, has_web_evidence=False)
    ctx = RunCtx(kind="plan", template="plan_user", lang=prepared.lang, game_id=prepared.game_id,
                 model_profile=prepared.model_profile, meta=context_meta)
    registry = tools.registry(web_context=prepared.web_enabled)
    registry.register(Tool(name="submit_lesson_plan", params=LessonPlan, handler=_submit,
                           validator=validate_plan, terminal=True))
    trace = Trace("plan", "plan_user", model_profile=prepared.model_profile, meta=ctx.meta,
                  request={"game_id":prepared.game_id,"players":prepared.players,"lang":prepared.lang},
                  redact=getattr(client, "_redact", None))
    trace.record_prompt("tutor_errors.yaml")
    if prepared.has_rulebook:
        trace.record_prompt("rulebook_context")
    settings = get_settings()
    web_sources: dict[str, dict] = {}
    schema_overrides = {"submit_lesson_plan": plan_tool_schema(False, has_rulebook=prepared.has_rulebook)}

    def record_read_pages(event: dict) -> None:
        name = event["name"]
        if name == "fetch_page" and not event["result"].get("content", "").strip():
            return
        if name not in {"fetch_page", "lookup_rulebook"}:
            return
        for source in web.event_sources([event]):
            web_sources[source["url"]] = source
        if web_sources and not ctx.meta["has_web_evidence"]:
            ctx.meta["has_web_evidence"] = True
            schema_overrides["submit_lesson_plan"] = plan_tool_schema(True, has_rulebook=prepared.has_rulebook)

    def exposed(_ctx: RunCtx) -> list[str]:
        names = []
        if prepared.web_enabled:
            if web.cached_pages(prepared.game_id):
                names.append("lookup_rulebook")
            if prepared.search_enabled and web.enabled():
                names.extend(["web_search", "fetch_page"])
        return [*names, "submit_lesson_plan"]

    result = run_loop(client, prepared.messages, registry, exposed, ctx,
                      settings.max_tool_iters, settings.max_submit_retries, True,
                      trace=trace, on_tool_start=on_tool_start, on_event=record_read_pages,
                      schema_overrides=schema_overrides, final_submit_tool="submit_lesson_plan",
                      fallback_on_failure=False)
    if result.submitted is None:
        raise RuntimeError("Lesson plan could not be generated")
    plan = LessonPlan.model_validate(result.submitted)
    plan._web_sources = list(web_sources.values())
    with closing(appdb.connect()) as connection, connection:
        connection.execute(
            "INSERT INTO plan_cache VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id, lang, players, model_profile, prompt_hash) DO UPDATE "
            "SET plan_json=excluded.plan_json, created_at=excluded.created_at",
            (prepared.game_id, prepared.lang, prepared.players, prepared.model_profile, prepared.prompt_hash,
             plan_to_storage(plan), datetime.now(UTC).isoformat()),
        )
    return plan
