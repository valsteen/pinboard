"""Shared checkpoint fixtures through native workflow boundaries and retained CLI checks."""

import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    candidate_snapshots,
    checkpoint_compatibility_models,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application.artifacts import NewArtifact
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.application.work_briefs import (
    canonical_checkpoint_review_package_bytes,
    canonical_work_brief_bytes,
    canonical_work_brief_review_bytes,
    decode_canonical_checkpoint_review_package,
)
from pinboard.cli.entrypoint import main
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, ItemId
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import mutation_operations as mcp_mutations
from pinboard.mcp import server as mcp_server
from tests.artifact_support import write_revision
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state, initialize_store
from tests.work_brief_support import ready_review, work_a_brief


@dataclass(frozen=True, slots=True)
class CheckpointFixture:
    project: Path
    work: Path
    store: SQLiteWorkStore
    brief: work_brief_models.WorkBrief
    candidate_revision: str
    candidate_bytes: bytes
    payload: Path

    @property
    def common(self) -> tuple[str, ...]:
        return "--project-root", str(self.project), "--work-root", str(self.work)


@dataclass(frozen=True, slots=True)
class AcceptedPackageFixture(CheckpointFixture):
    package_reference: stored_state.ArtifactReference


class CheckpointPackageSupport(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def json_array(self, value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            self.fail("JSON value must be an array")
        return value

    def run_json_cli(self, *arguments: str) -> JsonObject:
        result, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, f"{stdout}\n{stderr}")
        return self.json_object(json.loads(stdout))

    def commit_all(self, project: Path, message: str) -> str:
        subprocess.run(["git", "add", "--all"], cwd=project, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "commit",
                "-m",
                message,
            ],
            cwd=project,
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
        ).stdout.strip()

    def native_actions(
        self,
        fixture: CheckpointFixture,
        kind: str,
        subject: str,
        *,
        role: str = "project",
        lease: JsonObject | None = None,
    ) -> JsonObject:
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "role": role,
            "action_id": {"kind": kind, "subject": subject},
        }
        if lease is not None:
            request.update(lease_id=lease["lease_id"], generation=lease["generation"])
        result = call_native_tool(mcp_server.ACTIONS_TOOL, {"request": request})
        self.assertEqual("ok", result["status"], result)
        values = result["actions"]
        if not isinstance(values, list) or len(values) != 1:
            self.fail("Expected one exact native action")
        return self.json_object(values[0])

    def actions_result(self, fixture: CheckpointFixture, query: JsonObject) -> JsonObject:
        return call_native_tool(
            mcp_server.ACTIONS_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    **query,
                }
            },
        )

    def review_result(self, fixture: CheckpointFixture, review: JsonObject) -> JsonObject:
        return call_native_tool(
            mcp_server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": {
                    "attempt_id": "work-a-1",
                    "candidate_revision": "b" * 40,
                    "runtime": "codex",
                    "background": False,
                    **review,
                },
            },
        )

    def transition_result(self, fixture: CheckpointFixture, action: JsonObject, payload: JsonObject) -> JsonObject:
        return call_native_tool(
            mcp_server.TRANSITION_TOOL,
            self.native_transition_request(fixture, action, payload),
        )

    def project_action(self, fixture: CheckpointFixture, action_id: str) -> JsonObject:
        kind, subject = action_id.split(":", 1)
        return self.native_actions(fixture, kind, subject)

    def native_attempt_acquire(self, fixture: CheckpointFixture, worker: str) -> JsonObject:
        result = mcp_mutations._attempt_authority(
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "acquire",
                    "attempt_id": "work-a-1",
                    "task_id": worker,
                    "host_id": "local",
                    "ttl_seconds": 300,
                }
            },
            mcp_execution.CancellationToken(),
        )
        self.assertEqual("committed", result.content["status"], result.content)
        return result.content

    def native_transition_request(
        self,
        fixture: CheckpointFixture,
        action: JsonObject,
        payload: JsonObject,
        *,
        task_id: str = "review-owner",
    ) -> JsonObject:
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "role": "project" if action["authorization"] == "project" else "worker",
            "receipt": {
                "action_id": action["action_id"],
                "subject_revision": action["subject_revision"],
            },
            "payload": payload,
        }
        if action["authorization"] == "project":
            request.update(actor_task_id=task_id, actor_host_id="local")
        else:
            request.update(lease_id=action["lease_id"], generation=action["generation"])
        return {"request": request}

    def transition_json(
        self,
        fixture: CheckpointFixture,
        action: JsonObject,
        payload: Path,
    ) -> JsonObject:
        result = mcp_mutations._transition(
            self.native_transition_request(fixture, action, self.json_object(json.loads(payload.read_bytes()))),
            mcp_execution.CancellationToken(),
        )
        self.assertEqual("committed", result.content["status"], result.content)
        return result.content

    def submit_review(self, fixture: AcceptedPackageFixture, label: str, worker: str) -> str:
        tracked = fixture.project / "tracked.txt"
        tracked.write_text(f"{label}\n", encoding="utf-8")
        candidate_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        ).stdout
        preimage = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        candidate = working_tree_identity(preimage, candidate_diff)
        lease = self.native_attempt_acquire(fixture, worker)
        selected = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        payload = fixture.project / f"submit-{candidate}.json"
        payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        self.transition_json(fixture, selected, payload)
        return candidate

    def return_for_correction(self, fixture: AcceptedPackageFixture, reason: str, suffix: str) -> int:
        payload = fixture.project / f"return-{suffix}.json"
        payload.write_text(json.dumps({"reason": reason}), encoding="utf-8")
        rendered = self.transition_json(
            fixture,
            self.project_action(fixture, "return-for-correction:work-a-1"),
            payload,
        )
        history_id = rendered["history_id"]
        if not isinstance(history_id, int):
            self.fail("Transition must expose its committed history ID")
        return history_id

    def accept_checkpoint(
        self,
        fixture: CheckpointFixture,
        action: JsonObject,
        payload: Path,
    ) -> None:
        self.transition_json(fixture, action, payload)

    def local_brief(self, project: Path) -> work_brief_models.WorkBrief:
        candidate = work_a_brief(project)
        checkpoint = candidate.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        return replace_struct(
            candidate,
            checkpoint=work_brief_models.LocalCheckpoint(
                "local-package",
                "Preserve local evidence",
                work_brief_models.NoArchitectureImpact("The package owner remains unchanged."),
                "The local package remains reusable.",
                checkpoint.acceptance_criteria,
                (
                    work_brief_models.VerificationRecord(
                        work_brief_models.AcceptedScopeAuthorization("work-a", 1),
                        "Run the installed package path.",
                    ),
                ),
                (),
            ),
            obligation_correspondence=(
                work_brief_models.ObligationCorrespondence(
                    "next-decision",
                    work_brief_models.CriterionObligationTarget(checkpoint.acceptance_criteria[0].number),
                ),
            ),
        )

    def accept_candidate_snapshot(
        self,
        roots: DurableRoots,
        store: SQLiteWorkStore,
        attempt: stored_state.StoredAttempt,
        candidate_form: Literal["working-tree", "current-head"],
        candidate_revision: str,
        candidate_diff: bytes,
        preimage_revision: str,
        accepted_base_revision: str,
        recorded_at: datetime,
    ) -> None:
        if candidate_form == "working-tree":
            snapshot = candidate_snapshots.WorkingTreeCandidateSnapshot(
                "pinboard-candidate-snapshot/v2",
                str(attempt.attempt_id),
                str(attempt.item_id),
                candidate_revision,
                attempt.branch,
                preimage_revision,
                accepted_base_revision,
                recorded_at.isoformat(),
                candidate_diff,
            )
        else:
            snapshot = candidate_snapshots.CommitCandidateSnapshot(
                "pinboard-candidate-snapshot/v1",
                str(attempt.attempt_id),
                str(attempt.item_id),
                candidate_revision,
                attempt.branch,
                preimage_revision,
                accepted_base_revision,
                recorded_at.isoformat(),
                candidate_diff,
            )
        published = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.EVIDENCE,
                candidate_snapshots.candidate_snapshot_key(snapshot),
                1,
                ".json",
                candidate_snapshots.canonical_candidate_snapshot_bytes(snapshot),
            ),
        )
        accepted = store.accept_artifact_reference(roots.work_root, published, recorded_at)
        if isinstance(accepted, DecisionFailure):
            self.fail(str(accepted))
        with contextlib.closing(sqlite3.connect(roots.database_path)) as connection, connection:
            history_id = connection.execute(
                "SELECT COALESCE(MAX(history_id), 0) + 1 FROM transition_history"
            ).fetchone()[0]
            project_revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0]
            reference_id = int(accepted.reference.artifact_ref_id)
            connection.execute(
                """
                INSERT INTO transition_history(
                    history_id, project_revision, action_id, action_kind, subject_id,
                    artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                    input_schema, input_json, outcome_schema, outcome_json, committed_at
                ) VALUES (?, ?, 'submit-review:work-a-1', 'submit-review', 'work-a-1', ?, 'evidence',
                          'attempt', NULL, NULL, ?, ?,
                          'transition-receipt/v1', ?, ?)
                """,
                (
                    history_id,
                    project_revision,
                    reference_id,
                    snapshot.schema,
                    msgspec.json.encode(
                        {"candidate": candidate_revision, "snapshot_artifact_ref_id": reference_id},
                        order="sorted",
                    ).decode(),
                    history.encode_transition_receipt_outcome(
                        evidence=None,
                        outcome="submit-review",
                        candidate=candidate_revision,
                    ).decode(),
                    recorded_at.isoformat(),
                ),
            )

    def record_review_candidate(self, fixture: AcceptedPackageFixture, candidate: str) -> None:
        state = fixture.store.validated_snapshot()
        attempt = next(value for value in state.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.accept_candidate_snapshot(
            resolve_durable_roots(fixture.project),
            fixture.store,
            attempt,
            "current-head",
            candidate,
            fixture.candidate_bytes,
            attempt.base_revision,
            attempt.base_revision,
            SQLITE_NOW,
        )
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            current_state = connection.execute("SELECT state FROM work_items WHERE item_id = 'work-a'").fetchone()[0]
            connection.execute("UPDATE work_items SET state = 'review' WHERE item_id = 'work-a'")
            if current_state != "review":
                connection.execute(
                    "UPDATE work_item_state_counts SET item_count = item_count - 1 WHERE state = ?",
                    (current_state,),
                )
                connection.execute(
                    "UPDATE work_item_state_counts SET item_count = item_count + 1 WHERE state = 'review'"
                )
            connection.execute(
                """
                UPDATE attempts
                SET state = 'review', candidate_revision = ?, candidate_recorded_at = ?
                WHERE attempt_id = 'work-a-1'
                """,
                (candidate, SQLITE_NOW.isoformat()),
            )

    def checkpoint_fixture(  # noqa: PLR0915 - one persisted candidate and review fixture
        self,
        *,
        local: bool = False,
        candidate_form: Literal["working-tree", "current-head"] = "working-tree",
        accepted_base: str | None = None,
        committed_context: bool = False,
        review_condition: Literal["ready", "missing", "malformed", "stale", "wrong-owner"] = "ready",
    ) -> CheckpointFixture:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.REVIEW)
                    if value.item_id == ItemId("work-a")
                    else value
                    for value in state.lifecycle.work_items
                ),
                attempts=tuple(
                    replace(
                        value,
                        state=work_models.AttemptState.REVIEW,
                        candidate_revision="candidate-a",
                        candidate_recorded_at=now,
                    )
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in state.lifecycle.attempts
                ),
            ),
            artifact_references=(state.artifact_references[0],),
            transition_receipts=(
                replace(
                    state.transition_receipts[0],
                    artifact_ref_id=None,
                    outcome_schema="transition-receipt/v1",
                ),
            ),
        )
        project = Path(tempfile.mkdtemp()).resolve()
        brief = self.local_brief(project) if local else work_a_brief(project)
        subprocess.run(["git", "init", "-b", "codex/work-a"], cwd=project, check=True, capture_output=True)
        (project / ".git" / "info" / "exclude").write_text("/.codex/pinboard/\n", encoding="utf-8")
        tracked = project / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        base_revision = self.commit_all(project, "base")
        preimage_revision = base_revision
        if committed_context:
            (project / "context.txt").write_text("committed surrounding state\n", encoding="utf-8")
            preimage_revision = self.commit_all(project, "context")
        tracked.write_text("candidate\n", encoding="utf-8")
        if candidate_form == "current-head":
            candidate_revision = self.commit_all(project, "candidate")
            candidate_diff = subprocess.run(
                ["git", "diff", "--binary", base_revision, candidate_revision, "--"],
                cwd=project,
                check=True,
                capture_output=True,
            ).stdout
        else:
            candidate_diff = subprocess.run(
                ["git", "diff", "--binary", "HEAD", "--"],
                cwd=project,
                check=True,
                capture_output=True,
            ).stdout
            candidate_revision = working_tree_identity(preimage_revision, candidate_diff)
        brief_base_revision = base_revision if accepted_base is None else accepted_base
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                attempts=tuple(
                    replace(value, candidate_revision=candidate_revision, base_revision=brief_base_revision)
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in state.lifecycle.attempts
                ),
            ),
        )
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        brief = replace_struct(brief, base_revision=brief_base_revision)
        published_brief = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                brief.attempt_id,
                brief.artifact_revision,
                ".json",
                canonical_work_brief_bytes(brief),
            ),
        )
        brief_reference = replace(
            state.artifact_references[0],
            key=published_brief.key,
            revision=published_brief.revision,
            selector=published_brief.selector,
            content_sha256=published_brief.content_sha256,
            size_bytes=published_brief.size_bytes,
        )
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, replace(state, artifact_references=(brief_reference,)))
        attempt = next(value for value in state.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.accept_candidate_snapshot(
            roots,
            store,
            attempt,
            candidate_form,
            candidate_revision,
            candidate_diff,
            preimage_revision if candidate_form == "working-tree" else brief_base_revision,
            brief_base_revision,
            now,
        )
        if not local and review_condition != "missing":
            checkpoint = brief.checkpoint
            assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
            review_bytes = ready_review(brief)
            review = msgspec.json.decode(review_bytes, type=work_brief_models.WorkBriefReview)
            if review_condition == "malformed":
                review_bytes = b"not-json\n"
            elif review_condition == "stale":
                review_bytes = canonical_work_brief_review_bytes(replace_struct(review, checkpoint_sha256="f" * 64))
            elif review_condition == "wrong-owner":
                review_bytes = canonical_work_brief_review_bytes(
                    replace_struct(review, reviewer_task_id=brief.owner_task_id)
                )
            published_review = write_revision(
                roots,
                NewArtifact(
                    work_models.ArtifactKind.EVIDENCE,
                    f"work-a-1-brief-review-{work_briefs.ready_review_key_sha256(brief)}",
                    1,
                    ".json",
                    review_bytes,
                ),
            )
            accepted_review = store.accept_artifact_reference(roots.work_root, published_review, now)
            if isinstance(accepted_review, DecisionFailure):
                self.fail(str(accepted_review))
        attempt_root = roots.work_root / "attempts" / "work-a-1"
        attempt_root.mkdir(parents=True)
        (attempt_root / "result.md").write_text("candidate result\n", encoding="utf-8")
        (attempt_root / "review.md").write_text("implementation review\n", encoding="utf-8")
        checkpoint_id = brief.checkpoint.checkpoint_id
        payload = attempt_root / "accept-checkpoint.json"
        payload.write_text(
            json.dumps(
                {
                    "checkpoint": checkpoint_id,
                    "candidate": candidate_revision,
                    "evidence": "Accepted evidence.",
                }
            ),
            encoding="utf-8",
        )
        return CheckpointFixture(
            project,
            roots.work_root,
            store,
            brief,
            candidate_revision,
            candidate_diff,
            payload,
        )

    def accepted_package_fixture(
        self,
        *,
        local: bool = False,
        candidate_form: Literal["working-tree", "current-head"] = "working-tree",
        committed_context: bool = False,
    ) -> AcceptedPackageFixture:
        fixture = self.checkpoint_fixture(
            local=local, candidate_form=candidate_form, committed_context=committed_context
        )
        self.accept_checkpoint(
            fixture,
            self.project_action(fixture, "accept-checkpoint:work-a-1"),
            fixture.payload,
        )
        reloaded = fixture.store.validated_snapshot()
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        package_reference = next(
            value for value in reloaded.artifact_references if value.key == f"work-a-1-{checkpoint_id}-review-package"
        )
        return AcceptedPackageFixture(
            fixture.project,
            fixture.work,
            fixture.store,
            fixture.brief,
            fixture.candidate_revision,
            fixture.candidate_bytes,
            fixture.payload,
            package_reference,
        )

    def package(self, fixture: AcceptedPackageFixture) -> work_briefs.CheckpointPackage:
        package = decode_canonical_checkpoint_review_package(
            (fixture.work / fixture.package_reference.selector).read_bytes()
        )
        if isinstance(package, work_brief_models.WorkBriefFailure):
            self.fail(str(package))
        return package

    def review_job_fixture(self) -> tuple[AcceptedPackageFixture, int, int]:
        fixture = self.accepted_package_fixture()
        package_receipt = next(
            value
            for value in fixture.store.validated_snapshot().transition_receipts
            if value.outcome_schema == "checkpoint-acceptance/v2"
        )
        current_candidate = "b" * 40
        self.record_review_candidate(fixture, current_candidate)
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0]
            correction_history_id = connection.execute("SELECT max(history_id) + 1 FROM transition_history").fetchone()[
                0
            ]
            connection.execute(
                """
                INSERT INTO transition_history(
                    history_id, project_revision, action_id, action_kind, subject_id,
                    artifact_ref_id, authorization_kind, actor_task_id, actor_host_id,
                    input_schema, input_json, outcome_schema, outcome_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction_history_id,
                    revision + 1,
                    "return-for-correction:work-a-1",
                    decision_models.ActionKind.RETURN_FOR_CORRECTION.value,
                    "work-a-1",
                    None,
                    decision_models.AuthorizationKind.PROJECT.value,
                    "review-owner",
                    "local",
                    "return-for-correction/v1",
                    '{"reason":"Fix the affected sibling."}',
                    "transition-receipt/v1",
                    history.encode_transition_receipt_outcome(
                        evidence="Fix the affected sibling.",
                        outcome=decision_models.ActionKind.RETURN_FOR_CORRECTION.value,
                        candidate="candidate-a",
                    ).decode(),
                    SQLITE_NOW.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE project_meta SET revision = ?, updated_at = ? WHERE singleton = 1",
                (revision + 1, SQLITE_NOW.isoformat()),
            )
            connection.commit()
        finally:
            connection.close()
        attempt_root = fixture.work / "attempts" / "work-a-1"
        (attempt_root / "result.md").write_text("candidate B result\n", encoding="utf-8")
        (attempt_root / "review.md").write_text(
            "Candidate: candidate-a\nFinding: affected sibling.\n", encoding="utf-8"
        )
        return fixture, int(package_receipt.history_id), correction_history_id

    def replace_artifact_bytes(
        self,
        fixture: AcceptedPackageFixture,
        reference: stored_state.ArtifactReference,
        content: bytes,
    ) -> None:
        (fixture.work / reference.selector).write_bytes(content)
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE artifact_refs SET content_sha256 = ?, size_bytes = ? WHERE artifact_ref_id = ?",
                (hashlib.sha256(content).hexdigest(), len(content), int(reference.artifact_ref_id)),
            )
            connection.commit()
        finally:
            connection.close()

    def retain_v2_checkpoint(self, fixture: AcceptedPackageFixture) -> bytes:
        current = self.package(fixture)
        assert isinstance(current, work_brief_models.CheckpointReviewPackageV3)
        checkpoint = replace_struct(fixture.brief.checkpoint, checkpoint_id="historical-package")
        brief = replace_struct(fixture.brief, artifact_revision=2, checkpoint=checkpoint)
        roots = resolve_durable_roots(fixture.project)

        def accepted(
            role: Literal["accepted-brief", "candidate", "implementation-review", "result"],
            kind: work_models.ArtifactKind,
            key: str,
            revision: int,
            extension: str,
            content: bytes,
        ) -> work_brief_models.PortableArtifactIdentity:
            published = write_revision(roots, NewArtifact(kind, key, revision, extension, content))
            reference = fixture.store.accept_artifact_reference(fixture.work, published, SQLITE_NOW)
            if isinstance(reference, DecisionFailure):
                self.fail(str(reference))
            return msgspec.convert(
                {
                    "role": role,
                    "kind": kind.value,
                    "key": key,
                    "revision": revision,
                    "selector": published.selector,
                    "content_sha256": published.content_sha256,
                    "size_bytes": published.size_bytes,
                },
                type=work_brief_models.PortableArtifactIdentity,
                strict=True,
            )

        brief_identity = accepted(
            "accepted-brief",
            work_models.ArtifactKind.BRIEF,
            brief.attempt_id,
            2,
            ".json",
            canonical_work_brief_bytes(brief),
        )
        prefix = f"{brief.attempt_id}-{checkpoint.checkpoint_id}"
        candidate_identity = accepted(
            "candidate", work_models.ArtifactKind.EVIDENCE, f"{prefix}-candidate", 1, ".patch", fixture.candidate_bytes
        )
        result_identity = accepted(
            "result",
            work_models.ArtifactKind.RESULT,
            f"{prefix}-result",
            1,
            ".md",
            (fixture.work / current.result.selector).read_bytes(),
        )
        review_identity = accepted(
            "implementation-review",
            work_models.ArtifactKind.EVIDENCE,
            f"{prefix}-review",
            1,
            ".md",
            (fixture.work / current.implementation_review.selector).read_bytes(),
        )
        checkpoint_identity = work_brief_models.CheckpointIdentity(
            checkpoint.checkpoint_id, hashlib.sha256(msgspec.json.encode(checkpoint, order="sorted")).hexdigest()
        )
        legacy = checkpoint_compatibility_models.CheckpointReviewPackageV2(
            brief.attempt_id,
            brief.item_id,
            current.candidate,
            current.acceptance_evidence,
            current.accepted_scope,
            checkpoint_identity,
            candidate_identity,
            brief_identity,
            result_identity,
            review_identity,
            "ready",
            current.review_basis,
        )
        encoded = canonical_checkpoint_review_package_bytes(legacy)
        published_package = write_revision(
            roots, NewArtifact(work_models.ArtifactKind.EVIDENCE, f"{prefix}-review-package", 1, ".json", encoded)
        )
        accepted_package = fixture.store.accept_artifact_reference(fixture.work, published_package, SQLITE_NOW)
        if isinstance(accepted_package, DecisionFailure):
            self.fail(str(accepted_package))
        package_reference = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE, published_package.key, 1
        )
        assert package_reference is not None
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()[0] + 1
            connection.execute(
                """INSERT INTO transition_history(history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema, input_json, outcome_schema, outcome_json, committed_at)
                SELECT (SELECT max(history_id) + 1 FROM transition_history), ?, action_id, action_kind, subject_id, ?, 'evidence', authorization_kind, actor_task_id, actor_host_id, input_schema, ?, outcome_schema, ?, committed_at FROM transition_history WHERE outcome_schema = 'checkpoint-acceptance/v2' LIMIT 1""",
                (
                    revision,
                    int(package_reference.artifact_ref_id),
                    msgspec.json.encode(
                        {
                            "candidate": legacy.candidate,
                            "checkpoint": checkpoint.checkpoint_id,
                            "evidence": legacy.acceptance_evidence,
                        },
                        order="sorted",
                    ).decode(),
                    msgspec.json.encode(
                        history.CheckpointAcceptanceOutcome(
                            legacy.candidate, checkpoint.checkpoint_id, legacy.acceptance_evidence, "accept-checkpoint"
                        ),
                        order="sorted",
                    ).decode(),
                ),
            )
            connection.execute(
                "UPDATE project_meta SET revision = ?, updated_at = ? WHERE singleton = 1",
                (revision, SQLITE_NOW.isoformat()),
            )
        return encoded

    def replace_package(
        self,
        fixture: AcceptedPackageFixture,
        package: work_briefs.CheckpointPackage,
    ) -> None:
        self.replace_artifact_bytes(
            fixture,
            fixture.package_reference,
            canonical_checkpoint_review_package_bytes(package),
        )
