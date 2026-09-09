import contextlib
import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace

from pinboard.adapters.files.artifacts import ArtifactRepository, write_revision
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import translate_database_error
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HostId, ItemId, LeaseId, TaskId
from pinboard.interfaces import work_brief_models
from pinboard.interfaces.cli import main
from pinboard.interfaces.errors import WorkBriefErrorCode, WorkBriefFailure, WorkBriefResult
from pinboard.interfaces.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_reviewed_authority_set_bytes,
    canonical_work_brief_bytes,
    decode_work_brief,
    decode_work_brief_review,
    read_selected_work_brief_identity,
    render_work_brief_markdown,
    validate_reviewed_authority_digests,
    validate_work_brief_review,
)
from tests.support import SQLITE_NOW, complete_sqlite_state, decision_facts
from tests.work_brief_support import example_work_brief, work_a_brief, work_c_brief


def expect_work_brief_success[T](result: WorkBriefResult[T]) -> T:
    if isinstance(result, WorkBriefFailure):
        raise AssertionError(str(result))
    return result


def expect_work_brief_failure[T](result: WorkBriefResult[T], code: WorkBriefErrorCode) -> WorkBriefFailure:
    if not isinstance(result, WorkBriefFailure):
        raise AssertionError(f"Expected {code.value}, received success: {result!r}")
    if result.code != code:
        raise AssertionError(f"Expected {code.value}, received {result.code.value}: {result.message}")
    return result


class WorkBriefBoundaryTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_reviewed_authority_validation_returns_exact_expected_failures(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = work_a_brief(project)
        checkpoint = brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)

        self.assertIsNone(validate_reviewed_authority_digests(project, checkpoint.reviewed_authorities))

        source = project / "architecture.md"
        source.write_text("# Architecture\n\n## Contract\n\nChanged.\n", encoding="utf-8")
        stale = validate_reviewed_authority_digests(project, checkpoint.reviewed_authorities)
        self.assertIsInstance(stale, work_brief_models.ReviewedAuthorityDigestMismatch)
        assert isinstance(stale, work_brief_models.ReviewedAuthorityDigestMismatch)
        self.assertEqual("architecture", stale.authority_id)
        self.assertNotEqual(stale.expected_sha256, stale.observed_sha256)

        source.unlink()
        unreadable = validate_reviewed_authority_digests(project, checkpoint.reviewed_authorities)
        self.assertIsInstance(unreadable, work_brief_models.ReviewedAuthoritySelectionFailure)
        assert isinstance(unreadable, work_brief_models.ReviewedAuthoritySelectionFailure)
        self.assertEqual("architecture", unreadable.authority_id)
        self.assertIn("Cannot read authority", unreadable.reason)

    def test_candidate_decodes_strictly_and_canonicalizes(self) -> None:
        value = example_work_brief()
        candidate = msgspec.json.encode(value)

        decoded = expect_work_brief_success(decode_work_brief(candidate))

        self.assertEqual(value, decoded)
        canonical = canonical_work_brief_bytes(decoded)
        self.assertTrue(canonical.endswith(b"\n"))
        self.assertEqual(decoded, expect_work_brief_success(decode_work_brief(canonical)))
        expect_work_brief_failure(
            decode_work_brief(candidate[:-1] + b',"unknown":true}'), WorkBriefErrorCode.BRIEF_INVALID
        )
        for field, invalid in (("attempt_id", f"{value.attempt_id}\n"), ("title", f"{value.title}\n")):
            with self.subTest(field=field):
                payload = msgspec.json.decode(candidate)
                if not isinstance(payload, dict):
                    self.fail("work brief JSON must be an object")
                payload[field] = invalid
                expect_work_brief_failure(
                    decode_work_brief(msgspec.json.encode(payload)), WorkBriefErrorCode.BRIEF_INVALID
                )

    def test_cross_references_are_rejected_at_the_typed_boundary(self) -> None:
        value = example_work_brief()
        payload = msgspec.json.decode(msgspec.json.encode(value))
        if not isinstance(payload, dict):
            self.fail("work brief JSON must be an object")
        checkpoint = payload["checkpoint"]
        if not isinstance(checkpoint, dict):
            self.fail("work brief checkpoint JSON must be an object")
        coverage = checkpoint["coverage"]
        if not isinstance(coverage, list) or not isinstance(coverage[0], dict):
            self.fail("work brief coverage JSON must be a non-empty array of objects")
        coverage[0]["owner"] = {"disposition": "acceptance", "criterion": 99}

        expect_work_brief_failure(decode_work_brief(msgspec.json.encode(payload)), WorkBriefErrorCode.BRIEF_INVALID)

    def test_checkpoint_identity_may_equal_item_identity(self) -> None:
        value = example_work_brief()
        changed = replace(
            value,
            checkpoint=replace(value.checkpoint, checkpoint_id=value.item_id),
        )

        self.assertEqual(changed, expect_work_brief_success(decode_work_brief(canonical_work_brief_bytes(changed))))

    def test_every_tagged_variant_decodes_through_the_strict_boundary(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        authority_basis = work_brief_models.AuthorityAuthorization("repository-guidance", "repository-practice")
        existing_consumer_basis = work_brief_models.ExistingConsumerAuthorization(
            "repository-guidance", "repository-practice"
        )
        for name, changed in (
            (
                "architecture-none",
                replace(checkpoint, architecture_impact=work_brief_models.NoArchitectureImpact("No change.")),
            ),
            (
                "architecture-read-only",
                replace(
                    checkpoint,
                    architecture_impact=work_brief_models.ReadOnlyArchitecture("ARCHITECTURE.md", "Conform."),
                ),
            ),
            (
                "authority-authorization",
                replace(
                    checkpoint,
                    contracts=(replace(checkpoint.contracts[0], authorization_basis=authority_basis),),
                ),
            ),
            (
                "existing-consumer-authorization",
                replace(
                    checkpoint,
                    verification=(replace(checkpoint.verification[0], authorization_basis=existing_consumer_basis),),
                ),
            ),
            (
                "acceptance-owner",
                replace(
                    checkpoint,
                    coverage=(replace(checkpoint.coverage[0], owner=work_brief_models.AcceptanceCoverageOwner(1)),),
                ),
            ),
            (
                "deferred-owner",
                replace(
                    checkpoint,
                    coverage=(
                        replace(checkpoint.coverage[0], owner=work_brief_models.DeferredCoverageOwner("later-work")),
                    ),
                ),
            ),
            (
                "not-applicable-owner",
                replace(
                    checkpoint,
                    coverage=(
                        replace(
                            checkpoint.coverage[0],
                            owner=work_brief_models.NotApplicableCoverageOwner("No effect."),
                        ),
                    ),
                ),
            ),
            (
                "required-lifecycle",
                replace(
                    checkpoint,
                    lifecycle_partition=work_brief_models.RequiredLifecyclePartition(
                        (
                            work_brief_models.LifecycleRecord(
                                "publish", "candidate", "application", "receipt", "accepted", "overwrite"
                            ),
                        )
                    ),
                ),
            ),
        ):
            with self.subTest(name=name):
                candidate = replace(value, checkpoint=changed)
                self.assertEqual(
                    candidate, expect_work_brief_success(decode_work_brief(canonical_work_brief_bytes(candidate)))
                )

        invalid = replace(value, owner_task_id=" owner-task ")
        expect_work_brief_failure(decode_work_brief(msgspec.json.encode(invalid)), WorkBriefErrorCode.BRIEF_INVALID)

    def test_checkpoint_and_authority_digests_use_canonical_records(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)

        self.assertEqual(
            "a2941d05f3c61a40ca5014af48a095ee919e2cad2fe2d78a09a74a8835693f1f",
            hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest(),
        )
        renamed = replace(checkpoint, title="Renamed title")
        self.assertEqual(checkpoint.checkpoint_id, renamed.checkpoint_id)
        self.assertNotEqual(canonical_checkpoint_bytes(checkpoint), canonical_checkpoint_bytes(renamed))
        self.assertEqual(
            "ec8a74883f273b06ab9571a0b92c655280375fd3af2adcaa5c6bbe505a3c64a0",
            hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest(),
        )
        self.assertEqual(
            msgspec.json.encode(checkpoint.reviewed_authorities, order="sorted"),
            canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities),
        )
        second = replace(
            checkpoint.reviewed_authorities[0],
            authority_id="second-authority",
            reviewed_sha256="c" * 64,
        )
        ordered = (*checkpoint.reviewed_authorities, second)
        self.assertNotEqual(
            canonical_reviewed_authority_set_bytes(ordered),
            canonical_reviewed_authority_set_bytes(tuple(reversed(ordered))),
        )

    def test_review_is_strict_digest_bound_and_independent(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        coverage = checkpoint.coverage[0]
        review = work_brief_models.WorkBriefReview(
            schema="pinboard-work-brief-review/v2",
            attempt_id=value.attempt_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_sha256=hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest(),
            reviewed_authority_set_sha256=hashlib.sha256(
                canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)
            ).hexdigest(),
            reviewer_task_id="independent-reviewer",
            status="complete",
            verdict="ready",
            coverage=(
                work_brief_models.ReviewCoverageResult(
                    authority_id=coverage.authority_id,
                    family=coverage.family,
                    owner=coverage.owner,
                    verdict="covered",
                    counterexample_result="Untyped decoding is rejected by the strict model.",
                ),
            ),
        )

        decoded = expect_work_brief_success(decode_work_brief_review(msgspec.json.encode(review)))
        self.assertIsNone(validate_work_brief_review(decoded, value, reviewer_task_id=value.owner_task_id))
        payload = msgspec.json.decode(msgspec.json.encode(review))
        if not isinstance(payload, dict):
            self.fail("work brief review JSON must be an object")
        coverage_payload = payload["coverage"]
        if not isinstance(coverage_payload, list):
            self.fail("work brief review coverage JSON must be an array")
        coverage_payload.append(coverage_payload[0])
        expect_work_brief_failure(
            decode_work_brief_review(msgspec.json.encode(payload)), WorkBriefErrorCode.REVIEW_INVALID
        )
        payload = msgspec.json.decode(msgspec.json.encode(review))
        if not isinstance(payload, dict):
            self.fail("work brief review JSON must be an object")
        payload["checkpoint_sha256"] = f"{review.checkpoint_sha256}\n"
        expect_work_brief_failure(
            decode_work_brief_review(msgspec.json.encode(payload)), WorkBriefErrorCode.REVIEW_INVALID
        )
        same_owner = validate_work_brief_review(replace(review, reviewer_task_id=value.owner_task_id), value)
        assert same_owner is not None
        self.assertEqual(WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT, same_owner.code)
        stale = validate_work_brief_review(replace(review, checkpoint_sha256="f" * 64), value)
        assert stale is not None
        self.assertEqual(WorkBriefErrorCode.REVIEW_STALE, stale.code)

    def test_markdown_is_a_complete_generated_projection(self) -> None:
        rendered = render_work_brief_markdown(example_work_brief()).decode()

        self.assertIn("item_id: make-canonical-briefs-typed-json", rendered)
        self.assertIn("branch: codex/release-candidate", rendered)
        self.assertIn("base_revision: 2f61739541738bdd8a9ba2d484ddcdf3ab38a218", rendered)
        self.assertIn("owner_task_id: 01a04020-7d81-7602-a49e-b2d4f3ed6230", rendered)
        self.assertIn("accepted_scope_revision: 1", rendered)
        self.assertIn(f"accepted_scope_digest: {'b' * 64}", rendered)
        self.assertNotIn("database_revision:", rendered)
        self.assertIn("typed-json-cutover", rendered)
        self.assertIn("Strict typed JSON remains canonical.", rendered)
        self.assertIn("uv run --locked pyrefly check", rendered)
        self.assertIn("later-work", rendered)

    def test_activation_and_resume_reject_mismatched_typed_brief_identity(self) -> None:
        for name in ("activate", "resume"):
            with self.subTest(name=name):
                project = Path(tempfile.mkdtemp()).resolve()
                roots = resolve_durable_roots(project)
                if name == "activate":
                    value = work_c_brief()
                    preparation = work_models.PreparationCommandAuthority(
                        2,
                        ItemId("work-c"),
                        value.accepted_scope.revision,
                        value.accepted_scope.digest,
                        TaskId("preparer"),
                        HostId("host-a"),
                        LeaseId("preparation-c"),
                        1,
                        datetime.max.replace(tzinfo=UTC),
                    )
                    capability = decision_models.MutationActionCapability(
                        ItemId("work-c"),
                        "label",
                        "expected",
                        subject_revision="1",
                        preparation_authority=preparation,
                    )
                    command = decision_models.ActivateCommand(
                        decision_models.ActivateAction(capability),
                        work_models.ActivateInput(
                            AttemptId("work-c-1"),
                            "codex/work-c",
                            "candidate-base",
                            "worker-task",
                            ArtifactRefId(1),
                        ),
                    )
                else:
                    value = work_a_brief(project)
                    capability = decision_models.MutationActionCapability(
                        ItemId("work-a"), "label", "expected", subject_revision="1"
                    )
                    command = decision_models.ResumeCommand(
                        decision_models.ResumeAction(capability), work_models.ResumeInput(ArtifactRefId(1))
                    )
                published = write_revision(
                    roots,
                    NewArtifact(
                        work_models.ArtifactKind.BRIEF, value.attempt_id, 1, ".json", canonical_work_brief_bytes(value)
                    ),
                )
                state = complete_sqlite_state()
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
                )
                artifacts = ArtifactRepository(roots)

                identity = read_selected_work_brief_identity(reference, artifacts)
                self.assertNotIsInstance(identity, DecisionFailure)
                assert identity is not None
                self.assertIsNone(validate_transition_work_brief(decision_facts(state, SQLITE_NOW), command, identity))
                for identity_mismatch in (
                    dataclass_replace(identity, attempt_id="different-1"),
                    dataclass_replace(identity, item_id="different"),
                    dataclass_replace(identity, branch="codex/different"),
                    dataclass_replace(identity, base_revision="different-base"),
                    dataclass_replace(identity, accepted_scope_revision=identity.accepted_scope_revision + 1),
                    dataclass_replace(identity, accepted_scope_digest="f" * 64),
                ):
                    self.assertIsInstance(
                        validate_transition_work_brief(decision_facts(state, SQLITE_NOW), command, identity_mismatch),
                        DecisionFailure,
                    )
                if isinstance(command, decision_models.ActivateCommand):
                    authority = command.action.capability.preparation_authority
                    assert authority is not None
                    wrong_pin = dataclass_replace(
                        command,
                        action=dataclass_replace(
                            command.action,
                            capability=dataclass_replace(
                                command.action.capability,
                                preparation_authority=dataclass_replace(authority, definition_digest="f" * 64),
                            ),
                        ),
                    )
                    self.assertIsInstance(
                        validate_transition_work_brief(decision_facts(state, SQLITE_NOW), wrong_pin, identity),
                        DecisionFailure,
                    )
                artifact_failure = ArtifactError(
                    ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, "accepted brief cannot be read"
                )
                with (
                    patch.object(ArtifactRepository, "read", side_effect=artifact_failure),
                    self.assertRaises(ArtifactError),
                ):
                    read_selected_work_brief_identity(reference, artifacts)
                with patch.object(ArtifactRepository, "read", return_value=b"{}"):
                    invalid_identity = read_selected_work_brief_identity(reference, artifacts)
                self.assertIsInstance(invalid_identity, DecisionFailure)
                assert isinstance(invalid_identity, DecisionFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, invalid_identity.code)
                self.assertIn("not a valid canonical typed work brief", invalid_identity.message)
                with (
                    patch(
                        "pinboard.interfaces.work_briefs.decode_work_brief_identity",
                        side_effect=ValueError("unrelated value failure"),
                    ),
                    self.assertRaisesRegex(ValueError, "unrelated value failure"),
                ):
                    read_selected_work_brief_identity(reference, artifacts)

                mismatched = replace(value, branch="codex/different")
                mismatch = write_revision(
                    roots,
                    NewArtifact(
                        work_models.ArtifactKind.BRIEF,
                        f"{value.attempt_id}-mismatch",
                        1,
                        ".json",
                        canonical_work_brief_bytes(mismatched),
                    ),
                )
                mismatched_reference = dataclass_replace(
                    reference,
                    key=mismatch.key,
                    selector=mismatch.selector,
                    content_sha256=mismatch.content_sha256,
                    size_bytes=mismatch.size_bytes,
                )
                mismatched_state = dataclass_replace(
                    state,
                    artifact_references=(mismatched_reference, *state.artifact_references[1:]),
                )
                mismatched_identity = read_selected_work_brief_identity(mismatched_reference, artifacts)
                self.assertNotIsInstance(mismatched_identity, DecisionFailure)
                failure = validate_transition_work_brief(
                    decision_facts(mismatched_state, SQLITE_NOW), command, mismatched_identity
                )
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, failure.code)

    def test_installed_publication_is_canonical_scheduling_neutral_and_retryable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        result, _stdout, stderr = self.run_cli(*common, "init")
        self.assertEqual(0, result, stderr)
        candidate = project / "brief.json"
        candidate.write_bytes(msgspec.json.format(msgspec.json.encode(example_work_brief()), indent=2))
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()

        result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(0, result, stderr)
        receipt = msgspec.json.decode(stdout.encode())
        self.assertEqual("artifacts/briefs/make-canonical-briefs-typed-json-1/1.json", receipt["selector"])
        after = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
        self.assertEqual(before.authority, after.authority)
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        artifact = work / receipt["selector"]
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), artifact.read_bytes())

        retry_result, retry_stdout, retry_stderr = self.run_cli(
            *common, "brief", "publish", "--file", str(candidate), "--json"
        )
        self.assertEqual(0, retry_result, retry_stderr)
        self.assertEqual(receipt, msgspec.json.decode(retry_stdout.encode()))
        self.assertEqual(after, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

        candidate.write_bytes(canonical_work_brief_bytes(replace(example_work_brief(), title="Different title")))
        collision_result, _collision_stdout, collision_stderr = self.run_cli(
            *common, "brief", "publish", "--file", str(candidate)
        )
        self.assertEqual(12, collision_result)
        self.assertIn("STORAGE_INVARIANT_VIOLATION", collision_stderr)
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), artifact.read_bytes())

    def test_invalid_publication_is_a_stable_typed_cli_rejection(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_text("{}\n", encoding="utf-8")
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()

        result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(16, result)
        self.assertEqual("", stderr)
        failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("pinboard-rejected-operation/v1", failure["schema"])
        self.assertEqual("rejected", failure["status"])
        self.assertEqual(WorkBriefErrorCode.BRIEF_INVALID.value, failure["code"])
        self.assertFalse(failure["state_changed"])
        self.assertEqual("correct-input", failure["retry"])
        self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

    def test_publication_failure_leaves_reusable_verified_orphan(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))

        database_failure = StorageError(StorageErrorCode.BUSY, "database failed", retryable=True)
        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(12, result, stderr)
        committed = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", committed["status"])
        self.assertTrue(committed["state_changed"])
        self.assertEqual(["immutable-artifact"], committed["changed_surfaces"])
        self.assertEqual("do-not-retry", committed["retry"])
        orphan = work / "artifacts" / "briefs" / example_work_brief().attempt_id / "1.json"
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), orphan.read_bytes())
        self.assertEqual((), SQLiteWorkStore(work / "state.sqlite3").validated_snapshot().artifact_references)

        orphan = work / "artifacts" / "briefs" / example_work_brief().attempt_id / "1.json"
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), orphan.read_bytes())

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=database_failure):
            retry_result, retry_stdout, retry_stderr = self.run_cli(
                *common, "brief", "publish", "--file", str(candidate), "--json"
            )
        self.assertEqual(12, retry_result, retry_stderr)
        unchanged = msgspec.json.decode(retry_stdout.encode())
        self.assertEqual("rejected", unchanged["status"])
        self.assertFalse(unchanged["state_changed"])
        self.assertEqual([], unchanged["changed_surfaces"])
        self.assertEqual("retry-same-input", unchanged["retry"])

        result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate))
        self.assertEqual(0, result, stderr)
        self.assertIn("BRIEF_PUBLISHED", stdout)

    def test_post_link_sync_failure_reports_and_reuses_the_published_brief(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))
        selector = f"artifacts/briefs/{example_work_brief().attempt_id}/1.json"
        publication = work / selector
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        original_fsync = os.fsync

        def fail_after_link(descriptor: int) -> None:
            if publication.exists():
                raise OSError("injected post-link directory sync failure")
            original_fsync(descriptor)

        with patch("pinboard.adapters.files.file_io.os.fsync", side_effect=fail_after_link):
            result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(12, result, stderr)
        failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", failure["status"])
        self.assertEqual("DIRECTORY_SYNC_FAILED", failure["code"])
        self.assertEqual(["immutable-artifact"], failure["changed_surfaces"])
        self.assertEqual("do-not-retry", failure["retry"])
        self.assertEqual(
            [selector],
            [value["value"] for value in failure["observed"] if value["field"] == "published_artifact_selector"],
        )
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), publication.read_bytes())
        self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

        retry_result, retry_stdout, retry_stderr = self.run_cli(
            *common, "brief", "publish", "--file", str(candidate), "--json"
        )
        self.assertEqual(0, retry_result, retry_stderr)
        self.assertEqual(selector, msgspec.json.decode(retry_stdout.encode())["selector"])

    def test_readonly_publication_failure_preserves_artifact_and_database_diagnostics(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))
        readonly = translate_database_error(sqlite3.OperationalError("attempt to write a readonly database"))
        readonly = readonly.with_database_path(work / "state.sqlite3")

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=readonly):
            result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(12, result, stderr)
        failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", failure["status"])
        self.assertEqual("SQLITE_READONLY", failure["code"])
        self.assertTrue(failure["state_changed"])
        self.assertEqual(["immutable-artifact"], failure["changed_surfaces"])
        self.assertEqual("do-not-retry", failure["retry"])
        observations = {value["field"]: value["value"] for value in failure["observed"]}
        self.assertEqual(str(work / "state.sqlite3"), observations["database_path"])
        self.assertEqual("brief/publish", observations["operation"])
        self.assertEqual("SQLITE_READONLY", observations["sqlite_error_code"])
        self.assertIn(str(work), observations["permission_recovery"])
        self.assertEqual((), SQLiteWorkStore(work / "state.sqlite3").validated_snapshot().artifact_references)

    def test_store_verification_failure_preserves_exact_publication_effect(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))
        verification_failure = ArtifactError(
            ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION,
            "store verification failed",
        )

        with patch(
            "pinboard.adapters.sqlite.artifacts.verify_reference",
            side_effect=verification_failure,
        ):
            result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(12, result, stderr)
        committed = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", committed["status"])
        self.assertTrue(committed["state_changed"])
        self.assertEqual(["immutable-artifact"], committed["changed_surfaces"])
        self.assertEqual("do-not-retry", committed["retry"])

        with patch(
            "pinboard.adapters.sqlite.artifacts.verify_reference",
            side_effect=verification_failure,
        ):
            retry_result, retry_stdout, retry_stderr = self.run_cli(
                *common,
                "brief",
                "publish",
                "--file",
                str(candidate),
                "--json",
            )

        self.assertEqual(12, retry_result, retry_stderr)
        unchanged = msgspec.json.decode(retry_stdout.encode())
        self.assertEqual("rejected", unchanged["status"])
        self.assertFalse(unchanged["state_changed"])
        self.assertEqual([], unchanged["changed_surfaces"])
        self.assertEqual("do-not-retry", unchanged["retry"])
        self.assertEqual((), SQLiteWorkStore(work / "state.sqlite3").validated_snapshot().artifact_references)

    def test_store_programming_failures_propagate_after_publication(self) -> None:
        for programming_failure in (AssertionError("assertion failed"), ValueError("value failed")):
            with self.subTest(error=type(programming_failure).__name__):
                project = Path(tempfile.mkdtemp()).resolve()
                work = project / ".codex" / "work"
                common = ("--project-root", str(project), "--work-root", str(work))
                self.assertEqual(0, self.run_cli(*common, "init")[0])
                candidate = project / "brief.json"
                candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))

                with (
                    patch(
                        "pinboard.adapters.sqlite.artifacts.verify_reference",
                        side_effect=programming_failure,
                    ),
                    self.assertRaises(type(programming_failure)),
                ):
                    self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

    def test_returned_publication_rejection_reports_new_immutable_artifact(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))
        rejected = DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "acceptance changed", None)

        with patch.object(SQLiteWorkStore, "accept_artifact_reference", return_value=rejected):
            result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(11, result, stderr)
        failure = msgspec.json.decode(stdout.encode())
        self.assertEqual("committed-effect", failure["status"])
        self.assertEqual(["immutable-artifact"], failure["changed_surfaces"])
        self.assertEqual("do-not-retry", failure["retry"])


if __name__ == "__main__":
    unittest.main()
