"""Compose stable and variable prompt blocks."""
from __future__ import annotations

from pydantic import BaseModel

from .prompts import render


class Block(BaseModel):
    name: str
    content: str
    stable: bool


class MessageList(list):
    def __init__(self, messages: list[dict], templates: list[str]) -> None:
        super().__init__(messages)
        self.templates = templates


def render_blocks(blocks: list[Block]) -> str:
    return "\n".join(f"<{block.name}>\n{block.content}\n</{block.name}>" for block in blocks)


def build_messages(system_tpl: str, user_tpl: str, ctx: dict, history: list[dict] | None = None) -> list[dict]:
    return MessageList([{"role": "system", "content": render(system_tpl, **ctx)}, *(history or []),
                        {"role": "user", "content": render(user_tpl, **ctx)}], [system_tpl, user_tpl])
