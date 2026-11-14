"""Assemble the web application, feature routers, and shared HTTP handling."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from contextvars import copy_context
from typing import Annotated, Literal

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .database import get_games, get_games_by_id
from .setting import ROOT, get_settings, is_verified, product_profile, redact_product_value, setting
from .quickguide.quick_guide_service import router as quick_guide_router
from .recommend.recommend_service import router as recommend_router
from .setting import settings_router
from .harness.policies import check_input
from .harness.prompts import load_yaml
from .llm import make_client
from .tutor import plan, session
from .tutor.state import Phase

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


tutor_router = APIRouter(prefix="/api")


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


def _session(session_id: str) -> session.Session:
    try:
        return session.load_session(session_id)
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


@tutor_router.get("/meta")
def meta(lang: Literal["zh", "en"] = "zh") -> dict:
    texts = load_yaml("tutor_nudges")
    return {"nudges":{name:texts[name][lang] for name in ("didnt_get_it", "next_t0")},
            "phases":[{"id":name, "label":label[lang]} for name, label in texts["phase_labels"].items()]}


@tutor_router.get("/games/{game_id}")
def game(game_id: str, lang: Literal["zh", "en"] = "zh") -> dict:
    if game_id not in get_games_by_id():
        raise _error(404, "game_not_found", "Game not found")
    return session.game_card(game_id, lang)


@tutor_router.post("/sessions")
def create_session(body: CreateSession) -> StreamingResponse:
    client = _configured_client()
    if body.game_id not in get_games_by_id():
        raise _error(404, "game_not_found", "Game not found")
    mode = get_settings().tutor
    if mode not in ("t0", "t1"):
        raise _error(500, "invalid_configuration", "The tutor mode is invalid")
    names = body.player_names
    if names is None:
        pattern = load_yaml("tutor_nudges")["default_player"][body.lang]
        names = [pattern.format(index=index) for index in range(1, body.players + 1)]
    prepared = plan.prepare_plan(get_games_by_id()[body.game_id], body.lang, body.players) if mode == "t1" else None

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
                record = session.create_session(body.game_id, body.lang, names, mode, lesson)
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


@tutor_router.post("/sessions/{session_id}/next")
def next_step(session_id: str) -> dict:
    record = _session(session_id)
    try:
        return session.next_step(record)
    except session.NotAvailableInT0 as error:
        raise _error(409, "not_available_in_t0", "This action is unavailable in T0") from error
    except session.SessionConflict as error:
        raise _error(409, "session_conflict", "Session changed; reload and try again") from error


@tutor_router.post("/sessions/{session_id}/jump")
def jump(session_id: str, body: JumpRequest) -> dict:
    record = _session(session_id)
    try:
        return session.jump(record, body.phase)
    except session.NotAvailableInT0 as error:
        raise _error(409, "not_available_in_t0", "This action is unavailable in T0") from error
    except session.SessionConflict as error:
        raise _error(409, "session_conflict", "Session changed; reload and try again") from error


@tutor_router.post("/sessions/{session_id}/message")
def message(session_id: str, body: MessageRequest) -> StreamingResponse:
    client = _configured_client()
    record = _session(session_id)
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


@tutor_router.post("/sessions/{session_id}/tools/{name}")
def panel_tool(session_id: str, name: str, arguments: Annotated[dict, Body()]) -> dict:
    record = _session(session_id)
    if name not in session.PANEL_TOOLS:
        raise _error(404, "tool_not_found", "Tool not found")
    try:
        result = session.panel_tool(record, name, arguments)
    except session.SessionConflict as error:
        raise _error(409, "session_conflict", "Session changed; reload and try again") from error
    if "error" in result:
        raise _error(400, "invalid_tool_call", result["error"])
    return result


@tutor_router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    return session.session_view(_session(session_id), include_history=True)


app = FastAPI(
    title="玩局 · 桌游推荐与规则 API",
    version="0.1.0",
    description="Recommends board games and explains their rules using the SQLite catalog and configured model.",
)


class RedactJSONResponses:
    """Protect product credentials even if an upstream model echoes one."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        encoded = bytearray()
        is_json = False
        is_sse = False
        started = False
        finished = False

        async def safe_send(message):
            nonlocal is_json, is_sse, started, finished
            if message["type"] == "http.response.start":
                started = True
                headers = message.get("headers", [])
                is_json = any(key.lower() == b"content-type" and b"application/json" in value.lower()
                              for key, value in headers)
                is_sse = any(key.lower() == b"content-type" and b"text/event-stream" in value.lower()
                             for key, value in headers)
                if is_json:
                    message = {**message, "headers":[(key, value) for key, value in headers
                                                     if key.lower() != b"content-length"]}
            elif message["type"] == "http.response.body" and is_json:
                encoded.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                try:
                    parsed = json.loads(encoded)
                    body = json.dumps(redact_product_value(parsed), ensure_ascii=False).encode("utf-8")
                except (ValueError, UnicodeError):
                    body = bytes(encoded)
                message = {**message, "body":body}
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception as error:  # noqa: BLE001 - never expose or log an upstream exception's contents
            logger.warning("Request failed (%s)", type(error).__name__)
            if not started:
                api_path = scope["path"] == "/api" or scope["path"].startswith("/api/")
                body = (b'{"error":{"code":"internal_error","message":"Request failed"}}'
                        if api_path else b"Internal Server Error")
                content_type = b"application/json" if api_path else b"text/plain; charset=utf-8"
                await send({"type":"http.response.start", "status":500, "headers":[(b"content-type", content_type)]})
                await send({"type":"http.response.body", "body":body})
            elif not finished:
                body = (b'event: error\ndata: {"code":"internal_error","message":"Request failed"}\n\n'
                        b'event: done\ndata: {}\n\n') if is_sse else b""
                await send({"type":"http.response.body", "body":body})


app.add_middleware(RedactJSONResponses)


@app.get("/health")
def health():
    settings = product_profile()
    config = get_settings()
    return {
        "status": "ok",
        "games_loaded": len(get_games()),
        "llm_configured": settings is not None,
        "model_provider": settings.provider if settings else None,
        "model": settings.model or None if settings else None,
        "mode": config.deploy_mode,
        "strategies": {"recommend": config.recommender, "tutor": config.tutor},
        "provider_setting": setting("MODEL_PROVIDER", "RECOMMEND_PROVIDER", default="gemini").lower(),
        "verified": True if config.deploy_mode == "server" else is_verified(),
        "search_enabled": config.search_provider != "none",
    }


@app.get("/", include_in_schema=False)
def default_home() -> RedirectResponse:
    return RedirectResponse("/en/")


@app.exception_handler(APIError)
async def api_error_handler(request: Request, error: APIError) -> JSONResponse:
    content = ({"error":{"code":error.code, "message":error.message}} if request.url.path.startswith("/api/")
               else {"detail":error.message})
    return JSONResponse(status_code=error.status, content=content)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=422, content={"error":{"code":"invalid_request", "message":"Invalid request"}})
    return await request_validation_exception_handler(request, error)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, error: StarletteHTTPException):
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        code = "not_found" if error.status_code == 404 else "http_error"
        return JSONResponse(status_code=error.status_code, content={"error":{"code":code, "message":"Request failed"}})
    return await http_exception_handler(request, error)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, error: Exception):
    logger.warning("Request failed (%s)", type(error).__name__)
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"error":{"code":"internal_error", "message":"Request failed"}})
    return PlainTextResponse("Internal Server Error", status_code=500)


app.include_router(recommend_router)
app.include_router(quick_guide_router)
app.include_router(tutor_router)
app.include_router(settings_router)
app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != app.openapi_url]


@app.get(app.openapi_url, include_in_schema=False)
def openapi_schema() -> dict:
    return app.openapi()


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="website")
