import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactPublication, ArtifactRef, BriefArtifactRef, NewArtifact
from pinboard.application.dispatch_models import (
    FRESH_CONTEXT_REQUIRED,
    DispatchEnvironment,
    DispatchPermission,
    FreshContextRequired,
)
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import HostId, ReviewId
from pinboard.interfaces import dispatch_brief, work_brief_models
from pinboard.interfaces.cli import main
from pinboard.interfaces.dispatch_brief import (
    SuppliedDispatchReview,
    _read_dispatch_brief,
    _render_dispatch_prompt,
    prepare_dispatch,
    read_dispatch_environment,
)
from pinboard.interfaces.errors import DispatchErrorCode, DispatchFailure, DispatchResult
from pinboard.interfaces.work_briefs import canonical_work_brief_bytes, canonical_work_brief_review_bytes
from tests.artifact_support import write_revision
from tests.decision_support import discover_actions
from tests.domain_support import expect_success
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.work_brief_support import CHECKPOINT_ID, needs_correction_review, ready_review, work_a_brief


def dispatch_environment_enc_hook(value: FreshContextRequired) -> bool:
    if isinstance(value, FreshContextRequired):
        return True
    raise TypeError(f"unsupported dispatch environment value: {value!r}")


def expect_dispatch_success[T](result: DispatchResult[T]) -> T:
    if isinstance(result, DispatchFailure):
        raise AssertionError(str(result))
    return result


def expect_dispatch_failure[T](result: DispatchResult[T], code: DispatchErrorCode) -> DispatchFailure:
    if not isinstance(result, DispatchFailure):
        raise AssertionError(f"Expected {code.value}, got success: {result!r}")
    if result.code != code:
        raise AssertionError(f"Expected {code.value}, got {result.code.value}: {result.message}")
    return result


def prepare_dispatch_from_artifact(
    attempt_path: Path,
    attempt_id: str,
    attempt_branch: str,
    attempt_base_revision: str,
    source_checkout_root: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    *,
    accepted_item_id: str | None = None,
    accepted_scope_revision: int | None = None,
    accepted_scope_digest: str | None = None,
    supplied_prompt: bytes | None = None,
    accepted_review: bytes | None = None,
) -> DispatchResult[str]:
    brief = _read_dispatch_brief(
        attempt_path.read_bytes(),
        attempt_id,
        attempt_branch,
        attempt_base_revision,
        source_checkout_root,
        attempt_path.parent,
        checkpoint,
        environment,
        accepted_item_id,
        accepted_scope_revision,
        accepted_scope_digest,
        validate_original_authorities=True,
    )
    if isinstance(brief, DispatchFailure):
        return brief
    return _render_dispatch_prompt(
        brief,
        attempt_path.parent,
        attempt_path,
        checkpoint,
        environment,
        accepted_review,
        supplied_prompt,
    )


class DispatchTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def environment(self, project: Path) -> DispatchEnvironment:
        return DispatchEnvironment(
            "pinboard-dispatch/v2",
            str(project),
            "codex/work-a",
            "base-revision",
            HostId("local"),
            FRESH_CONTEXT_REQUIRED,
            3600,
            (DispatchPermission.REPOSITORY_READ,),
        )

    def run_git(self, cwd: Path, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)

    def initialized(
        self,
        project: Path | None = None,
        roots: DurableRoots | None = None,
    ) -> tuple[
        Path,
        DurableRoots,
        SQLiteWorkStore,
        work_brief_models.WorkBrief,
        Callable[[], decision_models.DispatchAction],
        DispatchEnvironment,
    ]:
        project = Path(tempfile.mkdtemp()).resolve() if project is None else project
        roots = resolve_durable_roots(project) if roots is None else roots
        initialize_database(roots, SQLITE_NOW)
        brief = work_a_brief(project)
        published = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                brief.attempt_id,
                brief.artifact_revision,
                ".json",
                canonical_work_brief_bytes(brief),
            ),
        )
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        leases = tuple(
            dataclass_replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
        )
        reference = dataclass_replace(
            state.artifact_references[0],
            key=published.key,
            revision=published.revision,
            selector=published.selector,
            content_sha256=published.content_sha256,
            size_bytes=published.size_bytes,
        )
        state = dataclass_replace(
            state,
            artifact_references=(reference, *state.artifact_references[1:]),
            authority=dataclass_replace(state.authority, attempt_leases=leases),
        )
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)

        def action() -> decision_models.DispatchAction:
            actions = expect_success(
                discover_actions(
                    store.validated_snapshot(),
                    decision_models.Role.PROJECT,
                    now=SQLITE_NOW,
                )
            )
            selected = next(candidate for candidate in actions if candidate.kind == decision_models.ActionKind.DISPATCH)
            assert isinstance(selected, decision_models.DispatchAction)
            return selected

        return project, roots, store, brief, action, self.environment(project)

    def test_direct_typed_dispatch_validates_identity_sources_review_and_prompt(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        path = project / "brief.json"
        path.write_bytes(canonical_work_brief_bytes(value))
        environment = self.environment(project)
        candidate = ready_review(value)

        prompt = expect_dispatch_success(
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                value.base_revision,
                project,
                CHECKPOINT_ID,
                environment,
                accepted_item_id=value.item_id,
                accepted_scope_revision=value.accepted_scope.revision,
                accepted_scope_digest=value.accepted_scope.digest,
                accepted_review=candidate,
            )
        )

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        self.assertIn(f"Canonical brief: {path}", prompt)
        self.assertIn("- Fresh context: required", prompt)
        self.assertIn("- Runtime host: local", prompt)
        self.assertIn(f"- Result: {project / 'attempts' / value.attempt_id / 'result.md'}", prompt)
        self.assertIn(f"- Blocker: {project / 'attempts' / value.attempt_id / 'blocker.md'}", prompt)
        self.assertIn(
            f"pinboard --project-root {project} --work-root {project} attempt acquire --attempt-id {value.attempt_id} "
            "--task-id <exact-worker-task-id> --host-id local --ttl-seconds 3600 --json",
            prompt,
        )
        self.assertIn(
            "pinboard --project-root "
            f"{project} --work-root {project} actions --role worker --lease-id <returned-lease-id> "
            "--generation <returned-generation> "
            f"--action-id continue:{value.attempt_id} --json",
            prompt,
        )
        self.assertIn("- Declared permissions: repository-read", prompt)
        altered_prompt = expect_dispatch_failure(
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                value.base_revision,
                project,
                CHECKPOINT_ID,
                environment,
                accepted_item_id=value.item_id,
                accepted_scope_revision=value.accepted_scope.revision,
                accepted_scope_digest=value.accepted_scope.digest,
                supplied_prompt=(prompt + "extra").encode(),
                accepted_review=candidate,
            ),
            DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL,
        )
        self.assertIn("launch adds or changes instructions", altered_prompt.message)

        project.joinpath("architecture.md").write_text("# Architecture\n\n## Contract\n\nChanged.\n", encoding="utf-8")
        stale_source = expect_dispatch_failure(
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                value.base_revision,
                project,
                CHECKPOINT_ID,
                environment,
                accepted_item_id=value.item_id,
                accepted_scope_revision=value.accepted_scope.revision,
                accepted_scope_digest=value.accepted_scope.digest,
                accepted_review=candidate,
            ),
            DispatchErrorCode.DISPATCH_AUTHORITY_STALE,
        )
        self.assertIn("changed after review", stale_source.message)

    def test_dispatch_samples_selection_publication_and_confirmation_times_separately(self) -> None:
        project, roots, store, brief, action, environment = self.initialized()
        selected_action = action()
        first = datetime.now(UTC)
        samples = tuple(first + timedelta(microseconds=index) for index in range(4))

        with patch("pinboard.interfaces.dispatch_brief.datetime") as clock:
            clock.now.side_effect = samples
            prompt = expect_dispatch_success(
                prepare_dispatch(
                    store,
                    ArtifactRepository(roots),
                    project,
                    selected_action,
                    CHECKPOINT_ID,
                    environment,
                    supplied_prompt=None,
                    supplied_review=SuppliedDispatchReview(ready_review(brief), ReviewId("timed-review")),
                    correction_history_id=None,
                )
            )

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        self.assertEqual(4, clock.now.call_count)

    def test_identity_review_and_environment_failure_matrix_is_stable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        path = project / "brief.json"
        path.write_bytes(canonical_work_brief_bytes(value))
        environment = self.environment(project)

        cases = (
            ("attempt", {"attempt_id": "other"}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("item", {"accepted_item_id": "other"}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("scope-revision", {"accepted_scope_revision": 2}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("scope-digest", {"accepted_scope_digest": "f" * 64}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("checkpoint", {"checkpoint": "other"}, DispatchErrorCode.DISPATCH_CHECKPOINT_MISSING),
            (
                "branch",
                {"environment": replace(environment, branch="other")},
                DispatchErrorCode.DISPATCH_BRANCH_MISMATCH,
            ),
            (
                "attempt-base-revision",
                {"attempt_base_revision": "other"},
                DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH,
            ),
            (
                "environment-base-revision",
                {"environment": replace(environment, starting_revision="other")},
                DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH,
            ),
            (
                "checkout",
                {"environment": replace(environment, checkout=str(project / "missing"))},
                DispatchErrorCode.DISPATCH_CHECKOUT_MISSING,
            ),
            (
                "checkout-mismatch",
                {"source_checkout_root": Path(tempfile.mkdtemp()).resolve()},
                DispatchErrorCode.DISPATCH_CHECKOUT_MISMATCH,
            ),
        )
        for _name, changed, code in cases:
            arguments = {
                "attempt_path": path,
                "attempt_id": value.attempt_id,
                "attempt_branch": value.branch,
                "attempt_base_revision": value.base_revision,
                "source_checkout_root": project,
                "checkpoint": CHECKPOINT_ID,
                "environment": environment,
                "accepted_item_id": value.item_id,
                "accepted_scope_revision": value.accepted_scope.revision,
                "accepted_scope_digest": value.accepted_scope.digest,
                "accepted_review": ready_review(value),
            }
            arguments.update(changed)
            with self.subTest(name=_name):
                failure = expect_dispatch_failure(prepare_dispatch_from_artifact(**arguments), code)
                if code == DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH:
                    assert failure.details is not None
                    self.assertEqual("correct-input", failure.details.retry.value)
                    self.assertEqual("unchanged", failure.details.effect.value)
                    self.assertTrue(failure.details.mismatches)
                    observed = {fact.field: fact.value for fact in failure.details.observed}
                    self.assertEqual(value.base_revision, observed["brief_base_revision"])
                    self.assertIn("tool-contract --operation dispatch --json", str(observed["tool_contract_command"]))
                    self.assertIn(
                        "actions --role project --action-id dispatch:work-a-1 --json",
                        str(observed["current_dispatch_action_command"]),
                    )

        negative = prepare_dispatch_from_artifact(
            path,
            value.attempt_id,
            value.branch,
            value.base_revision,
            project,
            CHECKPOINT_ID,
            environment,
            accepted_item_id=value.item_id,
            accepted_scope_revision=value.accepted_scope.revision,
            accepted_scope_digest=value.accepted_scope.digest,
            accepted_review=needs_correction_review(value),
        )
        expect_dispatch_failure(negative, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID)

        review = msgspec.json.decode(ready_review(value), type=work_brief_models.WorkBriefReview)
        for changed, code in (
            ({"reviewer_task_id": value.owner_task_id}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT),
            ({"checkpoint_sha256": "f" * 64}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE),
            ({"coverage": ()}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID),
        ):
            with self.subTest(changed=changed):
                failure = prepare_dispatch_from_artifact(
                    path,
                    value.attempt_id,
                    value.branch,
                    value.base_revision,
                    project,
                    CHECKPOINT_ID,
                    environment,
                    accepted_item_id=value.item_id,
                    accepted_scope_revision=value.accepted_scope.revision,
                    accepted_scope_digest=value.accepted_scope.digest,
                    accepted_review=canonical_work_brief_review_bytes(replace(review, **changed)),
                )
                expect_dispatch_failure(failure, code)

    def test_base_mismatch_reports_each_stale_source_and_recovery(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        path = project / "brief.json"
        path.write_bytes(canonical_work_brief_bytes(value))
        failure = expect_dispatch_failure(
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                "attempt-base",
                project,
                CHECKPOINT_ID,
                replace(self.environment(project), starting_revision="environment-base"),
                accepted_item_id=value.item_id,
                accepted_scope_revision=value.accepted_scope.revision,
                accepted_scope_digest=value.accepted_scope.digest,
                accepted_review=ready_review(value),
            ),
            DispatchErrorCode.DISPATCH_BASE_REVISION_MISMATCH,
        )
        assert failure.details is not None
        self.assertEqual("correct-input", failure.details.retry.value)
        self.assertEqual("unchanged", failure.details.effect.value)
        self.assertEqual(
            {"brief_base_revision", "environment_base_revision"},
            {mismatch.field for mismatch in failure.details.mismatches},
        )
        observed = {fact.field: fact.value for fact in failure.details.observed}
        self.assertEqual("attempt-base", observed["attempt_base_revision"])
        self.assertEqual(value.base_revision, observed["brief_base_revision"])
        self.assertEqual("environment-base", observed["environment_base_revision"])
        self.assertIn("tool-contract --operation dispatch --json", str(observed["tool_contract_command"]))
        self.assertIn(
            f"actions --role project --action-id dispatch:{value.attempt_id} --json",
            str(observed["current_dispatch_action_command"]),
        )

    def test_needs_correction_review_remains_invalid_as_ready_dispatch_evidence(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        path = project / "brief.json"
        path.write_bytes(canonical_work_brief_bytes(value))

        failure = prepare_dispatch_from_artifact(
            path,
            value.attempt_id,
            value.branch,
            value.base_revision,
            project,
            CHECKPOINT_ID,
            self.environment(project),
            accepted_item_id=value.item_id,
            accepted_scope_revision=value.accepted_scope.revision,
            accepted_scope_digest=value.accepted_scope.digest,
            accepted_review=needs_correction_review(value),
        )
        expect_dispatch_failure(failure, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID)

    def test_local_checkpoint_rejects_review_arguments(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        cross = value.checkpoint
        assert isinstance(cross, work_brief_models.CrossBoundaryCheckpoint)
        local = work_brief_models.LocalCheckpoint(
            "local-cutover",
            "Local cutover",
            cross.architecture_impact,
            cross.outcome_description,
            cross.acceptance_criteria,
            cross.verification,
            cross.deferrals,
        )
        value = replace(value, checkpoint=local)
        path = project / "local.json"
        path.write_bytes(canonical_work_brief_bytes(value))

        prompt = expect_dispatch_success(
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                value.base_revision,
                project,
                local.checkpoint_id,
                self.environment(project),
                accepted_item_id=value.item_id,
                accepted_scope_revision=1,
                accepted_scope_digest=SQLITE_DIGEST,
            )
        )
        self.assertIn("Checkpoint: local-cutover", prompt)
        rejected = prepare_dispatch_from_artifact(
            path,
            value.attempt_id,
            value.branch,
            value.base_revision,
            project,
            local.checkpoint_id,
            self.environment(project),
            accepted_item_id=value.item_id,
            accepted_scope_revision=1,
            accepted_scope_digest=SQLITE_DIGEST,
            accepted_review=b"{}",
        )
        expect_dispatch_failure(rejected, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID)

    def test_sqlite_dispatch_publishes_reuses_and_preserves_review_collisions(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        first_review = ready_review(value)

        with (
            patch.object(dispatch_brief, "select_dispatch", wraps=dispatch_brief.select_dispatch) as select,
            patch.object(
                dispatch_brief,
                "publish_dispatch_review",
                wraps=dispatch_brief.publish_dispatch_review,
            ) as publish_review,
            patch.object(
                dispatch_brief,
                "recheck_dispatch_authority",
                wraps=dispatch_brief.recheck_dispatch_authority,
            ) as recheck,
        ):
            prompt = expect_dispatch_success(
                prepare_dispatch(
                    store,
                    ArtifactRepository(roots),
                    project,
                    action(),
                    CHECKPOINT_ID,
                    environment,
                    supplied_prompt=None,
                    supplied_review=SuppliedDispatchReview(first_review, ReviewId("first-review")),
                    correction_history_id=None,
                )
            )
        self.assertIs(store, select.call_args.args[0])
        self.assertIs(store, publish_review.call_args.args[0])
        self.assertIs(store, recheck.call_args.args[0])

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        after_first = store.validated_snapshot()
        ready = tuple(
            reference
            for reference in after_first.artifact_references
            if reference.kind == work_models.ArtifactKind.EVIDENCE and "brief-review" in reference.key
        )
        self.assertEqual(1, len(ready))
        self.assertTrue(ready[0].selector.endswith(".json"))

        reused = expect_dispatch_success(
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                supplied_review=None,
                correction_history_id=None,
            )
        )
        self.assertEqual(prompt, reused)
        self.assertEqual(after_first, store.validated_snapshot())

        identical_retry = expect_dispatch_success(
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                supplied_review=SuppliedDispatchReview(first_review, ReviewId("identical-review")),
                correction_history_id=None,
            )
        )
        self.assertEqual(prompt, identical_retry)
        self.assertEqual(after_first, store.validated_snapshot())

        collision = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            project,
            action(),
            CHECKPOINT_ID,
            environment,
            supplied_prompt=None,
            supplied_review=SuppliedDispatchReview(
                ready_review(value, result="Different complete result."),
                ReviewId("later-review"),
            ),
            correction_history_id=None,
        )
        expect_dispatch_failure(collision, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION)
        self.assertTrue(
            any(
                "rejected-later-review" in reference.key for reference in store.validated_snapshot().artifact_references
            )
        )
        before_identical_retry = store.validated_snapshot()
        identical_retry = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            project,
            action(),
            CHECKPOINT_ID,
            environment,
            supplied_prompt=None,
            supplied_review=SuppliedDispatchReview(
                ready_review(value, result="Different complete result."),
                ReviewId("later-review"),
            ),
            correction_history_id=None,
        )
        repeated_collision = expect_dispatch_failure(
            identical_retry,
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION,
        )
        assert repeated_collision.details is not None
        self.assertEqual("unchanged", repeated_collision.details.effect.value)
        self.assertEqual((), repeated_collision.details.changed_surfaces)
        self.assertEqual(before_identical_retry, store.validated_snapshot())

    def test_installed_dispatch_reports_new_ready_and_collision_artifacts_after_database_failure(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))

        def arguments(selected: decision_models.DispatchAction, review_id: str) -> tuple[str, ...]:
            return (
                *common,
                "dispatch",
                "--action-id",
                str(decision_models.action_id(selected)),
                "--subject-revision",
                selected.capability.subject_revision,
                "--task-id",
                "project-task",
                "--host-id",
                "host-a",
                "--checkpoint",
                CHECKPOINT_ID,
                "--environment",
                str(environment_path),
                "--brief-review",
                str(review_path),
                "--review-id",
                review_id,
                "--json",
            )

        database_failure = StorageError(StorageErrorCode.BUSY, "database failed", retryable=True)
        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            result, stdout, stderr = self.run_cli(*arguments(action(), "new-ready"))
        self.assertEqual(12, result, stderr)
        ready_failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", ready_failure["status"])
        self.assertEqual(["immutable-artifact"], ready_failure["changed_surfaces"])
        self.assertEqual("do-not-retry", ready_failure["retry"])

        accept_reference = SQLiteWorkStore.accept_artifact_reference
        acceptance_calls = 0

        def fail_prompt_acceptance(
            selected_store: SQLiteWorkStore,
            work_root: Path,
            published: ArtifactRef,
            accepted_at: datetime,
        ) -> DecisionResult[ArtifactReferenceAcceptance]:
            nonlocal acceptance_calls
            acceptance_calls += 1
            if acceptance_calls == 2:
                raise database_failure
            return accept_reference(selected_store, work_root, published, accepted_at)

        with patch.object(
            SQLiteWorkStore,
            "accept_artifact_reference",
            autospec=True,
            side_effect=fail_prompt_acceptance,
        ):
            result, stdout, stderr = self.run_cli(*arguments(action(), "prompt-acceptance-failure"))
        self.assertEqual(12, result, stderr)
        prompt_failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", prompt_failure["status"])
        self.assertEqual(
            ["accepted-artifact-reference", "ledger", "immutable-artifact"],
            prompt_failure["changed_surfaces"],
        )
        self.assertEqual("do-not-retry", prompt_failure["retry"])

        accepted = expect_dispatch_success(
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("accepted-ready")),
                correction_history_id=None,
            )
        )
        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", accepted)
        review_path.write_bytes(ready_review(value, result="Different complete result."))

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            result, stdout, stderr = self.run_cli(*arguments(action(), "new-collision"))
        self.assertEqual(12, result, stderr)
        collision_failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", collision_failure["status"])
        self.assertEqual(["immutable-artifact"], collision_failure["changed_surfaces"])
        self.assertEqual("do-not-retry", collision_failure["retry"])

        with (
            patch.object(
                SQLiteWorkStore, "accept_artifact_reference", side_effect=AssertionError("programming defect")
            ),
            self.assertRaisesRegex(AssertionError, "programming defect"),
        ):
            self.run_cli(*arguments(action(), "assertion-must-propagate"))

    def test_installed_dispatch_preserves_review_publication_when_supplied_prompt_is_not_canonical(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        prompt_path = project / "prompt.txt"
        prompt_path.write_text("not the canonical worker prompt\n", encoding="utf-8")
        selected = action()
        before = store.validated_snapshot()

        result, stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--subject-revision",
            selected.capability.subject_revision,
            "--task-id",
            "project-task",
            "--host-id",
            "host-a",
            "--checkpoint",
            CHECKPOINT_ID,
            "--environment",
            str(environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "noncanonical-prompt-review",
            "--prompt",
            str(prompt_path),
            "--json",
        )

        self.assertEqual(14, result, stderr)
        self.assertEqual("", stderr)
        failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("DISPATCH_PROMPT_NOT_CANONICAL", failure["code"])
        self.assertEqual("committed-effect", failure["status"])
        self.assertEqual("do-not-retry", failure["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            failure["changed_surfaces"],
        )
        after = SQLiteWorkStore(roots.database_path).validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.artifact_references) + 1, len(after.artifact_references))
        self.assertTrue(any("brief-review" in reference.key for reference in after.artifact_references))

    def test_dispatch_recheck_ignores_an_unrelated_revision(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected_action = action()
        render_prompt = _render_dispatch_prompt

        def render_then_accept_unrelated_revision(
            brief: work_brief_models.WorkBrief,
            work_root: Path,
            attempt_path: Path,
            checkpoint: str,
            supplied_environment: DispatchEnvironment,
            accepted_review: bytes | None,
            supplied_prompt: bytes | None,
        ) -> DispatchResult[str]:
            rendered = render_prompt(
                brief,
                work_root,
                attempt_path,
                checkpoint,
                supplied_environment,
                accepted_review,
                supplied_prompt,
            )
            expect_success(
                store.accept_artifact_reference(
                    roots.work_root,
                    write_revision(
                        roots,
                        NewArtifact(
                            work_models.ArtifactKind.EVIDENCE,
                            "unrelated-dispatch-revision",
                            1,
                            ".json",
                            b"{}\n",
                        ),
                    ),
                    datetime.now(UTC),
                )
            )
            return rendered

        with patch(
            "pinboard.interfaces.dispatch_brief._render_dispatch_prompt",
            side_effect=render_then_accept_unrelated_revision,
        ):
            result = prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                selected_action,
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("raced-review")),
                correction_history_id=None,
            )

        self.assertIsInstance(result, str)

    def test_dispatch_ignores_an_unrelated_revision_before_review_publication(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected_action = action()
        self.assertTrue(selected_action.capability.subject_revision)
        accept_reference = store.accept_artifact_reference

        def accept_after_unrelated_revision(
            work_root: Path,
            published: ArtifactRef,
            accepted_at: datetime,
        ) -> DecisionResult[ArtifactReferenceAcceptance]:
            expect_success(
                accept_reference(
                    work_root,
                    write_revision(
                        roots,
                        NewArtifact(
                            work_models.ArtifactKind.EVIDENCE,
                            "prepublication-unrelated-revision",
                            1,
                            ".json",
                            b"{}\n",
                        ),
                    ),
                    accepted_at,
                )
            )
            return accept_reference(work_root, published, accepted_at)

        with patch.object(store, "accept_artifact_reference", side_effect=accept_after_unrelated_revision):
            result = prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                selected_action,
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("prepublication-race")),
                correction_history_id=None,
            )

        self.assertEqual(15, store.validated_snapshot().lifecycle.project.revision)
        self.assertIsInstance(result, str)

    def test_sqlite_dispatch_rejects_stale_action_and_cli_verifies_prompt(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        stale_selected = dataclass_replace(
            selected,
            capability=dataclass_replace(selected.capability, subject_revision="stale"),
        )
        stale = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            project,
            stale_selected,
            CHECKPOINT_ID,
            environment,
            supplied_prompt=None,
            supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("review-id")),
            correction_history_id=None,
        )
        expect_dispatch_failure(stale, DispatchErrorCode.STALE_ACTION)

        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        arguments = (
            *common,
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--subject-revision",
            selected.capability.subject_revision,
            "--task-id",
            "project-task",
            "--host-id",
            "host-a",
            "--checkpoint",
            CHECKPOINT_ID,
            "--environment",
            str(environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "cli-review",
        )
        result, ready_stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, stderr)
        ready = json.loads(ready_stdout)
        prompt_reference = ready["prompt_reference"]
        prompt = (roots.work_root / prompt_reference["selector"]).read_text(encoding="utf-8")
        prompt_path = project / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        verify_arguments = list(arguments)
        verify_arguments.extend(("--prompt", str(prompt_path)))
        result, stdout, stderr = self.run_cli(*verify_arguments)
        self.assertEqual(0, result, stderr)
        self.assertIn("immutable worker prompt", stdout)

    def test_fresh_agent_verifies_the_accepted_prompt_reference_and_bytes(self) -> None:
        project, roots, _store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        selected = action()
        result, stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--subject-revision",
            selected.capability.subject_revision,
            "--task-id",
            "project-task",
            "--host-id",
            "local",
            "--checkpoint",
            CHECKPOINT_ID,
            "--environment",
            str(environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "verified-prompt",
            "--json",
        )
        self.assertEqual(0, result, stderr)
        ready = json.loads(stdout)
        reference = ready["prompt_reference"]
        common = (
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "artifact",
            "verify",
            "--artifact-ref-id",
            str(reference["accepted_artifact_reference_id"]),
            "--selector",
            str(reference["selector"]),
            "--sha256",
            str(reference["sha256"]),
            "--size-bytes",
            str(reference["size_bytes"]),
            "--json",
        )
        verify_result, verify_stdout, verify_stderr = self.run_cli(*common)
        self.assertEqual(0, verify_result, verify_stderr)
        verified = json.loads(verify_stdout)
        self.assertEqual("pinboard-verified-artifact-reference/v1", verified["schema"])
        self.assertEqual(reference["accepted_artifact_reference_id"], verified["artifact_ref_id"])
        self.assertTrue(verified["verified"])
        launch_message = str(ready["native_launch"]["message"])
        verification_command = launch_message.split("run exactly: ", 1)[1].split(". Require", 1)[0]
        fresh_environment = os.environ.copy()
        fresh_environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        launched = subprocess.run(
            ["/bin/sh", "-c", verification_command],
            cwd=project,
            env=fresh_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, launched.returncode, launched.stderr or launched.stdout)
        self.assertEqual("pinboard-verified-artifact-reference/v1", json.loads(launched.stdout)["schema"])

        substitutions = {
            "--artifact-ref-id": str(int(reference["accepted_artifact_reference_id"]) + 1000),
            "--selector": f"{reference['selector']}.copy",
            "--sha256": "0" * 64,
            "--size-bytes": str(int(reference["size_bytes"]) + 1),
        }
        for flag, replacement in substitutions.items():
            with self.subTest(flag=flag):
                altered = list(common)
                altered[altered.index(flag) + 1] = replacement
                rejected_result, rejected_stdout, rejected_stderr = self.run_cli(*altered)
                self.assertEqual(11, rejected_result, rejected_stderr)
                rejected = json.loads(rejected_stdout)
                self.assertEqual("ARTIFACT_REFERENCE_MISMATCH", rejected["code"])
                self.assertEqual("rejected", rejected["status"])
                self.assertFalse(rejected["state_changed"])
                self.assertEqual("correct-input", rejected["retry"])

        prompt_path = roots.work_root / str(reference["selector"])
        original = prompt_path.read_bytes()
        prompt_path.write_bytes(b"altered" + original)
        rejected_result, rejected_stdout, rejected_stderr = self.run_cli(*common)
        self.assertEqual(11, rejected_result, rejected_stderr)
        rejected = json.loads(rejected_stdout)
        self.assertEqual("ARTIFACT_REFERENCE_MISMATCH", rejected["code"])
        self.assertEqual("rejected", rejected["status"])
        self.assertFalse(rejected["state_changed"])

    def test_reused_prompt_failure_preserves_prior_review_publication_effects(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        reference = store.validated_snapshot().artifact_references[0]
        rendered = expect_dispatch_success(
            _render_dispatch_prompt(
                value,
                roots.work_root,
                roots.work_root / reference.selector,
                CHECKPOINT_ID,
                environment,
                ready_review(value),
                None,
            )
        )
        prompt_bytes = rendered.encode()
        write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                f"{value.attempt_id}-worker-prompt-{sha256(prompt_bytes).hexdigest()}",
                1,
                ".txt",
                prompt_bytes,
            ),
        )
        accept_reference = store.accept_artifact_reference
        acceptance_calls = 0

        def reject_reused_prompt(
            work_root: Path,
            published: ArtifactRef,
            accepted_at: datetime,
        ) -> DecisionResult[ArtifactReferenceAcceptance]:
            nonlocal acceptance_calls
            acceptance_calls += 1
            if acceptance_calls == 2:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Prompt reference changed before acceptance.",
                    None,
                )
            return accept_reference(work_root, published, accepted_at)

        with patch.object(store, "accept_artifact_reference", side_effect=reject_reused_prompt):
            failed = prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("prior-review-effect")),
                correction_history_id=None,
            )

        failure = expect_dispatch_failure(failed, DispatchErrorCode.STALE_ACTION)
        self.assertIsNotNone(failure.details)
        assert failure.details is not None
        self.assertEqual("committed", failure.details.effect.value)
        self.assertEqual("do-not-retry", failure.details.retry.value)
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            [surface.value for surface in failure.details.changed_surfaces],
        )

    def test_installed_dispatch_preserves_prior_publication_effects_on_later_failures(  # noqa: PLR0915
        self,
    ) -> None:
        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        reference = store.validated_snapshot().artifact_references[0]
        rendered = expect_dispatch_success(
            _render_dispatch_prompt(
                value,
                roots.work_root,
                roots.work_root / reference.selector,
                CHECKPOINT_ID,
                environment,
                ready_review(value),
                None,
            )
        )
        prompt_bytes = rendered.encode()
        write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                f"{value.attempt_id}-worker-prompt-{sha256(prompt_bytes).hexdigest()}",
                1,
                ".txt",
                prompt_bytes,
            ),
        )
        common = (
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "dispatch",
        )

        def arguments(
            selected: decision_models.DispatchAction,
            *,
            publish_review: bool,
            review_id: str = "reused-prompt-storage-review",
        ) -> tuple[str, ...]:
            review_arguments = (
                (
                    "--brief-review",
                    str(review_path),
                    "--review-id",
                    review_id,
                )
                if publish_review
                else ()
            )
            return (
                *common,
                "--action-id",
                str(decision_models.action_id(selected)),
                "--subject-revision",
                selected.capability.subject_revision,
                "--task-id",
                "project-task",
                "--host-id",
                "host-a",
                "--checkpoint",
                CHECKPOINT_ID,
                "--environment",
                str(environment_path),
                *review_arguments,
                "--json",
            )

        database_failure = StorageError(StorageErrorCode.BUSY, "database failed", retryable=True)
        accept_reference = SQLiteWorkStore.accept_artifact_reference
        acceptance_calls = 0

        def fail_reused_prompt_acceptance(
            selected_store: SQLiteWorkStore,
            work_root: Path,
            published: ArtifactRef,
            accepted_at: datetime,
        ) -> DecisionResult[ArtifactReferenceAcceptance]:
            nonlocal acceptance_calls
            acceptance_calls += 1
            if acceptance_calls == 2:
                raise database_failure
            return accept_reference(selected_store, work_root, published, accepted_at)

        with patch.object(
            SQLiteWorkStore,
            "accept_artifact_reference",
            autospec=True,
            side_effect=fail_reused_prompt_acceptance,
        ):
            result, stdout, stderr = self.run_cli(*arguments(action(), publish_review=True))
        self.assertEqual(12, result, stderr)
        with_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", with_prior_effect["status"])
        self.assertEqual("do-not-retry", with_prior_effect["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            with_prior_effect["changed_surfaces"],
        )

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            result, stdout, stderr = self.run_cli(*arguments(action(), publish_review=False))
        self.assertEqual(12, result, stderr)
        without_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("rejected", without_prior_effect["status"])
        self.assertEqual("retry-same-input", without_prior_effect["retry"])
        self.assertEqual([], without_prior_effect["changed_surfaces"])

        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root), "dispatch")
        publish = ArtifactRepository.publish

        def fail_worker_prompt(repository: ArtifactRepository, artifact: NewArtifact) -> ArtifactPublication:
            if "-worker-prompt-" in artifact.key:
                raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, "prompt publication failed")
            return publish(repository, artifact)

        before = store.validated_snapshot()
        with patch.object(ArtifactRepository, "publish", autospec=True, side_effect=fail_worker_prompt):
            result, stdout, stderr = self.run_cli(
                *arguments(action(), publish_review=True, review_id="prompt-artifact-error-review")
            )
        self.assertEqual(12, result, stderr)
        with_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", with_prior_effect["status"])
        self.assertEqual("do-not-retry", with_prior_effect["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            with_prior_effect["changed_surfaces"],
        )
        after = SQLiteWorkStore(roots.database_path).validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.artifact_references) + 1, len(after.artifact_references))

        with patch.object(ArtifactRepository, "publish", autospec=True, side_effect=fail_worker_prompt):
            result, stdout, stderr = self.run_cli(*arguments(action(), publish_review=False))
        self.assertEqual(12, result, stderr)
        without_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("rejected", without_prior_effect["status"])
        self.assertEqual("do-not-retry", without_prior_effect["retry"])
        self.assertEqual([], without_prior_effect["changed_surfaces"])

        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root), "dispatch")
        read_artifact = ArtifactRepository.read

        def fail_accepted_review_read(
            repository: ArtifactRepository,
            reference: stored_state.ArtifactReference | BriefArtifactRef,
        ) -> bytes:
            if "-brief-review-" in reference.key:
                raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, "review read failed")
            return read_artifact(repository, reference)

        before = store.validated_snapshot()
        before_files = {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()}
        with patch.object(ArtifactRepository, "read", autospec=True, side_effect=fail_accepted_review_read):
            result, stdout, stderr = self.run_cli(
                *arguments(action(), publish_review=True, review_id="review-read-failure")
            )
        self.assertEqual(12, result, stderr)
        with_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", with_prior_effect["status"])
        self.assertEqual("do-not-retry", with_prior_effect["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            with_prior_effect["changed_surfaces"],
        )
        after = SQLiteWorkStore(roots.database_path).validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.artifact_references) + 1, len(after.artifact_references))
        after_files = {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()}
        self.assertEqual(len(before_files) + 1, len(after_files))

        with patch.object(ArtifactRepository, "read", autospec=True, side_effect=fail_accepted_review_read):
            result, stdout, stderr = self.run_cli(*arguments(action(), publish_review=False))
        self.assertEqual(12, result, stderr)
        without_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("rejected", without_prior_effect["status"])
        self.assertEqual("do-not-retry", without_prior_effect["retry"])
        self.assertEqual([], without_prior_effect["changed_surfaces"])
        self.assertEqual(after, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(
            after_files,
            {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()},
        )

        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root), "dispatch")
        authority_failure = StorageError(StorageErrorCode.BUSY, "authority recheck failed", retryable=True)

        before = store.validated_snapshot()
        before_files = {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()}
        with patch("pinboard.interfaces.dispatch_brief.recheck_dispatch_authority", side_effect=authority_failure):
            result, stdout, stderr = self.run_cli(
                *arguments(action(), publish_review=True, review_id="authority-recheck-failure")
            )
        self.assertEqual(12, result, stderr)
        with_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", with_prior_effect["status"])
        self.assertEqual("do-not-retry", with_prior_effect["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            with_prior_effect["changed_surfaces"],
        )
        after = SQLiteWorkStore(roots.database_path).validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 2, after.lifecycle.project.revision)
        self.assertEqual(len(before.artifact_references) + 2, len(after.artifact_references))
        after_files = {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()}
        self.assertEqual(len(before_files) + 2, len(after_files))

        with patch("pinboard.interfaces.dispatch_brief.recheck_dispatch_authority", side_effect=authority_failure):
            result, stdout, stderr = self.run_cli(*arguments(action(), publish_review=False))
        self.assertEqual(12, result, stderr)
        without_prior_effect = msgspec.json.decode(stdout.encode())
        self.assertEqual("rejected", without_prior_effect["status"])
        self.assertEqual("retry-same-input", without_prior_effect["retry"])
        self.assertEqual([], without_prior_effect["changed_surfaces"])
        self.assertEqual(after, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(
            after_files,
            {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()},
        )

    def test_cli_dispatch_revalidates_the_linked_source_checkout_against_the_shared_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            linked = root / "linked"
            repository.mkdir()
            self.run_git(repository, "init", "-b", "main")
            (repository / "architecture.md").write_text(
                "# Architecture\n\n## Contract\n\nTyped JSON is canonical.\n",
                encoding="utf-8",
            )
            self.run_git(repository, "add", "architecture.md")
            self.run_git(
                repository,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "initial",
            )
            self.run_git(repository, "worktree", "add", "-b", "codex/work-a", str(linked))
            _, roots, _, value, action, environment = self.initialized(linked, resolve_durable_roots(repository))
            (repository / "architecture.md").write_text(
                "# Architecture\n\n## Contract\n\nDirty primary authority.\n",
                encoding="utf-8",
            )
            environment_path = linked / "environment.json"
            environment_path.write_bytes(msgspec.json.encode(environment, enc_hook=dispatch_environment_enc_hook))
            review_path = linked / "review.json"
            review_path.write_bytes(ready_review(value))
            selected = action()

            result, ready_stdout, stderr = self.run_cli(
                "--project-root",
                str(linked),
                "dispatch",
                "--action-id",
                str(decision_models.action_id(selected)),
                "--subject-revision",
                selected.capability.subject_revision,
                "--task-id",
                "project-task",
                "--host-id",
                "host-a",
                "--checkpoint",
                CHECKPOINT_ID,
                "--environment",
                str(environment_path),
                "--brief-review",
                str(review_path),
                "--review-id",
                "linked-review",
                "--json",
            )
            ready = json.loads(ready_stdout)
            prompt = (roots.work_root / ready["prompt_reference"]["selector"]).read_text(encoding="utf-8")
            shared_database_exists = (roots.work_root / "state.sqlite3").is_file()
            duplicate_ledger_exists = (linked / ".pinboard").exists()
            linked_checkout = str(linked)

        self.assertEqual(0, result, stderr)
        self.assertIn(f"Checkout: {linked_checkout}", prompt)
        self.assertTrue(shared_database_exists)
        self.assertFalse(duplicate_ledger_exists)

    def test_dispatch_environment_is_strict(self) -> None:  # noqa: PLR0915 - one boundary matrix
        contract_result, contract_stdout, contract_stderr = self.run_cli(
            "tool-contract", "--operation", "dispatch:without-review", "--json"
        )
        self.assertEqual(0, contract_result, contract_stderr)
        contract = json.loads(contract_stdout)
        schema = contract["artifact_schema"]["$defs"]["DispatchEnvironment"]
        self.assertEqual({"const": True, "type": "boolean"}, schema["properties"]["fresh_context"])
        project = Path(tempfile.mkdtemp()).resolve()
        path = project / "environment.json"
        path.write_text(
            '{"schema":"pinboard-dispatch/v1","checkout":"x","branch":"b","starting_revision":"r",'
            '"host_id":"local","fresh_context":true,"lease_ttl_seconds":60,"permissions":[]}',
            encoding="utf-8",
        )
        schema_failure = expect_dispatch_failure(
            read_dispatch_environment(path),
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
        )
        self.assertIsNotNone(schema_failure.details)
        assert schema_failure.details is not None
        self.assertEqual("pinboard-dispatch/v1", schema_failure.details.observed[0].value)
        self.assertEqual("pinboard-dispatch/v2", schema_failure.details.mismatches[0].expected)
        path.write_text(
            '{"schema":"pinboard-dispatch/v2","checkout":"x","branch":"b\\n","starting_revision":"r",'
            '"host_id":"local","fresh_context":true,"lease_ttl_seconds":60,"permissions":[]}',
            encoding="utf-8",
        )
        expect_dispatch_failure(
            read_dispatch_environment(path),
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
        )
        path.write_text(
            '{"schema":"pinboard-dispatch/v2","checkout":"x","branch":"b","starting_revision":"r",'
            '"host_id":"local","fresh_context":false,"lease_ttl_seconds":60,"permissions":[]}',
            encoding="utf-8",
        )
        expect_dispatch_failure(
            read_dispatch_environment(path),
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
        )

        project, roots, _store, brief, action, _environment = self.initialized()
        selected = action()
        bad_environment = project / "stale-environment.json"
        bad_environment.write_text(
            '{"schema":"pinboard-dispatch/v1","checkout":"x","branch":"b","starting_revision":"r",'
            '"host_id":"local","fresh_context":true,"lease_ttl_seconds":60,"permissions":[]}',
            encoding="utf-8",
        )
        review_path = project / "strict-environment-review.json"
        review_path.write_bytes(ready_review(brief))
        result, stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--subject-revision",
            selected.capability.subject_revision,
            "--task-id",
            "project-task",
            "--host-id",
            "local",
            "--checkpoint",
            CHECKPOINT_ID,
            "--environment",
            str(bad_environment),
            "--brief-review",
            str(review_path),
            "--review-id",
            "strict-environment-review",
            "--json",
        )
        self.assertEqual(14, result, stderr)
        rejection = json.loads(stdout)
        self.assertEqual(
            [{"kind": "command", "command": "pinboard tool-contract --operation dispatch:with-review --json"}],
            rejection["next_actions"],
        )
        missing_environment = project / "missing-environment.json"
        unreadable = expect_dispatch_failure(
            read_dispatch_environment(missing_environment),
            DispatchErrorCode.DISPATCH_ENVIRONMENT_UNREADABLE,
        )
        self.assertTrue(unreadable.message.startswith(f"Cannot read '{missing_environment}': "))

        project, roots, _store, _value, action, _environment = self.initialized()
        selected = action()
        path = project / "invalid-environment.json"
        path.write_text(
            '{"schema":"pinboard-dispatch/v2","checkout":"x","branch":"b","starting_revision":"r","permissions":[]}',
            encoding="utf-8",
        )
        base_arguments = (
            "--project-root",
            str(project),
            "--work-root",
            str(roots.work_root),
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--subject-revision",
            selected.capability.subject_revision,
            "--task-id",
            "project-task",
            "--host-id",
            "host-a",
            "--checkpoint",
            CHECKPOINT_ID,
        )
        result, stdout, stderr = self.run_cli(
            *base_arguments,
            "--environment",
            str(path),
        )
        self.assertEqual(14, result)
        self.assertEqual("", stdout)
        self.assertTrue(stderr.startswith("DISPATCH_ENVIRONMENT_INVALID: Cannot decode dispatch environment: "))

        valid_environment = project / "valid-environment.json"
        valid_environment.write_bytes(
            msgspec.json.encode(self.environment(project), enc_hook=dispatch_environment_enc_hook)
        )
        missing_prompt = project / "missing-prompt.txt"
        result, stdout, stderr = self.run_cli(
            *base_arguments,
            "--environment",
            str(valid_environment),
            "--prompt",
            str(missing_prompt),
        )
        self.assertEqual(14, result)
        self.assertEqual("", stdout)
        self.assertTrue(stderr.startswith(f"DISPATCH_PROMPT_UNREADABLE: Cannot read '{missing_prompt}': "))

        missing_review = project / "missing-review.json"
        result, stdout, stderr = self.run_cli(
            *base_arguments,
            "--environment",
            str(valid_environment),
            "--brief-review",
            str(missing_review),
            "--review-id",
            "missing-review",
        )
        self.assertEqual(14, result)
        self.assertEqual("", stdout)
        self.assertTrue(stderr.startswith(f"DISPATCH_BRIEF_REVIEW_INVALID: Cannot read '{missing_review}': "))


if __name__ == "__main__":
    unittest.main()
