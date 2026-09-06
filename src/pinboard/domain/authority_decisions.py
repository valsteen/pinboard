from dataclasses import replace
from datetime import datetime
from typing import assert_never

from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId, ItemId
from pinboard.domain.ledger import LedgerSnapshot


def _attempt_token(value: authority_models.AttemptLeaseAuthority) -> work_models.CommandAttemptAuthority:
    return work_models.CommandAttemptAuthority(
        host_epoch=value.host_epoch,
        item=value.item,
        item_subject_revision="",
        attempt=value.attempt,
        attempt_subject_revision="",
        task_id=value.task_id,
        host_id=value.host_id,
        lease_id=value.lease_id,
        generation=value.generation,
        expires_at=value.expires_at,
    )


def _same_attempt_token(
    retained: authority_models.AttemptLeaseAuthority, supplied: work_models.CommandAttemptAuthority
) -> bool:
    token = _attempt_token(retained)
    return (
        token.host_epoch,
        token.item,
        token.attempt,
        token.task_id,
        token.host_id,
        token.lease_id,
        token.generation,
        token.expires_at,
    ) == (
        supplied.host_epoch,
        supplied.item,
        supplied.attempt,
        supplied.task_id,
        supplied.host_id,
        supplied.lease_id,
        supplied.generation,
        supplied.expires_at,
    )


def decide_attempt_authority(  # noqa: C901, PLR0912
    retained: authority_models.AttemptLeaseAuthority | None,
    counter: int,
    operation: authority_models.AttemptAuthorityOperation,
    *,
    live_attempt: tuple[AttemptId, ItemId] | None = None,
    transferable_attempt: tuple[AttemptId, ItemId] | None = None,
    project_host_epoch: int | None = None,
) -> DecisionResult[authority_models.AttemptAuthorityDecision]:
    match operation:
        case authority_models.AcquireInitialAttemptAuthority(
            host_epoch=host_epoch,
            attempt=attempt,
            item=item,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if live_attempt != (attempt, item) or project_host_epoch != host_epoch:
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "Initial attempt authority requires the exact live attempt and project host epoch.",
                    None,
                )
            if counter != 0 or (
                retained is not None
                and (retained.generation != 0 or retained.state != authority_models.AttemptLeaseStatus.RELEASED)
            ):
                return DecisionFailure(
                    DecisionFailureCode.LEASE_FENCED, "Initial attempt authority is already claimed.", None
                )
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Attempt authority requires a positive bounded interval.",
                    None,
                )
            proposed_replacement = authority_models.AttemptLeaseAuthority(
                host_epoch=host_epoch,
                attempt=attempt,
                item=item,
                task_id=task_id,
                host_id=host_id,
                lease_id=lease_id,
                generation=1,
                acquired_at=acquired_at,
                expires_at=expires_at,
                state=authority_models.AttemptLeaseStatus.ACTIVE,
            )
            return authority_models.AttemptAuthorityDecision(
                attempt=attempt,
                counter_before=0,
                counter_after=1,
                expected_retained=retained,
                proposed_replacement=proposed_replacement,
            )
        case authority_models.TransferAttemptAuthority(
            current=current,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if retained is None or transferable_attempt != (retained.attempt, retained.item):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "Attempt transfer requires the exact retained nonterminal attempt.",
                    None,
                )
            if (failure := _validate_attempt_transfer(retained, current, acquired_at)) is not None:
                return failure
            assert retained is not None
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Transferred attempt authority requires a positive bounded interval.",
                    None,
                )
            proposed_replacement = replace(
                retained,
                task_id=task_id,
                host_id=host_id,
                lease_id=lease_id,
                generation=counter + 1,
                acquired_at=acquired_at,
                expires_at=expires_at,
                state=authority_models.AttemptLeaseStatus.ACTIVE,
            )
            return authority_models.AttemptAuthorityDecision(
                attempt=retained.attempt,
                counter_before=counter,
                counter_after=counter + 1,
                expected_retained=retained,
                proposed_replacement=proposed_replacement,
            )
        case authority_models.RenewAttemptAuthority(current=current, renewed_at=renewed_at, expires_at=expires_at):
            if (failure := _validate_attempt_change(retained, current, renewed_at)) is not None:
                return failure
            assert retained is not None
            if expires_at <= retained.expires_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Attempt renewal must extend its bounded expiry.",
                    None,
                )
            return authority_models.AttemptAuthorityDecision(
                attempt=retained.attempt,
                counter_before=counter,
                counter_after=counter,
                expected_retained=retained,
                proposed_replacement=replace(retained, expires_at=expires_at),
            )
        case authority_models.ReleaseAttemptAuthority(current=current, released_at=released_at):
            if (failure := _validate_attempt_change(retained, current, released_at)) is not None:
                return failure
            assert retained is not None
            proposed_replacement = replace(
                retained,
                generation=counter + 1,
                expires_at=released_at,
                state=authority_models.AttemptLeaseStatus.RELEASED,
            )
            return authority_models.AttemptAuthorityDecision(
                attempt=retained.attempt,
                counter_before=counter,
                counter_after=counter + 1,
                expected_retained=retained,
                proposed_replacement=proposed_replacement,
            )
        case authority_models.RevokeAttemptAuthority(
            attempt=attempt,
            lease_id=lease_id,
            generation=generation,
            revoked_at=revoked_at,
        ):
            if retained is None or (retained.attempt, retained.lease_id, retained.generation) != (
                attempt,
                lease_id,
                generation,
            ):
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Attempt authority is fenced.", None)
            proposed_replacement = replace(
                retained,
                generation=counter + 1,
                expires_at=revoked_at,
                state=authority_models.AttemptLeaseStatus.REVOKED,
            )
            return authority_models.AttemptAuthorityDecision(
                attempt=attempt,
                counter_before=counter,
                counter_after=counter + 1,
                expected_retained=retained,
                proposed_replacement=proposed_replacement,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _validate_attempt_change(
    retained: authority_models.AttemptLeaseAuthority | None,
    current: work_models.CommandAttemptAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority does not exist.", None)
    if retained.state != authority_models.AttemptLeaseStatus.ACTIVE or not _same_attempt_token(retained, current):
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Attempt authority is fenced.", None)
    if retained.expires_at <= now:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_EXPIRED, "Attempt authority has expired.", None)
    return None


def _validate_attempt_transfer(
    retained: authority_models.AttemptLeaseAuthority | None,
    current: authority_models.InactiveAttemptAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority does not exist.", None)
    if retained.state == authority_models.AttemptLeaseStatus.ACTIVE and retained.expires_at > now:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority remains live.", None)
    expected_state = (
        authority_models.AttemptLeaseStatus.EXPIRED
        if retained.state == authority_models.AttemptLeaseStatus.ACTIVE
        else retained.state
    )
    if expected_state not in {
        authority_models.AttemptLeaseStatus.RELEASED,
        authority_models.AttemptLeaseStatus.REVOKED,
        authority_models.AttemptLeaseStatus.EXPIRED,
    }:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not inactive.", None)
    expected = authority_models.InactiveAttemptAuthority(
        host_epoch=retained.host_epoch,
        attempt=retained.attempt,
        item=retained.item,
        task_id=retained.task_id,
        host_id=retained.host_id,
        lease_id=retained.lease_id,
        generation=retained.generation,
        expires_at=retained.expires_at,
        state=expected_state,
    )
    if current != expected:
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Attempt authority is fenced.", None)
    return None


def _preparation_token(value: authority_models.PreparationLeaseAuthority) -> work_models.PreparationCommandAuthority:
    return work_models.PreparationCommandAuthority(
        host_epoch=value.host_epoch,
        item=value.item,
        definition_revision=value.definition_revision,
        definition_digest=value.definition_digest,
        task_id=value.task_id,
        host_id=value.host_id,
        lease_id=value.lease_id,
        generation=value.generation,
        expires_at=value.expires_at,
    )


def _validate_preparation_change(
    retained: authority_models.PreparationLeaseAuthority | None,
    current: work_models.PreparationCommandAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority does not exist.", None)
    if retained.state != authority_models.PreparationLeaseStatus.ACTIVE or _preparation_token(retained) != current:
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Preparation authority is fenced.", None)
    if retained.expires_at <= now:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority has expired.", None)
    return None


def _validate_preparation_transfer(
    retained: authority_models.PreparationLeaseAuthority | None,
    current: authority_models.InactivePreparationAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority does not exist.", None)
    if retained.state == authority_models.PreparationLeaseStatus.ACTIVE and retained.expires_at > now:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority remains live.", None)
    state = (
        authority_models.PreparationLeaseStatus.EXPIRED
        if retained.state == authority_models.PreparationLeaseStatus.ACTIVE
        else retained.state
    )
    expected = authority_models.InactivePreparationAuthority(
        host_epoch=retained.host_epoch,
        item=retained.item,
        definition_revision=retained.definition_revision,
        definition_digest=retained.definition_digest,
        task_id=retained.task_id,
        host_id=retained.host_id,
        lease_id=retained.lease_id,
        generation=retained.generation,
        expires_at=retained.expires_at,
        state=state,
    )
    if (
        state
        not in {
            authority_models.PreparationLeaseStatus.EXPIRED,
            authority_models.PreparationLeaseStatus.RELEASED,
            authority_models.PreparationLeaseStatus.REVOKED,
        }
        or current != expected
    ):
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Preparation authority is fenced.", None)
    return None


def decide_preparation_authority(  # noqa: C901, PLR0912
    retained: authority_models.PreparationLeaseAuthority | None,
    counter: int,
    operation: authority_models.PreparationAuthorityOperation,
    snapshot: LedgerSnapshot | None,
    now: datetime,
) -> DecisionResult[authority_models.PreparationAuthorityDecision]:
    match operation:
        case authority_models.AcquireInitialPreparationAuthority(
            host_epoch=host_epoch,
            item=item,
            expected_project_revision=expected_project_revision,
            expected_item_subject_revision=expected_item_subject_revision,
            expected_definition_revision=expected_definition_revision,
            expected_definition_digest=expected_definition_digest,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if snapshot is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation requires ledger state.", None
                )
            item_value = snapshot.item(item)
            definition = snapshot.definition(item)
            if snapshot.host_epoch != host_epoch:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Initial preparation requires the exact dependency-satisfied ready item and definition.",
                    None,
                )
            if snapshot.revision != expected_project_revision:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Project revision differs from the initial preparation request.",
                    None,
                )
            if item_value is None:
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item}' does not exist.", None)
            if snapshot.subject_revision(item) != expected_item_subject_revision:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Item '{item}' subject revision differs from the initial preparation request.",
                    None,
                )
            if item_value.state != work_models.WorkState.READY:
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item}' is not ready.", None)
            if any(dependency in snapshot.items_by_id() for dependency in item_value.depends_on):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Item '{item}' has live dependencies.",
                    None,
                )
            if definition is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Item '{item}' has no accepted definition.",
                    None,
                )
            if definition.revision != expected_definition_revision:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Item '{item}' definition revision differs from the initial preparation request.",
                    None,
                )
            if definition.digest != expected_definition_digest:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Item '{item}' definition digest differs from the initial preparation request.",
                    None,
                )
            if counter != 0 or retained is not None:
                return DecisionFailure(
                    DecisionFailureCode.LEASE_FENCED, "Initial preparation is already claimed.", None
                )
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Preparation authority requires a positive bounded interval.",
                    None,
                )
            proposed_replacement = authority_models.PreparationLeaseAuthority(
                host_epoch=host_epoch,
                item=item,
                definition_revision=definition.revision,
                definition_digest=definition.digest,
                task_id=task_id,
                host_id=host_id,
                lease_id=lease_id,
                generation=1,
                acquired_at=acquired_at,
                expires_at=expires_at,
                state=authority_models.PreparationLeaseStatus.ACTIVE,
            )
            return authority_models.PreparationAuthorityDecision(
                item=item,
                counter_before=0,
                counter_after=1,
                expected_retained=None,
                proposed_replacement=proposed_replacement,
            )
        case authority_models.TransferPreparationAuthority(
            current=current,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if snapshot is None or retained is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation transfer is unavailable.", None
                )
            if (failure := _validate_preparation_transfer(retained, current, now)) is not None:
                return failure
            item_value = snapshot.item(retained.item)
            definition = snapshot.definition(retained.item)
            if (
                item_value is None
                or item_value.state != work_models.WorkState.READY
                or any(dependency in snapshot.items_by_id() for dependency in item_value.depends_on)
                or definition is None
                or expires_at <= acquired_at
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation cannot be transferred.", None
                )
            proposed_replacement = replace(
                retained,
                definition_revision=definition.revision,
                definition_digest=definition.digest,
                task_id=task_id,
                host_id=host_id,
                lease_id=lease_id,
                generation=counter + 1,
                acquired_at=acquired_at,
                expires_at=expires_at,
                state=authority_models.PreparationLeaseStatus.ACTIVE,
            )
            return authority_models.PreparationAuthorityDecision(
                item=retained.item,
                counter_before=counter,
                counter_after=counter + 1,
                expected_retained=retained,
                proposed_replacement=proposed_replacement,
            )
        case authority_models.RenewPreparationAuthority(current=current, renewed_at=renewed_at, expires_at=expires_at):
            if (failure := _validate_preparation_change(retained, current, now)) is not None:
                return failure
            assert retained is not None
            if expires_at <= renewed_at or expires_at <= retained.expires_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Preparation renewal must extend its bounded expiry.",
                    None,
                )
            return authority_models.PreparationAuthorityDecision(
                item=retained.item,
                counter_before=counter,
                counter_after=counter,
                expected_retained=retained,
                proposed_replacement=replace(retained, expires_at=expires_at),
            )
        case authority_models.ReleasePreparationAuthority(current=current, released_at=released_at):
            if (failure := _validate_preparation_change(retained, current, now)) is not None:
                return failure
            assert retained is not None
            return authority_models.PreparationAuthorityDecision(
                item=retained.item,
                counter_before=counter,
                counter_after=counter + 1,
                expected_retained=retained,
                proposed_replacement=replace(
                    retained,
                    generation=counter + 1,
                    expires_at=released_at,
                    state=authority_models.PreparationLeaseStatus.RELEASED,
                ),
            )
        case authority_models.RevokePreparationAuthority(
            item=item,
            lease_id=lease_id,
            generation=generation,
            revoked_at=revoked_at,
        ):
            if snapshot is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation revocation is unavailable.", None
                )
            if retained is None or (retained.item, retained.lease_id, retained.generation) != (
                item,
                lease_id,
                generation,
            ):
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Preparation authority is fenced.", None)
            return authority_models.PreparationAuthorityDecision(
                item=item,
                counter_before=counter,
                counter_after=counter + 1,
                expected_retained=retained,
                proposed_replacement=replace(
                    retained,
                    generation=counter + 1,
                    expires_at=revoked_at,
                    state=authority_models.PreparationLeaseStatus.REVOKED,
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)
