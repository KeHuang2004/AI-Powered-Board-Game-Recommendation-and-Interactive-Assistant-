"""Exercise tool failures, bounded loops, audit behavior, and traces."""
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from backend.setting import Profile
from backend.harness.loop import run_loop
from backend.harness.prompts import render
from backend.harness.tools import RunCtx, Tool, ToolRegistry
from backend.llm import MockClient

SCRIPTS = yaml.safe_load((Path(__file__).parent / "fixtures/mock_scripts/interventions.yaml").read_text())


class Submission(BaseModel):
    analysis: str
    value: int


class Regular(BaseModel):
    value: int


def submit(args: Submission, ctx: RunCtx) -> dict:
    if args.value == 8:
        raise RuntimeError("handler failure")
    return {"accepted": True}


def validate(args: Submission, ctx: RunCtx) -> list[str]:
    return ["invalid value"] if args.value == 9 else []


def regular(args: Regular, ctx: RunCtx) -> dict:
    return {"value": args.value}


def registry() -> ToolRegistry:
    tools = ToolRegistry([
        Tool(name="submit_test", params=Submission, handler=submit, terminal=True, validator=validate),
        Tool(name="regular_test", params=Regular, handler=regular),
    ])
    tools.definitions.update(submit_test="Test submission", regular_test="Test regular tool")
    return tools


@pytest.fixture(autouse=True)
def trace_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRACE_FILE", str(tmp_path / "trace.jsonl"))


@pytest.mark.parametrize("scenario", ["unknown_tool", "parse_fail", "schema_fail", "tool_error", "validation_fail"])
def test_failure_is_returned_and_retried(scenario: str) -> None:
    result = run_loop(MockClient(script=SCRIPTS[scenario]), [], registry(), ["submit_test"],
                      RunCtx(kind="probe", template="probe_user"))
    assert result.interventions[scenario] == 1
    assert result.stop_reason == "submitted"
    assert result.submitted == {"analysis": "test", "value": 1}
    assert "error" in result.events[0]["result"]


def test_no_submit_hits_cap_and_fallback() -> None:
    result = run_loop(MockClient(script=SCRIPTS["no_submit"]), [], registry(), ["submit_test"],
                      RunCtx(kind="probe", template="probe_user"), max_submit_retries=0)
    assert result.interventions["no_submit"] == 1
    assert result.interventions["loop_cap"] == 1
    assert result.interventions["fallback"] == 1


def test_truncated_thinking_retries_once() -> None:
    profile = Profile(name="p", transport="mock", family="f", reasoning_effort="high")
    result = run_loop(MockClient(profile, SCRIPTS["truncated"]), [], registry(), ["submit_test"],
                      RunCtx(kind="probe", template="probe_user"))
    assert result.interventions["truncated"] == 1
    assert result.stop_reason == "submitted"
    assert result.iterations == 2


def test_audit_accepts_invalid_submission_but_records_error() -> None:
    result = run_loop(MockClient(script=SCRIPTS["audit"]), [], registry(), ["submit_test"],
                      RunCtx(kind="probe", template="probe_user", audit=True))
    assert result.submitted and result.submitted["value"] == 9
    assert result.audit_errors == ["invalid value"]
    assert result.interventions["validation_fail"] == 1
    assert "error" not in result.events[0]["result"]


def test_mixed_regular_and_submit_tools() -> None:
    result = run_loop(MockClient(script=SCRIPTS["mixed"]), [], registry(), ["regular_test", "submit_test"],
                      RunCtx(kind="probe", template="probe_user"))
    assert result.stop_reason == "submitted"
    assert [event["name"] for event in result.events] == ["regular_test", "submit_test"]


def test_final_non_submit_round_disables_tools() -> None:
    class RecordingClient(MockClient):
        def __init__(self) -> None:
            super().__init__(script=SCRIPTS["regular_then_text"])
            self.choices: list = []

        def chat(self, *args: object, **kwargs: object):
            self.choices.append(kwargs.get("tool_choice"))
            return super().chat(*args, **kwargs)

    client = RecordingClient()
    result = run_loop(client, [], registry(), ["regular_test"], RunCtx(kind="probe", template="probe_user"),
                      max_iters=1, require_submit=False)
    assert client.choices == [None, "none"]
    assert result.final_text == "Finished"


def test_strict_template_variables() -> None:
    with pytest.raises(Exception, match="value"):
        render("probe_user")
