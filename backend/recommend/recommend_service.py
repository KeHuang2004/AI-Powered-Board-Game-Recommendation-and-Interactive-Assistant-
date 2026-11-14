"""HTTP endpoints for game recommendations and refinement."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from ulid import ULID

from .. import database
from ..setting import get_settings, product_profile
from ..harness.trace import Trace
from ..llm import make_client
from .common import (
    InitialRecommendRequest,
    RecommendRequest,
    fallback,
    hard_filter,
    run_recommendation,
)

router = APIRouter()


class RefineRequest(BaseModel):
    rid: str
    message: str = Field(default="", max_length=200)


def _recommend(req: RecommendRequest, *, parent_rid: str | None = None,
               previous_ids: list[str] | None = None) -> dict:
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
    with closing(database.connect_runtime()) as connection, connection:
        connection.execute(
            "INSERT INTO recommendations (rid, created_at, request_json, shown_ids_json, response_json, parent_rid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rid, datetime.now(UTC).isoformat(), json.dumps(req.model_dump(), ensure_ascii=False),
             json.dumps(shown, ensure_ascii=False), json.dumps(response, ensure_ascii=False), parent_rid),
        )
    return response


@router.post("/recommend")
def recommend(req: InitialRecommendRequest) -> dict:
    return _recommend(req)


@router.post("/recommend/refine")
def refine(body: RefineRequest) -> dict:
    with closing(database.connect_runtime()) as connection:
        row = connection.execute("SELECT request_json, shown_ids_json FROM recommendations WHERE rid = ?",
                                 (body.rid,)).fetchone()
    if row is None:
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
    return _recommend(req, parent_rid=body.rid, previous_ids=shown)
