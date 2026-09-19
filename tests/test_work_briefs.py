import contextlib
import hashlib
import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import patch

import msgspec
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from msgspec.structs import replace

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, translate_database_error
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import checkpoint_compatibility_models, work_brief_compatibility_models, work_brief_models
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import NewArtifact
from pinboard.application.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_checkpoint_review_package_bytes,
    canonical_reviewed_authority_set_bytes,
    canonical_work_brief_bytes,
    canonical_work_brief_review_needs_correction_bytes,
    decode_canonical_checkpoint_review_package,
    decode_canonical_work_brief,
    decode_canonical_work_brief_review_needs_correction,
    decode_checkpoint_review_package,
    decode_work_brief,
    decode_work_brief_review,
    read_selected_work_brief_identity,
    render_work_brief_markdown,
    validate_definition_brief_agreement,
    validate_reviewed_authority_digests,
    validate_work_brief_review,
    validate_work_brief_review_needs_correction,
)
from pinboard.cli.entrypoint import main
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HostId, ItemId, LeaseId, TaskId
from pinboard.mcp import server
from tests.artifact_support import write_revision
from tests.native_support import call_native_tool
from tests.support import (
    SQLITE_NOW,
    JsonObject,
    complete_sqlite_state,
    decision_facts,
    initialize_store,
    test_definition,
)
from tests.work_brief_support import (
    example_work_brief,
    needs_correction_review,
    ready_review,
    work_a_brief,
    work_c_brief,
)


def expect_work_brief_success[T](result: work_brief_models.WorkBriefResult[T]) -> T:
    if isinstance(result, work_brief_models.WorkBriefFailure):
        raise AssertionError(str(result))
    return result


def expect_work_brief_failure[T](
    result: work_brief_models.WorkBriefResult[T], code: work_brief_models.WorkBriefErrorCode
) -> work_brief_models.WorkBriefFailure:
    if not isinstance(result, work_brief_models.WorkBriefFailure):
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

        select_source = partial(select_checkout_brief_source, project)
        self.assertIsNone(validate_reviewed_authority_digests(select_source, checkpoint.reviewed_authorities))

        source = project / "architecture.md"
        source.write_text("# Architecture\n\n## Contract\n\nChanged.\n", encoding="utf-8")
        stale = validate_reviewed_authority_digests(select_source, checkpoint.reviewed_authorities)
        self.assertIsInstance(stale, work_brief_models.ReviewedAuthorityDigestMismatch)
        assert isinstance(stale, work_brief_models.ReviewedAuthorityDigestMismatch)
        self.assertEqual("architecture", stale.authority_id)
        self.assertNotEqual(stale.expected_sha256, stale.observed_sha256)

        source.unlink()
        unreadable = validate_reviewed_authority_digests(select_source, checkpoint.reviewed_authorities)
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
            decode_work_brief(candidate[:-1] + b',"unknown":true}'), work_brief_models.WorkBriefErrorCode.BRIEF_INVALID
        )
        for field, invalid in (("attempt_id", f"{value.attempt_id}\n"), ("title", f"{value.title}\n")):
            with self.subTest(field=field):
                payload = msgspec.json.decode(candidate)
                if not isinstance(payload, dict):
                    self.fail("work brief JSON must be an object")
                payload[field] = invalid
                expect_work_brief_failure(
                    decode_work_brief(msgspec.json.encode(payload)), work_brief_models.WorkBriefErrorCode.BRIEF_INVALID
                )

    def test_retained_v2_brief_remains_exactly_readable_reviewable_and_renderable(self) -> None:
        current = example_work_brief()
        payload = msgspec.to_builtins(current)
        assert isinstance(payload, dict)
        payload["schema"] = "pinboard-work-brief/v2"
        del payload["checkout_selection"]
        del payload["obligation_correspondence"]
        legacy_bytes = msgspec.json.encode(payload, order="sorted") + b"\n"

        legacy = expect_work_brief_success(decode_canonical_work_brief(legacy_bytes))

        self.assertEqual("pinboard-work-brief/v2", legacy.schema)
        self.assertIn(b"authority: pinboard-work-brief/v2", render_work_brief_markdown(legacy))
        current_review = msgspec.json.decode(ready_review(current), type=work_brief_models.WorkBriefReview)
        review = work_brief_compatibility_models.WorkBriefReviewV2(
            "pinboard-work-brief-review/v2",
            current_review.attempt_id,
            current_review.checkpoint_id,
            current_review.checkpoint_sha256,
            current_review.reviewed_authority_set_sha256,
            current_review.reviewer_task_id,
            current_review.status,
            current_review.verdict,
            current_review.coverage,
        )
        self.assertIsNone(validate_work_brief_review(review, legacy))

    def test_definition_agreement_requires_complete_ids_and_permitted_checkout_and_deferral(self) -> None:
        brief = work_a_brief(Path(tempfile.mkdtemp()).resolve())
        checkpoint = brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        definition, _digest = test_definition(ItemId("work-a"))
        self.assertIsNone(validate_definition_brief_agreement(definition, brief))
        unknown = replace(
            brief,
            obligation_correspondence=(
                work_brief_models.ObligationCorrespondence(
                    "unknown-obligation",
                    work_brief_models.ContractObligationTarget(checkpoint.contracts[0].invariant),
                ),
            ),
        )
        forbidden = replace(
            brief,
            obligation_correspondence=(
                work_brief_models.ObligationCorrespondence(
                    "next-decision",
                    work_brief_models.DeferralObligationTarget("later-work"),
                ),
            ),
        )
        fixed = dataclass_replace(definition, checkout_policy=work_models.CheckoutPolicy.ISOLATED)

        self.assertIsNotNone(validate_definition_brief_agreement(definition, unknown))
        self.assertIsNotNone(validate_definition_brief_agreement(definition, forbidden))
        self.assertIsNotNone(validate_definition_brief_agreement(fixed, brief))

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

        expect_work_brief_failure(
            decode_work_brief(msgspec.json.encode(payload)), work_brief_models.WorkBriefErrorCode.BRIEF_INVALID
        )

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
        expect_work_brief_failure(
            decode_work_brief(msgspec.json.encode(invalid)), work_brief_models.WorkBriefErrorCode.BRIEF_INVALID
        )

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
            schema="pinboard-work-brief-review/v3",
            attempt_id=value.attempt_id,
            checkpoint_id=checkpoint.checkpoint_id,
            accepted_brief_sha256=hashlib.sha256(canonical_work_brief_bytes(value)).hexdigest(),
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
            decode_work_brief_review(msgspec.json.encode(payload)), work_brief_models.WorkBriefErrorCode.REVIEW_INVALID
        )
        payload = msgspec.json.decode(msgspec.json.encode(review))
        if not isinstance(payload, dict):
            self.fail("work brief review JSON must be an object")
        payload["checkpoint_sha256"] = f"{review.checkpoint_sha256}\n"
        expect_work_brief_failure(
            decode_work_brief_review(msgspec.json.encode(payload)), work_brief_models.WorkBriefErrorCode.REVIEW_INVALID
        )
        same_owner = validate_work_brief_review(replace(review, reviewer_task_id=value.owner_task_id), value)
        assert same_owner is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT, same_owner.code)
        stale = validate_work_brief_review(replace(review, checkpoint_sha256="f" * 64), value)
        assert stale is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.REVIEW_STALE, stale.code)

        for changed in (
            replace(value, checkout_selection=work_models.CheckoutSelection.ISOLATED),
            replace(
                value,
                obligation_correspondence=(
                    work_brief_models.ObligationCorrespondence(
                        value.obligation_correspondence[0].obligation_id,
                        work_brief_models.CriterionObligationTarget(checkpoint.acceptance_criteria[0].number),
                    ),
                ),
            ),
        ):
            with self.subTest(changed=changed.checkout_selection):
                stale = validate_work_brief_review(review, changed)
                assert stale is not None
                self.assertEqual(work_brief_models.WorkBriefErrorCode.REVIEW_STALE, stale.code)

    def test_needs_correction_review_is_canonical_digest_bound_and_independent(self) -> None:
        value = example_work_brief()
        candidate = needs_correction_review(value)
        review = expect_work_brief_success(decode_canonical_work_brief_review_needs_correction(candidate))

        self.assertEqual(candidate, canonical_work_brief_review_needs_correction_bytes(review))
        self.assertIsNone(validate_work_brief_review_needs_correction(review, value))
        same_owner = validate_work_brief_review_needs_correction(
            replace(review, reviewer_task_id=value.owner_task_id), value
        )
        assert same_owner is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT, same_owner.code)
        stale = validate_work_brief_review_needs_correction(replace(review, accepted_brief_sha256="f" * 64), value)
        assert stale is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.REVIEW_STALE, stale.code)

        payload = msgspec.json.decode(candidate)
        if not isinstance(payload, dict):
            self.fail("needs-correction review JSON must be an object")
        findings = payload["findings"]
        if not isinstance(findings, list):
            self.fail("needs-correction findings must be an array")
        findings.append(findings[0])
        expect_work_brief_failure(
            decode_canonical_work_brief_review_needs_correction(msgspec.json.encode(payload)),
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
        )
        expect_work_brief_failure(
            decode_canonical_work_brief_review_needs_correction(candidate.rstrip()),
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
        )

    def assert_current_and_retained_role_bindings(
        self,
        portable: checkpoint_compatibility_models.CheckpointReviewPackageV2,
    ) -> None:
        for schema, candidate in (
            ("pinboard-checkpoint-review-package/v2", portable.candidate),
            ("pinboard-checkpoint-review-package/v3", "working-tree-state-sha256:" + "d" * 64),
        ):
            valid = msgspec.json.decode(canonical_checkpoint_review_package_bytes(portable))
            assert isinstance(valid, dict)
            valid["schema"] = schema
            valid["candidate"] = candidate
            encoded = msgspec.json.encode(valid, order="sorted") + b"\n"
            decoded = expect_work_brief_success(decode_canonical_checkpoint_review_package(encoded))
            self.assertEqual(encoded, canonical_checkpoint_review_package_bytes(decoded))
            for field in ("candidate_snapshot", "accepted_brief", "result", "implementation_review"):
                with self.subTest(schema=schema, malformed_binding=field):
                    invalid = msgspec.json.decode(encoded)
                    assert isinstance(invalid, dict)
                    binding = invalid[field]
                    assert isinstance(binding, dict)
                    binding["role"] = "brief-review"
                    rejected = expect_work_brief_failure(
                        decode_checkpoint_review_package(msgspec.json.encode(invalid)),
                        work_brief_models.WorkBriefErrorCode.PACKAGE_INVALID,
                    )
                    self.assertIn("artifact roles and kinds do not match their bindings", rejected.message)

    def test_checkpoint_review_package_variants_are_strict_canonical_and_portable(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()

        def identity(
            role: str,
            kind: str,
            key: str,
        ) -> work_brief_models.PortableArtifactIdentity:
            return msgspec.convert(
                {
                    "role": role,
                    "kind": kind,
                    "key": key,
                    "revision": 1,
                    "selector": f"artifacts/{kind}/{key}/1.json",
                    "content_sha256": "c" * 64,
                    "size_bytes": 123,
                },
                type=work_brief_models.PortableArtifactIdentity,
            )

        accepted_brief = identity("accepted-brief", "brief", "accepted-brief")
        result = identity("result", "result", "result")
        implementation_review = identity("implementation-review", "evidence", "implementation-review")
        brief_review = identity("brief-review", "evidence", "brief-review")
        candidate_snapshot = identity("candidate", "evidence", "candidate")

        def make_compatibility_package(
            selected_brief: work_brief_models.PortableArtifactIdentity,
            selected_result: work_brief_models.PortableArtifactIdentity,
            selected_implementation_review: work_brief_models.PortableArtifactIdentity,
            review_basis: work_brief_models.ReviewBasis,
        ) -> checkpoint_compatibility_models.CheckpointReviewPackage:
            return checkpoint_compatibility_models.CheckpointReviewPackage(
                value.attempt_id,
                value.item_id,
                "candidate-a",
                "Accepted.",
                value.accepted_scope,
                work_brief_models.CheckpointIdentity(checkpoint.checkpoint_id, checkpoint_sha256),
                selected_brief,
                selected_result,
                selected_implementation_review,
                "ready",
                review_basis,
            )

        local = make_compatibility_package(
            accepted_brief, result, implementation_review, work_brief_models.LocalReviewBasis()
        )
        cross = make_compatibility_package(
            accepted_brief,
            result,
            implementation_review,
            work_brief_models.CrossBoundaryReviewBasis(
                brief_review,
                checkpoint_sha256,
                hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest(),
            ),
        )

        for candidate_package in (local, cross):
            with self.subTest(boundary=candidate_package.review_basis):
                encoded = canonical_checkpoint_review_package_bytes(candidate_package)
                self.assertTrue(encoded.endswith(b"\n"))
                self.assertEqual(
                    candidate_package,
                    expect_work_brief_success(decode_canonical_checkpoint_review_package(encoded)),
                )
                expect_work_brief_failure(
                    decode_canonical_checkpoint_review_package(encoded[:-1]),
                    work_brief_models.WorkBriefErrorCode.PACKAGE_NOT_CANONICAL,
                )

        portable = checkpoint_compatibility_models.CheckpointReviewPackageV2(
            value.attempt_id,
            value.item_id,
            f"working-tree-sha256:{candidate_snapshot.content_sha256}",
            "Accepted.",
            value.accepted_scope,
            work_brief_models.CheckpointIdentity(checkpoint.checkpoint_id, checkpoint_sha256),
            candidate_snapshot,
            accepted_brief,
            result,
            implementation_review,
            "ready",
            work_brief_models.CrossBoundaryReviewBasis(
                brief_review,
                checkpoint_sha256,
                hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest(),
            ),
        )
        for portable_package in (portable, replace(portable, candidate="a" * 40)):
            encoded_portable = canonical_checkpoint_review_package_bytes(portable_package)
            self.assertEqual(
                portable_package,
                expect_work_brief_success(decode_canonical_checkpoint_review_package(encoded_portable)),
            )
        self.assert_current_and_retained_role_bindings(portable)
        payload = msgspec.json.decode(canonical_checkpoint_review_package_bytes(cross))
        if not isinstance(payload, dict):
            self.fail("checkpoint review package JSON must be an object")
        payload["unknown"] = True
        expect_work_brief_failure(
            decode_checkpoint_review_package(msgspec.json.encode(payload)),
            work_brief_models.WorkBriefErrorCode.PACKAGE_INVALID,
        )
        invalid_payloads = []
        wrong_role = msgspec.json.decode(canonical_checkpoint_review_package_bytes(local))
        duplicate = msgspec.json.decode(canonical_checkpoint_review_package_bytes(cross))
        wrong_digest = msgspec.json.decode(canonical_checkpoint_review_package_bytes(cross))
        if not isinstance(wrong_role, dict) or not isinstance(duplicate, dict) or not isinstance(wrong_digest, dict):
            self.fail("checkpoint review package JSON must be an object")
        accepted_brief_payload = wrong_role["accepted_brief"]
        duplicate_basis = duplicate["review_basis"]
        implementation_review_payload = duplicate["implementation_review"]
        wrong_digest_basis = wrong_digest["review_basis"]
        if (
            not isinstance(accepted_brief_payload, dict)
            or not isinstance(duplicate_basis, dict)
            or not isinstance(implementation_review_payload, dict)
            or not isinstance(wrong_digest_basis, dict)
        ):
            self.fail("checkpoint review package nested identities must be objects")
        accepted_brief_payload["role"] = "result"
        duplicate_review_payload = duplicate_basis["brief_review"]
        if not isinstance(duplicate_review_payload, dict):
            self.fail("cross-boundary brief-review identity must be an object")
        duplicate_review_payload["key"] = implementation_review_payload["key"]
        wrong_digest_basis["checkpoint_sha256"] = "d" * 64
        invalid_payloads.extend((wrong_role, duplicate, wrong_digest))
        for invalid_payload in invalid_payloads:
            expect_work_brief_failure(
                decode_checkpoint_review_package(msgspec.json.encode(invalid_payload)),
                work_brief_models.WorkBriefErrorCode.PACKAGE_INVALID,
            )

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
                        ItemId("work-a"), "label", subject_revision="1"
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
                        "pinboard.application.work_briefs.decode_work_brief_identity",
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

    def initialized_publication(self) -> tuple[Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(project)), check=True)
        work = project / ".codex" / "work"
        initialize_database(resolve_durable_roots(project, work), SQLITE_NOW)
        initialize_store(SQLiteWorkStore(work / "state.sqlite3"), complete_sqlite_state())
        return project, work

    def publish(self, project: Path, work: Path, brief: work_brief_models.WorkBrief) -> JsonObject:
        payload: JsonObject = msgspec.to_builtins(brief)
        return call_native_tool(
            server.BRIEF_PUBLISH_TOOL, {"project_root": str(project), "work_root": str(work), "brief": payload}
        )

    def test_native_publication_is_canonical_scheduling_neutral_retryable_and_collision_safe(self) -> None:
        project, work = self.initialized_publication()
        brief = work_a_brief(project)
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        result = self.publish(project, work, brief)
        self.assertEqual("committed", result["status"])
        reference = result["reference"]
        assert isinstance(reference, dict) and isinstance(reference["selector"], str)
        self.assertEqual("artifacts/briefs/work-a-1/1.json", reference["selector"])
        after = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
        self.assertEqual(before.authority, after.authority)
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        artifact = work / reference["selector"]
        self.assertEqual(canonical_work_brief_bytes(brief), artifact.read_bytes())
        reused = self.publish(project, work, brief)
        self.assertEqual(
            ("unchanged", False, []), (reused["status"], reused["state_changed"], reused["changed_surfaces"])
        )
        self.assertEqual(reference, reused["reference"])
        self.assertEqual(after, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())
        with self.assertRaises(UnexpectedToolError) as failure:
            self.publish(project, work, replace(brief, title="Different title"))
        self.assertIsInstance(failure.exception.__cause__, ArtifactError)
        cause = failure.exception.__cause__
        assert isinstance(cause, ArtifactError)
        self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, cause.code)
        self.assertEqual(canonical_work_brief_bytes(brief), artifact.read_bytes())
        self.assertEqual(after, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

    def test_native_invalid_publication_rejects_before_effects(self) -> None:
        project, work = self.initialized_publication()
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        failure = call_native_tool(
            server.BRIEF_PUBLISH_TOOL, {"project_root": str(project), "work_root": str(work), "brief": {}}
        )
        self.assertEqual(
            ("rejected", work_brief_models.WorkBriefErrorCode.BRIEF_INVALID.value, False, "correct-input", []),
            (
                failure["status"],
                failure["code"],
                failure["state_changed"],
                failure["retry"],
                failure["changed_surfaces"],
            ),
        )
        self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

    def test_native_acceptance_fault_matrix_preserves_exact_orphan_and_fresh_store(self) -> None:
        database_failure = StorageError(StorageErrorCode.BUSY, "database failed", retryable=True)
        verification_failure = ArtifactError(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, "store verification failed")
        readonly = translate_database_error(sqlite3.OperationalError("attempt to write a readonly database"))
        for target, error in (
            ("pinboard.adapters.sqlite.store.SQLiteWorkStore.accept_artifact_reference", database_failure),
            ("pinboard.adapters.sqlite.artifacts.verify_reference", verification_failure),
            ("pinboard.adapters.sqlite.store.SQLiteWorkStore.accept_artifact_reference", readonly),
        ):
            with self.subTest(target=target, code=error.code):
                project, work = self.initialized_publication()
                before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
                brief = work_a_brief(project)
                selector = f"artifacts/briefs/{brief.attempt_id}/1.json"
                with patch(target, side_effect=error):
                    failure = self.publish(project, work, brief)
                self.assertEqual(
                    (
                        "failed-after-publication",
                        "ARTIFACT_ACCEPTANCE_FAILED",
                        True,
                        ["immutable-artifact"],
                        "do-not-retry",
                        selector,
                    ),
                    (
                        failure["status"],
                        failure["code"],
                        failure["state_changed"],
                        failure["changed_surfaces"],
                        failure["retry"],
                        failure["published_selector"],
                    ),
                )
                self.assertEqual(canonical_work_brief_bytes(brief), (work / selector).read_bytes())
                self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())
                # The caller explicitly selects the already verified orphan; no second publication is claimed.
                with patch(target, side_effect=error), self.assertRaises(UnexpectedToolError) as retry_failure:
                    self.publish(project, work, brief)
                cause = retry_failure.exception.__cause__
                if isinstance(error, ArtifactError):
                    self.assertIsInstance(cause, StorageError)
                    assert isinstance(cause, StorageError)
                    self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, cause.code)
                    self.assertIs(error, cause.__cause__)
                else:
                    self.assertIs(error, cause)
                self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())
                recovered = self.publish(project, work, brief)
                reference = recovered["reference"]
                assert isinstance(reference, dict)
                self.assertEqual(selector, reference["selector"])
                self.assertEqual(["accepted-artifact-reference", "ledger"], recovered["changed_surfaces"])
                reloaded = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
                self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
                self.assertEqual(before.lifecycle.work_items, reloaded.lifecycle.work_items)
                self.assertEqual(before.authority, reloaded.authority)
                self.assertEqual(len(before.artifact_references) + 1, len(reloaded.artifact_references))

    def test_native_post_link_sync_failure_preserves_and_reuses_exact_publication(self) -> None:
        project, work = self.initialized_publication()
        brief = work_a_brief(project)
        selector = f"artifacts/briefs/{brief.attempt_id}/1.json"
        publication = work / selector
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        original_fsync = os.fsync

        def fail_after_link(descriptor: int) -> None:
            if publication.exists():
                raise OSError("injected post-link directory sync failure")
            original_fsync(descriptor)

        with patch("pinboard.adapters.files.file_io.os.fsync", side_effect=fail_after_link):
            failure = self.publish(project, work, brief)
        self.assertEqual(
            (
                "failed-after-publication",
                "ARTIFACT_ACCEPTANCE_FAILED",
                ["immutable-artifact"],
                "do-not-retry",
                selector,
            ),
            (
                failure["status"],
                failure["code"],
                failure["changed_surfaces"],
                failure["retry"],
                failure["published_selector"],
            ),
        )
        self.assertEqual(canonical_work_brief_bytes(brief), publication.read_bytes())
        self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())
        recovered = self.publish(project, work, brief)
        reference = recovered["reference"]
        assert isinstance(reference, dict)
        self.assertEqual(selector, reference["selector"])
        self.assertEqual(["accepted-artifact-reference", "ledger"], recovered["changed_surfaces"])
        self.assertEqual(
            before.lifecycle.project.revision + 1,
            SQLiteWorkStore(work / "state.sqlite3").validated_snapshot().lifecycle.project.revision,
        )

    def test_native_programming_failures_remain_exceptional_after_publication(self) -> None:
        for error in (AssertionError("assertion failed"), ValueError("value failed")):
            with self.subTest(error=type(error).__name__):
                project, work = self.initialized_publication()
                before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
                brief = work_a_brief(project)
                with (
                    patch("pinboard.adapters.sqlite.artifacts.verify_reference", side_effect=error),
                    self.assertRaises(UnexpectedToolError) as failure,
                ):
                    self.publish(project, work, brief)
                self.assertIs(error, failure.exception.__cause__)
                selector = f"artifacts/briefs/{brief.attempt_id}/1.json"
                self.assertEqual(canonical_work_brief_bytes(brief), (work / selector).read_bytes())
                self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

    def test_native_returned_rejection_reports_new_immutable_artifact(self) -> None:
        project, work = self.initialized_publication()
        before = SQLiteWorkStore(work / "state.sqlite3").validated_snapshot()
        brief = work_a_brief(project)
        rejected = DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "acceptance changed", None)
        with patch.object(SQLiteWorkStore, "accept_artifact_reference", return_value=rejected):
            failure = self.publish(project, work, brief)
        self.assertEqual(
            ("rejected", "ACTION_NOT_AVAILABLE", ["immutable-artifact"], "do-not-retry"),
            (failure["status"], failure["code"], failure["changed_surfaces"], failure["retry"]),
        )
        selector = f"artifacts/briefs/{brief.attempt_id}/1.json"
        self.assertEqual(canonical_work_brief_bytes(brief), (work / selector).read_bytes())
        self.assertEqual(before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())


if __name__ == "__main__":
    unittest.main()
