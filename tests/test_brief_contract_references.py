"""Coverage owners and obligation targets reference contracts by position; retained invariant-text briefs stay readable."""

import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import msgspec
from msgspec.structs import replace

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import artifact_identity, work_brief_models, work_briefs
from pinboard.application.artifact_publication import AcceptedArtifactPublication, publish_accepted_artifact
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import contracts, server
from tests import test_mcp
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject
from tests.work_brief_support import ready_review, work_a_brief


def retained_invariant_text_form(brief: work_brief_models.WorkBrief) -> work_brief_models.WorkBrief:
    """Return the same brief in the retained form whose owners and targets repeat the contract's invariant text."""
    checkpoint = brief.checkpoint
    assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
    invariant = checkpoint.contracts[0].invariant
    return replace(
        brief,
        checkpoint=replace(
            checkpoint,
            coverage=tuple(
                replace(record, owner=work_brief_models.CompatibilityInvariantCoverageOwner(invariant))
                for record in checkpoint.coverage
            ),
        ),
        obligation_correspondence=tuple(
            replace(row, target=work_brief_models.CompatibilityInvariantObligationTarget(invariant))
            for row in brief.obligation_correspondence
        ),
    )


class ContractReferencePublicationTest(unittest.TestCase):
    def project(self) -> tuple[Path, DurableRoots, SQLiteWorkStore, work_brief_models.WorkBrief]:
        temporary, project, durable = test_mcp.McpTransportTest()._project()
        self.addCleanup(temporary.cleanup)
        brief = replace(work_a_brief(project), artifact_revision=2)
        return project, durable, SQLiteWorkStore(durable.database_path), brief

    def publish(self, project: Path, durable: DurableRoots, brief: JsonObject) -> JsonObject:
        return call_native_tool(
            server.BRIEF_PUBLISH_TOOL,
            {"project_root": str(project), "work_root": str(durable.work_root), "brief": brief},
        )

    def payload(self, brief: work_brief_models.WorkBrief) -> JsonObject:
        payload = msgspec.json.decode(msgspec.json.encode(brief))
        assert isinstance(payload, dict)
        return payload

    def test_reference_form_publishes_and_the_invariant_text_form_is_refused(self) -> None:
        project, durable, store, brief = self.project()
        retained = retained_invariant_text_form(brief)
        before = store.validated_snapshot()
        refused = self.publish(project, durable, self.payload(retained))
        self.assertEqual(("rejected", "WORK_BRIEF_INVALID"), (refused["status"], refused["code"]), refused)
        for identifier in ("contract-ref", "architecture#contract", "next-decision"):
            self.assertIn(identifier, str(refused["message"]))
        self.assertEqual(before, store.validated_snapshot())
        published = self.publish(project, durable, self.payload(brief))
        self.assertEqual("committed", published["status"], published)
        reference = published["reference"]
        assert isinstance(reference, dict)
        self.assertEqual(
            work_briefs.canonical_work_brief_bytes(brief), (durable.work_root / str(reference["selector"])).read_bytes()
        )

    def test_identical_republication_of_a_retained_brief_is_refused_rather_than_reused(self) -> None:
        project, durable, store, brief = self.project()
        retained = retained_invariant_text_form(brief)
        accepted = publish_accepted_artifact(
            store,
            ArtifactRepository(durable),
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                retained.attempt_id,
                retained.artifact_revision,
                ".json",
                work_briefs.canonical_work_brief_bytes(retained),
            ),
            SQLITE_NOW,
            artifact_identity.AttemptMembership(AttemptId(retained.attempt_id), artifact_identity.AttemptRole.SOURCE),
        )
        self.assertIsInstance(accepted, AcceptedArtifactPublication)
        before = store.validated_snapshot()
        refused = self.publish(project, durable, self.payload(retained))
        self.assertEqual(("rejected", "WORK_BRIEF_INVALID"), (refused["status"], refused["code"]), refused)
        self.assertEqual(before, store.validated_snapshot())

    def test_contract_positions_outside_the_contract_list_are_refused(self) -> None:
        project, durable, store, brief = self.project()
        checkpoint = brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        for position in (0, len(checkpoint.contracts) + 1):
            for path in ("coverage", "target"):
                with self.subTest(position=position, path=path):
                    changed = self.payload(brief)
                    reference: JsonObject = {"contract": position}
                    if path == "coverage":
                        checkpoint_payload = changed["checkpoint"]
                        assert isinstance(checkpoint_payload, dict)
                        coverage = checkpoint_payload["coverage"]
                        assert isinstance(coverage, list) and isinstance(coverage[0], dict)
                        coverage[0]["owner"] = reference | {"disposition": "contract-ref"}
                    else:
                        rows = changed["obligation_correspondence"]
                        assert isinstance(rows, list) and isinstance(rows[0], dict)
                        rows[0]["target"] = reference | {"kind": "contract-ref"}
                    failure = work_briefs.decode_canonical_accepted_brief(
                        msgspec.json.encode(changed, order="sorted") + b"\n"
                    )
                    assert isinstance(failure, work_brief_models.WorkBriefFailure)
                    self.assertEqual(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, failure.code)
                    before = store.validated_snapshot()
                    refused = self.publish(project, durable, changed)
                    self.assertEqual("rejected", refused["status"], refused)
                    self.assertEqual(before, store.validated_snapshot())

    def test_coverage_and_correspondence_refusals_name_their_rows(self) -> None:
        project, durable, store, brief = self.project()
        original = self.payload(brief)

        def changed(
            families: list[str] | None = None,
            coverage_families: list[str] | None = None,
            obligation_ids: list[str] | None = None,
        ) -> JsonObject:
            value = deepcopy(original)
            checkpoint = value["checkpoint"]
            assert isinstance(checkpoint, dict)
            authorities, coverage = checkpoint["reviewed_authorities"], checkpoint["coverage"]
            rows = value["obligation_correspondence"]
            assert isinstance(authorities, list) and isinstance(authorities[0], dict)
            assert isinstance(coverage, list) and isinstance(rows, list)
            if families is not None:
                authorities[0]["families"] = list(families)
            if coverage_families is not None:
                checkpoint["coverage"] = [
                    {**self.json_object(coverage[0]), "family": family} for family in coverage_families
                ]
            if obligation_ids is not None:
                value["obligation_correspondence"] = [
                    {**self.json_object(rows[0]), "obligation_id": obligation_id} for obligation_id in obligation_ids
                ]
            return value

        cases: tuple[tuple[str, JsonObject, tuple[str, ...]], ...] = (
            ("missing coverage", changed(families=["contract", "uncovered"]), ("architecture#uncovered",)),
            (
                "unreviewed coverage",
                changed(coverage_families=["contract", "unreviewed"]),
                ("architecture#unreviewed",),
            ),
            ("duplicated coverage", changed(coverage_families=["contract", "contract"]), ("architecture#contract",)),
            (
                "incomplete correspondence",
                changed(obligation_ids=["unaccepted-obligation"]),
                ("next-decision", "unaccepted-obligation"),
            ),
            (
                "duplicated correspondence",
                changed(obligation_ids=["next-decision", "next-decision"]),
                ("next-decision",),
            ),
        )
        for label, payload, identifiers in cases:
            with self.subTest(label):
                before = store.validated_snapshot()
                refused = self.publish(project, durable, payload)
                self.assertEqual("rejected", refused["status"], refused)
                for identifier in identifiers:
                    self.assertIn(identifier, str(refused["message"]))
                self.assertEqual(before, store.validated_snapshot())

    def json_object(self, value: contracts.JsonValue) -> JsonObject:
        assert isinstance(value, dict)
        return value


class RetainedInvariantTextFormTest(unittest.TestCase):
    def brief(self) -> work_brief_models.WorkBrief:
        return work_a_brief(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def test_retained_brief_and_ready_review_decode_validate_and_reencode_identically(self) -> None:
        retained = retained_invariant_text_form(self.brief())
        brief_bytes = work_briefs.canonical_work_brief_bytes(retained)
        self.assertIn(b'"disposition":"contract"', brief_bytes)
        self.assertIn(b'"kind":"contract"', brief_bytes)
        for decoded in (
            work_briefs.decode_canonical_accepted_brief(brief_bytes),
            work_briefs.decode_canonical_historical_work_brief(brief_bytes),
            work_briefs.decode_canonical_work_brief(brief_bytes),
        ):
            assert isinstance(decoded, work_brief_models.WorkBrief)
            self.assertEqual(retained, decoded)
            self.assertEqual(brief_bytes, work_briefs.canonical_work_brief_bytes(decoded))
        review_bytes = ready_review(retained)
        review = work_briefs.decode_canonical_work_brief_review(review_bytes)
        assert isinstance(review, work_brief_models.WorkBriefReview)
        self.assertEqual(review_bytes, work_briefs.canonical_work_brief_review_bytes(review))
        self.assertIsNone(work_briefs.validate_work_brief_review(review, retained))
        self.assertTrue(work_briefs.render_work_brief_markdown(retained))

    def test_ready_review_echoing_reference_owners_validates_against_its_brief(self) -> None:
        brief = self.brief()
        review = work_briefs.decode_canonical_work_brief_review(ready_review(brief))
        assert isinstance(review, work_brief_models.WorkBriefReview)
        self.assertIsNone(work_briefs.validate_work_brief_review(review, brief))
        retained = retained_invariant_text_form(brief)
        mixed = work_briefs.decode_canonical_work_brief_review(ready_review(retained))
        assert isinstance(mixed, work_brief_models.WorkBriefReview)
        echoed = replace(
            mixed,
            accepted_brief_sha256=review.accepted_brief_sha256,
            checkpoint_sha256=review.checkpoint_sha256,
        )
        failure = work_briefs.validate_work_brief_review(echoed, brief)
        assert isinstance(failure, work_brief_models.WorkBriefFailure)
        self.assertEqual(work_brief_models.WorkBriefErrorCode.REVIEW_NOT_READY, failure.code)
        self.assertEqual(
            hashlib.sha256(work_briefs.canonical_work_brief_bytes(brief)).hexdigest(), review.accepted_brief_sha256
        )


if __name__ == "__main__":
    unittest.main()
