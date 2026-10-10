import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import dispatch_models, history_archives, stored_state, work_brief_models, work_briefs
from pinboard.application.artifact_publication import AcceptedArtifactPublication
from pinboard.application.artifacts import NewArtifact
from pinboard.application.project_export import ProjectExport
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import WorkItemId
from pinboard.mcp import server as mcp_server
from tests.artifact_support import write_revision
from tests.checkpoint_support import AcceptedPackageFixture
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject
from tests.test_close_and_completion_enforcement import SubmittedCandidateSupport
from tests.test_proposals import proposal
from tests.work_brief_support import work_a_brief


class HistoryArchiveTest(SubmittedCandidateSupport):
    def completed_fixture(self) -> AcceptedPackageFixture:
        local_brief = self.local_brief

        def overlapping_brief(project: Path) -> work_brief_models.WorkBrief:
            brief = local_brief(project)
            return replace_struct(brief, checkpoint=replace_struct(brief.checkpoint, checkpoint_id="sibling-1"))

        with patch.object(self, "local_brief", side_effect=overlapping_brief):
            fixture, candidate = self.review_fixture()
        fixture = self.terminalize_brief(fixture)
        self.record_commissioned_review(fixture, candidate, "archive-reviewer")
        state = fixture.store.validated_snapshot()
        checkpoint = next(
            value for value in state.transition_receipts if value.outcome_schema == "checkpoint-acceptance/v2"
        )
        attempt_root = fixture.work / "attempts" / "work-a-1"
        payload: JsonObject = {
            "schema": "pinboard-reviewed-completion/v2",
            "candidate": candidate,
            "evidence": "The original accepted closure is complete.",
            "reviewer_task_id": "archive-reviewer",
            "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
            "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
            "packages": [
                {
                    "history_id": int(checkpoint.history_id),
                    "package_sha256": fixture.package_reference.content_sha256,
                    "disposition": "revalidated",
                    "evidence": "Current snapshot and historical relationship verified.",
                }
            ],
        }
        result = self.transition_result(fixture, self.project_action(fixture, "complete:work-a-1"), payload)
        self.assertEqual("committed", result["status"], result)
        return fixture

    def create_proposal(self, fixture: AcceptedPackageFixture, item_id: str) -> None:
        sibling_proposal = self.json_object(json.loads(json.dumps(proposal())))
        sibling_proposal.update(
            proposal_id=item_id,
            obligations=[
                {
                    "obligation_id": "next-decision",
                    "statement": "Preserve current sibling work.",
                    "deferral_policy": "forbidden",
                }
            ],
        )
        created = call_native_tool(
            mcp_server.PROPOSAL_CREATE_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "actor_task_id": "sibling-coordinator",
                "actor_host_id": "local",
                "proposal": sibling_proposal,
            },
        )
        self.assertEqual("committed", created["status"], created)

    def publish_sibling(self, fixture: AcceptedPackageFixture, artifacts: ArtifactRepository) -> None:
        sibling_item = "work-a-1-sibling"
        self.create_proposal(fixture, sibling_item)
        definition = fixture.store.read_item_definition(WorkItemId(sibling_item)).definition
        assert definition is not None
        brief = work_a_brief(fixture.project)
        checkpoint = brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        authorization = work_brief_models.AcceptedScopeAuthorization(sibling_item, definition.revision)
        sibling = replace_struct(
            brief,
            attempt_id=f"{sibling_item}-1",
            item_id=sibling_item,
            accepted_scope=work_brief_models.AcceptedScope(definition.revision, definition.digest),
            checkpoint=replace_struct(
                checkpoint,
                contracts=tuple(
                    replace_struct(value, authorization_basis=authorization) for value in checkpoint.contracts
                ),
                verification=tuple(
                    replace_struct(value, authorization_basis=authorization) for value in checkpoint.verification
                ),
            ),
        )
        published_brief = work_briefs.publish_work_brief(fixture.store, artifacts, sibling, SQLITE_NOW)
        self.assertIsInstance(published_brief, AcceptedArtifactPublication, published_brief)
        prompt = dispatch_models.publish_agent_prompt(
            fixture.store,
            artifacts,
            subject=dispatch_models.WorkerPromptSubject(sibling.attempt_id),
            prompt="Perform this independent sibling task.",
            accepted_at=SQLITE_NOW,
        )
        self.assertIsInstance(prompt, dispatch_models.PublishedAgentPrompt, prompt)
        for kind, suffix in (
            (work_models.ArtifactKind.RESULT, "terminal-result"),
            (work_models.ArtifactKind.EVIDENCE, "terminal-review"),
        ):
            roots = resolve_durable_roots(fixture.project, fixture.work)
            sibling_artifact = write_revision(
                roots, NewArtifact(kind, f"{sibling.attempt_id}-{suffix}", 1, ".md", b"Sibling evidence.\n")
            )
            self.assertNotIsInstance(
                fixture.store.accept_artifact_reference(fixture.work, sibling_artifact, SQLITE_NOW), DecisionFailure
            )

    def assert_archive_readable(
        self, fixture: AcceptedPackageFixture, archive: history_archives.HistoryArchive
    ) -> None:
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        self.assertEqual(0, self.run_cli(*fixture.common, "export", "--json")[0])
        self.assertEqual(0, self.run_cli(*fixture.common, "views", "rebuild")[0])
        self.assertEqual(
            history_archives.render_archive(archive), (fixture.work / "views/attempts/work-a-1.md").read_bytes()
        )

    def assert_same_name_work_preserves_archive(
        self,
        fixture: AcceptedPackageFixture,
        archive: history_archives.HistoryArchive,
        facts: stored_state.ArchiveHistoryFacts,
    ) -> None:
        self.create_proposal(fixture, archive.attempt_id)
        self.assert_archive_readable(fixture, archive)
        prepared = call_native_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "start",
                    "item_id": archive.attempt_id,
                    "task_id": "same-name-preparer",
                    "host_id": "local",
                    "ttl_seconds": 300,
                }
            },
        )
        self.assertEqual("committed", prepared["status"], prepared)
        self.assert_archive_readable(fixture, archive)
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        selected = history_archives.select_archive_facts(
            facts.attempt,
            reloaded.artifact_references,
            reloaded.transition_receipts,
            reloaded.lifecycle.definition_revisions,
        )
        self.assertEqual(facts.transition_receipts, selected.transition_receipts)
        self.assertIn("attempt-authority/v1", {value.input_schema for value in selected.transition_receipts})
        self.assertIn("submit-review", {value.action_kind.value for value in selected.transition_receipts})

    def test_archive_preserves_exact_history_through_sibling_and_same_name_proposal_publication(self) -> None:
        fixture = self.completed_fixture()
        original = fixture.store.validated_snapshot()
        attempt = original.lifecycle.attempts[0]
        facts = history_archives.select_archive_facts(
            attempt, original.artifact_references, original.transition_receipts, original.lifecycle.definition_revisions
        )
        roots = resolve_durable_roots(fixture.project, fixture.work)
        artifacts = ArtifactRepository(roots)
        source_bytes = {value.artifact_ref_id: artifacts.read(value) for value in facts.artifact_references}
        archive = history_archives.derive_archive(facts, source_bytes, ())
        assert isinstance(archive, history_archives.HistoryArchive)
        self.assertIsInstance(archive.checkpoints[0].candidate, history_archives.CompleteCandidate)
        self.assertEqual((archive.checkpoints[0].history_id,), archive.completions[0].checkpoint_history_ids)
        published = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                history_archives.archive_key(archive.attempt_id),
                1,
                ".json",
                history_archives.canonical_archive_bytes(archive),
            ),
        )
        accepted = fixture.store.accept_artifact_reference(fixture.work, published, SQLITE_NOW)
        assert not isinstance(accepted, DecisionFailure)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(original.lifecycle.work_items, fresh.lifecycle.work_items)
        self.assertEqual(original.lifecycle.attempts, fresh.lifecycle.attempts)
        self.assertEqual(original.lifecycle.definition_revisions, fresh.lifecycle.definition_revisions)
        self.assertEqual(original.transition_receipts, fresh.transition_receipts)
        self.assertEqual(original.artifact_references, fresh.artifact_references[:-1])
        self.assertEqual(0, self.run_cli(*fixture.common, "views", "rebuild")[0])
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        code, exported, errors = self.run_cli(*fixture.common, "export", "--json")
        self.assertEqual(0, code, errors)
        portable = msgspec.json.decode(exported, type=ProjectExport)
        self.assertEqual(1, len(portable.checkpoint_packages))
        self.assertEqual(1, len(portable.completion_packages))
        self.assertEqual(
            history_archives.render_archive(archive), (fixture.work / "views/attempts/work-a-1.md").read_bytes()
        )
        self.assertEqual(
            source_bytes, {value.artifact_ref_id: artifacts.read(value) for value in facts.artifact_references}
        )
        self.publish_sibling(fixture, artifacts)
        self.assert_archive_readable(fixture, archive)
        self.assert_same_name_work_preserves_archive(fixture, archive, facts)
        with patch(
            "pinboard.application.work_briefs.validate_executable_work_brief",
            side_effect=AssertionError("archive selected execution"),
        ):
            self.assertIsInstance(
                history_archives.derive_archive(
                    replace(facts, attempt=replace(attempt, state=work_models.AttemptState.ACTIVE)), source_bytes, ()
                ),
                work_brief_models.WorkBriefFailure,
            )
        for changed in (
            replace_struct(archive, attempt_sha256="0" * 64),
            replace_struct(
                archive,
                briefs=(replace_struct(archive.briefs[0], markdown="Invented accepted meaning"), *archive.briefs[1:]),
            ),
        ):
            with self.subTest(changed=changed.attempt_sha256):
                encoded = history_archives.canonical_archive_bytes(changed)
                reference = replace(
                    accepted.reference, content_sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded)
                )
                self.assertIsInstance(
                    history_archives.verify_archive(reference, encoded, facts, source_bytes),
                    work_brief_models.WorkBriefFailure,
                )
        for wrong_reference in (
            replace(accepted.reference, kind=work_models.ArtifactKind.RESULT),
            replace(accepted.reference, selector="artifacts/evidence/wrong/1.json"),
        ):
            self.assertIsInstance(
                history_archives.verify_archive(
                    wrong_reference, artifacts.read(accepted.reference), facts, source_bytes
                ),
                work_brief_models.WorkBriefFailure,
            )
        missing = dict(source_bytes)
        del missing[facts.artifact_references[0].artifact_ref_id]
        self.assertIsInstance(
            history_archives.verify_archive(accepted.reference, artifacts.read(accepted.reference), facts, missing),
            work_brief_models.WorkBriefFailure,
        )
        payload = json.loads(history_archives.canonical_archive_bytes(archive))
        for malformed in (
            payload | {"unknown": True},
            payload | {"sources": [payload["sources"][0], payload["sources"][0]]},
        ):
            self.assertIsInstance(
                history_archives.decode_archive(msgspec.json.encode(malformed, order="sorted") + b"\n"),
                work_brief_models.WorkBriefFailure,
            )
        self.assertIsInstance(history_archives.decode_archive(b"{}"), work_brief_models.WorkBriefFailure)
        self.assertIsInstance(
            history_archives.decode_archive(history_archives.canonical_archive_bytes(archive).rstrip()),
            work_brief_models.WorkBriefFailure,
        )
        self.replace_artifact_bytes(
            fixture,
            accepted.reference,
            history_archives.canonical_archive_bytes(replace_struct(archive, attempt_sha256="0" * 64)),
        )
        self.assertEqual(10, self.run_cli(*fixture.common, "validate", "--json")[0])
        self.assertNotEqual(0, self.run_cli(*fixture.common, "export", "--json")[0])
        self.assertNotEqual(0, self.run_cli(*fixture.common, "views", "rebuild")[0])

    def test_retired_checkpoint_uses_captured_facts_and_rejects_broken_bindings(self) -> None:
        fixture = self.completed_fixture()
        state = fixture.store.validated_snapshot()
        facts = history_archives.select_archive_facts(
            state.lifecycle.attempts[0],
            state.artifact_references,
            state.transition_receipts,
            state.lifecycle.definition_revisions,
        )
        artifacts = ArtifactRepository(resolve_durable_roots(fixture.project, fixture.work))
        source_bytes = {value.artifact_ref_id: artifacts.read(value) for value in facts.artifact_references}
        archive = history_archives.derive_archive(facts, source_bytes, ())
        assert isinstance(archive, history_archives.HistoryArchive)
        checkpoint = archive.checkpoints[0]
        package_reference = next(
            value
            for value in facts.artifact_references
            if int(value.artifact_ref_id) == checkpoint.package_artifact_ref_id
        )
        candidate_reference = next(value for value in facts.artifact_references if value.key.endswith("-candidate"))
        patch_bytes = b"historical patch bytes"
        patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
        candidate = f"working-tree-sha256:{patch_sha256}"
        package = json.loads(source_bytes[package_reference.artifact_ref_id])
        package.update(schema="pinboard-checkpoint-review-package/v1", candidate=candidate)
        package.pop("candidate_snapshot")
        package_bytes = msgspec.json.encode(package, order="sorted") + b"\n"
        references = tuple(
            replace(value, content_sha256=hashlib.sha256(package_bytes).hexdigest(), size_bytes=len(package_bytes))
            if value == package_reference
            else replace(value, content_sha256=patch_sha256, size_bytes=len(patch_bytes))
            if value == candidate_reference
            else value
            for value in facts.artifact_references
        )
        source_bytes[package_reference.artifact_ref_id] = package_bytes
        source_bytes[candidate_reference.artifact_ref_id] = patch_bytes
        receipts = []
        for receipt in facts.transition_receipts:
            if int(receipt.history_id) == checkpoint.history_id:
                outcome = json.loads(bytes(receipt.outcome_payload))
                outcome["candidate"] = candidate
                receipt = replace(
                    receipt, outcome_payload=work_models.CanonicalJson(msgspec.json.encode(outcome, order="sorted"))
                )
            receipts.append(receipt)
        facts = replace(facts, artifact_references=references, transition_receipts=tuple(receipts))
        captured = replace_struct(
            checkpoint, candidate=history_archives.PatchCandidate(candidate, int(candidate_reference.artifact_ref_id))
        )
        derived = history_archives.derive_archive(facts, source_bytes, (captured,))
        assert isinstance(derived, history_archives.HistoryArchive)
        self.assertEqual(captured, derived.checkpoints[0])
        for captured_checkpoints in (
            (),
            (replace_struct(captured, package_artifact_ref_id=999999),),
            (replace_struct(captured, checkpoint_id="foreign"),),
            (replace_struct(captured, accepted_scope_digest="0" * 64),),
            (replace_struct(captured, candidate=history_archives.UnavailableCandidate(candidate)),),
            (replace_struct(captured, candidate=replace_struct(captured.candidate, artifact_ref_id=999999)),),
            (checkpoint,),
        ):
            with self.subTest(captured=captured_checkpoints):
                self.assertIsInstance(
                    history_archives.derive_archive(facts, source_bytes, captured_checkpoints),
                    work_brief_models.WorkBriefFailure,
                )
        missing_candidate_facts = replace(
            facts,
            artifact_references=tuple(
                value
                for value in references
                if value
                != next(value for value in references if value.artifact_ref_id == candidate_reference.artifact_ref_id)
            ),
        )
        unavailable = replace_struct(captured, candidate=history_archives.UnavailableCandidate(candidate))
        self.assertIsInstance(
            history_archives.derive_archive(missing_candidate_facts, source_bytes, (unavailable,)),
            history_archives.HistoryArchive,
        )
        self.assertIsInstance(
            history_archives.derive_archive(missing_candidate_facts, source_bytes, (captured,)),
            work_brief_models.WorkBriefFailure,
        )
        invalid_receipts = tuple(
            replace(value, outcome_payload=work_models.CanonicalJson(b"{}"))
            if int(value.history_id) == captured.history_id
            else value
            for value in receipts
        )
        self.assertIsInstance(
            history_archives.derive_archive(
                replace(facts, transition_receipts=invalid_receipts), source_bytes, (captured,)
            ),
            work_brief_models.WorkBriefFailure,
        )
