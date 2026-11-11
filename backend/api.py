"""Guided-play API and server-sent events."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from contextvars import copy_context
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from .catalog import GAMES_BY_ID
from .config import get_settings, is_verified, product_profile, redact_product_value
from .errors import APIError
from .harness.policies import check_input
from .harness.prompts import load_yaml
from .llm import make_client
from .qa.answer import answer as answer_question
from .tutor import plan, session
from .tutor.state import Phase

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def _error(status: int, code: str, message: str) -> APIError:
    return APIError(status, code, message)


def _configured_client():
    try:
        profile = product_profile()
        if profile is None or (get_settings().deploy_mode == "local" and not is_verified()):
            raise _error(409, "llm_not_configured", "A model must be configured first")
        return make_client(profile)
    except APIError:
        raise
    except Exception as error:
        raise _error(502, "model_unavailable", "The configured model is unavailable") from error


def _session(session_id: str, request: Request) -> session.Session:
    try:
        record = session.load_session(session_id)
        if get_settings().deploy_mode == "server" and record.account != request.state.account:
            raise session.SessionNotFound(session_id)
        return record
    except session.SessionNotFound as error:
        raise _error(404, "session_not_found", "Session not found") from error


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(redact_product_value(data), ensure_ascii=False)}\n\n"


def _events_until_done(events: queue.Queue, cancelled: threading.Event):
    deadline = time.monotonic() + get_settings().sse_timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancelled.set()
            yield _sse("error", {"code":"stream_timeout", "message":"The request timed out"})
            yield _sse("done", {})
            return
        try:
            kind, data = events.get(timeout=min(15, remaining))
        except queue.Empty:
            if time.monotonic() < deadline:
                yield ": keepalive\n\n"
            continue
        if kind == "result":
            if data["message"] is not None:
                yield _sse("delta", {"text":data["message"]["text"]})
            if data["state_changed"]:
                yield _sse("state", data["state"])
            if data["message"] is not None:
                yield _sse("message", data["message"])
            else:
                yield _sse("error", {"code":"model_error", "message":"Could not generate a reply"})
        else:
            yield _sse(kind, data)
        if kind == "done":
            return


def _start_worker(worker) -> None:
    context = copy_context()
    threading.Thread(target=lambda: context.run(worker), daemon=True).start()


class CreateSession(BaseModel):
    game_id: str
    lang: Literal["zh", "en"]
    players: int = Field(ge=1, le=100, strict=True)
    player_names: list[str] | None = None
    rid: str | None = None

    @model_validator(mode="after")
    def validate_names(self) -> CreateSession:
        if self.player_names is not None:
            names = [name.strip() for name in self.player_names]
            if len(names) != self.players or any(not name for name in names) or len(set(names)) != len(names):
                raise ValueError("Player names must be unique and match the player count")
            self.player_names = names
        return self


class JumpRequest(BaseModel):
    phase: Phase


class MessageRequest(BaseModel):
    text: str = Field(max_length=2000)


class QARequest(BaseModel):
    game_id: str
    lang: Literal["zh", "en"]
    question: str = Field(max_length=2000)


@router.get("/meta")
def meta(lang: Literal["zh", "en"] = "zh") -> dict:
    texts = load_yaml("tutor_nudges")
    return {"nudges":{name:texts[name][lang] for name in ("didnt_get_it", "next_t0")},
            "phases":[{"id":name, "label":label[lang]} for name, label in texts["phase_labels"].items()]}


@router.get("/games/{game_id}")
def game(game_id: str, lang: Literal["zh", "en"] = "zh") -> dict:
    if game_id not in GAMES_BY_ID:
        raise _error(404, "game_not_found", "Game not found")
    return session.game_card(game_id, lang)


@router.post("/qa")
def qa(request: QARequest) -> dict:
    client = _configured_client()
    if request.game_id not in GAMES_BY_ID:
        raise _error(404, "game_not_found", "Game not found")
    grounding = get_settings().grounding
    if grounding not in ("g1", "g2", "g3"):
        raise _error(500, "invalid_configuration", "The grounding mode is invalid")
    try:
        result = answer_question(request.game_id, request.lang, request.question, grounding, client)
    except Exception as error:
        logger.warning("Question answering failed (%s)", type(error).__name__)
        raise _error(502, "model_error", "Could not answer the question") from error
    return result.model_dump()


@router.post("/sessions")
def create_session(body: CreateSession, request: Request) -> StreamingResponse:
    client = _configured_client()
    if body.game_id not in GAMES_BY_ID:
        raise _error(404, "game_not_found", "Game not found")
    mode = get_settings().tutor
    if mode not in ("t0", "t1"):
        raise _error(500, "invalid_configuration", "The tutor mode is invalid")
    names = body.player_names
    if names is None:
        pattern = load_yaml("tutor_nudges")["default_player"][body.lang]
        names = [pattern.format(index=index) for index in range(1, body.players + 1)]
    prepared = plan.prepare_plan(GAMES_BY_ID[body.game_id], body.lang, body.players) if mode == "t1" else None
    account = request.state.account

    def stream():
        stage = "ready" if prepared is None else ("cache_hit" if prepared.cached else "planning")
        yield _sse("status", {"stage":stage})
        events: queue.Queue = queue.Queue()
        cancelled = threading.Event()

        def report_plan_tool(name: str) -> None:
            tool_stage = {
                "web_search": "searching",
                "fetch_page": "reading",
                "lookup_rulebook": "reading",
                "submit_lesson_plan": "checking",
            }.get(name)
            if tool_stage and not cancelled.is_set():
                events.put(("status", {"stage": tool_stage}))

        def worker() -> None:
            try:
                lesson = plan.resolve_plan(prepared, client, on_tool_start=report_plan_tool) if prepared else None
                if cancelled.is_set():
                    return
                events.put(("status", {"stage": "starting"}))
                record = session.create_session(body.game_id, body.lang, names, mode, lesson, account=account)
                events.put(("session", session.session_view(record)))
            except Exception as error:  # noqa: BLE001 - report safely inside the stream
                logger.warning("Session creation failed (%s)", type(error).__name__)
                events.put(("error", {"code":"session_creation_failed", "message":"Could not create session"}))
            finally:
                events.put(("done", {}))

        _start_worker(worker)
        try:
            yield from _events_until_done(events, cancelled)
        finally:
            cancelled.set()

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/next")
def next_step(session_id: str, request: Request) -> dict:
    record = _session(session_id, request)
    try:
        return session.next_step(record)
    except session.NotAvailableInT0 as error:
        raise _error(409, "not_available_in_t0", "This action is unavailable in T0") from error
    except session.SessionConflict as error:
        raise _error(409, "session_conflict", "Session changed; reload and try again") from error


@router.post("/sessions/{session_id}/jump")
def jump(session_id: str, body: JumpRequest, request: Request) -> dict:
    record = _session(session_id, request)
    try:
        return session.jump(record, body.phase)
    except session.NotAvailableInT0 as error:
        raise _error(409, "not_available_in_t0", "This action is unavailable in T0") from error
    except session.SessionConflict as error:
        raise _error(409, "session_conflict", "Session changed; reload and try again") from error


@router.post("/sessions/{session_id}/message")
def message(session_id: str, body: MessageRequest, request: Request) -> StreamingResponse:
    client = _configured_client()
    record = _session(session_id, request)
    check_input(body.text)

    def stream():
        events: queue.Queue = queue.Queue()
        cancelled = threading.Event()

        def worker() -> None:
            try:
                result = session.message(record, body.text, client,
                                         on_tool=lambda event: events.put(("tool", event)),
                                         cancelled=cancelled.is_set)
                events.put(("result", result))
            except session.SessionConflict:
                events.put(("error", {"code":"session_conflict", "message":"Session changed; reload and try again"}))
            except Exception as error:  # noqa: BLE001 - SSE reports a safe error and always finishes
                logger.warning("Session message failed (%s)", type(error).__name__)
                events.put(("error", {"code":"message_failed", "message":"Could not process the message"}))
            finally:
                events.put(("done", {}))

        _start_worker(worker)
        yield from _events_until_done(events, cancelled)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/tools/{name}")
def panel_tool(session_id: str, name: str, request: Request, arguments: Annotated[dict, Body()]) -> dict:
    record = _session(session_id, request)
    if name not in session.PANEL_TOOLS:
        raise _error(404, "tool_not_found", "Tool not found")
    try:
        result = session.panel_tool(record, name, arguments)
    except session.SessionConflict as error:
        raise _error(409, "session_conflict", "Session changed; reload and try again") from error
    if "error" in result:
        raise _error(400, "invalid_tool_call", result["error"])
    return result


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict:
    return session.session_view(_session(session_id, request), include_history=True)
