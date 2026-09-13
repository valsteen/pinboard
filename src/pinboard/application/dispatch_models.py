import hashlib
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

import msgspec

from pinboard.application.artifact_publication import (
    AcceptedArtifactPublication,
    ArtifactPublisher,
    ArtifactReader,
    publish_accepted_artifact,
)
from pinboard.application.artifacts import NewArtifact
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.errors import ChangedSurface, DecisionFailure, DecisionFailureCode, DecisionResult, FailureDetails
from pinboard.domain.identifiers import HostId

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]

type DispatchSchema = Literal["pinboard-dispatch/v2"]
type StableHostId = Annotated[
    HostId,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z"),
]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]


class DispatchRejectionCode(Enum):
    ACTION_INVALID = "action-invalid"
    ACTION_UNAVAILABLE = "action-unavailable"
    ATTEMPT_NOT_ACTIVE = "attempt-not-active"
    BRIEF_MISSING = "brief-missing"
    REVIEW_COLLISION = "review-collision"
    REVIEW_MISSING = "review-missing"
    STALE_ACTION = "stale-action"


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


def dispatch_environment_enc_hook(value: FreshContextRequired) -> bool:
    if isinstance(value, FreshContextRequired):
        return True
    raise TypeError(f"unsupported dispatch environment value: {value!r}")


def dispatch_environment_schema_hook(value_type: type) -> dict[str, bool | str]:
    if value_type is FreshContextRequired:
        return {"type": "boolean", "const": True}
    raise TypeError(f"unsupported dispatch environment type: {value_type!r}")


class DispatchEnvironment(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: DispatchSchema
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
    accepted_artifact_reference_id: int
    kind: Literal["evidence"]
    key: str
    revision: int
    selector: str
    sha256: str
    size_bytes: int
    accepted_revision: int
    artifact_created: bool
    ledger_changed: bool


class NativeLaunchEnvelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-native-agent-launch/v1"]
    runtime: Literal["native-subagent"]
    message: str


class PublishedAgentPrompt(str):
    reference: PromptReferenceView
    native_launch: NativeLaunchEnvelope
    changed_surfaces: tuple[ChangedSurface, ...]

    def __new__(
        cls,
        prompt: str,
        reference: PromptReferenceView,
        native_launch: NativeLaunchEnvelope,
        changed_surfaces: tuple[ChangedSurface, ...],
    ) -> Self:
        value = super().__new__(cls, prompt)
        value.reference = reference
        value.native_launch = native_launch
        value.changed_surfaces = changed_surfaces
        return value


def pinboard_launcher_command() -> tuple[str, ...]:
    """Use the installed console script without relying on the launched agent's PATH."""

    return (str(Path(sys.executable).with_name("pinboard")),)


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


def publish_agent_prompt(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    *,
    project_root: Path,
    prompt_role: Literal["worker", "reviewer"],
    attempt_id: str,
    prompt: str,
    accepted_at: datetime,
) -> DecisionResult[PublishedAgentPrompt]:
    content = prompt.encode()
    digest = hashlib.sha256(content).hexdigest()
    accepted = publish_accepted_artifact(
        store,
        artifacts,
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            f"{attempt_id}-{prompt_role}-prompt-{digest}",
            1,
            ".txt",
            content,
        ),
        accepted_at,
    )
    if isinstance(accepted, DecisionFailure):
        return accepted
    reference = _reference_view(accepted)
    changed_surfaces = (
        *((ChangedSurface.IMMUTABLE_ARTIFACT,) if accepted.artifact_created else ()),
        *((ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE, ChangedSurface.LEDGER) if accepted.ledger_changed else ()),
    )
    prompt_path = artifacts.work_root / reference.selector
    verification_command = shlex.join(
        (
            *pinboard_launcher_command(),
            "--project-root",
            str(project_root),
            "--work-root",
            str(artifacts.work_root),
            "artifact",
            "verify",
            "--artifact-ref-id",
            str(reference.accepted_artifact_reference_id),
            "--selector",
            reference.selector,
            "--sha256",
            reference.sha256,
            "--size-bytes",
            str(reference.size_bytes),
            "--json",
        )
    )
    message = (
        f"Use accepted artifact reference {reference.accepted_artifact_reference_id}, the immutable {prompt_role} "
        f"prompt at '{prompt_path}'. Before any acquisition, implementation, or review, run exactly: "
        f"{verification_command}. Require `pinboard-verified-artifact-reference/v1`, then read exactly "
        f"{reference.size_bytes} bytes from that path. Stop before acting if the accepted identity, selector, size, "
        "digest, verification result, or bytes differ. After verification, follow those exact bytes as the complete "
        "task prompt."
    )
    return PublishedAgentPrompt(
        prompt,
        reference,
        NativeLaunchEnvelope("pinboard-native-agent-launch/v1", "native-subagent", message),
        changed_surfaces,
    )
