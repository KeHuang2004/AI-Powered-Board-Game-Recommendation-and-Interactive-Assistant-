"""Pydantic-backed tool schemas, validation, and safe execution."""
from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm import ToolCall
from .prompts import load_yaml


class RunCtx(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: str
    template: str
    strategy: str | None = None
    lang: Literal["zh", "en"] = "en"
    allowed_ids: list[str] = Field(default_factory=list)
    n_picks: int = 0
    audit: bool = False
    game_id: str | None = None
    state: Any = None
    model_profile: str = "product"
    meta: dict = Field(default_factory=dict)


class Tool(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str = ""
    params: type[BaseModel]
    handler: Callable[[BaseModel, RunCtx], dict | list]
    terminal: bool = False
    validator: Callable[[BaseModel, RunCtx], list[str]] | None = None


class ToolOutcome(BaseModel):
    result: dict | list = Field(default_factory=dict)
    error_kind: str | None = None
    terminal: bool = False
    submitted: dict | None = None
    audit_errors: list[str] = Field(default_factory=list)
    args: dict = Field(default_factory=dict)


class ToolInputError(ValueError):
    """An expected, safe-to-display tool validation failure."""


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self.tools = {tool.name: tool for tool in tools or []}
        self.definitions = load_yaml("tools")
        self.messages = load_yaml("harness_messages")

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self.tools[tool.name] = tool

    def _params(self, tool: Tool, ctx: RunCtx | None) -> type[BaseModel]:
        mapping = self.definitions.get("_params_by_template", {})
        path = mapping.get(ctx.template) if ctx and tool.terminal else None
        if not path:
            return tool.params
        module_name, class_name = path.split(":", 1)
        return getattr(importlib.import_module(module_name), class_name)

    def schemas(self, names: list[str], ctx: RunCtx | None = None,
                overrides: dict[str, dict] | None = None) -> list[dict]:
        schemas = []
        for name in names:
            tool = self.tools[name]
            schema = (overrides or {}).get(name) or self._params(tool, ctx).model_json_schema()
            description = self.definitions.get(name, tool.description)
            if not description:
                raise ValueError(f"Missing tool description: {name}")
            schemas.append({"type": "function", "function": {
                "name": name, "description": description, "parameters": schema
            }})
        return schemas

    def execute(self, call: ToolCall, ctx: RunCtx, exposed: list[str] | None = None) -> ToolOutcome:
        if call.name not in self.tools or (exposed is not None and call.name not in exposed):
            return ToolOutcome(result={"error": self.messages["unknown_tool"].format(name=call.name)},
                               error_kind="unknown_tool")
        tool = self.tools[call.name]
        try:
            args = json.loads(call.arguments)
        except (ValueError, TypeError):
            return ToolOutcome(result={"error": self.messages["parse_fail"]}, error_kind="parse_fail")
        try:
            params = self._params(tool, ctx).model_validate(args)
        except ValidationError as error:
            errors = [{"loc": item["loc"], "type": item["type"]}
                      for item in error.errors(include_input=False, include_url=False)]
            return ToolOutcome(result={"error": self.messages["schema_fail"].format(errors=errors)},
                               error_kind="schema_fail", args=args if isinstance(args, dict) else {})
        try:
            errors = tool.validator(params, ctx) if tool.validator else []
            if errors and not ctx.audit:
                return ToolOutcome(result={"error": self.messages["validation_fail"].format(errors=errors)},
                                   error_kind="validation_fail", args=params.model_dump())
            result = tool.handler(params, ctx)
            if not isinstance(result, (dict, list)):
                raise TypeError("Tool handlers must return a mapping or list")
            json.dumps(result, ensure_ascii=False)
        except ToolInputError as error:
            return ToolOutcome(result={"error": str(error)}, error_kind="tool_error", args=params.model_dump())
        except Exception as error:  # noqa: BLE001 - tool failures become tool results
            return ToolOutcome(result={"error": self.messages["tool_error"].format(error=type(error).__name__)},
                               error_kind="tool_error", args=params.model_dump())
        if tool.terminal:
            return ToolOutcome(result=result, terminal=True, submitted=params.model_dump(),
                               audit_errors=errors, args=params.model_dump())
        return ToolOutcome(result=result, args=params.model_dump())
