import base64
import contextlib
import io
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.artifacts import write_revision
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, NewArtifact
from pinboard.application.handover import ContentEncoding, HandoverState, ProjectHandover, merge_handover_batches
from pinboard.domain import work_models
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ArtifactRefId, ItemId, ProposalId, TaskId
from pinboard.interfaces.cli import main
from pinboard.interfaces.cli_output import RejectedOperationView

from .support import SQLITE_NOW, complete_sqlite_state, initialize_store, test_definition


def commit_item_after_project_read(
    database: Path,
    project_read: threading.Event,
    writer_finished: threading.Event,
    writer_errors: list[AssertionError | sqlite3.Error],
) -> None:
    try:
        if not project_read.wait(5):
            raise AssertionError("The snapshot did not reach its first component read.")
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE project_meta SET revision = revision + 1")
            connection.execute(
                "UPDATE work_items SET next_action = 'committed-between-selects' WHERE item_id = 'work-a'"
            )
            connection.commit()
        finally:
            connection.close()
    except (AssertionError, sqlite3.Error) as error:
        writer_errors.append(error)
    finally:
        writer_finished.set()


class HandoverTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def accepted_reference(self, identity: int, published: ArtifactRef) -> stored_state.ArtifactReference:
        return stored_state.ArtifactReference(
            ArtifactRefId(identity),
            published.key,
            published.revision,
            published.kind,
            published.selector,
            published.content_sha256,
            published.size_bytes,
            3,
            SQLITE_NOW,
        )

    def proposal_item(
        self,
        proposal_id: ProposalId,
        position: int,
    ) -> tuple[stored_state.StoredWorkItem, stored_state.ItemDefinitionRevision]:
        item_id = ItemId(proposal_id)
        definition, digest = test_definition(item_id)
        item = stored_state.StoredWorkItem(
            item_id,
            stored_state.StoredWorkItemState.INTAKE,
            None,
            f"proposal:{proposal_id}",
            None,
            "Review the proposal.",
            "Pending relationship evidence.",
            7,
            SQLITE_NOW,
            SQLITE_NOW,
            position,
        )
        revision = stored_state.ItemDefinitionRevision(
            item_id,
            1,
            digest,
            definition,
            "Accepted test proposal definition.",
            TaskId("proposal-source"),
            None,
            digest,
            3,
            SQLITE_NOW,
        )
        return item, revision

    def complete_handover_state(
        self,
        artifacts: tuple[stored_state.ArtifactReference, ...],
    ) -> stored_state.StoredWorkState:
        state = complete_sqlite_state()
        brief, _requirements, result, review = artifacts
        current_definition = next(
            value
            for value in state.lifecycle.definition_revisions
            if value.item_id == ItemId("work-a") and value.revision == 1
        )
        revised_definition = replace(current_definition.definition, objective="Export every accepted project fact.")
        revised_digest = work_item_definition_digest(revised_definition)
        assert isinstance(revised_digest, str)
        definition_revision = stored_state.ItemDefinitionRevision(
            ItemId("work-a"),
            2,
            revised_digest,
            revised_definition,
            "Clarified the export objective.",
            TaskId("definition-owner"),
            current_definition.digest,
            revised_digest,
            9,
            SQLITE_NOW,
        )
        done_item, done_definition = self.proposal_item(ProposalId("terminal-done"), 10)
        dropped_item, dropped_definition = self.proposal_item(ProposalId("terminal-dropped"), 11)
        done_item = replace(
            done_item,
            state=stored_state.StoredWorkItemState.DONE,
            outcome_evidence="Accepted outcome.",
            queue_position=None,
        )
        dropped_item = replace(
            dropped_item,
            state=stored_state.StoredWorkItemState.DROPPED,
            outcome_evidence="No longer required.",
            queue_position=None,
        )

        relation_values: tuple[tuple[str, work_models.ProposalRelation], ...] = (
            ("proposal-independent", work_models.IndependentProposalRelation()),
            ("proposal-prerequisite", work_models.PrerequisiteProposalRelation(ItemId("work-c"))),
            ("proposal-duplicate", work_models.DuplicateProposalRelation(ItemId("work-c"))),
            ("proposal-contradiction", work_models.ContradictionProposalRelation(ItemId("work-c"))),
            ("proposal-clarification", work_models.ClarificationProposalRelation()),
        )
        proposal_items: list[stored_state.StoredWorkItem] = []
        proposal_definitions: list[stored_state.ItemDefinitionRevision] = []
        proposals: list[stored_state.StoredProposal] = list(state.proposals.proposals)
        evidence: list[stored_state.ProposalEvidence] = list(state.proposals.evidence)
        freshness: list[stored_state.ProposalFreshness] = list(state.proposals.freshness)
        for position, (identity, relation) in enumerate(relation_values, start=5):
            proposal_id = ProposalId(identity)
            item, definition = self.proposal_item(proposal_id, position)
            proposal_items.append(item)
            proposal_definitions.append(definition)
            proposals.append(
                stored_state.StoredProposal(
                    proposal_id,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    TaskId("proposal-source"),
                    identity,
                    f"Trigger for {identity}.",
                    f"Why {identity} matters.",
                    relation,
                    f"Effect for {identity}.",
                    f"Unlock for {identity}.",
                    f"Urgency for {identity}.",
                    None,
                    4,
                )
            )
            evidence.append(stored_state.ProposalEvidence(proposal_id, 0, f"evidence:{identity}"))
            freshness.append(stored_state.ProposalFreshness(proposal_id, 0, f"{identity} remains current."))
        proposals.append(
            stored_state.StoredProposal(
                ProposalId("proposal-decided"),
                SQLITE_NOW,
                SQLITE_NOW,
                TaskId("proposal-source"),
                "decided",
                "Already decided.",
                "Must not be exported as pending.",
                work_models.IndependentProposalRelation(),
                "No pending effect.",
                "Already handled.",
                "None.",
                work_models.AcceptedProposalDisposition(ItemId("work-c"), SQLITE_NOW),
                5,
            )
        )

        attempt = replace(
            state.lifecycle.attempts[0],
            state=work_models.AttemptState.REVIEW,
            brief_artifact_ref_id=brief.artifact_ref_id,
            result_artifact_ref_id=result.artifact_ref_id,
            candidate_revision="candidate-123",
            candidate_recorded_at=SQLITE_NOW,
            accepted_scope_revision=2,
            accepted_scope_digest=revised_digest,
        )
        receipt = replace(
            state.transition_receipts[0],
            artifact_ref_id=review.artifact_ref_id,
            input_payload=work_models.CanonicalJson(b'{"request":{"mode":"complete"}}'),
            outcome_payload=work_models.CanonicalJson(b'{"accepted":true,"checks":["targeted","fresh-store"]}'),
        )
        return replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=(
                    *(
                        replace(value, state=stored_state.StoredWorkItemState.REVIEW)
                        if value.item_id == ItemId("work-a")
                        else value
                        for value in state.lifecycle.work_items
                    ),
                    *proposal_items,
                    done_item,
                    dropped_item,
                ),
                attempts=(attempt,),
                definition_revisions=(
                    *state.lifecycle.definition_revisions,
                    definition_revision,
                    *proposal_definitions,
                    done_definition,
                    dropped_definition,
                ),
            ),
            proposals=stored_state.ProposalRecords(tuple(proposals), tuple(evidence), tuple(freshness)),
            artifact_references=artifacts,
            transition_receipts=(receipt,),
        )

    def initialized_project(
        self,
    ) -> tuple[Path, Path, SQLiteWorkStore, tuple[stored_state.ArtifactReference, ...]]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        published = (
            write_revision(
                roots,
                NewArtifact(work_models.ArtifactKind.BRIEF, "work-a-brief", 1, ".json", b'{"brief":"work-a"}\n'),
            ),
            write_revision(
                roots,
                NewArtifact(
                    work_models.ArtifactKind.REQUIREMENTS,
                    "work-a-requirements",
                    1,
                    ".txt",
                    b"\x00\xffrequirements",
                ),
            ),
            write_revision(
                roots,
                NewArtifact(work_models.ArtifactKind.RESULT, "work-a-result", 1, ".md", b"Result: complete.\n"),
            ),
            write_revision(
                roots,
                NewArtifact(
                    work_models.ArtifactKind.EVIDENCE,
                    "work-a-review",
                    1,
                    ".json",
                    '{"review":"ready ✓"}\n'.encode(),
                ),
            ),
        )
        references = tuple(self.accepted_reference(index, value) for index, value in enumerate(published, start=1))
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, self.complete_handover_state(references))
        views = roots.work_root / "views"
        views.mkdir()
        (views / "sentinel.md").write_text("unchanged\n", encoding="utf-8")
        return project, roots.work_root, store, references

    def decode_handover(self, payload: str) -> ProjectHandover:
        return msgspec.json.decode(payload, type=ProjectHandover, strict=True)

    def assert_handover_read_scope(self, statements: list[str]) -> None:
        reads = "\n".join(
            statement.lower() for statement in statements if statement.lstrip().lower().startswith("select")
        )
        self.assertIn("where disposition is null", reads)
        for excluded in (
            "attempt_leases",
            "preparation_leases",
            "attempt_lease_generations",
            "preparation_lease_generations",
            "attempt_lease_counters",
            "preparation_lease_counters",
            "work_item_state_counts",
        ):
            self.assertNotIn(excluded, reads)

    def run_handover_with_trace(self, common: tuple[str, ...]) -> tuple[int, str, str, list[str]]:
        statements: list[str] = []
        original_open_database = sqlite_store.open_database

        def traced_open_database(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open_database(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            patch("pinboard.adapters.sqlite.state.read_authority", side_effect=AssertionError("authority read")),
            patch("pinboard.adapters.sqlite.state.read_proposals", side_effect=AssertionError("all proposals read")),
            patch("pinboard.adapters.sqlite.store.open_database", side_effect=traced_open_database),
        ):
            result, stdout, stderr = self.run_cli(*common)
        return result, stdout, stderr, statements

    def test_installed_handover_exports_one_strict_complete_read_only_snapshot(self) -> None:
        project, work, store, references = self.initialized_project()
        common = ("--project-root", str(project), "--work-root", str(work), "handover", "--json")
        database_before = (work / "state.sqlite3").read_bytes()
        files_before = {
            str(path.relative_to(work)): path.read_bytes()
            for path in sorted(work.rglob("*"))
            if path.is_file() and path.name != "state.sqlite3"
        }
        state_before = store.validated_snapshot()

        result, stdout, stderr, statements = self.run_handover_with_trace(common)
        self.assertEqual(0, result, stderr)
        handover = self.decode_handover(stdout)
        self.assertEqual("pinboard-project-handover/v3", handover.schema)
        self.assertEqual((), handover.checkpoint_packages)
        self.assertEqual("sqlite-v5", handover.authority)
        self.assertEqual(state_before.lifecycle.project.revision, handover.revision)
        with patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")):
            self.assertEqual(stdout, self.run_cli(*common)[1])

        self.assertEqual(
            {"done", "superseded", "dropped"},
            {item.state.value for item in handover.work_items if item.state.value in {"done", "superseded", "dropped"}},
        )
        self.assertEqual(
            [1, 2],
            [value.revision for value in handover.definition_revisions if value.item_id == "work-a"],
        )
        latest_definition = next(
            value for value in handover.definition_revisions if value.item_id == "work-a" and value.revision == 2
        )
        self.assertEqual("Clarified the export objective.", latest_definition.reason)
        self.assertEqual("definition-owner", latest_definition.source_task_id)
        self.assertEqual(
            {
                "IndependentProposalRelation",
                "PrerequisiteProposalRelation",
                "FollowUpProposalRelation",
                "DuplicateProposalRelation",
                "ContradictionProposalRelation",
                "ClarificationProposalRelation",
            },
            {type(value).__name__ for value in handover.proposal_relations},
        )
        self.assertNotIn("proposal-decided", {value.proposal_id for value in handover.proposals})
        self.assert_handover_read_scope(statements)
        self.assertEqual("candidate-123", handover.attempts[0].candidate_revision)
        self.assertEqual(
            [(1, "brief"), (3, "result"), (4, "evidence")],
            [(value.artifact_ref_id, value.role.value) for value in handover.item_artifact_links],
        )
        self.assertEqual(4, len(handover.artifact_references))
        self.assertEqual(4, len(handover.artifact_contents))
        self.assertEqual(
            [int(value.artifact_ref_id) for value in references],
            [value.artifact_ref_id for value in handover.artifact_references],
        )
        contents = {value.artifact_ref_id: value for value in handover.artifact_contents}
        for reference in references:
            content = contents[int(reference.artifact_ref_id)]
            decoded = (
                content.content.encode()
                if content.encoding is ContentEncoding.UTF8
                else base64.b64decode(content.content, validate=True)
            )
            self.assertEqual((work / reference.selector).read_bytes(), decoded)
        self.assertEqual(ContentEncoding.BASE64, contents[2].encoding)
        sparse_item = next(value for value in handover.work_items if value.item_id == "intake-work")
        self.assertIsNone(sparse_item.source)
        self.assertIsNone(sparse_item.notes)
        terminal_item = next(value for value in handover.work_items if value.item_id == "terminal-done")
        self.assertIsNone(terminal_item.queue_position)
        self.assertEqual({"request": {"mode": "complete"}}, msgspec.json.decode(handover.transitions[0].input))
        self.assertEqual(
            {"accepted": True, "checks": ["targeted", "fresh-store"]},
            msgspec.json.decode(handover.transitions[0].outcome),
        )

        self.assertEqual(database_before, (work / "state.sqlite3").read_bytes())
        self.assertEqual(
            files_before,
            {
                str(path.relative_to(work)): path.read_bytes()
                for path in sorted(work.rglob("*"))
                if path.is_file() and path.name != "state.sqlite3"
            },
        )
        self.assertEqual(state_before, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot())

        decoded = msgspec.json.decode(stdout)
        assert isinstance(decoded, dict)
        decoded["unexpected"] = True
        with self.assertRaises(msgspec.DecodeError):
            msgspec.json.decode(msgspec.json.encode(decoded), type=ProjectHandover, strict=True)

    def test_installed_handover_keeps_one_revision_during_a_concurrent_commit(self) -> None:
        project, work, store, _references = self.initialized_project()
        database = work / "state.sqlite3"
        before = store.validated_snapshot()
        journal_connection = sqlite3.connect(database, isolation_level=None)
        try:
            self.assertEqual("wal", journal_connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        finally:
            journal_connection.close()

        project_read = threading.Event()
        writer_finished = threading.Event()
        writer_errors: list[AssertionError | sqlite3.Error] = []
        original_read_project = sqlite_state._read_project

        def read_project_then_wait(connection: sqlite3.Connection) -> stored_state.ProjectRecord:
            record = original_read_project(connection)
            project_read.set()
            if not writer_finished.wait(5):
                raise AssertionError("The concurrent writer did not finish during the snapshot read.")
            return record

        writer = threading.Thread(
            target=commit_item_after_project_read,
            args=(database, project_read, writer_finished, writer_errors),
        )
        writer.start()
        try:
            with patch("pinboard.adapters.sqlite.state._read_project", side_effect=read_project_then_wait):
                result, stdout, stderr = self.run_cli(
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "handover",
                    "--json",
                )
        finally:
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual([], writer_errors)
        self.assertEqual(0, result, stderr)

        handover = self.decode_handover(stdout)
        after = store.validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(
            "committed-between-selects",
            next(value.next_action for value in after.lifecycle.work_items if value.item_id == ItemId("work-a")),
        )
        self.assertEqual(before.lifecycle.project.revision, handover.revision)
        self.assertEqual(
            next(value.next_action for value in before.lifecycle.work_items if value.item_id == ItemId("work-a")),
            next(value.next_action for value in handover.work_items if value.item_id == "work-a"),
        )

    def test_handover_application_boundary_accepts_multiple_batches(self) -> None:
        _project, _work, store, _references = self.initialized_project()
        state = store.read_handover_batches()[0]
        first = HandoverState(
            replace(
                state.lifecycle,
                attempts=(),
                definition_revisions=(),
            ),
            replace(state.proposals, evidence=(), freshness=()),
            (),
            (),
        )
        second = HandoverState(
            replace(
                state.lifecycle,
                work_items=(),
                dependencies=(),
            ),
            replace(state.proposals, proposals=()),
            state.artifact_references,
            state.transition_receipts,
        )

        self.assertEqual(state, merge_handover_batches(iter((first, second))))

    def test_artifact_failure_reports_structured_rejection_and_changes_no_state(self) -> None:
        for failure in ("missing", "digest-mismatch"):
            with self.subTest(failure=failure):
                project, work, store, references = self.initialized_project()
                common = ("--project-root", str(project), "--work-root", str(work), "handover", "--json")
                before = store.validated_snapshot()
                database_before = (work / "state.sqlite3").read_bytes()
                artifact = work / references[-1].selector
                if failure == "missing":
                    artifact.unlink()
                else:
                    artifact.write_bytes(b"different bytes")

                result, stdout, stderr = self.run_cli(*common)

                self.assertEqual(12, result)
                self.assertEqual("", stderr)
                rejection = msgspec.json.decode(stdout, type=RejectedOperationView)
                self.assertEqual("STORAGE_INVARIANT_VIOLATION", rejection.code)
                self.assertFalse(rejection.state_changed)
                self.assertEqual(database_before, (work / "state.sqlite3").read_bytes())
                self.assertEqual(before, store.validated_snapshot())

    def test_unsupported_artifact_media_type_is_rejected_without_output(self) -> None:
        project, work, store, references = self.initialized_project()
        connection = sqlite3.connect(work / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE artifact_refs SET relative_path = ? WHERE artifact_ref_id = ?",
                ("artifacts/requirements/work-a-requirements/1.bin", int(references[1].artifact_ref_id)),
            )
            connection.commit()
        finally:
            connection.close()
        before = store.validated_snapshot()

        result, stdout, stderr = self.run_cli(
            "--project-root", str(project), "--work-root", str(work), "handover", "--json"
        )

        self.assertEqual(12, result)
        self.assertEqual("", stderr)
        rejection = msgspec.json.decode(stdout, type=RejectedOperationView)
        self.assertEqual("STORAGE_INVARIANT_VIOLATION", rejection.code)
        self.assertIn("Unsupported artifact media suffix: .bin", rejection.message)
        self.assertFalse(rejection.state_changed)
        self.assertEqual(before, store.validated_snapshot())


if __name__ == "__main__":
    unittest.main()
