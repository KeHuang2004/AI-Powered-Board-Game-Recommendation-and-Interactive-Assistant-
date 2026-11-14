"""Persisted game-teaching sessions and their deterministic progression."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ulid import ULID

from .. import database
from ..database import get_games_by_id, game_view
from ..setting import get_settings, lang_name
from ..harness.loop import LoopResult, run_loop
from ..harness.policies import check_input
from ..harness.prompts import load_yaml, render
from ..harness.tools import RunCtx
from ..harness.trace import Trace
from ..llm import LLMClient, ToolCall
from . import tools, web
from .plan import (
    LessonPlan,
    game_context,
    plan_from_storage,
    plan_to_storage,
    rulebook_text,
)
from .state import Phase, SessionState


PANEL_TOOLS = ("roll_dice", "update_score", "reset_scores", "set_players")


class SessionNotFound(LookupError):
    pass


class NotAvailableInT0(ValueError):
    pass


class SessionConflict(RuntimeError):
    pass


@dataclass
class Session:
    session_id: str
    created_at: str
    updated_at: str
    game_id: str
    lang: Literal["zh", "en"]
    mode: Literal["t0", "t1"]
    plan: LessonPlan | None
    state: SessionState
    history: list[dict]


def game_card(game_id: str, lang: str) -> dict:
    game = get_games_by_id()[game_id]
    return {**game_view(game, lang), "other_lang_name": game["text"]["zh" if lang == "en" else "en"]["name"]}


def create_session(game_id: str, lang: Literal["zh", "en"], names: list[str], mode: Literal["t0", "t1"],
                   plan: LessonPlan | None) -> Session:
    now = datetime.now(UTC).isoformat()
    state = SessionState(mode=mode, phase="setup" if mode == "t1" else "qa", players=list(names),
                         scores=dict.fromkeys(names, 0), lang=lang)
    state._initial_player_count = len(names)
    state._plan = plan
    state.total_steps = len(state.steps())
    if mode == "t1" and not state.total_steps:
        state.phase = "qa"
    session = Session("ses_" + str(ULID()), now, now, game_id, lang, mode, plan, state, [])
    _save(session, create=True)
    return session


def _save(session: Session, *, create: bool = False) -> None:
    previous_updated_at = session.updated_at
    new_updated_at = datetime.now(UTC).isoformat()
    state = session.state.model_dump()
    state["initial_player_count"] = session.state._initial_player_count
    values = (session.created_at, new_updated_at, session.game_id, session.lang, session.mode,
              plan_to_storage(session.plan) if session.plan else None, json.dumps(state, ensure_ascii=False),
              json.dumps(session.history, ensure_ascii=False), session.session_id)
    with closing(database.connect_runtime()) as connection, connection:
        if create:
            connection.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at, game_id, lang, mode, "
                "plan_json, state_json, history_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session.session_id, *values[:-1]),
            )
        else:
            cursor = connection.execute(
                "UPDATE sessions SET created_at=?, updated_at=?, game_id=?, lang=?, mode=?, "
                "plan_json=?, state_json=?, history_json=? WHERE session_id=? AND updated_at=?",
                (*values, previous_updated_at),
            )
            if cursor.rowcount != 1:
                raise SessionConflict("Session changed concurrently")
    session.updated_at = new_updated_at


def load_session(session_id: str) -> Session:
    with closing(database.connect_runtime()) as connection:
        row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    data = json.loads(row["state_json"])
    initial_count = data.pop("initial_player_count")
    state = SessionState.model_validate(data)
    lesson = plan_from_storage(row["plan_json"]) if row["plan_json"] else None
    state._initial_player_count = initial_count
    state._plan = lesson
    return Session(row["session_id"], row["created_at"], row["updated_at"], row["game_id"],
                   row["lang"], row["mode"], lesson, state, json.loads(row["history_json"]))


def session_view(session: Session, *, include_history: bool = False) -> dict:
    result = {"session_id":session.session_id, "game":game_card(session.game_id, session.lang),
              "plan":session.plan.model_dump() if session.plan else None,
              "state":session.state.model_dump(), "current":session.state.current()}
    if include_history:
        result["history"] = session.history[-50:]
    return result


def next_step(session: Session) -> dict:
    if session.mode == "t0":
        raise NotAvailableInT0()
    session.state.advance()
    _save(session)
    return {"state":session.state.model_dump(), "current":session.state.current()}


def jump(session: Session, phase: Phase) -> dict:
    if session.mode == "t0":
        raise NotAvailableInT0()
    session.state.jump(phase)
    _save(session)
    return {"state":session.state.model_dump(), "current":session.state.current()}


def system_text(session: Session, *, experiment: bool = False) -> str:
    game = get_games_by_id()[session.game_id]
    search_enabled = not experiment and web.enabled()
    enabled = search_enabled or (not experiment and bool(web.cached_pages(session.game_id)))
    view = game_context(game, session.lang, web_enabled=enabled)
    book = rulebook_text(session.game_id) if game.get("rulebook") else None
    return render("tutor_system", mode=session.mode, game_name=view["name"],
                  players=session.state._initial_player_count, lang_name=lang_name(session.lang),
                  game_view=json.dumps(view, ensure_ascii=False), rulebook=book,
                  plan_json=session.plan.model_dump_json() if session.plan else None, web_enabled=enabled,
                  search_enabled=search_enabled)


def _event_line(event: dict) -> str:
    template = load_yaml("tutor_nudges")["tool_event_line"]
    return template.format(name=event["name"], args=json.dumps(event["args"], ensure_ascii=False, separators=(",", ":")),
                           result=json.dumps(event["result"], ensure_ascii=False, separators=(",", ":")))


def _history_messages(history: list[dict], turns: int) -> tuple[list[dict], list[str]]:
    messages = []
    panel = []
    tool_lines = []
    for item in history:
        if item["role"] == "tool":
            (panel if item.get("panel") else tool_lines).append(_event_line(item))
        elif item["role"] == "user":
            content = "\n".join([*panel, item["text"]])
            panel.clear()
            messages.append({"role":"user", "content":content})
        elif item["role"] == "assistant":
            content = "\n".join([*tool_lines, item["text"]])
            tool_lines.clear()
            messages.append({"role":"assistant", "content":content})
    user_positions = [index for index, item in enumerate(messages) if item["role"] == "user"]
    if turns <= 0:
        return [], panel
    if len(user_positions) > turns:
        messages = messages[user_positions[-turns]:]
    return messages, panel


def build_turn_messages(session: Session, text: str, *, experiment: bool = False) -> list[dict]:
    history, pending = _history_messages(session.history, get_settings().history_turns)
    user = "\n".join([*pending, text])
    if session.mode == "t1":
        current = session.state.current()
        label = load_yaml("tutor_nudges")["phase_labels"][session.state.phase][session.lang]
        user += "\n\n" + render("tutor_state", s=session.state,
                                current_text=current["text"] if current else None, phase_label=label)
    return [{"role":"system", "content":system_text(session, experiment=experiment)}, *history,
            {"role":"user", "content":user}]


def parse_tags(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\[\[(rulebook|dataset|general|web)\]\]", text)))


def invalid_tags(text: str) -> list[str]:
    values = re.findall(r"\[\[([^\]]*)\]\]", text)
    invalid = [value for value in values if value not in {"rulebook", "dataset", "general", "web"}]
    if text.count("[[") != text.count("]]"):
        invalid.append("unclosed_or_unmatched_tag")
    return invalid


def message(session: Session, text: str, client: LLMClient, *, model_profile: str = "product",
            meta: dict | None = None, on_tool: Callable[[dict], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> dict:
    check_input(text)
    experiment = bool(meta and meta.get("run_id"))
    messages = build_turn_messages(session, text, experiment=experiment)
    session.history.append({"role":"user", "text":text})
    initial_state = session.state.model_dump()
    ctx = RunCtx(kind="tutor_turn", template="tutor_turn", strategy=session.mode, lang=session.lang,
                 game_id=session.game_id, state=session.state, model_profile=model_profile, meta=meta or {})
    trace = Trace("tutor_turn", "tutor_system", strategy=session.mode, model_profile=model_profile,
                  meta=ctx.meta, request={"session_id":session.session_id, "text":text, "state":initial_state},
                  redact=getattr(client, "_redact", None))
    trace.record_prompt("tutor_nudges.yaml")
    trace.record_prompt("tutor_errors.yaml")
    if session.mode == "t1":
        trace.record_prompt("tutor_state")
    has_rulebook = get_games_by_id()[session.game_id].get("rulebook") is not None
    if has_rulebook:
        trace.record_prompt("rulebook_context")
    events = []

    def on_event(event: dict) -> None:
        events.append(event)
        session.history.append({"role":"tool", **event, "panel":False})
        if on_tool:
            on_tool(event)

    def on_finish(result: LoopResult, record: Trace) -> None:
        record.data["invalid_source_tags"] = invalid_tags(result.final_text or "")

    def exposed(run_ctx: RunCtx) -> list[str]:
        enabled = not experiment and web.enabled()
        return tools.exposed_tools(run_ctx.state, has_rulebook, web_enabled=enabled,
                                   has_cached=bool(not experiment and web.cached_pages(session.game_id)))

    context_available = not experiment and (web.enabled() or bool(web.cached_pages(session.game_id)))
    result = run_loop(client, messages, tools.registry(web_context=context_available), exposed, ctx,
                      get_settings().max_tool_iters, get_settings().max_submit_retries, False,
                      on_event=on_event, trace=trace, on_finish=on_finish)
    answer = result.final_text
    assistant = None
    if answer is not None:
        tags = parse_tags(answer)
        sources = web.event_sources(events) if "web" in tags else []
        if "web" in tags and not sources and session.plan:
            sources = session.plan._web_sources
        if "web" in tags and not sources:
            previous = next((item.get("sources", []) for item in reversed(session.history)
                             if item.get("role") == "assistant" and item.get("sources")), [])
            sources = previous
        assistant = {"role":"assistant", "text":answer, "tags":tags, "sources":sources}
        session.history.append(assistant)
    if cancelled is not None and cancelled():
        raise TimeoutError("Session request timed out")
    _save(session)
    return {"message":assistant, "events":events, "state":session.state.model_dump(),
            "state_changed":session.state.model_dump() != initial_state, "stop_reason":result.stop_reason}


def panel_tool(session: Session, name: str, arguments: dict) -> dict:
    if name not in PANEL_TOOLS:
        raise ValueError("Tool is unavailable in the panel")
    ctx = RunCtx(kind="tutor_turn", template="tutor_turn", lang=session.lang,
                 game_id=session.game_id, state=session.state, strategy=session.mode)
    call = ToolCall(id=str(ULID()), name=name, arguments=json.dumps(arguments, ensure_ascii=False))
    outcome = tools.registry().execute(call, ctx, [name])
    if outcome.error_kind:
        return {"error":outcome.result["error"]}
    session.history.append({"role":"tool", "name":name, "args":outcome.args,
                            "result":outcome.result, "panel":True})
    _save(session)
    return {"result":outcome.result, "state":session.state.model_dump()}
