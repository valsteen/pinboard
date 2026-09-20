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
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from msgspec.structs import replace

from pinboard.adapters import dispatch_operations as dispatch_brief
from pinboard.adapters.dispatch_operations import (
    DispatchErrorCode,
    DispatchFailure,
    DispatchResult,
    ReviewedDispatch,
    _read_dispatch_brief,
    _render_dispatch_prompt,
    prepare_dispatch,
)
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.files.root import classify_checkout
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state, work_brief_models
from pinboard.application.artifacts import ArtifactPublication, ArtifactRef, BriefArtifactRef, NewArtifact
from pinboard.application.dispatch_models import (
    FRESH_CONTEXT_REQUIRED,
    DispatchEnvironment,
    DispatchPermission,
    FreshContextRequired,
)
from pinboard.application.ports import ArtifactReferenceAcceptance
from pinboard.application.work_briefs import (
    canonical_work_brief_bytes,
    canonical_work_brief_review_bytes,
    decode_work_brief_review,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import HostId, ReviewId
from pinboard.mcp import server as mcp_server
from tests.artifact_support import write_revision
from tests.decision_support import discover_actions
from tests.domain_support import expect_success
from tests.native_support import call_native_tool
from tests.support import SQLITE_DIGEST, SQLITE_NOW, JsonObject, complete_sqlite_state, initialize_store
from tests.work_brief_support import CHECKPOINT_ID, needs_correction_review, ready_review, work_a_brief


def dispatch_environment_enc_hook(value: FreshContextRequired) -> bool:
    if isinstance(value, FreshContextRequired):
        return True
    raise TypeError(f"unsupported dispatch environment value: {value!r}")


def expect_dispatch_success[T](result: DispatchResult[T]) -> T:
    if isinstance(result, DispatchFailure):
        raise AssertionError(str(result))
    return result


def supplied_review(content: bytes, review_id: ReviewId) -> ReviewedDispatch:
    review = decode_work_brief_review(content)
    if isinstance(review, work_brief_models.WorkBriefFailure):
        raise AssertionError(review.message)
    if not isinstance(review, work_brief_models.WorkBriefReview):
        raise AssertionError("Current dispatch fixtures require current ready-review evidence.")
    return ReviewedDispatch(review, review_id)


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
        validate_original_authorities=True,
    )
    if isinstance(brief, DispatchFailure):
        return brief
    return _render_dispatch_prompt(
        brief,
        attempt_path.read_bytes(),
        attempt_path.parent,
        attempt_path,
        checkpoint,
        environment,
        accepted_review,
        supplied_prompt,
    )


class DispatchTest(unittest.TestCase):
    def dispatch_choice(
        self,
        selected: decision_models.DispatchAction,
        environment: DispatchEnvironment,
        review: bytes | None,
        review_id: str,
        prompt: str | None,
    ) -> JsonObject:
        choice: JsonObject = {
            "kind": "ordinary" if review is None else "reviewed",
            "receipt": {
                "action_id": {"kind": "dispatch", "subject": str(selected.capability.subject)},
                "subject_revision": selected.capability.subject_revision,
            },
            "checkpoint_id": CHECKPOINT_ID,
            "environment": msgspec.to_builtins(environment, enc_hook=dispatch_environment_enc_hook),
            "prompt": prompt,
        }
        if review is not None:
            selected_review = msgspec.json.decode(review, type=work_brief_models.WorkBriefReview)
            choice.update(brief_review=msgspec.to_builtins(selected_review), review_id=review_id)
        return choice

    def native_dispatch(self, project: Path, roots: DurableRoots, choice: JsonObject) -> JsonObject:
        return call_native_tool(
            mcp_server.DISPATCH_TOOL,
            {
                "project_root": str(project),
                "work_root": str(roots.work_root),
                "dispatch": choice,
            },
        )

    def environment(self, project: Path) -> DispatchEnvironment:
        return DispatchEnvironment(
            "pinboard-dispatch/v2",
            "codex",
            False,
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
        brief_content: Callable[[work_brief_models.WorkBrief], bytes] | None = None,
    ) -> tuple[
        Path,
        DurableRoots,
        SQLiteWorkStore,
        work_brief_models.WorkBrief,
        Callable[[], decision_models.DispatchAction],
        DispatchEnvironment,
    ]:
        if project is None:
            project = Path(tempfile.mkdtemp()).resolve()
        if not (project / ".git").exists():
            self.run_git(project, "init", "-q")
        roots = resolve_durable_roots(project) if roots is None else roots
        initialize_database(roots, SQLITE_NOW)
        brief = replace(work_a_brief(project), checkout_selection=classify_checkout(project))
        published = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                brief.attempt_id,
                brief.artifact_revision,
                ".json",
                canonical_work_brief_bytes(brief) if brief_content is None else brief_content(brief),
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

    def test_dispatch_rejects_legacy_and_checkout_mismatch_unchanged(self) -> None:
        def legacy_bytes(brief: work_brief_models.WorkBrief) -> bytes:
            payload = msgspec.to_builtins(brief)
            assert isinstance(payload, dict)
            payload["schema"] = "pinboard-work-brief/v2"
            checkpoint = payload["checkpoint"]
            assert isinstance(checkpoint, dict)
            disposition = checkpoint.pop("disposition")
            assert isinstance(disposition, dict)
            payload["remaining_work"] = disposition["remaining_work"]
            del payload["checkout_selection"]
            del payload["obligation_correspondence"]
            return msgspec.json.encode(payload, order="sorted") + b"\n"

        def mismatch_bytes(brief: work_brief_models.WorkBrief) -> bytes:
            selection = (
                work_models.CheckoutSelection.ISOLATED
                if brief.checkout_selection == work_models.CheckoutSelection.MAIN
                else work_models.CheckoutSelection.MAIN
            )
            return canonical_work_brief_bytes(replace(brief, checkout_selection=selection))

        for invalidity, content in (("legacy", legacy_bytes), ("checkout-mismatch", mismatch_bytes)):
            with self.subTest(invalidity=invalidity):
                project, roots, store, _brief, action, environment = self.initialized(brief_content=content)
                before = store.validated_snapshot()
                failure = expect_dispatch_failure(
                    prepare_dispatch(
                        store,
                        ArtifactRepository(roots),
                        project,
                        action(),
                        CHECKPOINT_ID,
                        environment,
                        supplied_prompt=None,
                        choice=dispatch_brief.OrdinaryDispatch(),
                    ),
                    DispatchErrorCode.DISPATCH_BRIEF_INVALID,
                )
                if invalidity == "legacy":
                    self.assertIn("Retained work brief", failure.message)
                else:
                    self.assertIn("checkout", failure.message)
                self.assertEqual(before, store.validated_snapshot())

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
        self.assertIn(canonical_work_brief_bytes(value).decode(), prompt)
        self.assertIn("- Fresh context: required", prompt)
        self.assertIn("- Runtime host: local", prompt)
        self.assertIn(f"- Result: {project / 'attempts' / value.attempt_id / 'result.md'}", prompt)
        self.assertIn(f"- Blocker: {project / 'attempts' / value.attempt_id / 'blocker.md'}", prompt)
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

        with patch("pinboard.adapters.dispatch_operations.datetime") as clock:
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
                    choice=supplied_review(ready_review(brief), ReviewId("timed-review")),
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
                    self.assertNotIn("tool_contract_command", observed)
                    self.assertNotIn("current_dispatch_action_command", observed)

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
            ({"accepted_brief_sha256": "f" * 64}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE),
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

    def test_base_mismatch_reports_each_stale_source(self) -> None:
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
            cross.disposition,
            cross.acceptance_criteria,
            cross.verification,
            cross.deferrals,
        )
        value = replace(
            value,
            checkpoint=local,
            obligation_correspondence=(
                work_brief_models.ObligationCorrespondence(
                    "next-decision",
                    work_brief_models.CriterionObligationTarget(local.acceptance_criteria[0].number),
                ),
            ),
        )
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
                    choice=supplied_review(first_review, ReviewId("first-review")),
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
        self.assertEqual(
            f"{value.attempt_id}-brief-review-{sha256(canonical_work_brief_bytes(value)).hexdigest()}",
            ready[0].key,
        )

        reused = expect_dispatch_success(
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                supplied_prompt=None,
                choice=dispatch_brief.OrdinaryDispatch(),
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
                choice=supplied_review(first_review, ReviewId("identical-review")),
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
            choice=supplied_review(
                ready_review(value, result="Different complete result."),
                ReviewId("later-review"),
            ),
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
            choice=supplied_review(
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
        self.assertEqual(before_identical_retry, store.validated_snapshot())

    def test_native_dispatch_reports_new_ready_and_collision_artifacts_after_database_failure(self) -> None:
        project, roots, store, value, action, environment = self.initialized()

        review_bytes = ready_review(value)

        def choice(selected: decision_models.DispatchAction, review_id: str) -> JsonObject:
            return self.dispatch_choice(selected, environment, review_bytes, review_id, None)

        database_failure = StorageError(StorageErrorCode.BUSY, "database failed", retryable=True)
        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            ready_failure = self.native_dispatch(project, roots, choice(action(), "new-ready"))
        self.assertEqual("failed-after-publication", ready_failure["status"])
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
            prompt_failure = self.native_dispatch(project, roots, choice(action(), "prompt-acceptance-failure"))
        self.assertEqual("failed-after-publication", prompt_failure["status"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
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
                choice=supplied_review(ready_review(value), ReviewId("accepted-ready")),
            )
        )
        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", accepted)
        review_bytes = ready_review(value, result="Different complete result.")

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            collision_failure = self.native_dispatch(project, roots, choice(action(), "new-collision"))
        self.assertEqual("failed-after-publication", collision_failure["status"])
        self.assertEqual(["immutable-artifact"], collision_failure["changed_surfaces"])
        self.assertEqual("do-not-retry", collision_failure["retry"])

        with (
            patch.object(
                SQLiteWorkStore, "accept_artifact_reference", side_effect=AssertionError("programming defect")
            ),
            self.assertRaises(UnexpectedToolError) as raised,
        ):
            self.native_dispatch(project, roots, choice(action(), "assertion-must-propagate"))
        self.assertIsInstance(raised.exception.__cause__, AssertionError)

    def test_native_dispatch_preserves_review_publication_when_supplied_prompt_is_not_canonical(self) -> None:
        project, roots, store, value, action, environment = self.initialized()

        review_bytes = ready_review(value)
        prompt_text = "not the canonical worker prompt\n"
        selected = action()
        before = store.validated_snapshot()

        failure = self.native_dispatch(
            project,
            roots,
            self.dispatch_choice(
                selected,
                environment,
                review_bytes,
                "noncanonical-prompt-review",
                prompt_text,
            ),
        )
        self.assertEqual("DISPATCH_PROMPT_NOT_CANONICAL", failure["code"])
        self.assertEqual("failed-after-publication", failure["status"])
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
            accepted_brief_bytes: bytes,
            work_root: Path,
            attempt_path: Path,
            checkpoint: str,
            supplied_environment: DispatchEnvironment,
            accepted_review: bytes | None,
            supplied_prompt: bytes | None,
        ) -> DispatchResult[str]:
            rendered = render_prompt(
                brief,
                accepted_brief_bytes,
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
            "pinboard.adapters.dispatch_operations._render_dispatch_prompt",
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
                choice=supplied_review(ready_review(value), ReviewId("raced-review")),
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
                choice=supplied_review(ready_review(value), ReviewId("prepublication-race")),
            )

        self.assertEqual(15, store.validated_snapshot().lifecycle.project.revision)
        self.assertIsInstance(result, str)

    def test_sqlite_dispatch_rejects_stale_action_and_native_verifies_prompt(self) -> None:
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
            choice=supplied_review(ready_review(value), ReviewId("review-id")),
        )
        expect_dispatch_failure(stale, DispatchErrorCode.STALE_ACTION)

        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        ready = self.native_dispatch(
            project,
            roots,
            self.dispatch_choice(
                selected,
                environment,
                ready_review(value),
                "native-review",
                None,
            ),
        )
        self.assertEqual("ready", ready["status"])
        reference = ready["prompt_reference"]
        assert isinstance(reference, dict)
        prompt = (roots.work_root / str(reference["selector"])).read_text()
        verified = self.native_dispatch(
            project,
            roots,
            self.dispatch_choice(
                action(),
                environment,
                ready_review(value),
                "native-review",
                prompt,
            ),
        )
        self.assertEqual("ready", verified["status"])
        verified_reference = verified["prompt_reference"]
        assert isinstance(verified_reference, dict)
        self.assertEqual(
            reference["accepted_artifact_reference_id"], verified_reference["accepted_artifact_reference_id"]
        )
        self.assertEqual(reference["selector"], verified_reference["selector"])
        self.assertEqual(reference["sha256"], verified_reference["sha256"])
        self.assertEqual(reference["size_bytes"], verified_reference["size_bytes"])
        self.assertFalse(verified_reference["artifact_created"])
        self.assertFalse(verified_reference["ledger_changed"])
        self.assertEqual([], verified["changed_surfaces"])

    def test_native_agent_verifies_exact_accepted_prompt_identity_and_bytes(self) -> None:
        project, roots, _store, value, action, environment = self.initialized()
        ready = self.native_dispatch(
            project,
            roots,
            self.dispatch_choice(
                action(),
                environment,
                ready_review(value),
                "verified-prompt",
                None,
            ),
        )
        self.assertEqual("ready", ready["status"])
        reference = ready["prompt_reference"]
        assert isinstance(reference, dict)
        verification: JsonObject = {
            "project_root": str(project),
            "work_root": str(roots.work_root),
            "artifact_ref_id": reference["accepted_artifact_reference_id"],
            "selector": reference["selector"],
            "sha256": reference["sha256"],
            "size_bytes": reference["size_bytes"],
        }
        before = SQLiteWorkStore(roots.database_path).validated_snapshot()
        verified = call_native_tool(mcp_server.ARTIFACT_VERIFY_TOOL, verification)
        self.assertEqual("pinboard-verified-artifact-reference/v1", verified["schema"])
        self.assertEqual(reference["accepted_artifact_reference_id"], verified["artifact_ref_id"])
        self.assertTrue(verified["verified"])
        substitutions: JsonObject = {
            "artifact_ref_id": int(str(reference["accepted_artifact_reference_id"])) + 1000,
            "selector": str(reference["selector"]) + ".copy",
            "sha256": "0" * 64,
            "size_bytes": int(str(reference["size_bytes"])) + 1,
        }
        for field, replacement in substitutions.items():
            with self.subTest(field=field):
                rejected = call_native_tool(mcp_server.ARTIFACT_VERIFY_TOOL, verification | {field: replacement})
                self.assertEqual("ARTIFACT_REFERENCE_MISMATCH", rejected["code"])
                self.assertEqual("rejected", rejected["status"])
                self.assertFalse(rejected["state_changed"])
                self.assertEqual("correct-input", rejected["retry"])
        prompt_path = roots.work_root / str(reference["selector"])
        prompt_path.write_bytes(b"altered" + prompt_path.read_bytes())
        rejected = call_native_tool(mcp_server.ARTIFACT_VERIFY_TOOL, verification)
        self.assertEqual("ARTIFACT_BYTES_INVALID", rejected["code"])
        self.assertEqual("do-not-retry", rejected["retry"])
        self.assertFalse(rejected["state_changed"])
        self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())

    def test_reused_prompt_failure_preserves_prior_review_publication_effects(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        reference = store.validated_snapshot().artifact_references[0]
        rendered = expect_dispatch_success(
            _render_dispatch_prompt(
                value,
                canonical_work_brief_bytes(value),
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
                choice=supplied_review(ready_review(value), ReviewId("prior-review-effect")),
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

    def test_native_dispatch_preserves_prior_publication_effects_on_later_failures(  # noqa: PLR0915
        self,
    ) -> None:
        project, roots, store, value, action, environment = self.initialized()

        review_bytes = ready_review(value)
        reference = store.validated_snapshot().artifact_references[0]
        rendered = expect_dispatch_success(
            _render_dispatch_prompt(
                value,
                canonical_work_brief_bytes(value),
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

        def choice(
            selected: decision_models.DispatchAction,
            *,
            publish_review: bool,
            review_id: str = "reused-prompt-storage-review",
        ) -> JsonObject:
            return self.dispatch_choice(
                selected,
                environment,
                review_bytes if publish_review else None,
                review_id,
                None,
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
            with_prior_effect = self.native_dispatch(project, roots, choice(action(), publish_review=True))
        self.assertEqual("failed-after-publication", with_prior_effect["status"])
        self.assertEqual("do-not-retry", with_prior_effect["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            with_prior_effect["changed_surfaces"],
        )

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            before_no_effect = SQLiteWorkStore(roots.database_path).validated_snapshot()
            with self.assertRaises(UnexpectedToolError) as raised:
                self.native_dispatch(project, roots, choice(action(), publish_review=False))
        self.assertIsInstance(raised.exception.__cause__, StorageError)
        self.assertEqual(before_no_effect, SQLiteWorkStore(roots.database_path).validated_snapshot())

        project, roots, store, value, action, environment = self.initialized()

        review_bytes = ready_review(value)
        publish = ArtifactRepository.publish

        def fail_worker_prompt(repository: ArtifactRepository, artifact: NewArtifact) -> ArtifactPublication:
            if "-worker-prompt-" in artifact.key:
                raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, "prompt publication failed")
            return publish(repository, artifact)

        before = store.validated_snapshot()
        with patch.object(ArtifactRepository, "publish", autospec=True, side_effect=fail_worker_prompt):
            with_prior_effect = self.native_dispatch(
                project, roots, choice(action(), publish_review=True, review_id="prompt-artifact-error-review")
            )
        self.assertEqual("failed-after-publication", with_prior_effect["status"])
        self.assertEqual("do-not-retry", with_prior_effect["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            with_prior_effect["changed_surfaces"],
        )
        after = SQLiteWorkStore(roots.database_path).validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.artifact_references) + 1, len(after.artifact_references))

        with patch.object(ArtifactRepository, "publish", autospec=True, side_effect=fail_worker_prompt):
            before_no_effect = SQLiteWorkStore(roots.database_path).validated_snapshot()
            with self.assertRaises(UnexpectedToolError) as raised:
                self.native_dispatch(project, roots, choice(action(), publish_review=False))
        self.assertIsInstance(raised.exception.__cause__, ArtifactError)
        self.assertEqual(before_no_effect, SQLiteWorkStore(roots.database_path).validated_snapshot())

        project, roots, store, value, action, environment = self.initialized()

        review_bytes = ready_review(value)

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
            with_prior_effect = self.native_dispatch(
                project, roots, choice(action(), publish_review=True, review_id="review-read-failure")
            )
        self.assertEqual("failed-after-publication", with_prior_effect["status"])
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
            before_no_effect = SQLiteWorkStore(roots.database_path).validated_snapshot()
            with self.assertRaises(UnexpectedToolError) as raised:
                self.native_dispatch(project, roots, choice(action(), publish_review=False))
        self.assertIsInstance(raised.exception.__cause__, ArtifactError)
        self.assertEqual(before_no_effect, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(after, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(
            after_files,
            {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()},
        )

        project, roots, store, value, action, environment = self.initialized()

        review_bytes = ready_review(value)

        authority_failure = StorageError(StorageErrorCode.BUSY, "authority recheck failed", retryable=True)

        before = store.validated_snapshot()
        before_files = {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()}
        with patch("pinboard.adapters.dispatch_operations.recheck_dispatch_authority", side_effect=authority_failure):
            with_prior_effect = self.native_dispatch(
                project, roots, choice(action(), publish_review=True, review_id="authority-recheck-failure")
            )
        self.assertEqual("failed-after-publication", with_prior_effect["status"])
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

        with patch("pinboard.adapters.dispatch_operations.recheck_dispatch_authority", side_effect=authority_failure):
            before_no_effect = SQLiteWorkStore(roots.database_path).validated_snapshot()
            with self.assertRaises(UnexpectedToolError) as raised:
                self.native_dispatch(project, roots, choice(action(), publish_review=False))
        self.assertIsInstance(raised.exception.__cause__, StorageError)
        self.assertEqual(before_no_effect, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(after, SQLiteWorkStore(roots.database_path).validated_snapshot())
        self.assertEqual(
            after_files,
            {path.relative_to(roots.work_root) for path in roots.artifacts_root.rglob("*") if path.is_file()},
        )

    def test_native_dispatch_revalidates_the_linked_source_checkout_against_the_shared_ledger(self) -> None:
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

            review_bytes = ready_review(value)
            selected = action()

            ready = self.native_dispatch(
                linked,
                roots,
                self.dispatch_choice(
                    selected,
                    environment,
                    review_bytes,
                    "linked-review",
                    None,
                ),
            )
            self.assertEqual("ready", ready["status"])
            reference = ready["prompt_reference"]
            assert isinstance(reference, dict)
            prompt = (roots.work_root / str(reference["selector"])).read_text()
            shared_database_exists = (roots.work_root / "state.sqlite3").is_file()
            duplicate_ledger_exists = (linked / ".pinboard").exists()
            linked_checkout = str(linked)

        self.assertIn(f"Checkout: {linked_checkout}", prompt)
        self.assertTrue(shared_database_exists)
        self.assertFalse(duplicate_ledger_exists)

    def test_native_dispatch_environment_rejects_invalid_shapes_before_effects(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        before = store.validated_snapshot()
        valid = self.dispatch_choice(action(), environment, ready_review(value), "strict-environment-review", None)
        environment_json = valid["environment"]
        assert isinstance(environment_json, dict)
        changes: tuple[JsonObject, ...] = (
            {"schema": "pinboard-dispatch/v1"},
            {"runtime": "unknown"},
            {"branch": "b\n"},
            {"fresh_context": False},
            {"unexpected": True},
        )
        for changed in changes:
            with (
                self.subTest(changed=changed),
                patch(
                    "pinboard.mcp.common.compose_store",
                    side_effect=AssertionError("invalid dispatch reached effects"),
                ),
            ):
                rejected = self.native_dispatch(project, roots, valid | {"environment": environment_json | changed})
                self.assertEqual("DISPATCH_INVALID", rejected["code"])
                self.assertFalse(rejected["state_changed"])
                self.assertEqual("unchanged", rejected["effect"])
                self.assertEqual([], rejected["changed_surfaces"])
            self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())


if __name__ == "__main__":
    unittest.main()
