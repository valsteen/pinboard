import sqlite3
import subprocess
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
from pinboard.cli import cli_commands
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import ChangedSurface, EffectDisposition, RetryDisposition
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, ReviewId
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import mutation_operations as mcp_mutations
from pinboard.mcp import read_operations as mcp_reads
from tests.decision_support import discover_actions
from tests.domain_support import expect_success
from tests.support import SQLITE_DIGEST, SQLITE_NOW, JsonObject, complete_sqlite_state, decision_facts, initialize_store


class OperationFailureTest(unittest.TestCase):
    def initialized(
        self,
        state: stored_state.StoredWorkState | None,
    ) -> tuple[SQLiteWorkStore, cli_commands.ResolvedRoots]:
        project = Path(tempfile.mkdtemp()).resolve()
        subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state() if state is None else state)
        resolved = cli_commands.ResolvedRoots(project, project, roots.work_root, False)
        return store, resolved

    def transition(self, roots: cli_commands.ResolvedRoots, receipt: JsonObject) -> JsonObject:
        with patch("pinboard.mcp.mutation_operations.datetime") as clock:
            clock.now.return_value = SQLITE_NOW
            return mcp_mutations._transition(
                {
                    "request": {
                        "project_root": str(roots.source_checkout),
                        "work_root": str(roots.work),
                        "role": "project",
                        "actor_task_id": "task",
                        "actor_host_id": "host",
                        "receipt": receipt,
                        "payload": {"reason": "Pause for review."},
                    }
                },
                mcp_execution.CancellationToken(),
            ).content

    def test_sqlite_readonly_is_classified_separately_from_generic_storage_io(self) -> None:
        error = sqlite3.OperationalError("attempt to write a readonly database")
        error.sqlite_errorcode = sqlite3.SQLITE_READONLY
        error.sqlite_errorname = "SQLITE_READONLY"

        translated = translate_database_error(error)

        self.assertEqual("SQLITE_READONLY", translated.code.value)

    def test_native_receipt_rejections_are_structured_before_effect(self) -> None:
        store, roots = self.initialized(None)
        before = store.validated_snapshot()
        identities: tuple[JsonObject, ...] = (
            {"kind": "pause", "subject": ""},
            {"kind": "invented", "subject": "work-a"},
        )
        for identity in identities:
            with self.subTest(identity=identity):
                rejected = self.transition(roots, {"action_id": identity, "subject_revision": "12"})
                self.assertEqual("TRANSITION_INPUT_INVALID", rejected["code"])
                self.assertEqual("rejected", rejected["status"])
                self.assertFalse(rejected["state_changed"])
                self.assertEqual("unchanged", rejected["effect"])
                self.assertEqual(before, store.validated_snapshot())

    def test_native_selection_preserves_staleness_authority_and_lifecycle_rejections(self) -> None:
        state = complete_sqlite_state()
        pause = next(
            value
            for value in expect_success(discover_actions(state, decision_models.Role.PROJECT, now=SQLITE_NOW))
            if isinstance(value, decision_models.PauseAction)
        )
        store, roots = self.initialized(state)
        before = store.validated_snapshot()
        stale = self.transition(
            roots,
            {
                "action_id": {"kind": "pause", "subject": str(pause.capability.subject)},
                "subject_revision": "11",
            },
        )
        self.assertEqual("ACTION_NOT_AVAILABLE", stale["code"])
        self.assertEqual("refresh-action", stale["retry"])
        self.assertEqual(
            [{"field": "subject_revision", "expected": pause.capability.subject_revision, "observed": "11"}],
            stale["mismatches"],
        )
        self.assertEqual(before, store.validated_snapshot())

        for status in (authority_models.AttemptLeaseStatus.RELEASED, authority_models.AttemptLeaseStatus.EXPIRED):
            inactive = replace(
                state,
                authority=replace(
                    state.authority,
                    attempt_leases=tuple(replace(value, state=status) for value in state.authority.attempt_leases),
                ),
            )
            status_store, status_roots = self.initialized(inactive)
            unchanged = status_store.validated_snapshot()
            with patch("pinboard.mcp.mutation_operations.datetime") as clock:
                clock.now.return_value = SQLITE_NOW
                rejected = mcp_reads._read_actions(
                    {
                        "request": {
                            "project_root": str(status_roots.source_checkout),
                            "work_root": str(status_roots.work),
                            "role": "worker",
                            "lease_id": "attempt-lease-a",
                            "generation": 3,
                            "action_id": {"kind": "continue", "subject": "work-a-1"},
                        }
                    },
                    mcp_execution.CancellationToken(),
                ).content
            self.assertEqual("ATTEMPT_LEASE_REQUIRED", rejected["code"])
            self.assertEqual("reacquire-authority", rejected["retry"])
            self.assertFalse(rejected["state_changed"])
            self.assertEqual(unchanged, status_store.validated_snapshot())

        paused = replace(
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
        lifecycle_store, lifecycle_roots = self.initialized(paused)
        before = lifecycle_store.validated_snapshot()
        unavailable = self.transition(
            lifecycle_roots,
            {
                "action_id": {"kind": "pause", "subject": str(pause.capability.subject)},
                "subject_revision": pause.capability.subject_revision,
            },
        )
        self.assertEqual("ACTION_NOT_AVAILABLE", unavailable["code"])
        self.assertFalse(unavailable["state_changed"])
        self.assertEqual(before, lifecycle_store.validated_snapshot())

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
            lifecycle=replace(
                state.lifecycle,
                project=replace(state.lifecycle.project, revision=13),
                attempts=tuple(
                    replace(value, subject_revision=2) if value.attempt_id == AttemptId("work-a-1") else value
                    for value in state.lifecycle.attempts
                ),
            ),
        )
        _store, roots = self.initialized(drifted)
        store = SQLiteWorkStore(roots.work / "state.sqlite3")

        publication_surfaces = (
            ChangedSurface.IMMUTABLE_ARTIFACT,
            ChangedSurface.ACCEPTED_ARTIFACT_REFERENCE,
            ChangedSurface.LEDGER,
        )
        failure = recheck_dispatch_authority(store, supplied, publication_surfaces, SQLITE_NOW)

        self.assertIsInstance(failure, DispatchFailure)
        assert isinstance(failure, DispatchFailure)
        assert failure.details is not None
        self.assertEqual(EffectDisposition.COMMITTED, failure.details.effect)
        self.assertEqual(RetryDisposition.DO_NOT_RETRY, failure.details.retry)
        self.assertEqual((), failure.details.observed)
        self.assertEqual(publication_surfaces, failure.details.changed_surfaces)

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
