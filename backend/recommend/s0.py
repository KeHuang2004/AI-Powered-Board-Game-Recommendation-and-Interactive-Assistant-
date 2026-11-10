"""Full-catalog recommendations in audit mode."""
from __future__ import annotations

from ..harness.tools import RunCtx
from ..llm import LLMClient
from .common import RecommendRequest, RecommendResult


def run(req: RecommendRequest, eligible: list[dict], client: LLMClient, ctx: RunCtx, *,
        order: str | int, language_name: str) -> RecommendResult:
    from .strategies import recommend

    return recommend(req, eligible, client, ctx, strategy="s0", order=order, language_name=language_name)
