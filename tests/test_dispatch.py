import contextlib
import io
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace

from pinboard.adapters.files.artifacts import ArtifactRepository, write_revision
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import ArtifactRef, NewArtifact
from pinboard.application.dispatch_models import DispatchEnvironment, DispatchPermission
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ReviewId
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
from tests.decision_support import discover_actions
from tests.domain_support import expect_success
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.work_brief_support import CHECKPOINT_ID, ready_review, work_a_brief


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
        checkpoint,
        environment,
        accepted_item_id,
        accepted_scope_revision,
        accepted_scope_digest,
    )
    if isinstance(brief, DispatchFailure):
        return brief
    return _render_dispatch_prompt(brief, attempt_path, checkpoint, environment, accepted_review, supplied_prompt)


class DispatchTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def environment(self, project: Path) -> DispatchEnvironment:
        return DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
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
                    store.snapshot(),
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

        self.assertTrue(prompt.startswith("Use $pinboard-deliver for this repository attempt.\n"))
        self.assertNotIn("$deliver", prompt)
        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        self.assertIn(f"Canonical brief: {path}", prompt)
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
        samples = (first, first + timedelta(microseconds=1), first + timedelta(microseconds=2))

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
                    supplied_review=SuppliedDispatchReview(ready_review(brief), ReviewId("timed-review")),
                )
            )

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        self.assertEqual(3, clock.now.call_count)

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
                expect_dispatch_failure(prepare_dispatch_from_artifact(**arguments), code)

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
                    supplied_review=SuppliedDispatchReview(first_review, ReviewId("first-review")),
                )
            )
        self.assertIs(store, select.call_args.args[0])
        self.assertIs(store, publish_review.call_args.args[0])
        self.assertIs(store, recheck.call_args.args[0])

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        after_first = store.snapshot()
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
            )
        )
        self.assertEqual(prompt, reused)
        self.assertEqual(after_first, store.snapshot())

        identical_retry = expect_dispatch_success(
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_review=SuppliedDispatchReview(first_review, ReviewId("identical-review")),
            )
        )
        self.assertEqual(prompt, identical_retry)
        self.assertEqual(after_first, store.snapshot())

        collision = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            project,
            action(),
            CHECKPOINT_ID,
            environment,
            supplied_review=SuppliedDispatchReview(
                ready_review(value, result="Different complete result."),
                ReviewId("later-review"),
            ),
        )
        expect_dispatch_failure(collision, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION)
        self.assertTrue(
            any("rejected-later-review" in reference.key for reference in store.snapshot().artifact_references)
        )
        before_identical_retry = store.snapshot()
        identical_retry = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            project,
            action(),
            CHECKPOINT_ID,
            environment,
            supplied_review=SuppliedDispatchReview(
                ready_review(value, result="Different complete result."),
                ReviewId("later-review"),
            ),
        )
        repeated_collision = expect_dispatch_failure(
            identical_retry,
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION,
        )
        assert repeated_collision.details is not None
        self.assertEqual("unchanged", repeated_collision.details.effect.value)
        self.assertEqual((), repeated_collision.details.changed_surfaces)
        self.assertEqual(before_identical_retry, store.snapshot())

    def test_installed_dispatch_reports_new_ready_and_collision_artifacts_after_database_failure(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))

        def arguments(selected: decision_models.DispatchAction, review_id: str) -> tuple[str, ...]:
            return (
                *common,
                "dispatch",
                "--action-id",
                str(decision_models.action_id(selected)),
                "--expected-revision",
                selected.capability.expected_revision,
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

        accepted = expect_dispatch_success(
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("accepted-ready")),
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

    def test_dispatch_rechecks_authority_after_an_unrelated_revision(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected_action = action()
        render_prompt = _render_dispatch_prompt

        def render_then_accept_unrelated_revision(
            brief: work_brief_models.WorkBrief,
            attempt_path: Path,
            checkpoint: str,
            supplied_environment: DispatchEnvironment,
            accepted_review: bytes | None,
            supplied_prompt: bytes | None,
        ) -> DispatchResult[str]:
            rendered = render_prompt(
                brief,
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
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("raced-review")),
            )

        expect_dispatch_failure(result, DispatchErrorCode.DISPATCH_ACTION_UNAVAILABLE)

    def test_dispatch_rejects_an_unrelated_revision_before_review_publication(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected_action = action()
        self.assertEqual("12", selected_action.capability.expected_revision)
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
                supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("prepublication-race")),
            )

        self.assertEqual(14, store.snapshot().lifecycle.project.revision)
        expect_dispatch_failure(result, DispatchErrorCode.DISPATCH_ACTION_UNAVAILABLE)

    def test_sqlite_dispatch_rejects_stale_action_and_cli_verifies_prompt(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        expect_success(
            store.accept_artifact_reference(
                roots.work_root,
                write_revision(
                    roots, NewArtifact(work_models.ArtifactKind.EVIDENCE, "revision-bump", 1, ".json", b"{}\n")
                ),
                datetime.now(UTC),
            )
        )
        stale = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            project,
            selected,
            CHECKPOINT_ID,
            environment,
            supplied_review=SuppliedDispatchReview(ready_review(value), ReviewId("review-id")),
        )
        expect_dispatch_failure(stale, DispatchErrorCode.STALE_ACTION)

        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        arguments = (
            *common,
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--expected-revision",
            selected.capability.expected_revision,
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
        result, prompt, stderr = self.run_cli(*arguments)
        self.assertEqual(0, result, stderr)
        prompt_path = project / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        refreshed = action()
        verify_arguments = list(arguments)
        verify_arguments[verify_arguments.index(selected.capability.expected_revision)] = (
            refreshed.capability.expected_revision
        )
        verify_arguments.extend(("--prompt", str(prompt_path)))
        result, stdout, stderr = self.run_cli(*verify_arguments)
        self.assertEqual(0, result, stderr)
        self.assertIn("DISPATCH_READY", stdout)

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
            environment_path.write_bytes(msgspec.json.encode(environment))
            review_path = linked / "review.json"
            review_path.write_bytes(ready_review(value))
            selected = action()

            result, prompt, stderr = self.run_cli(
                "--project-root",
                str(linked),
                "dispatch",
                "--action-id",
                str(decision_models.action_id(selected)),
                "--expected-revision",
                selected.capability.expected_revision,
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
            )
            shared_database_exists = (roots.work_root / "state.sqlite3").is_file()
            duplicate_ledger_exists = (linked / ".codex" / "pinboard").exists()
            linked_checkout = str(linked)

        self.assertEqual(0, result, stderr)
        self.assertIn(f"Checkout: {linked_checkout}", prompt)
        self.assertTrue(shared_database_exists)
        self.assertFalse(duplicate_ledger_exists)

    def test_dispatch_environment_is_strict(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        path = project / "environment.json"
        path.write_text(
            '{"schema":"pinboard-dispatch/v2","checkout":"x","branch":"b","starting_revision":"r","permissions":[]}',
            encoding="utf-8",
        )
        expect_dispatch_failure(
            read_dispatch_environment(path),
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
        )
        path.write_text(
            '{"schema":"pinboard-dispatch/v1","checkout":"x","branch":"b\\n","starting_revision":"r","permissions":[]}',
            encoding="utf-8",
        )
        expect_dispatch_failure(
            read_dispatch_environment(path),
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
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
            "--expected-revision",
            selected.capability.expected_revision,
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
        valid_environment.write_bytes(msgspec.json.encode(self.environment(project)))
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
