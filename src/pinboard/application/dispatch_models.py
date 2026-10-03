import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Protocol, Self, assert_never

import msgspec

from pinboard.application.artifact_publication import (
    AcceptedArtifactPublication,
    ArtifactAcceptanceFailure,
    ArtifactPublisher,
    ArtifactReader,
    ArtifactWriteFailure,
    publish_accepted_artifact,
)
from pinboard.application.artifacts import NewArtifact
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    DescribedCode,
    FailureDetails,
)
from pinboard.domain.identifiers import HostId

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]

type DispatchSchema = Literal["pinboard-dispatch/v2"]
type NativeRuntime = Literal["codex", "claude-code"]
type StableHostId = Annotated[
    HostId,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z"),
]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]


class DispatchRejectionCode(DescribedCode):
    ACTION_INVALID = ("action-invalid", "The selected dispatch action receipt has an invalid shape or subject.")
    ACTION_UNAVAILABLE = ("action-unavailable", "The selected dispatch action is not legal for the current attempt.")
    ATTEMPT_NOT_ACTIVE = ("attempt-not-active", "The attempt is no longer active for worker dispatch.")
    BRIEF_MISSING = ("brief-missing", "The active attempt has no accepted brief for worker dispatch.")
    REVIEW_COLLISION = ("review-collision", "A different dispatch brief review is already bound to this attempt.")
    REVIEW_MISSING = ("review-missing", "The dispatch brief has no independent review record.")
    STALE_ACTION = ("stale-action", "The selected dispatch action receipt no longer matches current attempt facts.")


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    code: DispatchRejectionCode | DecisionFailureCode
    message: str
    details: FailureDetails | None


type DispatchResult[T] = T | DispatchFailure


class DispatchPermission(Enum):
    """Task-declared worker limits that Pinboard validates and forwards but does not grant or enforce."""

    REPOSITORY_READ = "repository-read"
    REPOSITORY_WRITE = "repository-write"
    NETWORK = "network"
    EXTERNAL_WRITE = "external-write"
    LIVE_APPLICATION = "live-application"


class FreshContextRequired:
    __slots__ = ()


FRESH_CONTEXT_REQUIRED: FreshContextRequired = FreshContextRequired()


def dispatch_environment_dec_hook(value_type: type, value: bool) -> FreshContextRequired:
    if value_type is FreshContextRequired and value is True:
        return FRESH_CONTEXT_REQUIRED
    raise ValueError("fresh_context must be true")


def dispatch_environment_schema_hook(value_type: type) -> dict[str, bool | str]:
    if value_type is FreshContextRequired:
        return {"type": "boolean", "const": True}
    raise TypeError(f"unsupported dispatch environment type: {value_type!r}")


class DispatchEnvironment(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: DispatchSchema
    runtime: NativeRuntime
    background: bool
    checkout: NonEmptyLine
    branch: NonEmptyLine
    starting_revision: NonEmptyLine
    host_id: StableHostId
    fresh_context: FreshContextRequired
    lease_ttl_seconds: PositiveInt
    permissions: tuple[DispatchPermission, ...]


class DispatchArtifactPort(ArtifactPublisher, ArtifactReader, Protocol):
    pass


class PromptReferenceView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-agent-prompt-reference/v1"]
    accepted_artifact_reference_id: PositiveInt
    kind: Literal["evidence"]
    key: Annotated[str, msgspec.Meta(min_length=1)]
    revision: PositiveInt
    selector: Annotated[str, msgspec.Meta(min_length=1)]
    sha256: Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
    size_bytes: PositiveInt
    accepted_revision: PositiveInt
    artifact_created: bool
    ledger_changed: bool


class CodexLaunchArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_name: Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9_]+\z")]
    message: Annotated[str, msgspec.Meta(min_length=1)]
    fork_turns: Literal["none"]


class ClaudeLaunchArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    description: NonEmptyLine
    prompt: Annotated[str, msgspec.Meta(min_length=1)]
    run_in_background: bool


class CodexNativeLaunchEnvelope(
    msgspec.Struct, tag="codex", tag_field="runtime", frozen=True, forbid_unknown_fields=True
):
    schema: Literal["pinboard-native-agent-launch/v2"]
    tool: Literal["spawn_agent"]
    background: bool
    arguments: CodexLaunchArguments


class ClaudeNativeLaunchEnvelope(
    msgspec.Struct, tag="claude-code", tag_field="runtime", frozen=True, forbid_unknown_fields=True
):
    schema: Literal["pinboard-native-agent-launch/v2"]
    tool: Literal["Agent"]
    background: bool
    arguments: ClaudeLaunchArguments


type NativeLaunchEnvelope = CodexNativeLaunchEnvelope | ClaudeNativeLaunchEnvelope


class PublishedAgentPrompt(str):
    reference: PromptReferenceView
    changed_surfaces: tuple[ChangedSurface, ...]

    def __new__(
        cls,
        prompt: str,
        reference: PromptReferenceView,
        changed_surfaces: tuple[ChangedSurface, ...],
    ) -> Self:
        value = super().__new__(cls, prompt)
        value.reference = reference
        value.changed_surfaces = changed_surfaces
        return value


def _reference_view(publication: AcceptedArtifactPublication) -> PromptReferenceView:
    reference = publication.reference
    return PromptReferenceView(
        "pinboard-agent-prompt-reference/v1",
        int(reference.artifact_ref_id),
        "evidence",
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
        reference.accepted_revision,
        publication.artifact_created,
        publication.ledger_changed,
    )


@dataclass(frozen=True, slots=True)
class WorkerPromptSubject:
    attempt_id: str


@dataclass(frozen=True, slots=True)
class ReviewerPromptSubject:
    """The exact review subject a reviewer prompt commissions; record-ready recomputes it from its digests."""

    attempt_id: str
    candidate: str
    candidate_snapshot_sha256: str
    accepted_brief_sha256: str
    result_sha256: str


type AgentPromptSubject = WorkerPromptSubject | ReviewerPromptSubject


def agent_prompt_key(subject: AgentPromptSubject, prompt_sha256: str) -> str:
    """Name one accepted prompt artifact; a reviewer key binds its prompt to the commissioned review subject."""

    match subject:
        case WorkerPromptSubject(attempt_id=attempt_id):
            return f"{attempt_id}-worker-prompt-{prompt_sha256}"
        case ReviewerPromptSubject():
            identity = msgspec.json.encode(
                (
                    subject.attempt_id,
                    subject.candidate,
                    subject.candidate_snapshot_sha256,
                    subject.accepted_brief_sha256,
                    subject.result_sha256,
                    prompt_sha256,
                )
            )
            return f"{subject.attempt_id}-reviewer-prompt-{hashlib.sha256(identity).hexdigest()}"
        case _ as unreachable:
            assert_never(unreachable)


def publish_agent_prompt(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    *,
    subject: AgentPromptSubject,
    prompt: str,
    accepted_at: datetime,
) -> DecisionResult[PublishedAgentPrompt | ArtifactAcceptanceFailure | ArtifactWriteFailure]:
    content = prompt.encode()
    digest = hashlib.sha256(content).hexdigest()
    accepted = publish_accepted_artifact(
        store,
        artifacts,
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            agent_prompt_key(subject, digest),
            1,
            ".txt",
            content,
        ),
        accepted_at,
    )
    if isinstance(accepted, (DecisionFailure, ArtifactAcceptanceFailure, ArtifactWriteFailure)):
        return accepted
    reference = _reference_view(accepted)
    changed_surfaces = (
        *((ChangedSurface.IMMUTABLE_ARTIFACT,) if accepted.artifact_created else ()),
        *((ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE, ChangedSurface.LEDGER) if accepted.ledger_changed else ()),
    )
    return PublishedAgentPrompt(
        prompt,
        reference,
        changed_surfaces,
    )
