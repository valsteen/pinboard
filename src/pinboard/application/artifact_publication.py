from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, override

from pinboard.application import artifact_identity, query_models, stored_state
from pinboard.application.artifacts import (
    ArtifactPublication,
    BriefArtifactRef,
    CurrentAttemptWorkBriefIdentity,
    NewArtifact,
    WorkBriefIdentity,
)
from pinboard.application.ports import StaleWriteCode, WorkStore, WorkStoreError
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    DescribedCode,
    EffectDisposition,
    FailureAction,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)


class ArtifactPublisher(Protocol):
    @property
    def work_root(self) -> Path: ...

    def publish(self, artifact: NewArtifact) -> ArtifactPublication | ArtifactWriteFailure: ...


class ArtifactReader(Protocol):
    def read(
        self, reference: stored_state.ArtifactReference | stored_state.EvidenceArtifactReference | BriefArtifactRef
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class AcceptedArtifactPublication:
    reference: stored_state.ArtifactReference
    artifact_created: bool
    ledger_changed: bool


@dataclass(frozen=True, slots=True)
class AcceptedEvidencePublication:
    reference: stored_state.EvidenceArtifactReference
    artifact_created: bool
    ledger_changed: bool


# The durable surfaces an artifact publication can change; no other surface reaches a publication result.
type PublicationSurface = Literal[
    ChangedSurface.IMMUTABLE_ARTIFACT, ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE, ChangedSurface.LEDGER
]


@dataclass(frozen=True, slots=True)
class PublishedFailureDetails:
    """Failure evidence after publication changed durable surfaces, so the caller must not retry or replay it."""

    observed: tuple[FailureFact, ...]
    mismatches: tuple[FailureMismatch, ...]
    changed_surfaces: tuple[PublicationSurface, ...]
    alternatives: tuple[FailureAction, ...]


@dataclass(frozen=True, slots=True)
class UnpublishedFailureDetails:
    """Failure evidence before publication changed anything; only a retryable store failure may repeat."""

    observed: tuple[FailureFact, ...]
    mismatches: tuple[FailureMismatch, ...]
    retry: Literal[RetryDisposition.RETRY_SAME_INPUT, RetryDisposition.DO_NOT_RETRY]


@dataclass(frozen=True, slots=True)
class ArtifactAcceptanceFailure:
    selector: str
    cause: WorkStoreError
    details: PublishedFailureDetails | UnpublishedFailureDetails


@dataclass(frozen=True, slots=True)
class ArtifactWriteCause:
    """The publisher's write failure: its stable code and the diagnostic it reported, which `str` presents."""

    code: DescribedCode
    diagnostic: str

    @override
    def __str__(self) -> str:
        return self.diagnostic


@dataclass(frozen=True, slots=True)
class ArtifactWriteFailure:
    selector: str
    cause: ArtifactWriteCause
    details: PublishedFailureDetails


@dataclass(frozen=True, slots=True)
class PublishedArtifactRejected:
    """The artifact was published immutably, but its accepted reference lost a stale compare-and-set write."""

    message: str
    details: PublishedFailureDetails


def accepted_publication_surfaces(
    publication: AcceptedArtifactPublication | AcceptedEvidencePublication,
) -> tuple[PublicationSurface, ...]:
    return (
        *((ChangedSurface.IMMUTABLE_ARTIFACT,) if publication.artifact_created else ()),
        *((ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE, ChangedSurface.LEDGER) if publication.ledger_changed else ()),
    )


def publish_accepted_artifact(
    store: WorkStore,
    publisher: ArtifactPublisher,
    artifact: NewArtifact,
    accepted_at: datetime,
    ownership: artifact_identity.Ownership | None,
) -> DecisionResult[
    AcceptedArtifactPublication | ArtifactAcceptanceFailure | ArtifactWriteFailure | PublishedArtifactRejected,
    StaleWriteCode,
]:
    """Publish immutable bytes, then accept their verified reference in SQLite.

    A newly published immutable artifact cannot be rolled back if SQLite acceptance fails, so a failure after it
    carries published failure details; a refused acceptance without a new artifact remains an unchanged rejection.
    A SQLite invariant violation during acceptance is corrupt storage and propagates from the store's effect boundary.
    """

    publication = publisher.publish(artifact)
    if isinstance(publication, ArtifactWriteFailure):
        return publication
    published_reference = publication.reference
    artifact_created = publication.created
    selector_facts = (FailureFact("published_artifact_selector", published_reference.selector),)
    try:
        accepted = store.accept_artifact_reference(publisher.work_root, published_reference, accepted_at, ownership)
    except WorkStoreError as error:
        if error.invariant_violation:
            raise
        return ArtifactAcceptanceFailure(
            published_reference.selector,
            error,
            PublishedFailureDetails(selector_facts, (), (ChangedSurface.IMMUTABLE_ARTIFACT,), ())
            if artifact_created
            else UnpublishedFailureDetails(
                selector_facts,
                (),
                RetryDisposition.RETRY_SAME_INPUT if error.retryable else RetryDisposition.DO_NOT_RETRY,
            ),
        )
    if isinstance(accepted, DecisionFailure):
        if artifact_created:
            prior = accepted.details
            return PublishedArtifactRejected(
                accepted.message,
                PublishedFailureDetails(
                    (*selector_facts, *(() if prior is None else prior.observed)),
                    () if prior is None else prior.mismatches,
                    (ChangedSurface.IMMUTABLE_ARTIFACT,),
                    (),
                ),
            )
        return accepted
    return AcceptedArtifactPublication(accepted.reference, artifact_created, accepted.ledger_changed)


def publish_accepted_evidence(
    store: WorkStore,
    publisher: ArtifactPublisher,
    key: str,
    revision: int,
    suffix: str,
    content: bytes,
    accepted_at: datetime,
    ownership: artifact_identity.Ownership | None,
) -> DecisionResult[
    AcceptedEvidencePublication | ArtifactAcceptanceFailure | ArtifactWriteFailure | PublishedArtifactRejected,
    StaleWriteCode,
]:
    """Publish and accept one evidence artifact, keeping the evidence kind its acceptance stored."""
    kind = work_models.ArtifactKind.EVIDENCE
    accepted = publish_accepted_artifact(
        store, publisher, NewArtifact(kind, key, revision, suffix, content), accepted_at, ownership
    )
    if not isinstance(accepted, AcceptedArtifactPublication):
        return accepted
    # Acceptance stores the published kind, or returns the row already accepted under that kind.
    return AcceptedEvidencePublication(
        stored_state.evidence_reference(accepted.reference, kind),
        accepted.artifact_created,
        accepted.ledger_changed,
    )


def validate_transition_work_brief(  # noqa: C901, PLR0912
    facts: query_models.DecisionFacts,
    command: decision_models.TransitionCommand,
    identity: WorkBriefIdentity | None,
) -> (
    DecisionFailure[Literal[DecisionFailureCode.ITEM_DEFINITION_INVALID, DecisionFailureCode.TRANSITION_INPUT_INVALID]]
    | None
):
    """Validate a selected brief identity against exact locked decision facts."""

    snapshot = facts.snapshot

    if isinstance(command, (decision_models.AcceptCheckpointCommand, decision_models.CoveredCompleteCommand)):
        expected_disposition = (
            "continue" if isinstance(command, decision_models.AcceptCheckpointCommand) else "terminal"
        )
        attempt_id = command.action.capability.subject
        attempt = next((value for value in snapshot.attempts if value.attempt == attempt_id), None)
        mismatches = (
            (FailureMismatch("accepted_brief_identity", "current accepted brief", None),)
            if not isinstance(identity, CurrentAttemptWorkBriefIdentity)
            else tuple(
                mismatch
                for mismatch in (
                    FailureMismatch(
                        "brief_artifact_ref_id",
                        None if attempt is None else attempt.brief_artifact_ref_id,
                        identity.artifact_ref_id,
                    ),
                    FailureMismatch("attempt_id", str(attempt_id), identity.attempt_id),
                    FailureMismatch(
                        "item_id",
                        None if attempt is None else str(attempt.work_item_id),
                        identity.item_id,
                    ),
                    FailureMismatch(
                        "accepted_scope_revision",
                        None if attempt is None else attempt.accepted_scope_revision,
                        identity.accepted_scope_revision,
                    ),
                    FailureMismatch(
                        "accepted_scope_digest",
                        None if attempt is None else attempt.accepted_scope_digest,
                        identity.accepted_scope_digest,
                    ),
                    FailureMismatch("checkpoint_disposition", expected_disposition, identity.disposition),
                )
                if mismatch.expected != mismatch.observed
            )
        )
        if mismatches:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "The transition requires the same current accepted brief and matching checkpoint disposition under lock.",
                FailureDetails(
                    observed=(FailureFact("attempt_id", str(attempt_id)),),
                    mismatches=mismatches,
                    retry=RetryDisposition.REFRESH_ACTION,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        return None

    match command:
        case decision_models.ActivateCommand(action=action, value=value):
            attempt_id = str(value.attempt)
            item_id = str(action.capability.subject)
            branch = value.branch
            base_revision = value.base_revision
        case decision_models.ResumeCommand(action=action, value=value) if value.brief_artifact_ref_id is not None:
            item_id = str(action.capability.subject)
            attempt = next(
                (candidate for candidate in facts.attempt_lineage if str(candidate.work_item_id) == item_id), None
            )
            if attempt is None:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Resuming with a revised brief requires an existing attempt.",
                    None,
                )
            attempt_id = str(attempt.attempt_id)
            branch = attempt.branch
            base_revision = attempt.base_revision
        case decision_models.RebindAttemptCommand(action=action, value=value):
            attempt_id = str(action.capability.subject)
            attempt = next(
                (candidate for candidate in facts.attempt_lineage if candidate.attempt_id == action.capability.subject),
                None,
            )
            if attempt is None:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Rebinding requires an existing attempt.",
                    None,
                )
            item_id = str(attempt.work_item_id)
            branch = value.branch
            base_revision = value.base_revision
        case _:
            return None
    match command:
        case (
            decision_models.ActivateCommand(value=value)
            | decision_models.ResumeCommand(value=value)
            | decision_models.RebindAttemptCommand(value=value)
        ) if value.brief_artifact_ref_id is not None:
            brief_artifact_ref_id = value.brief_artifact_ref_id
        case _:
            return None
    reference = next(
        (candidate for candidate in snapshot.artifacts if candidate.artifact_ref_id == brief_artifact_ref_id),
        None,
    )
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return None
    if identity is None:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact identity was not decoded from the accepted reference.",
            FailureDetails(
                observed=(FailureFact("brief_identity", None),),
                mismatches=(FailureMismatch("brief_identity", "decoded", None),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    item = next((candidate for candidate in snapshot.items if str(candidate.work_item_id) == item_id), None)
    if item is None:
        return None
    definition = snapshot.definition(item.work_item_id)
    if definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "The selected work item has no current definition.",
            None,
        )
    if isinstance(command, decision_models.ActivateCommand):
        preparation = command.action.capability.preparation_authority
        preparation_mismatches = (
            (FailureMismatch("preparation_item", str(item.work_item_id), None),)
            if preparation is None
            else tuple(
                mismatch
                for mismatch in (
                    FailureMismatch("preparation_item", str(item.work_item_id), str(preparation.work_item_id)),
                    FailureMismatch(
                        "preparation_definition_revision",
                        definition.revision,
                        preparation.definition_revision,
                    ),
                    FailureMismatch(
                        "preparation_definition_digest",
                        definition.digest,
                        preparation.definition_digest,
                    ),
                )
                if mismatch.expected != mismatch.observed
            )
        )
        if preparation_mismatches:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "The selected work brief does not match the live preparation pin.",
                FailureDetails(
                    observed=(),
                    mismatches=preparation_mismatches,
                    retry=RetryDisposition.REFRESH_ACTION,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
    expected = WorkBriefIdentity(
        attempt_id,
        item_id,
        branch,
        base_revision,
        definition.revision,
        definition.digest,
    )
    identity_mismatches = tuple(
        mismatch
        for mismatch in (
            FailureMismatch("attempt_id", expected.attempt_id, identity.attempt_id),
            FailureMismatch("item_id", expected.item_id, identity.item_id),
            FailureMismatch("branch", expected.branch, identity.branch),
            FailureMismatch("base_revision", expected.base_revision, identity.base_revision),
            FailureMismatch(
                "accepted_scope_revision",
                expected.accepted_scope_revision,
                identity.accepted_scope_revision,
            ),
            FailureMismatch(
                "accepted_scope_digest",
                expected.accepted_scope_digest,
                identity.accepted_scope_digest,
            ),
        )
        if mismatch.expected != mismatch.observed
    )
    if identity_mismatches:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact does not match the attempt, item, branch, base revision, and accepted scope.",
            FailureDetails(
                observed=(),
                mismatches=identity_mismatches,
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return None
