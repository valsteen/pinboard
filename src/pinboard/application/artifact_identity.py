"""Typed lifecycle addresses and explicit, possibly overlapping attempt membership."""

from enum import Enum
from hashlib import sha256
from typing import Annotated, assert_never

import msgspec

from pinboard.domain.identifiers import AttemptId, CheckpointId

type AttemptIdentity = Annotated[AttemptId, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type CheckpointIdentity = Annotated[CheckpointId, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]


class CheckpointRole(Enum):
    CANDIDATE = "candidate"
    RESULT = "result"
    REVIEW = "review"
    PACKAGE = "package"


class CompletionRole(Enum):
    RESULT = "result"
    REVIEW = "review"
    PACKAGE = "package"


class AttemptRole(Enum):
    SOURCE = "source"
    ARCHIVE = "archive"


class AttemptMembership(msgspec.Struct, tag="attempt", tag_field="scope", frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptIdentity
    role: AttemptRole


class CheckpointAddress(msgspec.Struct, tag="checkpoint", tag_field="scope", frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptIdentity
    checkpoint_id: CheckpointIdentity
    role: CheckpointRole
    revision: Annotated[int, msgspec.Meta(ge=1)]


class CompletionAddress(msgspec.Struct, tag="completion", tag_field="scope", frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptIdentity
    role: CompletionRole
    revision: Annotated[int, msgspec.Meta(ge=1)]


type Ownership = AttemptMembership | CheckpointAddress | CompletionAddress
type LifecycleAddress = CheckpointAddress | CompletionAddress


def artifact_key(address: LifecycleAddress) -> str:
    """Bound free components independently of fixed system scope and role positions."""
    match address:
        case CheckpointAddress(attempt_id=attempt_id, checkpoint_id=checkpoint_id, role=role):
            digest = sha256(msgspec.json.encode((str(attempt_id), str(checkpoint_id)))).hexdigest()
            return f"checkpoint-{role.value}-{digest}"
        case CompletionAddress(attempt_id=attempt_id, role=role):
            digest = sha256(msgspec.json.encode((str(attempt_id),))).hexdigest()
            return f"completion-{role.value}-{digest}"
        case _ as unreachable:
            assert_never(unreachable)


def canonical_ownership(value: Ownership) -> bytes:
    return msgspec.json.encode(value, order="sorted")
