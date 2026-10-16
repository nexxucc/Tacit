from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


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


class Expression(Contract):
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "all", "any", "not"]
    field: str | None = Field(default=None, min_length=1, max_length=100)
    value: Scalar | None = None
    children: tuple["Expression", ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def shape(self) -> "Expression":
        def size(node: "Expression", depth: int = 0) -> int:
            if depth > 6:
                raise ValueError("Expression depth exceeds six")
            return 1 + sum(size(c, depth + 1) for c in node.children)

        if size(self) > 64:
            raise ValueError("Expression exceeds 64 nodes")
        if self.op in {"all", "any", "not"}:
            if (
                self.field is not None
                or self.value is not None
                or not self.children
                or (self.op == "not" and len(self.children) != 1)
            ):
                raise ValueError("Invalid boolean expression")
        elif self.field is None or self.value is None or self.children:
            raise ValueError("Comparison requires field and value")
        elif self.op in {"lt", "le", "gt", "ge"} and type(self.value) is not int:
            raise ValueError("Ordered comparisons require integers")
        return self


class Rule(Contract):
    when: tuple[Predicate, ...] = Field(default=(), max_length=16)
    condition: Expression | None = None
    action: Literal["compute", "upload", "publish_result", "retry"] | None = None
    domain: Literal["scheduler-v1"] | None = None
    outcome: Literal["permitted", "forbidden"]

    @model_validator(mode="after")
    def one_condition(self) -> "Rule":
        if bool(self.when) == (self.condition is not None):
            raise ValueError("Provide equality predicates or one condition expression")
        if self.domain is not None:
            allowed = {
                "worker": (str, ("worker-1", "worker-2")),
                "attempt": (int, (0, 1)),
                **{
                    k: (bool, (False, True))
                    for k in ("lease_expired", "cancelled", "completed", "owns_attempt")
                },
            }

            def check(field: str, value: Scalar) -> None:
                if (
                    field not in allowed
                    or type(value) is not allowed[field][0]
                    or value not in allowed[field][1]
                ):
                    raise ValueError("Predicate is outside scheduler-v1")

            for predicate in self.when:
                check(predicate.field, predicate.equals)

            def walk(node: Expression) -> None:
                if node.field is not None and node.value is not None:
                    check(node.field, node.value)
                for child in node.children:
                    walk(child)

            if self.condition:
                walk(self.condition)
            if self.action is None:
                raise ValueError("Scheduler rules require an action")
        return self


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
    grounding: dict[str, object] | None = None
    evidence: tuple[Evidence, ...]
    rule: Rule | None = None


class Usage(Contract):
    stt_calls: int = 0
    tts_calls: int = 0
    reasoning_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class ContextRequest(Contract):
    snapshot_id: str = Field(min_length=1)
    source_commit: str = Field(min_length=1)
    files: tuple[str, ...] = Field(default=(), max_length=50)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: int = Field(default=0, ge=0)


class QuestionRequest(ContextRequest):
    question: str = Field(min_length=1, max_length=2000)
    reason: bool = False
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def reason_operation(self) -> "QuestionRequest":
        if self.reason and self.operation_id is None:
            raise ValueError("Reasoned questions require an operation ID")
        return self


class ScenarioRequest(Contract):
    snapshot_id: str
    source_commit: str
    facts: dict[str, Scalar] = Field(max_length=32)
    action: Literal["compute", "upload", "publish_result", "retry"] | None = None


class ContextResponse(Contract):
    workspace_id: str
    repository_id: str
    snapshot_id: str
    source_commit: str
    applicability: Literal["confirmed", "unknown"]
    status: Literal["answered", "unknown", "conflict"]
    decisions: tuple[Decision, ...] = ()
    answer: str | None = None
    answer_kind: Literal["confirmed_statements", "cited_reasoning", "none"] = "none"
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


class VoiceEvidence(Contract):
    original_transcript: str = Field(min_length=1, max_length=120000)
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audio_format: Literal["wav", "pcm_s16le"] = "wav"
    language: str = Field(min_length=2, max_length=32)
    duration_seconds: float = Field(gt=0, le=900)
    operation_id: str | None = None
    session_id: str | None = None
    segments: tuple[dict[str, StrictStr | StrictInt], ...] = ()
    original_draft_id: str | None = None
    corrected: bool = False
    capture_complete: bool = True
    transcription_complete: bool = True


class DraftRequest(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=8000)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[str, ...] = Field(min_length=1, max_length=20)
    expected_snapshot_id: str | None
    decision_id: str | None = Field(default=None, min_length=1, max_length=64)
    rule: Rule | None = None
    transcript_draft_id: str | None = Field(default=None, min_length=1, max_length=64)
    transcript_operation_id: str | None = Field(default=None, min_length=1, max_length=128)

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


class VoiceRequest(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    audio_base64: str = Field(min_length=1, max_length=1282000)
    language: Literal[
        "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN"
    ] = "en-IN"
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[str, ...] = Field(min_length=1, max_length=20)
    expected_snapshot_id: str | None

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: tuple[str, ...]) -> tuple[str, ...]:
        return DraftRequest.valid_files(files)


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
    voice: VoiceEvidence | None = None
    grounding: dict[str, object] | None = None
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


class VoiceSessionRequest(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    language: Literal[
        "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN"
    ] = "en-IN"
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[str, ...] = Field(min_length=1, max_length=20)
    expected_snapshot_id: str | None

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: tuple[str, ...]) -> tuple[str, ...]:
        return DraftRequest.valid_files(files)


class VoiceFinishRequest(Contract):
    capture_complete: bool = True
    chunks: int = Field(ge=1, le=900)
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VoiceSessionView(Contract):
    id: str
    operation_id: str
    repository_id: str
    status: Literal[
        "uploading",
        "ready",
        "processing",
        "completed",
        "uncertain",
        "needs_correction",
        "cancelled",
        "expired",
    ]
    chunks: int
    pcm_bytes: int
    capture_complete: bool = False
    audio_sha256: str | None = None
    expires_at: str
    draft: DraftView | None = None


class Selection(Contract):
    path: str = Field(min_length=1, max_length=512)
    symbol: str | None = Field(default=None, max_length=200)
    start_line: int = Field(default=1, ge=1, le=100000)
    end_line: int = Field(default=200, ge=1, le=100000)
    dirty_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    captured_at: str = Field(max_length=64)

    @model_validator(mode="after")
    def bounded(self) -> "Selection":
        DraftRequest.valid_files((self.path,))
        if self.end_line < self.start_line or self.end_line - self.start_line >= 400:
            raise ValueError("Select at most 400 source lines")
        from datetime import datetime

        if datetime.fromisoformat(self.captured_at).tzinfo is None:
            raise ValueError("Context timestamp must include timezone")
        return self


class GroundingCitation(Contract):
    path: str
    blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class Candidate(Contract):
    id: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=8000)
    transcript_quote: str = Field(min_length=1, max_length=4000)
    citations: tuple[GroundingCitation, ...] = Field(min_length=1, max_length=20)
    rule: Rule | None = None
    assumptions: tuple[str, ...] = Field(default=(), max_length=12)
    questions: tuple[str, ...] = Field(default=(), max_length=12)


class CandidateSet(Contract):
    candidates: tuple[Candidate, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique(self) -> "CandidateSet":
        if len({c.id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate IDs must be unique")
        return self


class TeachingRequest(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[str, ...] = Field(min_length=1, max_length=20)
    expected_snapshot_id: str | None
    transcript_operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    statement: str | None = Field(default=None, min_length=1, max_length=120000)
    selections: tuple[Selection, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def input_binding(self) -> "TeachingRequest":
        DraftRequest.valid_files(self.files)
        if (self.statement is None) == (self.transcript_operation_id is None):
            raise ValueError("Choose text or original transcript input")
        if any(selection.path not in self.files for selection in self.selections):
            raise ValueError("Selections must belong to the bound files")
        return self


class ClarificationAnswer(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    candidate_id: str = Field(min_length=1, max_length=64)
    answers: dict[str, str] = Field(default_factory=dict, max_length=32)
    statement: str | None = Field(default=None, min_length=1, max_length=8000)
    rule: Rule | None = None
    replace_rule: bool = False
    accept_assumptions: bool = False


class TeachingView(Contract):
    id: str
    repository_id: str
    author_id: str
    version: int
    status: Literal[
        "processing", "uncertain", "needs_clarification", "ready_for_review", "drafted", "cancelled"
    ]
    candidates: tuple[Candidate, ...] = ()
    baseline: tuple[Decision, ...] = ()
    selected_candidate_id: str | None = None
    scenarios: tuple[dict[str, object], ...] = ()
    questions: dict[str, str] = Field(default_factory=dict)
    answers: dict[str, str] = Field(default_factory=dict)
    source: TeachingRequest
    usage: Usage = Field(default_factory=Usage)
    draft: DraftView | None = None


class PrepareDraft(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    decision_id: str | None = Field(default=None, max_length=64)


class CitedAnswer(Contract):
    answer: str = Field(min_length=1, max_length=8000)
    revision_ids: tuple[str, ...] = Field(min_length=1, max_length=100)


class SpeakRequest(Contract):
    operation_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(min_length=1, max_length=100)
    language: str = Field(default="en-IN", max_length=32)
