"""Team-compatible recommendation routes and the static website."""
from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from ulid import ULID

from . import appdb
from .api import APIError, router
from .config import get_settings, product_profile, redact_product_value
from .harness.trace import Trace
from .llm import make_client
from .recommend.common import (
    InitialRecommendRequest,
    RecommendRequest,
    fallback,
    hard_filter,
    run_recommendation,
)
from .rules_api import BASE_DIR, app
from .settings_api import router as settings_router

logger = logging.getLogger(__name__)


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


class RefineRequest(BaseModel):
    rid: str
    message: str = Field(default="", max_length=200)


def _recommend(req: RecommendRequest, *, parent_rid: str | None = None,
               previous_ids: list[str] | None = None, account: str | None = None) -> dict:
    strategy = get_settings().recommender
    failed_to_initialize = False
    try:
        profile = product_profile()
        client = make_client(profile) if profile else None
    except Exception as error:  # noqa: BLE001 - initialization failure uses the team's catalog fallback
        trace = Trace("recommend", "s_team_user" if strategy == "s_team" else "recommend_user",
                      strategy=strategy, request=req.model_dump())
        trace.add_error(error)
        trace.data["interventions"] = {"fallback": 1}
        trace.data["fallback"] = True
        trace.write()
        client = None
        failed_to_initialize = True
    result = run_recommendation(req, strategy, client)
    response = result.model_dump(exclude={"submitted", "audit_errors", "interventions", "error"})
    if failed_to_initialize:
        response["fallback"] = True
    expected = min(3, len(hard_filter(req)))
    if len(result.recommendations) != expected or any(game.get("unresolved") for game in result.recommendations):
        # An audited submission may contain an ID with no catalog record to render.
        response.update(method="catalog", recommendations=fallback(req), fallback=True)
    rid = "rec_" + str(ULID())
    response["rid"] = rid
    shown = list(dict.fromkeys([*(previous_ids or []), *(item["game_id"] for item in response["recommendations"])]))
    with closing(appdb.connect()) as connection, connection:
        connection.execute(
            "INSERT INTO recommendations VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, datetime.now(UTC).isoformat(), account, json.dumps(req.model_dump(), ensure_ascii=False),
             json.dumps(shown, ensure_ascii=False), json.dumps(response, ensure_ascii=False), parent_rid),
        )
    return response


@app.post("/recommend")
def recommend(req: InitialRecommendRequest, request: Request) -> dict:
    return _recommend(req, account=request.state.account)


@app.post("/recommend/refine")
def refine(body: RefineRequest, request: Request) -> dict:
    with closing(appdb.connect()) as connection:
        row = connection.execute("SELECT request_json, shown_ids_json, account FROM recommendations WHERE rid = ?",
                                 (body.rid,)).fetchone()
    if row is None or (get_settings().deploy_mode == "server" and row["account"] != request.state.account):
        raise HTTPException(status_code=404, detail="Recommendation not found")
    previous = json.loads(row["request_json"])
    shown = json.loads(row["shown_ids_json"])
    previous["exclude"] = list(dict.fromkeys([*previous.get("exclude", []), *shown]))
    if body.message.strip():
        previous["free_text"] = "\n".join(part for part in (previous.get("free_text", ""), body.message.strip()) if part)
    try:
        req = RecommendRequest.model_validate(previous)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Combined preference text exceeds its limit") from error
    return _recommend(req, parent_rid=body.rid, previous_ids=shown, account=request.state.account)


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


app.include_router(router)
app.include_router(settings_router)
app.title = "玩局 · 桌游推荐与规则 API"
# FastAPI's generated schema route is a plain Starlette route, so give it the
# same dependency-based protection as the other non-static endpoints.
app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != app.openapi_url]


@app.get(app.openapi_url, include_in_schema=False)
def openapi_schema() -> dict:
    return app.openapi()


app.mount("/", StaticFiles(directory=BASE_DIR / "frontend", html=True), name="website")
