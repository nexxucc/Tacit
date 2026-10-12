from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


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
    kind: Literal["transcript", "text"] = "transcript"


class SourceBinding(Contract):
    path: str
    blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class Decision(Contract):
    id: str
    revision_id: str
    statement: str
    files: tuple[str, ...]
    source_commit: str
    sources: tuple[SourceBinding, ...] = ()
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


class DevicePoll(Contract):
    pairing_token: str = Field(min_length=32, max_length=256)


class RoleChange(Contract):
    user_id: str = Field(min_length=1, max_length=64)
    role: Role
    repository_id: str | None = Field(default=None, min_length=1, max_length=64)


class InvitationRequest(Contract):
    github_id: str = Field(pattern=r"^[0-9]+$", max_length=32)
    repository_ids: tuple[str, ...] = Field(min_length=1, max_length=100)


class AgentTokenRequest(Contract):
    repository_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    expires_in: int = Field(default=3600, ge=60, le=28800)


class DraftRequest(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=8000)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[str, ...] = Field(min_length=1, max_length=20)
    expected_snapshot_id: str | None
    decision_id: str | None = Field(default=None, min_length=1, max_length=64)
    rule: Rule | None = None

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: tuple[str, ...]) -> tuple[str, ...]:
        for path in files:
            if (
                not path
                or len(path) > 512
                or path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
            ):
                raise ValueError("Files must be canonical repository-relative paths")
        if len(set(files)) != len(files):
            raise ValueError("Duplicate file bindings")
        return tuple(sorted(files))

    @field_validator("statement")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Statement must not be blank")
        return value


class DraftView(Contract):
    id: str
    workspace_id: str
    repository_id: str
    author_id: str
    decision_id: str
    expected_snapshot_id: str | None
    statement: str
    source_commit: str
    sources: tuple[SourceBinding, ...]
    rule: Rule | None = None
    previous: Decision | None = None
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready_for_review", "published"] = "ready_for_review"
    published_snapshot_id: str | None = None


class PublicationRequest(Contract):
    draft_id: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=128)
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_snapshot_id: str | None


class PublicationResponse(Contract):
    repository_id: str
    decision_id: str
    revision_id: str
    snapshot_id: str
    source_commit: str
    manifest_digest: str
    parent_snapshot_id: str | None
