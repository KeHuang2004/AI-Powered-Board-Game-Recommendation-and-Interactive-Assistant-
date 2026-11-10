"""Deterministic lesson progress and mutable table state."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

from .plan import LessonPlan, Step

Phase = Literal["setup", "first_round", "ending", "qa"]


class DiceRoll(BaseModel):
    spec: str
    values: list[int]
    ts: str


class SessionState(BaseModel):
    _initial_player_count: int = PrivateAttr(default=0)
    _plan: LessonPlan | None = PrivateAttr(default=None)

    mode: Literal["t0", "t1"]
    phase: Phase
    index: int = 0
    total_steps: int = 0
    players: list[str]
    scores: dict[str, int]
    dice_log: list[DiceRoll] = Field(default_factory=list)
    lang: Literal["zh", "en"]

    def steps(self) -> list[tuple[Phase, Step]]:
        if self._plan is None:
            return []
        return [(phase, step) for phase in ("setup", "first_round", "ending")
                for step in getattr(self._plan, phase)]

    def current(self) -> dict | None:
        steps = self.steps()
        if self.mode == "t0" or self.index >= len(steps):
            return None
        phase, step = steps[self.index]
        return {"phase": phase, "index": self.index, "text": step.text, "src": step.src}

    def advance(self) -> None:
        self.index = min(self.index + 1, self.total_steps)
        current = self.current()
        self.phase = current["phase"] if current else "qa"

    def jump(self, phase: Phase) -> None:
        if phase == "qa":
            self.index = self.total_steps
            self.phase = "qa"
            return
        phases = ("setup", "first_round", "ending")
        allowed = phases[phases.index(phase):]
        self.index = next((index for index, (name, _) in enumerate(self.steps()) if name in allowed), self.total_steps)
        current = self.current()
        self.phase = current["phase"] if current else "qa"
