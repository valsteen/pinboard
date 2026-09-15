"""Shared current and historical checkpoint fixtures through installed commands."""

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
from pinboard.application import candidate_snapshots, stored_state, work_brief_models, work_briefs
from pinboard.application.artifacts import NewArtifact
from pinboard.application.work_briefs import (
    canonical_checkpoint_review_package_bytes,
    canonical_work_brief_bytes,
    decode_canonical_checkpoint_review_package,
)
from pinboard.cli.entrypoint import main
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, ItemId
from tests.artifact_support import write_revision
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

    def project_action(self, common: tuple[str, ...], action_id: str) -> JsonObject:
        actions = self.run_json_cli(*common, "actions", "--role", "project", "--action-id", action_id)
        values = actions["actions"]
        if not isinstance(values, list) or len(values) != 1:
            self.fail("Expected one exact project action")
        return self.json_object(values[0])

    def transition_json(
        self,
        fixture: CheckpointFixture,
        action: JsonObject,
        payload: Path,
    ) -> JsonObject:
        arguments = [
            *fixture.common,
            "transition",
            "--action-id",
            str(action["action_id"]),
            "--subject-revision",
            str(action["subject_revision"]),
            "--authorization",
            str(action["authorization"]),
            "--payload",
            str(payload),
        ]
        lease_id = action.get("lease_id")
        if lease_id:
            arguments.extend(("--lease-id", str(lease_id), "--generation", str(action["generation"])))
        else:
            arguments.extend(("--task-id", "review-owner", "--host-id", "local"))
        result, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, f"{stdout}\n{stderr}")
        return self.json_object(json.loads(stdout))

    def project_transition_arguments(
        self,
        fixture: CheckpointFixture,
        action: JsonObject,
        payload: Path,
        *,
        task_id: str = "review-owner",
    ) -> list[str]:
        return [
            *fixture.common,
            "transition",
            "--action-id",
            str(action["action_id"]),
            "--subject-revision",
            str(action["subject_revision"]),
            "--authorization",
            str(action["authorization"]),
            "--payload",
            str(payload),
            "--task-id",
            task_id,
            "--host-id",
            "local",
        ]

    def submit_review(self, fixture: AcceptedPackageFixture, label: str, worker: str) -> str:
        tracked = fixture.project / "tracked.txt"
        tracked.write_text(f"{label}\n", encoding="utf-8")
        candidate_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
        ).stdout
        candidate = f"working-tree-sha256:{hashlib.sha256(candidate_diff).hexdigest()}"
        lease = self.run_json_cli(
            *fixture.common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-a-1",
            "--task-id",
            worker,
            "--host-id",
            "local",
            "--ttl-seconds",
            "300",
        )
        selected = self.run_json_cli(
            *fixture.common,
            "actions",
            "--role",
            "worker",
            "--lease-id",
            str(lease["lease_id"]),
            "--generation",
            str(lease["generation"]),
            "--action-id",
            "submit-review:work-a-1",
        )
        actions = selected["actions"]
        if not isinstance(actions, list) or len(actions) != 1:
            self.fail("Expected one exact worker action")
        payload = fixture.project / f"submit-{candidate}.json"
        payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        self.transition_json(fixture, self.json_object(actions[0]), payload)
        return candidate

    def return_for_correction(self, fixture: AcceptedPackageFixture, reason: str, suffix: str) -> int:
        payload = fixture.project / f"return-{suffix}.json"
        payload.write_text(json.dumps({"reason": reason}), encoding="utf-8")
        rendered = self.transition_json(
            fixture,
            self.project_action(fixture.common, "return-for-correction:work-a-1"),
            payload,
        )
        history_id = rendered["history_id"]
        if not isinstance(history_id, int):
            self.fail("Transition must expose its committed history ID")
        return history_id

    def accept_checkpoint(
        self,
        common: tuple[str, ...],
        action: JsonObject,
        payload: Path,
    ) -> None:
        arguments = [
            *common,
            "transition",
            "--action-id",
            str(action["action_id"]),
            "--subject-revision",
            str(action["subject_revision"]),
            "--authorization",
            str(action["authorization"]),
            "--task-id",
            "package-test-owner",
            "--host-id",
            "local",
            "--payload",
            str(payload),
        ]
        result, _stdout, stderr = self.run_cli(*arguments)
        self.assertEqual(0, result, stderr)

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
        snapshot_type = (
            candidate_snapshots.WorkingTreeCandidateSnapshot
            if candidate_form == "working-tree"
            else candidate_snapshots.CommitCandidateSnapshot
        )
        snapshot = snapshot_type(
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
        with sqlite3.connect(roots.database_path) as connection:
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
                          'attempt', NULL, NULL, 'pinboard-candidate-snapshot/v1', ?,
                          'transition-receipt/v1', ?, ?)
                """,
                (
                    history_id,
                    project_revision,
                    reference_id,
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
        with sqlite3.connect(fixture.work / "state.sqlite3") as connection:
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

    def checkpoint_fixture(
        self,
        *,
        local: bool = False,
        candidate_form: Literal["working-tree", "current-head"] = "working-tree",
        accepted_base: str | None = None,
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
            candidate_revision = f"working-tree-sha256:{hashlib.sha256(candidate_diff).hexdigest()}"
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
            base_revision if candidate_form == "working-tree" else brief_base_revision,
            brief_base_revision,
            now,
        )
        if not local:
            checkpoint = brief.checkpoint
            assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
            checkpoint_sha256 = hashlib.sha256(msgspec.json.encode(checkpoint, order="sorted")).hexdigest()
            published_review = write_revision(
                roots,
                NewArtifact(
                    work_models.ArtifactKind.EVIDENCE,
                    f"work-a-1-brief-review-{checkpoint_sha256}",
                    1,
                    ".json",
                    ready_review(brief),
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
    ) -> AcceptedPackageFixture:
        fixture = self.checkpoint_fixture(local=local, candidate_form=candidate_form)
        self.accept_checkpoint(
            fixture.common,
            self.project_action(fixture.common, "accept-checkpoint:work-a-1"),
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
