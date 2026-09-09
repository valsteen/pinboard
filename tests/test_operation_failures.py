import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, translate_database_error
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import WorkBriefIdentity
from pinboard.application.dispatch import publish_dispatch_review, recheck_dispatch_authority
from pinboard.application.dispatch_models import DispatchFailure
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import ChangedSurface, EffectDisposition, RetryDisposition
from pinboard.domain.identifiers import ActionId, ArtifactRefId, AttemptId, HostId, LeaseId, ReviewId, TaskId
from pinboard.interfaces import action_selection, cli_commands
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure
from tests.decision_support import discover_actions
from tests.domain_support import expect_success
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state, decision_facts, initialize_store


class OperationFailureTest(unittest.TestCase):
    def initialized(
        self,
        state: stored_state.StoredWorkState | None,
    ) -> tuple[SQLiteWorkStore, cli_commands.ResolvedRoots]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state() if state is None else state)
        resolved = cli_commands.ResolvedRoots(project, project, roots.work_root, False)
        return store, resolved

    def project_transition(self, action_id: str) -> cli_commands.ProjectTransitionCommand:
        return cli_commands.ProjectTransitionCommand(
            ActionId(action_id),
            "12",
            Path("payload.json"),
            TaskId("task"),
            HostId("host"),
        )

    def worker_action(self) -> decision_models.ContinueAction:
        actions = expect_success(
            discover_actions(
                complete_sqlite_state(),
                decision_models.Role.WORKER,
                lease_id=LeaseId("attempt-lease-a"),
                generation=3,
                now=SQLITE_NOW,
            )
        )
        selected = next(value for value in actions if decision_models.action_id(value) == "continue:work-a-1")
        assert isinstance(selected, decision_models.ContinueAction)
        return selected

    def test_sqlite_readonly_is_classified_separately_from_generic_storage_io(self) -> None:
        error = sqlite3.OperationalError("attempt to write a readonly database")
        error.sqlite_errorcode = sqlite3.SQLITE_READONLY
        error.sqlite_errorname = "SQLITE_READONLY"

        translated = translate_database_error(error)

        self.assertEqual("SQLITE_READONLY", translated.code.value)

    def test_action_receipt_rejections_are_distinct_and_structured(self) -> None:
        malformed = action_selection.parse_action_receipt(self.project_transition("invalid"))
        self.assertIsInstance(malformed, CommandFailure)
        assert isinstance(malformed, CommandFailure)
        self.assertEqual(CommandErrorCode.ACTION_ID_MALFORMED, malformed.code)
        assert malformed.details is not None
        self.assertEqual(RetryDisposition.CORRECT_INPUT, malformed.details.retry)
        self.assertEqual("invalid", malformed.details.observed[0].value)

        unknown = action_selection.parse_action_receipt(self.project_transition("invented:work-a"))
        self.assertIsInstance(unknown, CommandFailure)
        assert isinstance(unknown, CommandFailure)
        self.assertEqual(CommandErrorCode.ACTION_KIND_UNKNOWN, unknown.code)
        assert unknown.details is not None
        self.assertEqual("invented", unknown.details.mismatches[0].observed)

    def test_action_selection_distinguishes_staleness_authority_and_lifecycle(self) -> None:  # noqa: PLR0915 - one rejection-family matrix
        worker_action = self.worker_action()
        worker_receipt = action_selection.ParsedActionReceipt(worker_action, decision_models.Role.WORKER, 3)

        store, _roots = self.initialized(None)
        stale_action = replace(
            worker_action,
            capability=replace(worker_action.capability, expected_revision="11"),
        )
        with patch("pinboard.interfaces.action_selection.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            stale = action_selection.select_current_action(
                store,
                action_selection.ParsedActionReceipt(stale_action, decision_models.Role.WORKER, 3),
            )
        self.assertIsInstance(stale, CommandFailure)
        assert isinstance(stale, CommandFailure)
        self.assertEqual(CommandErrorCode.ACTION_REVISION_STALE, stale.code)
        assert stale.details is not None
        self.assertEqual(
            (worker_action.capability.expected_revision, "11"),
            (stale.details.mismatches[0].expected, stale.details.mismatches[0].observed),
        )
        continuation = next(value for value in stale.details.alternatives if value.action_id == "continue:work-a-1")
        self.assertEqual(
            (
                "continue:work-a-1",
                "worker",
                worker_action.capability.expected_revision,
                "attempt",
                "attempt-lease-a",
                3,
            ),
            (
                continuation.action_id,
                continuation.role,
                continuation.expected_revision,
                continuation.authorization,
                continuation.lease_id,
                continuation.generation,
            ),
        )

        wrong_action = replace(
            worker_action,
            capability=replace(worker_action.capability, lease_id=LeaseId("wrong-lease")),
        )
        with patch("pinboard.interfaces.action_selection.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            wrong = action_selection.select_current_action(
                store,
                action_selection.ParsedActionReceipt(wrong_action, decision_models.Role.WORKER, 3),
            )
        self.assertIsInstance(wrong, CommandFailure)
        assert isinstance(wrong, CommandFailure)
        self.assertEqual(CommandErrorCode.ACTION_AUTHORITY_WRONG, wrong.code)
        assert wrong.details is not None
        self.assertEqual("attempt-lease-a", wrong.details.mismatches[0].expected)
        self.assertEqual("wrong-lease", wrong.details.mismatches[0].observed)

        for status, expected_code in (
            (authority_models.AttemptLeaseStatus.RELEASED, CommandErrorCode.ACTION_AUTHORITY_RELEASED),
            (authority_models.AttemptLeaseStatus.EXPIRED, CommandErrorCode.ACTION_AUTHORITY_EXPIRED),
        ):
            state = complete_sqlite_state()
            state = replace(
                state,
                authority=replace(
                    state.authority,
                    attempt_leases=tuple(replace(value, state=status) for value in state.authority.attempt_leases),
                ),
            )
            status_store, _status_roots = self.initialized(state)
            before = status_store.validated_snapshot()
            with patch("pinboard.interfaces.action_selection.datetime") as clock:
                clock.now.return_value = SQLITE_NOW
                rejected = action_selection.select_current_action(status_store, worker_receipt)
            self.assertIsInstance(rejected, CommandFailure)
            assert isinstance(rejected, CommandFailure)
            self.assertEqual(expected_code, rejected.code)
            assert rejected.details is not None
            self.assertEqual(status.value, rejected.details.observed[0].value)
            self.assertEqual(before, status_store.validated_snapshot())

        state = complete_sqlite_state()
        project_pause = next(
            value
            for value in expect_success(discover_actions(state, decision_models.Role.PROJECT, now=SQLITE_NOW))
            if isinstance(value, decision_models.PauseAction)
        )
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.PAUSED)
                    if str(value.item_id) == "work-a"
                    else value
                    for value in state.lifecycle.work_items
                ),
                attempts=tuple(
                    replace(value, state=work_models.AttemptState.PAUSED) for value in state.lifecycle.attempts
                ),
            ),
        )
        lifecycle_store, _lifecycle_roots = self.initialized(state)
        with patch("pinboard.interfaces.action_selection.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            unavailable = action_selection.select_current_action(
                lifecycle_store,
                action_selection.ParsedActionReceipt(project_pause, decision_models.Role.PROJECT, 0),
            )
        self.assertIsInstance(unavailable, CommandFailure)
        assert isinstance(unavailable, CommandFailure)
        self.assertEqual(CommandErrorCode.ACTION_LIFECYCLE_UNAVAILABLE, unavailable.code)
        assert unavailable.details is not None
        self.assertEqual("active-attempt", unavailable.details.mismatches[0].expected)
        self.assertEqual("paused", unavailable.details.mismatches[0].observed)
        self.assertTrue(unavailable.details.alternatives)
        self.assertTrue(all(value.expected_revision == "12" for value in unavailable.details.alternatives))

    def test_transition_brief_reports_every_identity_mismatch(self) -> None:
        state = complete_sqlite_state()
        capability = decision_models.MutationActionCapability(
            AttemptId("work-a-1"),
            "Rebind work-a-1",
            "12",
        )
        command = decision_models.RebindAttemptCommand(
            decision_models.RebindAttemptAction(capability),
            work_models.RebindAttemptInput(
                AttemptId("work-a-1"),
                "codex/work-a",
                "base-revision",
                ArtifactRefId(1),
            ),
        )
        supplied = WorkBriefIdentity("wrong-attempt", "wrong-item", "wrong-branch", "wrong-base", 2, "f" * 64)

        failure = validate_transition_work_brief(decision_facts(state, SQLITE_NOW), command, supplied)

        self.assertIsNotNone(failure)
        assert failure is not None
        assert failure.details is not None
        self.assertEqual(RetryDisposition.CORRECT_INPUT, failure.details.retry)
        self.assertEqual(EffectDisposition.UNCHANGED, failure.details.effect)
        self.assertEqual(
            {
                "attempt_id": ("work-a-1", "wrong-attempt"),
                "item_id": ("work-a", "wrong-item"),
                "branch": ("codex/work-a", "wrong-branch"),
                "base_revision": ("base-revision", "wrong-base"),
                "accepted_scope_revision": (1, 2),
                "accepted_scope_digest": (SQLITE_DIGEST, "f" * 64),
            },
            {value.field: (value.expected, value.observed) for value in failure.details.mismatches},
        )
        self.assertEqual("unchanged", msgspec.to_builtins(failure.details)["effect"])

    def test_dispatch_post_publication_drift_reports_committed_effect(self) -> None:
        state = complete_sqlite_state()
        supplied = next(
            value
            for value in expect_success(discover_actions(state, decision_models.Role.PROJECT, now=SQLITE_NOW))
            if isinstance(value, decision_models.DispatchAction)
        )
        drifted = replace(
            state,
            lifecycle=replace(state.lifecycle, project=replace(state.lifecycle.project, revision=14)),
        )
        _store, roots = self.initialized(drifted)
        store = SQLiteWorkStore(roots.work / "state.sqlite3")

        failure = recheck_dispatch_authority(store, supplied, 13, SQLITE_NOW)

        self.assertIsInstance(failure, DispatchFailure)
        assert isinstance(failure, DispatchFailure)
        assert failure.details is not None
        self.assertEqual(EffectDisposition.COMMITTED, failure.details.effect)
        self.assertEqual(RetryDisposition.DO_NOT_RETRY, failure.details.retry)
        self.assertEqual(13, failure.details.observed[0].value)
        self.assertEqual(
            (
                ChangedSurface.IMMUTABLE_ARTIFACT,
                ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE,
                ChangedSurface.LEDGER,
            ),
            failure.details.changed_surfaces,
        )

    def test_dispatch_review_collision_reports_preserved_evidence_surfaces(self) -> None:
        store, roots = self.initialized(None)
        artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository))
        checkpoint_sha256 = "a" * 64
        first = publish_dispatch_review(
            store,
            artifacts,
            AttemptId("work-a-1"),
            checkpoint_sha256,
            b"first review\n",
            ReviewId("first"),
            SQLITE_NOW,
        )
        self.assertNotIsInstance(first, DispatchFailure)

        collision = publish_dispatch_review(
            store,
            artifacts,
            AttemptId("work-a-1"),
            checkpoint_sha256,
            b"different review\n",
            ReviewId("second"),
            SQLITE_NOW,
        )

        self.assertIsInstance(collision, DispatchFailure)
        assert isinstance(collision, DispatchFailure)
        assert collision.details is not None
        self.assertEqual(EffectDisposition.COMMITTED, collision.details.effect)
        self.assertEqual(
            (
                ChangedSurface.IMMUTABLE_ARTIFACT,
                ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE,
                ChangedSurface.LEDGER,
            ),
            collision.details.changed_surfaces,
        )
        self.assertTrue(str(collision.details.observed[0].value).endswith(f"rejected-{ReviewId('second')}/1.json"))
        self.assertTrue(any("rejected-second" in value.key for value in store.validated_snapshot().artifact_references))


if __name__ == "__main__":
    unittest.main()
