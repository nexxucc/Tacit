from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(StrEnum):
    ADMIN = "workspace_admin"
    MAINTAINER = "maintainer"
    LEARNER = "learner"


class Outcome(StrEnum):
    PERMITTED = "permitted"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


Scalar = StrictStr | StrictBool | StrictInt


class Predicate(Contract):
    field: str = Field(min_length=1, max_length=100)
    equals: Scalar


class Rule(Contract):
    when: tuple[Predicate, ...] = Field(min_length=1, max_length=16)
    outcome: Literal["permitted", "forbidden"]


class Evidence(Contract):
    decision_id: str
    revision_id: str
    segment_id: str


class Decision(Contract):
    id: str
    revision_id: str
    statement: str
    files: tuple[str, ...]
    source_commit: str
    evidence: tuple[Evidence, ...]
    rule: Rule | None = None


class Usage(Contract):
    stt_calls: int = 0
    tts_calls: int = 0
    reasoning_calls: int = 0


class ContextRequest(Contract):
    snapshot_id: str = Field(min_length=1)
    source_commit: str = Field(min_length=1)
    files: tuple[str, ...] = Field(default=(), max_length=50)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: int = Field(default=0, ge=0)


class QuestionRequest(ContextRequest):
    question: str = Field(min_length=1, max_length=2000)


class ScenarioRequest(Contract):
    snapshot_id: str
    source_commit: str
    facts: dict[str, Scalar] = Field(max_length=32)


class ContextResponse(Contract):
    workspace_id: str
    repository_id: str
    snapshot_id: str
    source_commit: str
    applicability: Literal["confirmed", "unknown"]
    status: Literal["answered", "unknown", "conflict"]
    decisions: tuple[Decision, ...] = ()
    answer: str | None = None
    answer_kind: Literal["confirmed_statements", "none"] = "none"
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    next_cursor: int | None = None
    usage: Usage = Field(default_factory=Usage)


class EvaluationResponse(Contract):
    repository_id: str
    snapshot_id: str
    decision: Outcome
    rule_references: tuple[str, ...]
    evaluator_version: str = "typed-equality-v1"
    usage: Usage = Field(default_factory=Usage)


class ErrorResponse(Contract):
    code: str
    message: str
    request_id: str
    retryable: bool = False
