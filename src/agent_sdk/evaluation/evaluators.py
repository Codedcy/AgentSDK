from __future__ import annotations

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from .models import (
    EvaluationDecision,
    EvaluationSubject,
    EvaluationVerdict,
)


class Evaluator(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def method(self) -> str: ...

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision: ...


class ExactOutputEvaluator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ClassVar[str] = "exact_output"
    version: ClassVar[str] = "1"
    method: ClassVar[str] = "deterministic_exact_match"

    expected: str

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        matched = subject.snapshot.output_text == self.expected
        terminal = next(
            (
                observed.event.event_id
                for observed in reversed(subject.timeline.events)
                if observed.event.type in {"run.completed", "run.failed"}
            ),
            None,
        )
        if terminal is None:
            raise ValueError("terminal Run evidence is missing")
        return EvaluationDecision(
            verdict=(EvaluationVerdict.PASS if matched else EvaluationVerdict.FAIL),
            metrics={"exact_match": 1.0 if matched else 0.0},
            reason="output exactly matched" if matched else "output did not exactly match",
            confidence=1.0,
            evidence_event_ids=(terminal,),
        )
