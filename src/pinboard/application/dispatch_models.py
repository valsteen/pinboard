from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Protocol

import msgspec

from pinboard.application.artifact_publication import ArtifactPublisher, ArtifactReader
from pinboard.domain.errors import DecisionFailureCode, FailureDetails

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]

type DispatchSchema = Literal["pinboard-dispatch/v1"]


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


class DispatchEnvironment(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: DispatchSchema
    checkout: NonEmptyLine
    branch: NonEmptyLine
    starting_revision: NonEmptyLine
    permissions: tuple[DispatchPermission, ...]


class DispatchArtifactPort(ArtifactPublisher, ArtifactReader, Protocol):
    pass
