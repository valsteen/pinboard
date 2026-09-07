import contextlib
import hashlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import views as file_views
from pinboard.adapters.files.artifacts import write_revision
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import service, stored_state
from pinboard.application.actions import discover_actions
from pinboard.application.artifacts import NewArtifact, WorkBriefIdentity
from pinboard.application.mutation_models import MutationReceipt
from pinboard.application.ports import WorkStore
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionResult
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId
from pinboard.interfaces import (
    action_selection,
    dispatch_brief,
    work_brief_models,
    work_inspection_models,
    work_state_commands,
)
from pinboard.interfaces import transitions as transition_interface
from pinboard.interfaces.cli import build_parser, main
from pinboard.interfaces.errors import WorkBriefErrorCode, WorkBriefFailure
from pinboard.interfaces.work_briefs import canonical_work_brief_bytes

from .domain_support import expect_success
from .support import (
    SQLITE_NOW,
    JsonObject,
    JsonValue,
    complete_sqlite_state,
    initialize_store,
    test_definition,
    with_definition_dependencies,
)
from .work_brief_support import CHECKPOINT_ID, ready_review, work_a_brief, work_c_brief


class CliTest(unittest.TestCase):
    def assert_repository_care_pointer(self, output: str, *, present: bool) -> None:
        expected_count = 1 if present else 0
        for skill in ("$repository-readiness", "$slop-cleanup", "$maintaining-agent-guidance"):
            self.assertEqual(expected_count, output.count(skill), skill)

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def run_json_cli(self, *arguments: str) -> JsonObject:
        result, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, stderr)
        value = json.loads(stdout)
        if not isinstance(value, dict):
            self.fail("CLI JSON result must be an object")
        return value

    def run_cli_parse_error(self, *arguments: str) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(arguments)
        self.assertEqual(2, raised.exception.code)
        return stderr.getvalue()

    def json_list(self, value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            self.fail("JSON value must be a list")
        return value

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def json_int(self, value: JsonValue) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            self.fail("JSON value must be an integer")
        return value

    def run_transition(
        self, common: tuple[str, ...], action: JsonObject, payload: Path, *, json_output: bool
    ) -> tuple[int, str, str]:
        arguments = [
            *common,
            "transition",
            "--action-id",
            str(action["action_id"]),
            "--expected-revision",
            str(action["expected_revision"]),
            "--authorization",
            str(action["authorization"]),
        ]
        subject_revision = action.get("subject_revision")
        if subject_revision:
            arguments.extend(("--subject-revision", str(subject_revision)))
        lease_id = action.get("lease_id")
        if lease_id:
            arguments.extend(("--lease-id", str(lease_id), "--generation", str(action["generation"])))
        else:
            arguments.extend(("--task-id", "project-task", "--host-id", "studio"))
        arguments.extend(("--payload", str(payload)))
        if json_output:
            arguments.append("--json")
        result, stdout, stderr = self.run_cli(*arguments)
        if result == 0 and json_output:
            transition = msgspec.json.decode(stdout, type=work_inspection_models.TransitionView)
            if transition.continuation is not None:
                inspected = self.run_json_cli(
                    *common, "attempt", "inspect", "--attempt-id", transition.continuation.attempt_id
                )
                self.assertEqual(
                    json.loads(msgspec.json.encode(work_inspection_models.AttemptView(transition.continuation))),
                    inspected,
                )
        elif result == 0 and "\n{" in stdout:
            encoded = stdout[stdout.index("\n{") + 1 :].encode()
            view = msgspec.json.decode(encoded, type=work_inspection_models.AttemptView)
            inspected = self.run_json_cli(*common, "attempt", "inspect", "--attempt-id", view.continuation.attempt_id)
            self.assertEqual(json.loads(encoded), inspected)
        return result, stdout, stderr

    def project_action(self, common: tuple[str, ...], action_id: str) -> JsonObject:
        return self.json_object(
            self.json_list(
                self.run_json_cli(*common, "actions", "--role", "project", "--action-id", action_id)["actions"]
            )[0]
        )

    def assert_prepared_activation_rejections(
        self,
        common: tuple[str, ...],
        action: JsonObject,
        prepared: JsonObject,
        project: Path,
        store: SQLiteWorkStore,
        valid_payload: Path,
    ) -> JsonObject:
        for lease_id, generation in (
            ("wrong-holder", prepared["generation"]),
            (prepared["lease_id"], self.json_int(prepared["generation"]) + 1),
        ):
            rejected, _stdout, rejected_stderr = self.run_cli(
                *common,
                "actions",
                "--role",
                "preparer",
                "--lease-id",
                str(lease_id),
                "--generation",
                str(generation),
                "--action-id",
                "activate:work-c",
            )
            self.assertEqual(11, rejected)
            self.assertIn("ACTION_NOT_AVAILABLE", rejected_stderr)

        before_rejection = store.snapshot()
        wrong_reference_payload = project / "activate-wrong-reference.json"
        wrong_reference_payload.write_text(
            json.dumps(
                {
                    "attempt": "work-c-1",
                    "branch": "codex/work-c",
                    "base_revision": "candidate-base",
                    "owner": "worker-task",
                    "brief_artifact_ref_id": 999999,
                }
            ),
            encoding="utf-8",
        )
        rejected, rejected_stdout, rejected_stderr = self.run_transition(
            common, action, wrong_reference_payload, json_output=True
        )
        self.assertNotEqual(0, rejected)
        self.assertEqual("", rejected_stderr)
        rejected_payload = self.json_object(json.loads(rejected_stdout))
        self.assertEqual("TRANSITION_INPUT_INVALID", rejected_payload["code"])
        self.assertEqual("correct-input", rejected_payload["retry"])
        self.assertEqual(before_rejection, store.snapshot())

        expires_at = datetime.fromisoformat(str(prepared["expires_at"]))
        with patch("pinboard.interfaces.work_inspection.datetime") as inspection_clock:
            inspection_clock.now.return_value = expires_at
            rejected, _stdout, rejected_stderr = self.run_cli(
                *common,
                "actions",
                "--role",
                "preparer",
                "--lease-id",
                str(prepared["lease_id"]),
                "--generation",
                str(prepared["generation"]),
                "--action-id",
                "activate:work-c",
            )
        self.assertEqual(11, rejected)
        self.assertIn("ACTION_NOT_AVAILABLE", rejected_stderr)
        before_expired_activation = store.snapshot()
        for label, observed_at in (
            ("at", expires_at),
            ("after", expires_at + timedelta(microseconds=1)),
        ):
            with (
                self.subTest(expired_activation=label),
                patch("pinboard.interfaces.action_selection.datetime") as action_clock,
            ):
                action_clock.now.return_value = observed_at
                rejected, _stdout, rejected_stderr = self.run_transition(
                    common, action, valid_payload, json_output=False
                )
            self.assertEqual(11, rejected)
            self.assertIn("ACTION_AUTHORITY_EXPIRED", rejected_stderr)
            self.assertEqual(before_expired_activation, store.snapshot())
        return self.assert_installed_activation_identity_rejections(common, action, project, store, valid_payload)

    def assert_installed_activation_identity_rejections(
        self,
        common: tuple[str, ...],
        action: JsonObject,
        project: Path,
        store: SQLiteWorkStore,
        valid_payload: Path,
    ) -> JsonObject:
        payload_values = self.json_object(json.loads(valid_payload.read_text(encoding="utf-8")))
        for field, mismatch in (
            ("attempt", "different-1"),
            ("branch", "codex/different"),
            ("base_revision", "different-base"),
        ):
            mismatched_payload = project / f"activate-wrong-{field}.json"
            mismatched_payload.write_text(json.dumps({**payload_values, field: mismatch}), encoding="utf-8")
            before = store.snapshot()
            rejected, _stdout, rejected_stderr = self.run_transition(
                common, action, mismatched_payload, json_output=False
            )
            self.assertEqual(11, rejected)
            self.assertIn("TRANSITION_INPUT_INVALID", rejected_stderr)
            self.assertEqual(before, store.snapshot())

        retained_preparation = store.snapshot().authority.preparation_leases[0]
        observed_at = retained_preparation.expires_at - timedelta(microseconds=1)
        available = expect_success(
            discover_actions(
                store.snapshot(),
                decision_models.Role.PREPARER,
                lease_id=LeaseId(str(action["lease_id"])),
                generation=self.json_int(action["generation"]),
                now=observed_at,
            )
        )
        typed_action = next(
            candidate
            for candidate in available
            if isinstance(candidate, decision_models.ActivateAction)
            and decision_models.action_id(candidate) == "activate:work-c"
        )
        authority = typed_action.capability.preparation_authority
        assert authority is not None
        wrong_authority = replace(authority, definition_digest="f" * 64)
        wrong_action = replace(
            typed_action,
            capability=replace(typed_action.capability, preparation_authority=wrong_authority),
        )
        decision_snapshot = service.project_decision_snapshot(store.snapshot(), observed_at)
        wrong_snapshot = replace(decision_snapshot, command_preparation_authorities=(wrong_authority,))
        before = store.snapshot()
        with (
            patch("pinboard.interfaces.action_selection.select_current_action", return_value=wrong_action),
            patch("pinboard.application.service.project_decision_snapshot", return_value=wrong_snapshot),
        ):
            rejected, _stdout, rejected_stderr = self.run_transition(common, action, valid_payload, json_output=False)
        self.assertEqual(11, rejected)
        self.assertIn("live preparation pin", rejected_stderr)
        self.assertEqual(before, store.snapshot())

        candidate = work_c_brief()
        checkpoint = candidate.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint | work_brief_models.LocalCheckpoint)
        verification = checkpoint.verification[0]
        wrong_item_checkpoint = replace_struct(
            checkpoint,
            verification=(
                replace_struct(
                    verification,
                    authorization_basis=work_brief_models.AcceptedScopeAuthorization(
                        "work-b", candidate.accepted_scope.revision
                    ),
                ),
            ),
        )
        mismatched_briefs = (
            (
                "item",
                replace_struct(
                    candidate,
                    attempt_id="work-c-wrong-item",
                    item_id="work-b",
                    checkpoint=wrong_item_checkpoint,
                ),
            ),
            (
                "definition",
                replace_struct(
                    candidate,
                    attempt_id="work-c-wrong-definition",
                    accepted_scope=work_brief_models.AcceptedScope(candidate.accepted_scope.revision, "f" * 64),
                ),
            ),
        )
        current_action = action
        for name, mismatched_brief in mismatched_briefs:
            brief_path = project / f"activate-wrong-{name}-brief.json"
            brief_path.write_bytes(canonical_work_brief_bytes(mismatched_brief))
            publication = self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
            payload = project / f"activate-wrong-{name}.json"
            payload.write_text(
                json.dumps(
                    {
                        **payload_values,
                        "attempt": mismatched_brief.attempt_id,
                        "brief_artifact_ref_id": publication["artifact_ref_id"],
                    }
                ),
                encoding="utf-8",
            )
            current_action = self.json_object(
                self.json_list(
                    self.run_json_cli(
                        *common,
                        "actions",
                        "--role",
                        "preparer",
                        "--lease-id",
                        str(action["lease_id"]),
                        "--generation",
                        str(action["generation"]),
                        "--action-id",
                        "activate:work-c",
                    )["actions"]
                )[0]
            )
            before = store.snapshot()
            rejected, _stdout, rejected_stderr = self.run_transition(common, current_action, payload, json_output=False)
            self.assertEqual(11, rejected)
            self.assertIn("TRANSITION_INPUT_INVALID", rejected_stderr)
            self.assertEqual(before, store.snapshot())
        return current_action

    def assert_installed_preparation_visibility(
        self,
        common: tuple[str, ...],
        prepared: JsonObject,
        store: SQLiteWorkStore,
    ) -> None:
        self.assertEqual(
            decision_models.AuthorizationKind.PREPARATION,
            store.snapshot().transition_receipts[-1].authorization,
        )
        overview = self.run_json_cli(*common, "overview")
        overview_item = next(
            self.json_object(value)
            for value in self.json_list(overview["items"])
            if self.json_object(value)["item_id"] == "work-c"
        )
        overview_preparation = self.json_object(overview_item["preparation"])
        item = self.run_json_cli(*common, "item", "status", "--item-id", "work-c")
        item_preparation = self.json_object(item["preparation"])
        for visible in (overview_preparation, item_preparation):
            self.assertEqual("preparer-task", visible["task_id"])
            self.assertEqual("studio", visible["host_id"])
            self.assertEqual(prepared["lease_id"], visible["lease_id"])
            self.assertEqual(prepared["generation"], visible["generation"])
            self.assertEqual(prepared["expires_at"], visible["expires_at"])
            self.assertEqual("active", visible["status"])
        overview_result, overview_stdout, overview_stderr = self.run_cli(*common, "overview")
        item_result, item_stdout, item_stderr = self.run_cli(*common, "item", "status", "--item-id", "work-c")
        self.assertEqual(0, overview_result, overview_stderr)
        self.assertEqual(0, item_result, item_stderr)
        self.assertIn("preparation=active preparer=preparer-task@studio", overview_stdout)
        self.assertIn("preparation=active preparer=preparer-task@studio", item_stdout)

    def write_item_revision(
        self,
        path: Path,
        item_id: ItemId,
        revision: int,
        digest: str,
        definition: work_models.WorkItemDefinition,
        *,
        objective: str | None = None,
        dependencies: tuple[ItemId, ...] | None = None,
    ) -> Path:
        path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-item-revision/v1",
                    "item_id": item_id,
                    "expected_revision": revision,
                    "expected_digest": digest,
                    "source_task": "owner-task",
                    "reason": "Clarify the observable outcome.",
                    "definition": {
                        "schema": "pinboard-work-item-definition/v1",
                        "title": definition.title,
                        "objective": objective or definition.objective,
                        "hypothesis": definition.hypothesis,
                        "evidence": list(definition.evidence),
                        "scope": list(definition.scope),
                        "non_scope": list(definition.non_scope),
                        "acceptance_criteria": list(definition.acceptance_criteria),
                        "dependencies": list(definition.dependencies if dependencies is None else dependencies),
                        "effect": definition.effect,
                        "unlock": definition.unlock,
                    },
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return path

    def initialized_state(
        self, state: stored_state.StoredWorkState | None = None
    ) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        if state is not None:
            reference = state.artifact_references[0]
            if reference.selector.endswith(".opaque"):
                value = work_a_brief(project)
                attempt = state.lifecycle.attempts[0]
                value = replace_struct(
                    value,
                    accepted_scope=work_brief_models.AcceptedScope(
                        attempt.accepted_scope_revision,
                        attempt.accepted_scope_digest,
                    ),
                )
                published = write_revision(
                    roots,
                    NewArtifact(
                        work_models.ArtifactKind.BRIEF, value.attempt_id, 1, ".json", canonical_work_brief_bytes(value)
                    ),
                )
                reference = replace(
                    reference,
                    key=published.key,
                    revision=published.revision,
                    selector=published.selector,
                    content_sha256=published.content_sha256,
                    size_bytes=published.size_bytes,
                )
                state = replace(state, artifact_references=(reference, *state.artifact_references[1:]))
            initialize_store(store, state)
        return project, roots.work_root, store

    def prepared_state(self, expires_at: datetime) -> stored_state.StoredWorkState:
        state = complete_sqlite_state()
        definition = next(value for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-c"))
        return replace(
            state,
            authority=replace(
                state.authority,
                preparation_counters=(stored_state.PreparationLeaseCounter(ItemId("work-c"), 1),),
                preparation_generations=(
                    stored_state.PreparationLeaseGeneration(
                        ItemId("work-c"),
                        1,
                        LeaseId("preparation-c"),
                        TaskId("preparer-c"),
                        HostId("studio"),
                    ),
                ),
                preparation_leases=(
                    stored_state.StoredPreparationLease(
                        ItemId("work-c"),
                        1,
                        definition.revision,
                        definition.digest,
                        SQLITE_NOW,
                        expires_at,
                        authority_models.PreparationLeaseStatus.ACTIVE,
                    ),
                ),
            ),
        )

    def test_preparation_start_selects_current_definition_and_transfers_inactive_claims(self) -> None:
        for retained_status in (None, "expired", "released", "revoked"):
            with self.subTest(retained_status=retained_status):
                state = complete_sqlite_state()
                if retained_status is not None:
                    state = self.prepared_state(SQLITE_NOW + timedelta(minutes=1))
                    if retained_status != "expired":
                        state = replace(
                            state,
                            authority=replace(
                                state.authority,
                                preparation_leases=(
                                    replace(
                                        state.authority.preparation_leases[0],
                                        state=authority_models.PreparationLeaseStatus(retained_status),
                                    ),
                                ),
                            ),
                        )
                project, work, store = self.initialized_state(state)
                common = ("--project-root", str(project), "--work-root", str(work))
                started = self.run_json_cli(
                    *common,
                    "preparation",
                    "start",
                    "--item-id",
                    "work-c",
                    "--task-id",
                    "new-preparer",
                    "--host-id",
                    "studio",
                    "--ttl-seconds",
                    "300",
                )
                fresh = SQLiteWorkStore(work / "state.sqlite3").snapshot()
                definition = next(value for value in fresh.lifecycle.definition_revisions if value.item_id == "work-c")
                self.assertEqual(definition.digest, started["definition_digest"])
                self.assertEqual(definition.revision, started["definition_revision"])
                self.assertEqual("new-preparer", started["task_id"])
                self.assertEqual(1 if retained_status is None else 2, started["generation"])
                before = store.snapshot()
                rejected, _, _ = self.run_cli(
                    *common,
                    "preparation",
                    "start",
                    "--item-id",
                    "work-c",
                    "--task-id",
                    "racing-preparer",
                    "--host-id",
                    "studio",
                    "--ttl-seconds",
                    "300",
                )
                self.assertEqual(11, rejected)
                self.assertEqual(before, store.snapshot())
                self.run_cli_parse_error(
                    *common,
                    "preparation",
                    "start",
                    "--item-id",
                    "work-c",
                    "--task-id",
                    "new-preparer",
                    "--host-id",
                    "studio",
                    "--ttl-seconds",
                    "0",
                )

    def test_ordinary_preparation_start_rejects_unknown_or_ineligible_items_without_change(self) -> None:
        state = complete_sqlite_state()
        state = with_definition_dependencies(state, ItemId("work-c"), (ItemId("intake-work"),))
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        before = store.snapshot()
        for item_id in ("unknown", "work-a", "work-c", "intake-work"):
            with self.subTest(item=item_id):
                result, _, _ = self.run_cli(
                    *common,
                    "preparation",
                    "start",
                    "--item-id",
                    item_id,
                    "--task-id",
                    "preparer",
                    "--host-id",
                    "host-a",
                    "--ttl-seconds",
                    "300",
                )
                self.assertEqual(11, result)
                self.assertEqual(before, store.snapshot())

    def test_active_definition_replacement_requires_pause_before_rebinding(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        definition, digest = test_definition(ItemId("work-a"))
        payload = self.write_item_revision(
            project / "active-definition.json",
            ItemId("work-a"),
            1,
            digest,
            definition,
            objective="Follow the new accepted target.",
        )
        result, stdout, stderr = self.run_transition(
            common, self.project_action(common, "revise-item:work-a"), payload, json_output=True
        )
        self.assertEqual(0, result, stderr)
        continuation = self.json_object(json.loads(stdout)["continuation"])
        self.assertEqual("active", continuation["state"])
        self.assertEqual("pause", self.json_object(continuation["next_operation"])["action_kind"])
        self.assertEqual(work_a_brief(project).owner_task_id, continuation["owner_task_id"])
        before = store.snapshot()
        attempt = next(value for value in before.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        brief = next(
            value for value in before.artifact_references if value.artifact_ref_id == attempt.brief_artifact_ref_id
        )
        (work / brief.selector).unlink()
        rejected, _, rejected_stderr = self.run_cli(*common, "attempt", "inspect", "--attempt-id", "work-a-1")
        self.assertEqual(12, rejected)
        self.assertIn("STORAGE_INVARIANT_VIOLATION", rejected_stderr)
        self.assertEqual(before, store.snapshot())

    def test_current_command_surface_lists_every_command(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        for retained in (
            "root",
            "validate",
            "status",
            "overview",
            "item",
            "close",
            "actions",
            "input-contract",
            "brief",
            "brief-sources",
            "init",
            "proposal",
            "transition",
            "dispatch",
            "attempt",
            "preparation",
            "parallel",
            "review-job",
            "views",
        ):
            self.assertIn(retained, help_text)
        item_status_help = io.StringIO()
        with contextlib.redirect_stdout(item_status_help), self.assertRaises(SystemExit) as raised:
            parser.parse_args(("item", "status", "--help"))
        self.assertEqual(0, raised.exception.code)
        self.assertIn("--item-id", item_status_help.getvalue())
        item_revise_help = io.StringIO()
        with contextlib.redirect_stdout(item_revise_help), self.assertRaises(SystemExit) as raised:
            parser.parse_args(("item", "revise", "--help"))
        self.assertEqual(0, raised.exception.code)
        self.assertIn("--file", item_revise_help.getvalue())

    def test_item_revise_round_trips_through_the_installed_command(self) -> None:
        state = complete_sqlite_state()
        current = work_models.WorkItemDefinition(
            "Work work-a",
            "Make the state explicit.",
            "The workflow needs this fact.",
            ("artifacts/design.md",),
            ("The state becomes explicit.",),
            (),
            ("The next decision can run.",),
            (ItemId("work-c"),),
            "The state becomes explicit.",
            "The next decision can run.",
        )
        digest = expect_success(work_item_definition_digest(current))
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                definition_revisions=(
                    *(value for value in state.lifecycle.definition_revisions if value.item_id != ItemId("work-a")),
                    stored_state.ItemDefinitionRevision(
                        ItemId("work-a"),
                        1,
                        digest,
                        current,
                        "Accepted proposal definition.",
                        TaskId("proposal-source"),
                        None,
                        digest,
                        3,
                        SQLITE_NOW,
                    ),
                ),
                attempts=(replace(state.lifecycle.attempts[0], accepted_scope_digest=digest),),
            ),
        )
        project, work, store = self.initialized_state(state)
        payload = self.write_item_revision(
            project / "revision.json",
            ItemId("work-a"),
            1,
            digest,
            current,
            objective="Make the state explicit and observable.",
            dependencies=(ItemId("intake-work"),),
        )
        value = self.run_json_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "item",
            "revise",
            "--file",
            str(payload),
            "--task-id",
            "owner-task",
            "--host-id",
            "local",
        )

        self.assertEqual("work-a", value["item_id"])
        self.assertEqual(2, value["definition_revision"])
        self.assertEqual("13", value["project_revision"])
        reopened = store.snapshot()
        self.assertEqual(
            2,
            sum(value.item_id == ItemId("work-a") for value in reopened.lifecycle.definition_revisions),
        )
        self.assertEqual(
            (ItemId("intake-work"),),
            tuple(
                dependency.dependency_id
                for dependency in reopened.lifecycle.dependencies
                if dependency.item_id == ItemId("work-a")
            ),
        )
        current_value = self.run_json_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "item",
            "definition",
            "--item-id",
            "work-a",
        )
        self.assertEqual(2, current_value["definition_revision"])
        self.assertEqual(
            "Make the state explicit and observable.", self.json_object(current_value["definition"])["objective"]
        )
        first_page = self.run_json_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "item",
            "definition-history",
            "--item-id",
            "work-a",
            "--limit",
            "1",
        )
        self.assertEqual(2, first_page["next_before_revision"])
        latest_revision = self.json_object(self.json_list(first_page["revisions"])[0])
        self.assertEqual(2, latest_revision["revision"])
        self.assertEqual("owner-task", latest_revision["source_task"])
        self.assertEqual("Clarify the observable outcome.", latest_revision["reason"])
        second_page = self.run_json_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "item",
            "definition-history",
            "--item-id",
            "work-a",
            "--limit",
            "1",
            "--before-revision",
            "2",
        )
        self.assertIsNone(second_page["next_before_revision"])
        self.assertEqual(1, self.json_object(self.json_list(second_page["revisions"])[0])["revision"])

    def test_item_definition_emits_every_preparation_revision_in_json_and_text(self) -> None:
        state = complete_sqlite_state()
        item = next(value for value in state.lifecycle.work_items if value.item_id == ItemId("work-c"))
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work), "item", "definition", "--item-id", "work-c")

        definition = self.run_json_cli(*common)
        result, stdout, stderr = self.run_cli(*common)

        self.assertEqual(state.lifecycle.project.revision, definition["project_revision"])
        self.assertEqual(item.subject_revision, definition["item_subject_revision"])
        self.assertEqual(0, result, stderr)
        self.assertIn(f"item_subject_revision={item.subject_revision}", stdout)

    def test_item_revise_rejections_preserve_sqlite_and_generated_views(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        rebuild_result, _rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuild_result, rebuild_stderr)
        views_root = work / "views"
        before = store.snapshot()
        before_views = tuple(
            (path.relative_to(views_root), path.read_bytes())
            for path in sorted(views_root.rglob("*"))
            if path.is_file()
        )
        cases = (
            (ItemId("work-a"), (ItemId("absent-work"),), "DEPENDENCY_NOT_SATISFIED"),
            (ItemId("work-c"), (ItemId("work-a"),), "ITEM_DEPENDENCY_CYCLE"),
        )
        for item_id, dependencies, error_code in cases:
            definition, digest = test_definition(item_id)
            payload = self.write_item_revision(
                project / f"{item_id}-rejected-revision.json",
                item_id,
                1,
                digest,
                definition,
                dependencies=dependencies,
            )
            action_values = self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "project",
                    "--action-id",
                    f"revise-item:{item_id}",
                )["actions"]
            )
            action = self.json_object(action_values[0])

            result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)

            with self.subTest(item_id=item_id):
                self.assertNotEqual(0, result)
                self.assertEqual("", stdout)
                self.assertIn(error_code, stderr)
                self.assertEqual(before, store.snapshot())
                self.assertEqual(
                    before_views,
                    tuple(
                        (path.relative_to(views_root), path.read_bytes())
                        for path in sorted(views_root.rglob("*"))
                        if path.is_file()
                    ),
                )

    def test_revised_review_attempt_rejects_every_acceptance_path_through_the_cli(self) -> None:
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
                        candidate_revision="candidate-review",
                        candidate_recorded_at=now,
                    )
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in state.lifecycle.attempts
                ),
            ),
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, acquired_at=now, expires_at=now + timedelta(minutes=5))
                    for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        action_values = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "project",
            )["actions"]
        )
        before_actions = {
            str(action["action_id"]): action
            for value in action_values
            if (action := self.json_object(value))["action_id"]
            in {
                "accept-checkpoint:work-a-1",
                "accept-review-and-continue:work-a-1",
                "complete:work-a-1",
            }
        }
        self.assertEqual(
            {
                "accept-checkpoint:work-a-1",
                "accept-review-and-continue:work-a-1",
                "complete:work-a-1",
            },
            set(before_actions),
        )
        definition, digest = test_definition(ItemId("work-a"))
        revision = self.write_item_revision(
            project / "review-item-revision.json",
            ItemId("work-a"),
            1,
            digest,
            definition,
            objective="Make the reviewed state explicitly stale.",
        )
        revision_action = next(
            self.json_object(value)
            for value in action_values
            if self.json_object(value)["action_id"] == "revise-item:work-a"
        )
        revision_result, revision_stdout, revision_stderr = self.run_transition(
            common, revision_action, revision, json_output=False
        )
        self.assertEqual(0, revision_result, revision_stderr)
        self.assertIn("OK TRANSITION_APPLIED revise-item:work-a", revision_stdout)
        after_ids = {
            str(self.json_object(value)["action_id"])
            for value in self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "project",
                )["actions"]
            )
        }
        self.assertTrue(set(before_actions).isdisjoint(after_ids))
        after_revision = store.snapshot()
        continuation = self.json_object(
            self.run_json_cli(*common, "attempt", "inspect", "--attempt-id", "work-a-1")["continuation"]
        )
        self.assertEqual("return-for-correction", self.json_object(continuation["next_operation"])["action_kind"])
        rejected, _, _ = self.run_cli(
            *common, "review-job", "--attempt-id", "work-a-1", "--candidate-revision", "candidate-review"
        )
        self.assertEqual(11, rejected)
        self.assertEqual(after_revision, store.snapshot())
        payloads = {
            "accept-checkpoint:work-a-1": '{"checkpoint":"checkpoint-a","candidate":"candidate-review","evidence":"accepted"}',
            "accept-review-and-continue:work-a-1": '{"candidate":"candidate-review","evidence":"accepted"}',
            "complete:work-a-1": '{"evidence":"accepted"}',
        }
        for action_id, action in before_actions.items():
            payload = project / f"{action_id.split(':', 1)[0]}.json"
            payload.write_text(payloads[action_id], encoding="utf-8")

            result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)

            with self.subTest(action_id=action_id):
                self.assertNotEqual(0, result)
                self.assertEqual("", stdout)
                self.assertIn("ACTION_LIFECYCLE_UNAVAILABLE", stderr)
                self.assertEqual(after_revision, store.snapshot())

    def test_item_revise_post_commit_view_warning_preserves_receipt_and_repairs(self) -> None:
        state = complete_sqlite_state()
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        definition, digest = test_definition(ItemId("work-a"))
        payload = self.write_item_revision(
            project / "warning-revision.json",
            ItemId("work-a"),
            1,
            digest,
            definition,
            objective="Preserve the authoritative revision when projection fails.",
            dependencies=(ItemId("intake-work"),),
        )

        with patch(
            "pinboard.interfaces.work_views.read_attempt_brief_views",
            return_value=WorkBriefFailure(WorkBriefErrorCode.BRIEF_INVALID, "injected revision projection failure"),
        ):
            result, stdout, stderr = self.run_cli(
                *common,
                "item",
                "revise",
                "--file",
                str(payload),
                "--task-id",
                "owner-task",
                "--host-id",
                "local",
                "--json",
            )

        self.assertEqual(0, result, stderr)
        self.assertEqual(2, self.json_int(self.json_object(json.loads(stdout))["definition_revision"]))
        self.assertIn("injected revision projection failure", stderr)
        self.assertIn("pinboard views rebuild", stderr)
        reopened = store.snapshot()
        revisions = tuple(
            value for value in reopened.lifecycle.definition_revisions if value.item_id == ItemId("work-a")
        )
        self.assertEqual((1, 2), tuple(value.revision for value in revisions))
        self.assertEqual((ItemId("intake-work"),), revisions[-1].definition.dependencies)
        receipt = next(
            value
            for value in reopened.transition_receipts
            if value.action_kind == decision_models.ActionKind.REVISE_ITEM
        )
        self.assertEqual(revisions[-1].accepted_project_revision, receipt.project_revision)
        rebuild_result, _rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuild_result, rebuild_stderr)
        item_view = (work / "views/items/work-a.md").read_text(encoding="utf-8")
        self.assertIn("- Revision: 2", item_view)

    def test_init_and_current_read_commands_need_no_filesystem_authority(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        result, stdout, stderr = self.run_cli(*common, "init")

        self.assertEqual(0, result, stderr)
        self.assertIn("WORK_STATE_INITIALIZED", stdout)
        self.assert_repository_care_pointer(stdout, present=True)
        self.assertTrue((work / "state.sqlite3").is_file())
        self.assertFalse((work / "authority.json").exists())
        self.assertFalse((work / "queue.md").exists())
        self.assertTrue(self.run_json_cli(*common, "validate")["valid"])
        self.assertEqual("sqlite-v5", self.run_json_cli(*common, "status")["authority"])
        overview = self.run_json_cli(*common, "overview")
        self.assertEqual("sqlite-v5", overview["authority"])
        self.assertEqual("pinboard-overview/v3", overview["schema"])
        actions = self.run_json_cli(*common, "actions", "--role", "observer")["actions"]
        self.assertIsInstance(actions, list)
        assert isinstance(actions, list)
        action_ids: list[str] = []
        for action in actions:
            self.assertIsInstance(action, dict)
            assert isinstance(action, dict)
            action_id = action.get("action_id")
            self.assertIsInstance(action_id, str)
            assert isinstance(action_id, str)
            action_ids.append(action_id)
            self.assertIsNone(action.get("lease_id"))
            self.assertIsNone(action.get("generation"))
        self.assertEqual(["inspect:ledger"], action_ids)
        self.assertEqual(
            "pinboard-parallel-preview/v1",
            self.run_json_cli(*common, "parallel", "preview")["schema"],
        )
        result, stdout, stderr = self.run_cli(*common, "overview")
        self.assertEqual(0, result, stderr)
        self.assertIn("live_work=none", stdout)
        exact_result, _exact_stdout, exact_stderr = self.run_cli(
            *common, "actions", "--role", "observer", "--action-id", "inspect:missing"
        )
        self.assertEqual(11, exact_result)
        self.assertIn("ACTION_NOT_AVAILABLE", exact_stderr)

    def test_fresh_init_has_one_structured_json_receipt(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"

        created = self.run_json_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "init",
        )

        self.assertEqual("pinboard-work-state-initialized/v1", created["schema"])
        self.assertEqual(str(work), created["work_root"])
        self.assertFalse(created["resumed"])
        self.assertEqual(
            ["repository-readiness", "slop-cleanup", "maintaining-agent-guidance"],
            created["optional_next_skills"],
        )

    def test_fresh_init_accepts_a_proposal_and_rejects_its_stale_receipt_without_changes(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "pinboard"
        common = ("--project-root", str(project), "--work-root", str(work))
        initialized, _stdout, stderr = self.run_cli(*common, "init")
        self.assertEqual(0, initialized, stderr)

        proposal: JsonObject = {
            "schema": "pinboard-proposal/v1",
            "proposal_id": "fresh-proposal",
            "created_at": SQLITE_NOW.isoformat(),
            "source_task_id": "project-task",
            "user_label": "Fresh proposal",
            "trigger": "Exercise proposal acceptance from the schema created by init.",
            "evidence": ["source:fresh-init"],
            "why_it_matters": "Required empty state must support its first transition.",
            "relation": {"kind": "independent", "item": None},
            "effect": "The proposal becomes ready work.",
            "unlock": "The initialized ledger can begin work.",
            "urgency_evidence": "This is the initialization regression.",
            "freshness_assumptions": ["The ledger was initialized in this test."],
        }
        proposal_path = project / "fresh-proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        created, _stdout, stderr = self.run_cli(
            *common,
            "proposal",
            "--file",
            str(proposal_path),
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
        )
        self.assertEqual(0, created, stderr)
        stale_action = self.project_action(common, "accept-proposal:fresh-proposal")

        proposal["proposal_id"] = "intervening-proposal"
        proposal["user_label"] = "Intervening proposal"
        intervening_path = project / "intervening-proposal.json"
        intervening_path.write_text(json.dumps(proposal), encoding="utf-8")
        intervening, _stdout, stderr = self.run_cli(
            *common,
            "proposal",
            "--file",
            str(intervening_path),
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
        )
        self.assertEqual(0, intervening, stderr)

        payload = project / "accept-fresh-proposal.json"
        payload.write_text(
            json.dumps({"item": "fresh-proposal", "state": "ready", "next_action": "activate"}),
            encoding="utf-8",
        )
        store = SQLiteWorkStore(work / "state.sqlite3")
        before_stale = store.snapshot()
        rejected, rejected_stdout, rejected_stderr = self.run_transition(
            common, stale_action, payload, json_output=True
        )
        self.assertEqual(11, rejected)
        self.assertEqual("", rejected_stderr)
        rejection = self.json_object(json.loads(rejected_stdout))
        self.assertEqual("ACTION_REVISION_STALE", rejection["code"])
        self.assertFalse(rejection["state_changed"])
        alternatives = self.json_list(rejection["next_actions"])
        fresh_accept = next(
            self.json_object(value)
            for value in alternatives
            if self.json_object(value).get("action_id") == "accept-proposal:fresh-proposal"
        )
        self.assertEqual("action", fresh_accept["kind"])
        self.assertEqual("project", fresh_accept["role"])
        self.assertEqual(before_stale.lifecycle.project.revision, int(str(fresh_accept["expected_revision"])))
        self.assertIsNone(fresh_accept["generation"])
        self.assertEqual(before_stale, SQLiteWorkStore(work / "state.sqlite3").snapshot())

        current_action = self.project_action(common, "accept-proposal:fresh-proposal")
        accepted, stdout, stderr = self.run_transition(common, current_action, payload, json_output=False)
        self.assertEqual(0, accepted, stderr)
        self.assertIn("OK TRANSITION_APPLIED accept-proposal:fresh-proposal", stdout)
        reloaded = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        accepted_item = next(
            value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("fresh-proposal")
        )
        self.assertEqual(stored_state.StoredWorkItemState.READY, accepted_item.state)

    def test_preparation_lease_recovery_needs_no_global_coordination(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        definition = self.run_json_cli(*common, "item", "definition", "--item-id", "work-c")
        acquired = self.run_json_cli(
            *common,
            "preparation",
            "acquire",
            "--item-id",
            "work-c",
            "--expected-project-revision",
            str(definition["project_revision"]),
            "--expected-item-subject-revision",
            str(definition["item_subject_revision"]),
            "--expected-definition-revision",
            str(definition["definition_revision"]),
            "--expected-definition-digest",
            str(definition["definition_digest"]),
            "--task-id",
            "preparer-a",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        released = self.run_json_cli(
            *common,
            "preparation",
            "release",
            "--item-id",
            "work-c",
            "--lease-id",
            str(acquired["lease_id"]),
            "--generation",
            str(acquired["generation"]),
        )
        transferred = self.run_json_cli(
            *common,
            "preparation",
            "transfer",
            "--item-id",
            "work-c",
            "--task-id",
            "preparer-b",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        revoked = self.run_json_cli(
            *common,
            "preparation",
            "revoke",
            "--item-id",
            "work-c",
            "--lease-id",
            str(transferred["lease_id"]),
            "--generation",
            str(transferred["generation"]),
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
        )

        self.assertEqual("released", released["status"])
        self.assertEqual(("active", "preparer-b"), (transferred["status"], transferred["task_id"]))
        self.assertEqual("revoked", revoked["status"])
        retained = SQLiteWorkStore(work / "state.sqlite3").snapshot().authority.preparation_leases[0]
        self.assertEqual(authority_models.PreparationLeaseStatus.REVOKED, retained.state)
        self.assertEqual(revoked["generation"], retained.generation)

    def test_attempt_lease_recovery_needs_no_global_coordination(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(),
                attempt_generations=(),
                attempt_leases=(),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        acquired = self.run_json_cli(
            *common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-a-1",
            "--task-id",
            "worker-a",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        revoked = self.run_json_cli(
            *common,
            "attempt",
            "revoke",
            "--attempt-id",
            "work-a-1",
            "--lease-id",
            str(acquired["lease_id"]),
            "--generation",
            str(acquired["generation"]),
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
        )
        reacquired = self.run_json_cli(
            *common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-a-1",
            "--task-id",
            "worker-b",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )

        self.assertEqual("active", acquired["status"])
        self.assertEqual("revoked", revoked["status"])
        self.assertEqual(("active", "worker-b"), (reacquired["status"], reacquired["task_id"]))
        self.assertGreater(self.json_int(reacquired["generation"]), self.json_int(revoked["generation"]))
        retained = SQLiteWorkStore(work / "state.sqlite3").snapshot().authority.attempt_leases[0]
        self.assertEqual(authority_models.AttemptLeaseStatus.ACTIVE, retained.state)
        self.assertEqual(reacquired["generation"], retained.generation)

    def test_first_init_recommends_body_after_prefix_once_when_user_setting_is_absent(self) -> None:
        for label, config_contents in (("missing-config", None), ("other-setting", 'model = "gpt-5"\n')):
            with self.subTest(label=label):
                project = Path(tempfile.mkdtemp()).resolve()
                work = project / ".codex" / "work"
                codex_home = Path(tempfile.mkdtemp()).resolve()
                config = codex_home / "config.toml"
                if config_contents is not None:
                    config.write_text(config_contents, encoding="utf-8")
                common = ("--project-root", str(project), "--work-root", str(work), "init")
                with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                    first_result, first_stdout, first_stderr = self.run_cli(*common)
                    second_result, second_stdout, second_stderr = self.run_cli(*common)

                self.assertEqual(0, first_result, first_stderr)
                self.assertEqual(0, second_result, second_stderr)
                self.assertEqual(1, first_stdout.count("model_auto_compact_token_limit_scope"))
                self.assertIn(str(config), first_stdout)
                self.assertNotIn("model_auto_compact_token_limit_scope", second_stdout)
                self.assert_repository_care_pointer(first_stdout, present=True)
                self.assert_repository_care_pointer(second_stdout, present=False)
                if config_contents is None:
                    self.assertFalse(config.exists())
                else:
                    self.assertEqual(config_contents, config.read_text(encoding="utf-8"))

    def test_explicit_claude_runtime_omits_only_codex_configuration_advice(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        codex_home = Path(tempfile.mkdtemp()).resolve()

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home), "PINBOARD_RUNTIME": "claude"}):
            result, stdout, stderr = self.run_cli(
                "--project-root",
                str(project),
                "--work-root",
                str(work),
                "init",
            )

        self.assertEqual(0, result, stderr)
        self.assertNotIn("model_auto_compact_token_limit_scope", stdout)
        self.assert_repository_care_pointer(stdout, present=True)
        self.assertTrue((work / "state.sqlite3").is_file())

    def test_first_init_config_recommendation_is_only_about_the_user_default(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        project_config = project / ".codex" / "config.toml"
        project_config.parent.mkdir()
        project_contents = 'model_auto_compact_token_limit_scope = "total"\n'
        project_config.write_text(project_contents, encoding="utf-8")
        work = project / ".codex" / "work"
        codex_home = Path(tempfile.mkdtemp()).resolve()

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            result, stdout, stderr = self.run_cli(
                "--project-root",
                str(project),
                "--work-root",
                str(work),
                "init",
            )

        self.assertEqual(0, result, stderr)
        self.assertIn("model_auto_compact_token_limit_scope", stdout)
        self.assertIn(str(codex_home / "config.toml"), stdout)
        self.assert_repository_care_pointer(stdout, present=True)
        self.assertEqual(project_contents, project_config.read_text(encoding="utf-8"))

    def test_failed_init_does_not_print_optional_guidance(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        invalid_parent = project / "not-a-directory"
        invalid_parent.write_text("occupied", encoding="utf-8")
        codex_home = Path(tempfile.mkdtemp()).resolve()

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            result, stdout, stderr = self.run_cli(
                "--project-root",
                str(project),
                "--work-root",
                str(invalid_parent / "work"),
                "init",
            )

        self.assertEqual(12, result)
        self.assertNotIn("model_auto_compact_token_limit_scope", stdout)
        self.assertNotIn("model_auto_compact_token_limit_scope", stderr)
        self.assert_repository_care_pointer(stdout, present=False)
        self.assert_repository_care_pointer(stderr, present=False)

    def test_first_init_omits_config_recommendation_without_reliable_user_setting_absence(self) -> None:
        for label, config_contents in (
            ("total", 'model_auto_compact_token_limit_scope = "total"\n'),
            ("body-after-prefix", 'model_auto_compact_token_limit_scope = "body_after_prefix"\n'),
            ("invalid", "[\n"),
            ("invalid-encoding", b"\xff"),
            ("unreadable", None),
        ):
            with self.subTest(label=label):
                project = Path(tempfile.mkdtemp()).resolve()
                work = project / ".codex" / "work"
                codex_home = Path(tempfile.mkdtemp()).resolve()
                config = codex_home / "config.toml"
                if config_contents is None:
                    config.mkdir()
                elif isinstance(config_contents, bytes):
                    config.write_bytes(config_contents)
                else:
                    config.write_text(config_contents, encoding="utf-8")

                with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                    result, stdout, stderr = self.run_cli(
                        "--project-root",
                        str(project),
                        "--work-root",
                        str(work),
                        "init",
                    )

                self.assertEqual(0, result, stderr)
                self.assertNotIn("model_auto_compact_token_limit_scope", stdout)
                self.assert_repository_care_pointer(stdout, present=True)
                self.assertTrue((work / "state.sqlite3").is_file())

    def test_installed_initialization_samples_its_operation_time_once(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        initialized_at = datetime.now(UTC)

        with patch("pinboard.interfaces.work_state_commands.datetime") as clock:
            clock.now.return_value = initialized_at
            result, stdout, stderr = self.run_cli(
                "--project-root",
                str(project),
                "--work-root",
                str(work),
                "init",
            )

        self.assertEqual(0, result, stderr)
        self.assertIn("WORK_STATE_INITIALIZED", stdout)
        self.assertEqual(1, clock.now.call_count)
        self.assertEqual(
            initialized_at, SQLiteWorkStore(work / "state.sqlite3").snapshot().lifecycle.project.updated_at
        )

    def test_installed_initialization_observes_preparation_expiry_boundary(self) -> None:
        expires_at = SQLITE_NOW + timedelta(minutes=1)
        for label, observed_at, expected_status in (
            ("before", expires_at - timedelta(microseconds=1), "active"),
            ("at", expires_at, "expired"),
            ("after", expires_at + timedelta(microseconds=1), "expired"),
        ):
            with self.subTest(label=label):
                project, work, _store = self.initialized_state(self.prepared_state(expires_at))
                with patch("pinboard.interfaces.work_state_commands.datetime") as clock:
                    clock.now.return_value = observed_at
                    result, stdout, stderr = self.run_cli(
                        "--project-root",
                        str(project),
                        "--work-root",
                        str(work),
                        "init",
                    )
                self.assertEqual(0, result, stderr)
                self.assertIn("WORK_STATE_INITIALIZED", stdout)
                self.assertEqual(1, clock.now.call_count)
                self.assertIn(
                    f"- Preparation: {expected_status}".encode(),
                    (work / "views" / "items" / "work-c.md").read_bytes(),
                )
                self.assertIn(
                    f"| {expected_status} |".encode(),
                    (work / "views" / "queue.md").read_bytes(),
                )

    def assert_activation_commit_and_duplicate(
        self,
        common: tuple[str, ...],
        activation: JsonObject,
        prepared: JsonObject,
        payload: Path,
        store: SQLiteWorkStore,
    ) -> None:
        activation_expiry = datetime.fromisoformat(str(prepared["expires_at"]))
        with (
            patch(
                "pinboard.adapters.files.views.atomic_replace",
                side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "injected view failure"),
            ),
            patch("pinboard.interfaces.action_selection.datetime") as selection_clock,
            patch("pinboard.interfaces.transitions.datetime") as transition_clock,
        ):
            selection_clock.now.return_value = activation_expiry - timedelta(microseconds=3)
            transition_clock.now.side_effect = (
                activation_expiry - timedelta(microseconds=2),
                activation_expiry - timedelta(microseconds=1),
                activation_expiry,
            )
            result, _stdout, stderr = self.run_transition(common, activation, payload, json_output=False)
        self.assertEqual(0, result, stderr)
        self.assertIn("generated views need repair", stderr)
        self.assertEqual(1, selection_clock.now.call_count)
        self.assertEqual(3, transition_clock.now.call_count)
        self.assertEqual(
            "revoked", self.run_json_cli(*common, "preparation", "status", "--item-id", "work-c")["status"]
        )
        activated_state = store.snapshot()
        duplicate, _stdout, duplicate_stderr = self.run_transition(common, activation, payload, json_output=False)
        self.assertEqual(11, duplicate)
        self.assertIn("ACTION_AUTHORITY_WRONG", duplicate_stderr)
        self.assertEqual(activated_state, store.snapshot())

    def test_activation_requires_exact_preparation_and_brief_identity(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        state = store.snapshot()
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(),
                attempt_generations=(),
                attempt_leases=(),
            ),
        )
        work.unlink(missing_ok=True) if work.is_file() else None
        database = work / "state.sqlite3"
        database.unlink()
        initialize_database(resolve_durable_roots(project), SQLITE_NOW)
        store = SQLiteWorkStore(database)
        initialize_store(store, state)
        common = ("--project-root", str(project), "--work-root", str(work))
        candidate = work_c_brief()
        brief_path = project / "work-c-brief.json"
        brief_path.write_bytes(canonical_work_brief_bytes(candidate))
        publication = self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
        payload = project / "activate.json"
        payload.write_text(
            json.dumps(
                {
                    "attempt": "work-c-1",
                    "branch": "codex/work-c",
                    "base_revision": "candidate-base",
                    "owner": "worker-task",
                    "brief_artifact_ref_id": publication["artifact_ref_id"],
                }
            ),
            encoding="utf-8",
        )

        definition = self.run_json_cli(*common, "item", "definition", "--item-id", "work-c")
        prepared = self.run_json_cli(
            *common,
            "preparation",
            "acquire",
            "--item-id",
            "work-c",
            "--expected-project-revision",
            str(definition["project_revision"]),
            "--expected-item-subject-revision",
            str(definition["item_subject_revision"]),
            "--expected-definition-revision",
            str(definition["definition_revision"]),
            "--expected-definition-digest",
            str(definition["definition_digest"]),
            "--task-id",
            "preparer-task",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        self.assert_installed_preparation_visibility(common, prepared, store)
        activation = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "preparer",
                    "--lease-id",
                    str(prepared["lease_id"]),
                    "--generation",
                    str(prepared["generation"]),
                    "--action-id",
                    "activate:work-c",
                )["actions"]
            )[0]
        )
        activation = self.assert_prepared_activation_rejections(common, activation, prepared, project, store, payload)
        self.assert_activation_commit_and_duplicate(common, activation, prepared, payload, store)

    def test_installed_authority_callers_sample_operation_refresh_and_preparation_render_separately(self) -> None:
        operation_time = SQLITE_NOW + timedelta(seconds=1)
        render_time = operation_time + timedelta(microseconds=1)
        project, work, state_store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        attempt = state_store.snapshot().authority.attempt_leases[0]
        with patch("pinboard.interfaces.attempt_authority.datetime") as attempt_clock:
            attempt_clock.now.side_effect = (operation_time, render_time)
            renewed = self.run_json_cli(
                *common,
                "attempt",
                "renew",
                "--attempt-id",
                "work-a-1",
                "--lease-id",
                "attempt-lease-a",
                "--generation",
                str(attempt.generation),
                "--ttl-seconds",
                "600",
            )
        self.assertEqual((operation_time + timedelta(seconds=600)).isoformat(), renewed["expires_at"])
        self.assertEqual(2, attempt_clock.now.call_count)

        expires_at = SQLITE_NOW + timedelta(minutes=1)
        project, work, _store = self.initialized_state(self.prepared_state(expires_at))
        common = ("--project-root", str(project), "--work-root", str(work))
        preparation_render_time = render_time + timedelta(microseconds=1)
        with patch("pinboard.interfaces.preparation_authority.datetime") as preparation_clock:
            preparation_clock.now.side_effect = (operation_time, render_time, preparation_render_time)
            renewed = self.run_json_cli(
                *common,
                "preparation",
                "renew",
                "--item-id",
                "work-c",
                "--lease-id",
                "preparation-c",
                "--generation",
                "1",
                "--ttl-seconds",
                "60",
            )
        self.assertEqual((operation_time + timedelta(seconds=60)).isoformat(), renewed["expires_at"])
        self.assertEqual(3, preparation_clock.now.call_count)

    def test_project_task_never_advertises_claimless_activation(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        with patch("pinboard.interfaces.work_inspection.datetime") as inspection_clock:
            inspection_clock.now.return_value = SQLITE_NOW
            result, _stdout, stderr = self.run_cli(
                *common,
                "actions",
                "--role",
                "project",
                "--action-id",
                "activate:work-c",
            )
        self.assertEqual(11, result)
        self.assertIn("ACTION_NOT_AVAILABLE", stderr)

    def test_installed_proposal_and_brief_publication_sample_commit_and_render_separately(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        proposal_path = project / "timed-proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-proposal/v1",
                    "proposal_id": "timed-proposal",
                    "created_at": SQLITE_NOW.isoformat(),
                    "source_task_id": "discoverer",
                    "user_label": "Timed proposal",
                    "trigger": "Prove fresh time sampling.",
                    "evidence": ["source:test"],
                    "why_it_matters": "Separate phases must not reuse time.",
                    "relation": {"kind": "independent", "item": None},
                    "effect": "The proposal is stored.",
                    "unlock": "The caller contract is covered.",
                    "urgency_evidence": "The accepted brief requires it.",
                    "freshness_assumptions": ["SQLite remains authoritative."],
                }
            ),
            encoding="utf-8",
        )
        commit_time = SQLITE_NOW + timedelta(seconds=1)
        render_time = commit_time + timedelta(microseconds=1)
        with patch("pinboard.interfaces.proposal_commands.datetime") as proposal_clock:
            proposal_clock.fromisoformat.side_effect = datetime.fromisoformat
            proposal_clock.now.side_effect = (commit_time, render_time)
            result, _stdout, stderr = self.run_cli(
                *common,
                "proposal",
                "--file",
                str(proposal_path),
                "--task-id",
                "discoverer",
                "--host-id",
                "studio",
            )
        self.assertEqual(0, result, stderr)
        self.assertEqual(2, proposal_clock.now.call_count)

        brief_path = project / "timed-brief.json"
        timed_brief = replace_struct(work_c_brief(), attempt_id="timed-brief-attempt")
        brief_path.write_bytes(canonical_work_brief_bytes(timed_brief))
        with patch("pinboard.interfaces.work_brief_publication.datetime") as publication_clock:
            publication_clock.now.side_effect = (commit_time, render_time)
            self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
        self.assertEqual(2, publication_clock.now.call_count)

    def test_installed_brief_publication_repairs_a_post_acceptance_attempt_view_failure(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        candidate = replace_struct(work_a_brief(project), artifact_revision=2)
        canonical_candidate = canonical_work_brief_bytes(candidate)
        brief_path = project / "repairable-brief.json"
        brief_path.write_bytes(canonical_candidate)
        attempt_view = work / "views/attempts/work-a-1.md"
        atomic_replace = file_views.atomic_replace

        def fail_attempt_view(path: Path, content: bytes) -> None:
            if path == attempt_view:
                raise FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "injected attempt-view failure")
            atomic_replace(path, content)

        with patch("pinboard.adapters.files.views.atomic_replace", side_effect=fail_attempt_view):
            result, stdout, stderr = self.run_cli(
                *common,
                "brief",
                "publish",
                "--file",
                str(brief_path),
                "--json",
            )

        self.assertEqual(0, result, stderr)
        self.assertIn("injected attempt-view failure", stderr)
        self.assertIn("pinboard views rebuild", stderr)
        publication = self.json_object(json.loads(stdout))
        fresh_state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        accepted_reference = next(
            reference
            for reference in fresh_state.artifact_references
            if int(reference.artifact_ref_id) == self.json_int(publication["artifact_ref_id"])
        )
        self.assertEqual(
            (
                accepted_reference.kind.value,
                accepted_reference.key,
                accepted_reference.revision,
                accepted_reference.selector,
                accepted_reference.content_sha256,
                accepted_reference.size_bytes,
                accepted_reference.accepted_revision,
            ),
            (
                publication["kind"],
                publication["key"],
                publication["revision"],
                publication["selector"],
                publication["content_sha256"],
                publication["size_bytes"],
                publication["accepted_revision"],
            ),
        )
        self.assertEqual(canonical_candidate, (work / accepted_reference.selector).read_bytes())

        rebuild_result, rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuild_result, f"{rebuild_stdout}\n{rebuild_stderr}")
        repaired_views = tuple(
            (path.relative_to(work / "views"), path.read_bytes())
            for path in sorted((work / "views").rglob("*"))
            if path.is_file()
        )
        clean_result, clean_stdout, clean_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, clean_result, f"{clean_stdout}\n{clean_stderr}")
        self.assertEqual(
            repaired_views,
            tuple(
                (path.relative_to(work / "views"), path.read_bytes())
                for path in sorted((work / "views").rglob("*"))
                if path.is_file()
            ),
        )

    def test_installed_read_render_and_validation_matrix_agrees_before_at_and_after_preparation_expiry(self) -> None:
        expires_at = SQLITE_NOW + timedelta(minutes=1)
        for label, observed_at, expected_status, expected_available in (
            ("before", expires_at - timedelta(microseconds=1), "active", False),
            ("at", expires_at, "expired", True),
            ("after", expires_at + timedelta(microseconds=1), "expired", True),
        ):
            with self.subTest(label=label):
                project, work, _store = self.initialized_state(self.prepared_state(expires_at))
                common = ("--project-root", str(project), "--work-root", str(work))
                with patch("pinboard.interfaces.work_inspection.datetime") as inspection_clock:
                    inspection_clock.now.return_value = observed_at
                    overview = self.run_json_cli(*common, "overview")
                    item = self.run_json_cli(*common, "item", "status", "--item-id", "work-c")
                    parallel = self.run_json_cli(*common, "parallel", "preview", "--item", "work-c")
                    action_result, _stdout, action_stderr = self.run_cli(
                        *common,
                        "actions",
                        "--role",
                        "preparer",
                        "--lease-id",
                        "preparation-c",
                        "--generation",
                        "1",
                        "--action-id",
                        "activate:work-c",
                    )
                self.assertEqual(4, inspection_clock.now.call_count)
                overview_item = next(
                    self.json_object(value)
                    for value in self.json_list(overview["items"])
                    if self.json_object(value)["item_id"] == "work-c"
                )
                self.assertEqual(expected_status, self.json_object(overview_item["preparation"])["status"])
                self.assertEqual(expected_status, self.json_object(item["preparation"])["status"])
                self.assertEqual(expected_available, "work-c" in self.json_list(overview["immediate_options"]))
                self.assertEqual(expected_available, parallel["safe"])
                self.assertEqual(0 if not expected_available else 11, action_result)
                if expected_available:
                    self.assertIn("ACTION_NOT_AVAILABLE", action_stderr)
                with patch("pinboard.interfaces.work_state_commands.datetime") as work_state_clock:
                    work_state_clock.now.side_effect = (observed_at, observed_at)
                    view_result, _view_stdout, view_stderr = self.run_cli(*common, "views", "rebuild")
                    validation_result, validation_stdout, validation_stderr = self.run_cli(
                        *common, "validate", "--json"
                    )
                self.assertEqual(0, view_result, view_stderr)
                self.assertEqual(10, validation_result, validation_stderr)
                validation = self.json_object(json.loads(validation_stdout))
                self.assertNotIn(
                    "VIEW_REFRESH_REQUIRED",
                    tuple(self.json_object(value)["code"] for value in self.json_list(validation["diagnostics"])),
                )
                self.assertEqual(2, work_state_clock.now.call_count)
                item_bytes = (work / "views" / "items" / "work-c.md").read_bytes()
                queue_bytes = (work / "views" / "queue.md").read_bytes()
                self.assertIn(f"- Preparation: {expected_status}".encode(), item_bytes)
                self.assertIn(f"| {expected_status} |".encode(), queue_bytes)

    def test_installed_prerequisite_proposal_observes_preparation_expiry_boundary(self) -> None:
        expires_at = SQLITE_NOW + timedelta(minutes=1)
        for label, observed_at, accepted in (
            ("before", expires_at - timedelta(microseconds=1), False),
            ("at", expires_at, True),
            ("after", expires_at + timedelta(microseconds=1), True),
        ):
            with self.subTest(label=label):
                project, work, store = self.initialized_state(self.prepared_state(expires_at))
                common = ("--project-root", str(project), "--work-root", str(work))
                proposal_id = f"expiry-{label}"
                proposal_path = project / f"{proposal_id}.json"
                proposal_path.write_text(
                    json.dumps(
                        {
                            "schema": "pinboard-proposal/v1",
                            "proposal_id": proposal_id,
                            "created_at": SQLITE_NOW.isoformat(),
                            "source_task_id": "discoverer",
                            "user_label": f"Expiry {label}",
                            "trigger": "Exercise the preparation boundary.",
                            "evidence": ["source:test"],
                            "why_it_matters": "Prerequisites must respect live preparation.",
                            "relation": {"kind": "prerequisite", "item": "work-c"},
                            "effect": "The prerequisite is stored.",
                            "unlock": "The expiry contract is observable.",
                            "urgency_evidence": "The accepted brief requires boundary evidence.",
                            "freshness_assumptions": ["SQLite remains authoritative."],
                        }
                    ),
                    encoding="utf-8",
                )
                before = store.snapshot()
                with patch("pinboard.interfaces.proposal_commands.datetime") as clock:
                    clock.fromisoformat.side_effect = datetime.fromisoformat
                    clock.now.side_effect = (observed_at, observed_at)
                    result, _stdout, stderr = self.run_cli(
                        *common,
                        "proposal",
                        "--file",
                        str(proposal_path),
                        "--task-id",
                        "discoverer",
                        "--host-id",
                        "studio",
                    )
                self.assertEqual(0 if accepted else 13, result, stderr)
                self.assertEqual(2 if accepted else 1, clock.now.call_count)
                if accepted:
                    self.assertTrue(
                        any(str(value.proposal_id) == proposal_id for value in store.snapshot().proposals.proposals)
                    )
                else:
                    self.assertIn("ACTION_NOT_AVAILABLE", stderr)
                    self.assertEqual(before, store.snapshot())

    def test_checkpoint_acceptance_archives_exact_attempt_receipts_in_one_transition(self) -> None:  # noqa: PLR0915 - one transaction scenario
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
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        attempt_root = work / "attempts" / "work-a-1"
        attempt_root.mkdir(parents=True)
        result_bytes = b"candidate result\n"
        review_bytes = b"independent review\n"
        (attempt_root / "result.md").write_bytes(result_bytes)
        payload = project / "accept-checkpoint.json"
        payload.write_text(
            '{"checkpoint":"checkpoint-a","candidate":"candidate-a","evidence":"Accepted."}\n',
            encoding="utf-8",
        )
        action = next(
            self.json_object(value)
            for value in self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "project",
                )["actions"]
            )
            if self.json_object(value)["action_id"] == "accept-checkpoint:work-a-1"
        )
        before_missing = store.snapshot()

        missing_result, _missing_stdout, missing_stderr = self.run_transition(
            common, action, payload, json_output=False
        )

        self.assertNotEqual(0, missing_result)
        self.assertIn("TRANSITION_INPUT_INVALID", missing_stderr)
        self.assertEqual(before_missing, store.snapshot())
        (attempt_root / "review.md").write_bytes(review_bytes)

        with patch(
            "pinboard.adapters.sqlite.state.append_history",
            side_effect=StorageError(
                StorageErrorCode.IO_ERROR,
                "injected checkpoint write failure",
                retryable=True,
            ),
        ):
            failed_result, failed_stdout, failed_stderr = self.run_transition(common, action, payload, json_output=True)

        self.assertNotEqual(0, failed_result)
        self.assertEqual("", failed_stderr)
        failure = self.json_object(json.loads(failed_stdout))
        self.assertEqual("pinboard-rejected-operation/v1", failure["schema"])
        self.assertEqual("committed-effect", failure["status"])
        self.assertEqual("transition:project", failure["operation"])
        self.assertEqual("STORAGE_IO_ERROR", failure["code"])
        self.assertTrue(failure["state_changed"])
        self.assertEqual(["immutable-artifact"], failure["changed_surfaces"])
        self.assertEqual("do-not-retry", failure["retry"])
        self.assertEqual(before_missing, store.snapshot())
        self.assertEqual(result_bytes, (work / "artifacts/results/work-a-1-checkpoint-a-result/1.md").read_bytes())
        self.assertEqual(review_bytes, (work / "artifacts/evidence/work-a-1-checkpoint-a-review/1.md").read_bytes())

        (attempt_root / "review.md").write_bytes(b"conflicting review\n")
        collision_result, _collision_stdout, collision_stderr = self.run_transition(
            common, action, payload, json_output=False
        )

        self.assertNotEqual(0, collision_result)
        self.assertIn("STORAGE_INVARIANT_VIOLATION", collision_stderr)
        self.assertEqual(before_missing, store.snapshot())
        self.assertEqual(review_bytes, (work / "artifacts/evidence/work-a-1-checkpoint-a-review/1.md").read_bytes())
        (attempt_root / "review.md").write_bytes(review_bytes)

        accepted_result, _accepted_stdout, accepted_stderr = self.run_transition(
            common, action, payload, json_output=False
        )

        self.assertEqual(0, accepted_result, accepted_stderr)
        reloaded = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        result_reference = next(
            value for value in reloaded.artifact_references if value.key == "work-a-1-checkpoint-a-result"
        )
        review_reference = next(
            value for value in reloaded.artifact_references if value.key == "work-a-1-checkpoint-a-review"
        )
        self.assertEqual(result_reference.artifact_ref_id, attempt.result_artifact_ref_id)
        self.assertEqual(review_reference.artifact_ref_id, reloaded.transition_receipts[-1].artifact_ref_id)
        self.assertEqual(result_bytes, (work / result_reference.selector).read_bytes())
        self.assertEqual(review_bytes, (work / review_reference.selector).read_bytes())
        self.assertEqual(before_missing.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(len(before_missing.transition_receipts) + 1, len(reloaded.transition_receipts))

    def test_current_read_surface_has_human_and_json_views(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        status = self.run_json_cli(*common, "status")
        self.assertEqual("sqlite-v5", status["authority"])
        self.assertEqual(2, status["intake_item_count"])
        status_result, status_stdout, status_stderr = self.run_cli(*common, "status")
        self.assertEqual(0, status_result, status_stderr)
        self.assertIn("intake_items=2", status_stdout)
        overview = self.run_json_cli(*common, "overview")
        self.assertEqual("12", overview["revision"])
        intake_item = next(
            self.json_object(value)
            for value in self.json_list(overview["items"])
            if self.json_object(value)["item_id"] == "intake-work"
        )
        self.assertIsNone(intake_item["source"])
        self.assertIsNone(intake_item["notes"])
        actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "project",
            )["actions"]
        )
        self.assertTrue(actions)
        self.assertEqual(
            "active", self.run_json_cli(*common, "attempt", "status", "--attempt-id", "work-a-1")["status"]
        )
        parallel = self.run_json_cli(*common, "parallel", "preview")
        self.assertEqual(
            {"excluded", "launchable", "revision", "safe", "schema", "selection"},
            set(parallel),
        )
        self.assertEqual(
            ("pinboard-parallel-preview/v1", "12", "all-safe", True),
            (parallel["schema"], parallel["revision"], parallel["selection"], parallel["safe"]),
        )
        items = [
            self.json_object(item)
            for item in (*self.json_list(parallel["launchable"]), *self.json_list(parallel["excluded"]))
        ]
        self.assertTrue(
            all(set(item) == {"attempt_id", "item_id", "label", "outcome", "reasons", "state"} for item in items)
        )
        self.assertEqual(
            [
                ("work-c", "Work work-c", "ready", None, "launchable", []),
                (
                    "intake-work",
                    "Work intake-work",
                    "intake",
                    None,
                    "excluded",
                    [
                        {
                            "code": "state-not-launchable",
                            "message": "Item 'intake-work' is intake; only ready items and unowned active attempts can launch.",
                        }
                    ],
                ),
                (
                    "work-a",
                    "Work work-a",
                    "active",
                    "work-a-1",
                    "excluded",
                    [
                        {
                            "code": "dependency-live",
                            "message": "Item 'work-a' still depends on live work: work-c.",
                        }
                    ],
                ),
                (
                    "zz-proposal-a",
                    "Proposal A",
                    "intake",
                    None,
                    "excluded",
                    [
                        {
                            "code": "state-not-launchable",
                            "message": "Item 'zz-proposal-a' is intake; only ready items and unowned active attempts can launch.",
                        }
                    ],
                ),
            ],
            [
                (
                    item["item_id"],
                    item["label"],
                    item["state"],
                    item["attempt_id"],
                    item["outcome"],
                    item["reasons"],
                )
                for item in items
            ],
        )
        self.assertIn("payload_schema", self.run_json_cli(*common, "input-contract", "activate"))

        human_commands = (
            ("root",),
            ("status",),
            ("overview",),
            ("actions", "--role", "observer"),
            ("input-contract", "activate"),
            ("attempt", "status", "--attempt-id", "work-a-1"),
            ("parallel", "preview"),
            ("views", "rebuild"),
        )
        for command in human_commands:
            with self.subTest(command=command):
                result, stdout, stderr = self.run_cli(*common, *command)
                self.assertEqual(0, result, stderr)
                self.assertTrue(stdout)
        result, stdout, stderr = self.run_cli(*common, "parallel", "preview")
        self.assertEqual(0, result, stderr)
        self.assertEqual(
            """OK PARALLEL_PREVIEW revision=12 selection=all-safe safe=yes
Ready to launch together:
- work-c (ready)
Not launchable:
- intake-work (intake) — Item 'intake-work' is intake; only ready items and unowned active attempts can launch.
- work-a (active, attempt work-a-1) — Item 'work-a' still depends on live work: work-c.
- zz-proposal-a (intake) — Item 'zz-proposal-a' is intake; only ready items and unowned active attempts can launch.
""",
            stdout,
        )

    def test_blocker_actions_and_input_contracts_are_unambiguous(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        project_actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "project",
            )["actions"]
        )
        worker_actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "worker",
                "--lease-id",
                "attempt-lease-a",
                "--generation",
                "3",
            )["actions"]
        )
        expected: dict[str, tuple[str, str, JsonObject]] = {
            "report-blocker": (
                "report-blocker:work-a-1",
                "Prepare blocker report for work-a",
                {
                    "use_case": "Preserve blocker evidence for the project.",
                    "effect": "advisory",
                    "permitted_roles": ["worker"],
                    "subject_kind": "attempt",
                    "lifecycle_precondition": "active-attempt",
                    "practical_result": "Prepare a blocker report without changing shared lifecycle state.",
                },
            ),
            "block": (
                "block:work-a-1",
                "Block active attempt for work-a",
                {
                    "use_case": "Stop an active attempt on dependencies already accepted in its definition.",
                    "effect": "mutating",
                    "permitted_roles": ["project"],
                    "subject_kind": "attempt",
                    "lifecycle_precondition": "active-attempt",
                    "practical_result": "Move the item and attempt to blocked without changing accepted dependencies.",
                },
            ),
            "block-item": (
                "block-item:intake-work",
                "Block unstarted work item intake-work",
                {
                    "use_case": "Stop unstarted intake work on dependencies already accepted in its definition.",
                    "effect": "mutating",
                    "permitted_roles": ["project"],
                    "subject_kind": "item",
                    "lifecycle_precondition": "intake-item",
                    "practical_result": "Move the item to blocked without changing accepted dependencies or creating an attempt.",
                },
            ),
        }
        all_actions = tuple(self.json_object(action) for action in (*project_actions, *worker_actions))
        selected = {
            kind: next(action for action in all_actions if action["action_id"] == action_id)
            for kind, (action_id, _label, _semantics) in expected.items()
        }
        for kind, (action_id, label, semantics) in expected.items():
            with self.subTest(kind=kind):
                action = selected[kind]
                self.assertEqual(action_id, action["action_id"])
                self.assertEqual(label, action["label"])
                self.assertEqual(semantics, self.json_object(action["semantics"]))
                contract = self.run_json_cli(*common, "input-contract", kind)
                self.assertEqual(kind, contract["action_kind"])
                self.assertEqual(semantics, self.json_object(contract["semantics"]))
                if kind == "report-blocker":
                    self.assertIsNone(contract["payload_schema"])
                else:
                    self.assertIsInstance(contract["payload_schema"], dict)

        continue_contract = self.run_json_cli(*common, "input-contract", "continue")
        self.assertEqual(["project", "worker"], self.json_object(continue_contract["semantics"])["permitted_roles"])
        self.assertIsNone(continue_contract["payload_schema"])
        continue_actions = tuple(action for action in all_actions if action["action_id"] == "continue:work-a-1")
        self.assertEqual({"project", "attempt"}, {action["authorization"] for action in continue_actions})
        for action in continue_actions:
            self.assertEqual(continue_contract["semantics"], action["semantics"])

    def test_resume_and_reopen_command_semantics_match_contextual_action_results(self) -> None:
        state = complete_sqlite_state()
        lifecycle = replace(
            state.lifecycle,
            work_items=tuple(
                replace(value, state=stored_state.StoredWorkItemState.PAUSED, next_action="resume")
                if value.item_id == ItemId("work-a")
                else replace(value, state=stored_state.StoredWorkItemState.BLOCKED, next_action="resume")
                if value.item_id == ItemId("work-c")
                else replace(value, state=stored_state.StoredWorkItemState.DEFERRED, next_action="reopen")
                if value.item_id == ItemId("intake-work")
                else value
                for value in state.lifecycle.work_items
            ),
            attempts=tuple(
                replace(value, state=work_models.AttemptState.PAUSED)
                if value.attempt_id == AttemptId("work-a-1")
                else value
                for value in state.lifecycle.attempts
            ),
            dependencies=tuple(value for value in state.lifecycle.dependencies if value.item_id != ItemId("work-a")),
        )
        state = replace(state, lifecycle=lifecycle)
        state = with_definition_dependencies(state, ItemId("work-a"), ())
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        actions = {
            action["action_id"]: action
            for value in self.json_list(self.run_json_cli(*common, "actions", "--role", "project")["actions"])
            if (action := self.json_object(value))["action_id"]
            in {"resume:work-a", "resume:work-c", "reopen:intake-work"}
        }

        self.assertEqual("Return work-a to active", actions["resume:work-a"]["label"])
        self.assertEqual("Return work-c to ready", actions["resume:work-c"]["label"])
        self.assertEqual("Reopen intake-work for intake", actions["reopen:intake-work"]["label"])
        resume_contract = self.run_json_cli(*common, "input-contract", "resume")
        reopen_contract = self.run_json_cli(*common, "input-contract", "reopen")
        self.assertEqual(resume_contract["semantics"], actions["resume:work-a"]["semantics"])
        self.assertEqual(resume_contract["semantics"], actions["resume:work-c"]["semantics"])
        self.assertEqual(reopen_contract["semantics"], actions["reopen:intake-work"]["semantics"])
        self.assertEqual(
            "Return paused or blocked work to active when an attempt exists, otherwise ready.",
            self.json_object(resume_contract["semantics"])["practical_result"],
        )
        self.assertEqual(
            "Return deferred work to intake.",
            self.json_object(reopen_contract["semantics"])["practical_result"],
        )

    def test_paused_definition_stale_attempt_with_live_dependency_can_rebind_but_not_resume(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.PAUSED)
                    if value.item_id == ItemId("work-a")
                    else value
                    for value in state.lifecycle.work_items
                ),
                attempts=(replace(state.lifecycle.attempts[0], state=work_models.AttemptState.PAUSED),),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        definition, digest = test_definition(ItemId("work-a"))
        revision = self.write_item_revision(
            project / "paused-stale-item-revision.json",
            ItemId("work-a"),
            1,
            digest,
            definition,
            objective="Accept a corrected definition while the attempt remains paused.",
        )
        revision_action = self.project_action(common, "revise-item:work-a")
        revision_result, _revision_stdout, revision_stderr = self.run_transition(
            common, revision_action, revision, json_output=False
        )
        self.assertEqual(0, revision_result, revision_stderr)

        actions = tuple(
            self.json_object(value)
            for value in self.json_list(self.run_json_cli(*common, "actions", "--role", "project")["actions"])
        )
        action_ids = {str(value["action_id"]) for value in actions}

        self.assertIn("rebind-attempt:work-a-1", action_ids)
        self.assertNotIn("resume:work-a", action_ids)
        rebind = next(value for value in actions if value["action_id"] == "rebind-attempt:work-a-1")
        self.assertEqual(
            "active-or-paused-attempt",
            self.json_object(rebind["semantics"])["lifecycle_precondition"],
        )

    def test_active_attempt_blocker_flow_persists_dependencies_and_resumes_through_commands(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        lifecycle = replace(
            state.lifecycle,
            dependencies=tuple(value for value in state.lifecycle.dependencies if value.item_id != ItemId("work-a")),
        )
        state = replace(
            state,
            lifecycle=lifecycle,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        state = with_definition_dependencies(state, ItemId("work-a"), (ItemId("intake-work"),))
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        report = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "worker",
                    "--lease-id",
                    "attempt-lease-a",
                    "--generation",
                    "3",
                    "--action-id",
                    "report-blocker:work-a-1",
                )["actions"]
            )[0]
        )
        self.assertEqual("advisory", self.json_object(report["semantics"])["effect"])
        released = self.run_json_cli(
            *common,
            "attempt",
            "release",
            "--attempt-id",
            "work-a-1",
            "--lease-id",
            "attempt-lease-a",
            "--generation",
            "3",
        )
        self.assertEqual("released", released["status"])

        block = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "project",
                    "--action-id",
                    "block:work-a-1",
                )["actions"]
            )[0]
        )
        self.assertEqual("active-attempt", self.json_object(block["semantics"])["lifecycle_precondition"])
        block_payload = project / "block.json"
        block_payload.write_text(
            '{"reason":"Waiting for the intake prerequisite.","depends_on":["intake-work"]}\n',
            encoding="utf-8",
        )
        block_result, _block_stdout, block_stderr = self.run_transition(common, block, block_payload, json_output=False)
        self.assertEqual(0, block_result, block_stderr)

        blocked = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        blocked_item = next(value for value in blocked.lifecycle.work_items if value.item_id == ItemId("work-a"))
        blocked_attempt = next(
            value for value in blocked.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
        )
        self.assertEqual(stored_state.StoredWorkItemState.BLOCKED, blocked_item.state)
        self.assertEqual(work_models.AttemptState.BLOCKED, blocked_attempt.state)
        self.assertEqual(
            ("intake-work",),
            tuple(
                str(value.dependency_id)
                for value in blocked.lifecycle.dependencies
                if value.item_id == ItemId("work-a")
            ),
        )

        self.run_json_cli(
            *common,
            "close",
            "intake-work",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite is satisfied.",
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
        )
        resume = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "project",
                    "--action-id",
                    "resume:work-a",
                )["actions"]
            )[0]
        )
        self.assertEqual("resume:work-a", resume["action_id"])
        resume_payload = project / "resume.json"
        resume_payload.write_text("{}\n", encoding="utf-8")
        resume_result, _resume_stdout, resume_stderr = self.run_transition(
            common, resume, resume_payload, json_output=False
        )
        self.assertEqual(0, resume_result, resume_stderr)
        resumed = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        resumed_item = next(value for value in resumed.lifecycle.work_items if value.item_id == ItemId("work-a"))
        resumed_attempt = next(
            value for value in resumed.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
        )
        self.assertEqual(stored_state.StoredWorkItemState.ACTIVE, resumed_item.state)
        self.assertEqual(work_models.AttemptState.ACTIVE, resumed_attempt.state)

    def test_revised_brief_resume_keeps_scope_identity_atomic_through_supported_commands(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                dependencies=tuple(
                    value for value in state.lifecycle.dependencies if value.item_id != ItemId("work-a")
                ),
            ),
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
            artifact_references=(state.artifact_references[0],),
            transition_receipts=(replace(state.transition_receipts[0], artifact_ref_id=None),),
        )
        state = with_definition_dependencies(state, ItemId("work-a"), ())
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        pause_payload = project / "pause.json"
        pause_payload.write_text('{"reason":"Pause before accepting revised scope."}\n', encoding="utf-8")

        pause_action = self.project_action(common, "pause:work-a-1")
        pause_result, _pause_stdout, pause_stderr = self.run_transition(
            common, pause_action, pause_payload, json_output=False
        )
        self.assertEqual(0, pause_result, pause_stderr)
        proposal_path = project / "required-first.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-proposal/v1",
                    "proposal_id": "required-first",
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_task_id": "discovering-task",
                    "user_label": "Required first",
                    "trigger": "Work A needs one newly discovered prerequisite.",
                    "evidence": ["source:command-scenario"],
                    "why_it_matters": "The revised accepted scope must remain aligned with its resumed brief.",
                    "relation": {"kind": "prerequisite", "item": "work-a"},
                    "effect": "Record the prerequisite candidate and relationship.",
                    "unlock": "Resume Work A from one revised canonical brief.",
                    "urgency_evidence": "This reproduces the supported release-blocking sequence.",
                    "freshness_assumptions": ["Work A remains live."],
                }
            ),
            encoding="utf-8",
        )
        proposal_result, _proposal_stdout, proposal_stderr = self.run_cli(
            *common,
            "proposal",
            "--file",
            str(proposal_path),
            "--task-id",
            "discovering-task",
            "--host-id",
            "studio",
        )
        self.assertEqual(0, proposal_result, proposal_stderr)
        self.run_json_cli(
            *common,
            "close",
            "required-first",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite is satisfied.",
            "--task-id",
            "project-task",
            "--host-id",
            "studio",
        )
        revised_definition = next(
            value
            for value in reversed(store.snapshot().lifecycle.definition_revisions)
            if value.item_id == ItemId("work-a")
        )
        brief = work_a_brief(project)
        checkpoint = brief.checkpoint
        self.assertIsInstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        authorization = work_brief_models.AcceptedScopeAuthorization("work-a", revised_definition.revision)
        checkpoint = replace_struct(
            checkpoint,
            contracts=(replace_struct(checkpoint.contracts[0], authorization_basis=authorization),),
            verification=(replace_struct(checkpoint.verification[0], authorization_basis=authorization),),
        )
        brief = replace_struct(
            brief,
            artifact_revision=2,
            accepted_scope=work_brief_models.AcceptedScope(revised_definition.revision, revised_definition.digest),
            checkpoint=checkpoint,
        )
        brief_path = project / "work-a-brief-2.json"
        brief_path.write_bytes(canonical_work_brief_bytes(brief))
        publication = self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
        resume_payload = project / "resume-revised.json"
        resume_payload.write_text(
            json.dumps({"brief_artifact_ref_id": publication["artifact_ref_id"]}), encoding="utf-8"
        )

        resume_action = self.project_action(common, "resume:work-a")
        resume_result, resume_stdout, resume_stderr = self.run_transition(
            common, resume_action, resume_payload, json_output=False
        )
        self.assertEqual(0, resume_result, resume_stderr)

        reloaded = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        reloaded_item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        reloaded_definition = next(
            value for value in reversed(reloaded.lifecycle.definition_revisions) if value.item_id == ItemId("work-a")
        )
        reloaded_attempt = next(
            value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
        )
        self.assertEqual(
            (
                stored_state.StoredWorkItemState.ACTIVE,
                work_models.AttemptState.ACTIVE,
                publication["artifact_ref_id"],
                reloaded_definition.revision,
                reloaded_definition.digest,
            ),
            (
                reloaded_item.state,
                reloaded_attempt.state,
                reloaded_attempt.brief_artifact_ref_id,
                reloaded_attempt.accepted_scope_revision,
                reloaded_attempt.accepted_scope_digest,
            ),
        )
        self.assertIn(f"revision={reloaded.lifecycle.project.revision}", resume_stdout)
        validation_result, validation_stdout, validation_stderr = self.run_cli(*common, "validate")
        self.assertEqual(0, validation_result, f"{validation_stdout}\n{validation_stderr}")
        self.assertIn("OK WORK_STATE_VALID", validation_stdout)
        project_actions = self.json_list(self.run_json_cli(*common, "actions", "--role", "project")["actions"])
        self.assertIn(
            "dispatch:work-a-1",
            tuple(str(self.json_object(value)["action_id"]) for value in project_actions),
        )

    def test_rebind_attempt_updates_every_read_surface_and_dispatch_identity_without_touching_checkout(  # noqa: PLR0915
        self,
    ) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
            artifact_references=(state.artifact_references[0],),
            transition_receipts=(replace(state.transition_receipts[0], artifact_ref_id=None),),
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        definition, digest = test_definition(ItemId("work-a"))
        revision = self.write_item_revision(
            project / "rebind-item-revision.json",
            ItemId("work-a"),
            1,
            digest,
            definition,
            objective="Accept current scope while correcting the Git lineage.",
        )
        revision_action = self.project_action(common, "revise-item:work-a")
        revision_result, _revision_stdout, revision_stderr = self.run_transition(
            common, revision_action, revision, json_output=False
        )
        self.assertEqual(0, revision_result, revision_stderr)
        original = store.snapshot()
        original_attempt = original.lifecycle.attempts[0]
        original_counter = original.authority.attempt_counters[0]
        current_definition = next(
            value for value in reversed(original.lifecycle.definition_revisions) if value.item_id == ItemId("work-a")
        )

        replacement_checkpoint = work_a_brief(project).checkpoint
        self.assertIsInstance(replacement_checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        assert isinstance(replacement_checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        authorization = work_brief_models.AcceptedScopeAuthorization("work-a", current_definition.revision)
        replacement_checkpoint = replace_struct(
            replacement_checkpoint,
            contracts=tuple(
                replace_struct(contract, authorization_basis=authorization)
                for contract in replacement_checkpoint.contracts
            ),
            verification=tuple(
                replace_struct(obligation, authorization_basis=authorization)
                for obligation in replacement_checkpoint.verification
            ),
        )
        replacement = replace_struct(
            work_a_brief(project),
            artifact_revision=2,
            branch="codex/corrected-work-a",
            base_revision="corrected-base-revision",
            accepted_scope=work_brief_models.AcceptedScope(
                current_definition.revision,
                current_definition.digest,
            ),
            checkpoint=replacement_checkpoint,
        )
        brief_path = project / "work-a-brief-2.json"
        brief_path.write_bytes(canonical_work_brief_bytes(replacement))
        publication = self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
        payload = project / "rebind-attempt.json"
        payload.write_text(
            json.dumps(
                {
                    "attempt": "work-a-1",
                    "branch": replacement.branch,
                    "base_revision": replacement.base_revision,
                    "brief_artifact_ref_id": publication["artifact_ref_id"],
                }
            ),
            encoding="utf-8",
        )
        checkout_file = project / "architecture.md"
        checkout_bytes = checkout_file.read_bytes()
        contract = self.run_json_cli(*common, "input-contract", "rebind-attempt")
        self.assertEqual("rebind-attempt", contract["action_kind"])
        action = self.project_action(common, "rebind-attempt:work-a-1")

        result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)

        self.assertEqual(0, result, stderr)
        self.assertIn("OK TRANSITION_APPLIED rebind-attempt:work-a-1", stdout)
        rebound = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        rebound_attempt = rebound.lifecycle.attempts[0]
        self.assertEqual(
            (
                original_attempt.attempt_id,
                original_attempt.item_id,
                original_attempt.state,
                replacement.branch,
                replacement.base_revision,
                publication["artifact_ref_id"],
                original_attempt.result_artifact_ref_id,
                original_attempt.candidate_revision,
                current_definition.revision,
                current_definition.digest,
                rebound.lifecycle.project.revision,
            ),
            (
                rebound_attempt.attempt_id,
                rebound_attempt.item_id,
                rebound_attempt.state,
                rebound_attempt.branch,
                rebound_attempt.base_revision,
                rebound_attempt.brief_artifact_ref_id,
                rebound_attempt.result_artifact_ref_id,
                rebound_attempt.candidate_revision,
                rebound_attempt.accepted_scope_revision,
                rebound_attempt.accepted_scope_digest,
                rebound_attempt.subject_revision,
            ),
        )
        self.assertEqual(
            original_counter.generation_high_water + 1, rebound.authority.attempt_counters[0].generation_high_water
        )
        self.assertEqual(original_counter.generation_high_water + 1, rebound.authority.attempt_leases[0].generation)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, rebound.authority.attempt_leases[0].state)
        self.assertEqual(decision_models.ActionKind.REBIND_ATTEMPT, rebound.transition_receipts[-1].action_kind)
        old_worker_result, _old_worker_stdout, old_worker_stderr = self.run_cli(
            *common,
            "actions",
            "--role",
            "worker",
            "--lease-id",
            "attempt-lease-a",
            "--generation",
            "3",
        )
        self.assertNotEqual(0, old_worker_result)
        self.assertIn("ATTEMPT_LEASE_REQUIRED", old_worker_stderr)

        validation_result, validation_stdout, validation_stderr = self.run_cli(*common, "validate")
        self.assertEqual(0, validation_result, f"{validation_stdout}\n{validation_stderr}")
        rebuild_result, rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuild_result, f"{rebuild_stdout}\n{rebuild_stderr}")
        handover = self.run_json_cli(*common, "handover")
        handover_attempt = self.json_object(self.json_list(handover["attempts"])[0])
        self.assertEqual(replacement.branch, handover_attempt["branch"])
        self.assertEqual(replacement.base_revision, handover_attempt["base_revision"])
        self.assertEqual(publication["artifact_ref_id"], handover_attempt["brief_artifact_ref_id"])
        self.assertEqual("rebind-attempt", self.json_object(self.json_list(handover["transitions"])[-1])["action_kind"])

        dispatch_action = self.project_action(common, "dispatch:work-a-1")
        review_path = project / "work-a-brief-review.json"
        review_path.write_bytes(ready_review(replacement))
        old_environment_path = project / "old-dispatch.json"
        old_environment_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-dispatch/v1",
                    "checkout": str(project),
                    "branch": original_attempt.branch,
                    "starting_revision": original_attempt.base_revision,
                    "permissions": ["repository-read"],
                }
            ),
            encoding="utf-8",
        )
        dispatch_arguments = (
            *common,
            "dispatch",
            "--action-id",
            str(dispatch_action["action_id"]),
            "--expected-revision",
            str(dispatch_action["expected_revision"]),
            "--task-id",
            "replacement-worker",
            "--host-id",
            "studio",
            "--checkpoint",
            CHECKPOINT_ID,
        )
        old_result, _old_stdout, old_stderr = self.run_cli(
            *dispatch_arguments,
            "--environment",
            str(old_environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "replacement-review",
        )
        self.assertNotEqual(0, old_result)
        self.assertIn("DISPATCH_BRANCH_MISMATCH", old_stderr)
        corrected_environment_path = project / "corrected-dispatch.json"
        corrected_environment_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-dispatch/v1",
                    "checkout": str(project),
                    "branch": replacement.branch,
                    "starting_revision": replacement.base_revision,
                    "permissions": ["repository-read"],
                }
            ),
            encoding="utf-8",
        )
        dispatch_result, dispatch_stdout, dispatch_stderr = self.run_cli(
            *dispatch_arguments,
            "--environment",
            str(corrected_environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "replacement-review",
        )
        self.assertEqual(0, dispatch_result, dispatch_stderr)
        self.assertIn(f"- Branch: {replacement.branch}", dispatch_stdout)
        self.assertIn(f"- Starting revision: {replacement.base_revision}", dispatch_stdout)
        self.assertEqual(checkout_bytes, checkout_file.read_bytes())

        review_path.write_bytes(
            ready_review(
                replacement,
                reviewer="different-reviewer",
                result="A different reviewer reached the same coverage verdict.",
            )
        )
        current_dispatch = self.project_action(common, "dispatch:work-a-1")
        collision_result, collision_stdout, collision_stderr = self.run_cli(
            *common,
            "dispatch",
            "--action-id",
            str(current_dispatch["action_id"]),
            "--expected-revision",
            str(current_dispatch["expected_revision"]),
            "--task-id",
            "replacement-worker",
            "--host-id",
            "studio",
            "--checkpoint",
            CHECKPOINT_ID,
            "--environment",
            str(corrected_environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "collision-review",
            "--json",
        )
        self.assertEqual(14, collision_result)
        self.assertEqual("", collision_stderr)
        collision = self.json_object(json.loads(collision_stdout))
        self.assertEqual("committed-effect", collision["status"])
        self.assertEqual("DISPATCH_BRIEF_REVIEW_COLLISION", collision["code"])
        self.assertTrue(collision["state_changed"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            collision["changed_surfaces"],
        )

    def test_revised_brief_identity_mismatches_reject_at_command_boundary_without_effects(self) -> None:
        base_brief = replace_struct(work_a_brief(Path(tempfile.mkdtemp()).resolve()), artifact_revision=2)
        checkpoint = base_brief.checkpoint
        self.assertIsInstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)

        def with_scope_identity(item_id: str, revision: int) -> work_brief_models.CrossBoundaryCheckpoint:
            authorization = work_brief_models.AcceptedScopeAuthorization(item_id, revision)
            return replace_struct(
                checkpoint,
                contracts=(replace_struct(checkpoint.contracts[0], authorization_basis=authorization),),
                verification=(replace_struct(checkpoint.verification[0], authorization_basis=authorization),),
            )

        mismatches = (
            ("attempt", replace_struct(base_brief, attempt_id="different-1")),
            (
                "item",
                replace_struct(
                    base_brief,
                    item_id="different",
                    checkpoint=with_scope_identity("different", base_brief.accepted_scope.revision),
                ),
            ),
            ("branch", replace_struct(base_brief, branch="codex/different")),
            ("base", replace_struct(base_brief, base_revision="different-base")),
            (
                "scope-revision",
                replace_struct(
                    base_brief,
                    accepted_scope=work_brief_models.AcceptedScope(2, base_brief.accepted_scope.digest),
                    checkpoint=with_scope_identity(base_brief.item_id, 2),
                ),
            ),
            (
                "scope-digest",
                replace_struct(base_brief, accepted_scope=work_brief_models.AcceptedScope(1, "f" * 64)),
            ),
        )

        for name, mismatched_brief in mismatches:
            with self.subTest(identity=name):
                state = complete_sqlite_state()
                state = replace(
                    state,
                    lifecycle=replace(
                        state.lifecycle,
                        work_items=tuple(
                            replace(value, state=stored_state.StoredWorkItemState.PAUSED)
                            if value.item_id == ItemId("work-a")
                            else value
                            for value in state.lifecycle.work_items
                        ),
                        attempts=(replace(state.lifecycle.attempts[0], state=work_models.AttemptState.PAUSED),),
                        dependencies=tuple(
                            value for value in state.lifecycle.dependencies if value.item_id != ItemId("work-a")
                        ),
                    ),
                )
                state = with_definition_dependencies(state, ItemId("work-a"), ())
                project, work, _store = self.initialized_state(state)
                common = ("--project-root", str(project), "--work-root", str(work))
                brief_path = project / f"mismatched-{name}.json"
                brief_path.write_bytes(canonical_work_brief_bytes(mismatched_brief))
                publication = self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
                rebuild_result, rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
                self.assertEqual(0, rebuild_result, f"{rebuild_stdout}\n{rebuild_stderr}")
                action = self.project_action(common, "resume:work-a")
                payload = project / f"resume-{name}.json"
                payload.write_text(
                    json.dumps({"brief_artifact_ref_id": publication["artifact_ref_id"]}), encoding="utf-8"
                )
                database_path = work / "state.sqlite3"
                before = SQLiteWorkStore(database_path).snapshot()
                views_root = work / "views"
                before_views = tuple(
                    (path.relative_to(views_root), path.read_bytes())
                    for path in sorted(views_root.rglob("*"))
                    if path.is_file()
                )

                result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)

                self.assertNotEqual(0, result)
                self.assertEqual("", stdout)
                self.assertIn("TRANSITION_INPUT_INVALID", stderr)
                self.assertEqual(before, SQLiteWorkStore(database_path).snapshot())
                self.assertEqual(
                    before_views,
                    tuple(
                        (path.relative_to(views_root), path.read_bytes())
                        for path in sorted(views_root.rglob("*"))
                        if path.is_file()
                    ),
                )

    def test_post_commit_brief_projection_failure_keeps_direct_transition_receipt(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        action = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "worker",
                    "--lease-id",
                    "attempt-lease-a",
                    "--generation",
                    "3",
                    "--action-id",
                    "submit-review:work-a-1",
                )["actions"]
            )[0]
        )
        payload = project / "submit-review.json"
        payload.write_text('{"candidate":"projection-failure-candidate"}\n', encoding="utf-8")

        with patch(
            "pinboard.interfaces.work_views.read_attempt_brief_views",
            return_value=WorkBriefFailure(WorkBriefErrorCode.BRIEF_INVALID, "injected projection failure"),
        ):
            result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)

        self.assertEqual(0, result, stderr)
        self.assertIn("OK TRANSITION_APPLIED submit-review:work-a-1 revision=13", stdout)
        self.assertIn("SQLite transition succeeded, but generated views need repair", stderr)
        self.assertIn("pinboard views rebuild", stderr)
        reloaded = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.assertEqual(work_models.AttemptState.REVIEW, attempt.state)
        self.assertEqual(13, store.snapshot().transition_receipts[-1].project_revision)
        rebuild_result, rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuild_result, f"{rebuild_stdout}\n{rebuild_stderr}")

    def test_unexpected_post_commit_projection_exception_remains_exceptional(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        action = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "worker",
                    "--lease-id",
                    "attempt-lease-a",
                    "--generation",
                    "3",
                    "--action-id",
                    "submit-review:work-a-1",
                )["actions"]
            )[0]
        )
        payload = project / "submit-review.json"
        payload.write_text('{"candidate":"unexpected-projection-candidate"}\n', encoding="utf-8")

        with (
            patch("pinboard.interfaces.work_views.read_attempt_brief_views", side_effect=RuntimeError("unexpected")),
            self.assertRaisesRegex(RuntimeError, "unexpected"),
        ):
            self.run_transition(common, action, payload, json_output=False)

        reloaded = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.assertEqual(work_models.AttemptState.REVIEW, attempt.state)
        self.assertEqual(13, store.snapshot().transition_receipts[-1].project_revision)

    def test_invalid_current_inputs_map_to_stable_cli_failures(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        invalid = project / "invalid.json"
        invalid.write_text("[]", encoding="utf-8")
        cases = (
            (
                (
                    "proposal",
                    "--file",
                    str(invalid),
                    "--task-id",
                    "discovering-task",
                    "--host-id",
                    "studio",
                ),
                "PROPOSAL_INVALID",
            ),
            (("attempt", "status", "--attempt-id", "missing"), "ATTEMPT_LEASE_REQUIRED"),
        )
        for arguments, code in cases:
            with self.subTest(arguments=arguments):
                result, _stdout, stderr = self.run_cli(*common, *arguments)
                self.assertNotEqual(0, result)
                self.assertIn(code, stderr)

        identifier_stderr = self.run_cli_parse_error(
            *common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-a-1",
            "--task-id",
            "..",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        self.assertIn("pinboard attempt acquire", identifier_stderr)
        self.assertIn("$.task_id", identifier_stderr)

    def test_direct_transition_selects_exact_worker_capability(self) -> None:  # noqa: PLR0915 - one submit and read-only review journey
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        exact = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "worker",
                "--lease-id",
                "attempt-lease-a",
                "--generation",
                "3",
                "--action-id",
                "submit-review:work-a-1",
            )["actions"]
        )
        action = self.json_object(exact[0])
        before_mismatch = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        mismatched_payload = project / "pause-payload.json"
        mismatched_payload.write_text('{"reason":"pause"}\n', encoding="utf-8")

        mismatch_result, _mismatch_stdout, mismatch_stderr = self.run_transition(
            common, action, mismatched_payload, json_output=False
        )

        self.assertEqual(11, mismatch_result)
        self.assertIn("TRANSITION_INPUT_INVALID:", mismatch_stderr)
        self.assertEqual(before_mismatch, SQLiteWorkStore(work / "state.sqlite3").snapshot())

        payload = project / "submit-review.json"
        payload.write_text('{"candidate":"candidate-cli-direct"}\n', encoding="utf-8")

        result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)
        self.assertEqual(0, result, stderr)
        self.assertIn("OK TRANSITION_APPLIED submit-review:work-a-1", stdout)

        inspected = self.run_json_cli(*common, "attempt", "inspect", "--attempt-id", "work-a-1")
        continuation = self.json_object(inspected["continuation"])
        self.assertEqual("review", continuation["state"])
        self.assertFalse(continuation["terminal"])
        self.assertFalse(continuation["user_input_required"])
        self.assertEqual(work_a_brief(project).owner_task_id, continuation["owner_task_id"])
        self.assertEqual("review-subagent", self.json_object(continuation["next_operation"])["kind"])
        self.assertEqual("candidate-cli-direct", self.json_object(continuation["next_operation"])["candidate_revision"])
        before_review_job = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        self.assertIsNone(before_review_job.lifecycle.attempts[0].result_artifact_ref_id)
        review_arguments = (
            *common,
            "review-job",
            "--attempt-id",
            "work-a-1",
            "--candidate-revision",
            "candidate-cli-direct",
        )
        missing, _, _ = self.run_cli(*review_arguments)
        self.assertEqual(11, missing)
        result_path = work / "attempts" / "work-a-1" / "result.md"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("Candidate evidence.\n", encoding="utf-8")
        job = self.run_json_cli(*review_arguments)
        self.assertEqual(str(result_path), job["result_path"])
        self.assertEqual(hashlib.sha256(result_path.read_bytes()).hexdigest(), job["result_sha256"])
        self.assertEqual("candidate-cli-direct", job["candidate_revision"])
        self.assertEqual(before_review_job, SQLiteWorkStore(work / "state.sqlite3").snapshot())
        result_path.write_text("", encoding="utf-8")
        empty, _, _ = self.run_cli(*review_arguments)
        self.assertEqual(11, empty)
        result_path.unlink()
        result_path.mkdir()
        unreadable, _, _ = self.run_cli(*review_arguments)
        self.assertEqual(11, unreadable)
        mismatch, _, _ = self.run_cli(*review_arguments[:-1], "different-candidate")
        self.assertEqual(11, mismatch)
        self.assertEqual(before_review_job, SQLiteWorkStore(work / "state.sqlite3").snapshot())

        invalid_cases = (
            (("--action-id", "invalid"), "ACTION_ID_MALFORMED"),
            (
                (
                    "--action-id",
                    "invented:work-a",
                ),
                "ACTION_KIND_UNKNOWN",
            ),
        )
        for replacement, code in invalid_cases:
            arguments = [
                *common,
                "transition",
                "--action-id",
                "pause:work-a-1",
                "--expected-revision",
                "stale",
                "--generation",
                "3",
                "--authorization",
                "attempt",
                "--lease-id",
                "attempt-lease-a",
                "--payload",
                str(project / "missing-transition-payload.json"),
            ]
            option = replacement[0]
            if option == "--action-id":
                index = arguments.index(option)
                arguments[index : index + 2] = replacement
            else:
                arguments.extend(replacement)
            with self.subTest(code=code):
                invalid_result, _invalid_stdout, invalid_stderr = self.run_cli(*arguments)
                self.assertEqual(11, invalid_result)
                self.assertIn(code, invalid_stderr)

    def test_review_acceptance_and_correction_continue_in_the_same_attempt(self) -> None:
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
                        candidate_revision="candidate-cli-review",
                        candidate_recorded_at=now,
                    )
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in state.lifecycle.attempts
                ),
            ),
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        for action_id, value in (
            ("accept-review-and-continue:work-a-1", '{"candidate":"candidate-cli-review","evidence":"accepted"}'),
            ("return-for-correction:work-a-1", '{"reason":"Correct the candidate."}'),
        ):
            with self.subTest(action=action_id):
                project, work, store = self.initialized_state(state)
                common = ("--project-root", str(project), "--work-root", str(work))
                action = self.project_action(common, action_id)
                payload = project / "review-disposition.json"
                payload.write_text(value, encoding="utf-8")
                result, stdout, stderr = self.run_transition(common, action, payload, json_output=True)
                self.assertEqual(0, result, stderr)
                rendered = msgspec.json.decode(stdout, type=work_inspection_models.TransitionView)
                self.assertIsNotNone(rendered.continuation)
                assert rendered.continuation is not None
                self.assertEqual(work_a_brief(project).owner_task_id, rendered.continuation.owner_task_id)
                self.assertEqual(work_models.AttemptState.ACTIVE, rendered.continuation.state)
                reloaded = store.snapshot()
                attempt = next(
                    value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
                )
                self.assertEqual(work_models.AttemptState.ACTIVE, attempt.state)
                self.assertIsNone(attempt.candidate_revision)
                rejected, _, _ = self.run_cli(
                    *common, "review-job", "--attempt-id", "work-a-1", "--candidate-revision", "candidate-cli-review"
                )
                self.assertEqual(11, rejected)
                self.assertEqual(reloaded, store.snapshot())

    def test_completion_continuation_is_terminal_and_inspection_is_read_only(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        action = self.project_action(common, "complete:work-a-1")
        payload = project / "complete.json"
        payload.write_text('{"evidence":"All accepted work is complete."}', encoding="utf-8")
        with patch(
            "pinboard.interfaces.work_views.read_attempt_brief_views",
            return_value=WorkBriefFailure(WorkBriefErrorCode.BRIEF_INVALID, "injected view failure"),
        ):
            result, stdout, stderr = self.run_transition(common, action, payload, json_output=True)
        self.assertEqual(0, result, stderr)
        self.assertIn("injected view failure", stderr)
        rendered = msgspec.json.decode(stdout, type=work_inspection_models.TransitionView)
        continuation = rendered.continuation
        assert continuation is not None
        self.assertEqual(work_models.AttemptState.DONE, continuation.state)
        self.assertTrue(continuation.terminal)
        self.assertIsNone(continuation.owner_task_id)
        self.assertIsNone(continuation.next_operation)
        self.assertEqual((), continuation.legal_actions)
        before = store.snapshot()
        for arguments in (
            ("attempt", "inspect", "--attempt-id", "unknown"),
            ("review-job", "--attempt-id", "work-a-1", "--candidate-revision", "candidate"),
            ("review-job", "--attempt-id", "unknown", "--candidate-revision", "candidate"),
        ):
            rejected, _, _ = self.run_cli(*common, *arguments)
            self.assertEqual(11, rejected)
            self.assertEqual(before, store.snapshot())

    def test_direct_transition_reports_its_own_revision_across_a_disjoint_commit(self) -> None:
        state = complete_sqlite_state()
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        action = self.project_action(common, "pause:work-a-1")
        payload = project / "pause.json"
        payload.write_text('{"reason":"Pause at a stable checkpoint."}\n', encoding="utf-8")
        proposal_path = project / "interleaved-direct-proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-proposal/v1",
                    "proposal_id": "interleaved-direct-proposal",
                    "created_at": "2026-08-25T12:00:00+02:00",
                    "source_task_id": "discovering-task",
                    "user_label": "Interleaved direct proposal",
                    "trigger": "A disjoint commit must not change the direct transition receipt.",
                    "evidence": ["source:cli"],
                    "why_it_matters": "The command must report the revision it committed.",
                    "relation": {"kind": "independent", "item": None},
                    "effect": "The proposal commits before transition presentation.",
                    "unlock": "Prove exact revision attribution.",
                    "urgency_evidence": "The direct command exposes a revision.",
                    "freshness_assumptions": ["SQLite remains authoritative."],
                }
            ),
            encoding="utf-8",
        )
        commit_transition = transition_interface.decide_and_commit_transition

        def commit_then_create_disjoint_proposal(
            selected_store: WorkStore,
            command: decision_models.NonCheckpointTransitionCommand,
            decided_at: datetime,
            *,
            actor_task_id: TaskId | None,
            actor_host_id: HostId | None,
            transition_brief_identity: WorkBriefIdentity | None,
        ) -> DecisionResult[MutationReceipt]:
            commit_result = commit_transition(
                selected_store,
                command,
                decided_at,
                actor_task_id=actor_task_id,
                actor_host_id=actor_host_id,
                transition_brief_identity=transition_brief_identity,
            )
            if isinstance(commit_result, DecisionFailure):
                return commit_result
            proposal_result, _, proposal_stderr = self.run_cli(
                *common,
                "proposal",
                "--file",
                str(proposal_path),
                "--task-id",
                "discovering-task",
                "--host-id",
                "studio",
            )
            self.assertEqual(0, proposal_result, proposal_stderr)
            return commit_result

        with patch(
            "pinboard.interfaces.transitions.decide_and_commit_transition",
            side_effect=commit_then_create_disjoint_proposal,
        ):
            result, stdout, stderr = self.run_transition(common, action, payload, json_output=False)

        self.assertEqual(0, result, stderr)
        self.assertIn("OK TRANSITION_APPLIED pause:work-a-1 revision=13", stdout)
        self.assertEqual(
            (
                (decision_models.ActionKind.PAUSE, 13),
                (decision_models.ActionKind.INSPECT, 14),
            ),
            tuple(
                (receipt.action_kind, receipt.project_revision) for receipt in store.snapshot().transition_receipts[-2:]
            ),
        )

    def test_locked_rejection_returns_fresh_same_subject_alternatives(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        action = self.project_action(common, "pause:work-a-1")
        payload = project / "pause-after-race.json"
        payload.write_text('{"reason":"Pause after current work."}\n', encoding="utf-8")
        commit_transition = transition_interface.decide_and_commit_transition

        def commit_disjoint_change_then_recheck(
            selected_store: WorkStore,
            command: decision_models.NonCheckpointTransitionCommand,
            decided_at: datetime,
            *,
            actor_task_id: TaskId | None,
            actor_host_id: HostId | None,
            transition_brief_identity: WorkBriefIdentity | None,
        ) -> DecisionResult[MutationReceipt]:
            current_actions = expect_success(
                discover_actions(store.snapshot(), decision_models.Role.PROJECT, now=decided_at)
            )
            defer_action = next(
                value
                for value in current_actions
                if isinstance(value, decision_models.DeferAction) and str(value.capability.subject) == "work-c"
            )
            competing = decision_models.DeferCommand(
                defer_action,
                work_models.DeferInput(
                    work_models.Timing.SAFE_TO_DEFER,
                    "Reopen after the observed transition race.",
                ),
            )
            competing_result = commit_transition(
                selected_store,
                competing,
                decided_at,
                actor_task_id=actor_task_id,
                actor_host_id=actor_host_id,
            )
            self.assertNotIsInstance(competing_result, DecisionFailure)
            return commit_transition(
                selected_store,
                command,
                decided_at,
                actor_task_id=actor_task_id,
                actor_host_id=actor_host_id,
                transition_brief_identity=transition_brief_identity,
            )

        with patch(
            "pinboard.interfaces.transitions.decide_and_commit_transition",
            side_effect=commit_disjoint_change_then_recheck,
        ):
            result, stdout, stderr = self.run_transition(common, action, payload, json_output=True)

        self.assertEqual(11, result)
        self.assertEqual("", stderr)
        rejection = self.json_object(json.loads(stdout))
        self.assertEqual("ACTION_NOT_AVAILABLE", rejection["code"])
        self.assertFalse(rejection["state_changed"])
        alternatives = tuple(self.json_object(value) for value in self.json_list(rejection["next_actions"]))
        fresh_pause = next(value for value in alternatives if value.get("action_id") == "pause:work-a-1")
        self.assertEqual("13", fresh_pause["expected_revision"])
        self.assertIsNone(fresh_pause["generation"])
        work_c = next(value for value in store.snapshot().lifecycle.work_items if value.item_id == ItemId("work-c"))
        self.assertEqual(stored_state.StoredWorkItemState.DEFERRED, work_c.state)

    def test_proposal_persists_once_through_native_intake(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        proposal_path = project / "proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-proposal/v1",
                    "proposal_id": "cli-sqlite-proposal",
                    "created_at": "2026-08-25T12:00:00+02:00",
                    "source_task_id": "discovering-task",
                    "user_label": "SQLite CLI proposal",
                    "trigger": "The public proposal command must use current authority.",
                    "evidence": ["source:cli"],
                    "why_it_matters": "A filesystem writer cannot persist to SQLite authority.",
                    "relation": {"kind": "follow-up", "item": "work-c"},
                    "effect": "The proposal appears once as an intake item.",
                    "unlock": "Use the application proposal service.",
                    "urgency_evidence": "The installed command must remain current.",
                    "freshness_assumptions": ["SQLite remains authoritative."],
                }
            ),
            encoding="utf-8",
        )
        common = ("--project-root", str(project), "--work-root", str(work))
        before = store.snapshot()

        proposal_arguments = (
            *common,
            "proposal",
            "--file",
            str(proposal_path),
            "--task-id",
            "discovering-task",
            "--host-id",
            "studio",
        )
        created = self.run_json_cli(*proposal_arguments)
        duplicate_result, _, duplicate_stderr = self.run_cli(*proposal_arguments)

        self.assertEqual("pinboard-proposal-created/v1", created["schema"])
        self.assertEqual("cli-sqlite-proposal", created["proposal_id"])
        self.assertEqual("intake", created["state"])
        after = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        self.assertEqual(str(after.lifecycle.project.revision), created["committed_revision"])
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
        self.assertEqual(before.authority, after.authority)
        self.assertEqual(
            before.lifecycle.definition_revisions,
            tuple(
                value
                for value in after.lifecycle.definition_revisions
                if value.item_id != ItemId("cli-sqlite-proposal")
            ),
        )
        self.assertEqual(
            before.lifecycle.work_items,
            tuple(value for value in after.lifecycle.work_items if value.item_id != ItemId("cli-sqlite-proposal")),
        )
        persisted_proposal = next(
            value for value in after.proposals.proposals if str(value.proposal_id) == "cli-sqlite-proposal"
        )
        intake_item = next(value for value in after.lifecycle.work_items if str(value.item_id) == "cli-sqlite-proposal")
        self.assertEqual(intake_item.queue_position, created["position"])
        self.assertEqual(
            (
                datetime(2026, 8, 25, 10, tzinfo=UTC),
                TaskId("discovering-task"),
                "SQLite CLI proposal",
                "The public proposal command must use current authority.",
                "A filesystem writer cannot persist to SQLite authority.",
                "The proposal appears once as an intake item.",
                "Use the application proposal service.",
                "The installed command must remain current.",
            ),
            (
                persisted_proposal.created_at,
                persisted_proposal.source_task_id,
                persisted_proposal.user_label,
                persisted_proposal.trigger,
                persisted_proposal.why_it_matters,
                persisted_proposal.effect,
                persisted_proposal.unlock,
                persisted_proposal.urgency_evidence,
            ),
        )
        self.assertEqual(
            ("source:cli",),
            tuple(
                value.selector
                for value in after.proposals.evidence
                if value.proposal_id == persisted_proposal.proposal_id
            ),
        )
        self.assertEqual(
            ("SQLite remains authoritative.",),
            tuple(
                value.assumption
                for value in after.proposals.freshness
                if value.proposal_id == persisted_proposal.proposal_id
            ),
        )
        self.assertEqual(
            (stored_state.StoredWorkItemState.INTAKE, 5),
            (intake_item.state, intake_item.queue_position),
        )
        self.assertEqual(
            ("work-c",),
            tuple(
                str(value.dependency_id)
                for value in after.lifecycle.dependencies
                if value.item_id == intake_item.item_id
            ),
        )
        self.assertEqual(13, duplicate_result)
        self.assertIn("PROPOSAL_ALREADY_EXISTS", duplicate_stderr)

    def test_proposal_changes_refresh_the_same_items_as_a_full_rebuild(self) -> None:
        def assert_partial_matches_full(
            work: Path,
            common: tuple[str, ...],
            selectors: tuple[str, ...],
        ) -> None:
            partial = {selector: (work / "views" / selector).read_bytes() for selector in selectors}
            rebuilt, _stdout, stderr = self.run_cli(*common, "views", "rebuild")
            self.assertEqual(0, rebuilt, stderr)
            self.assertEqual(
                partial,
                {selector: (work / "views" / selector).read_bytes() for selector in selectors},
            )

        for relation, proposal_id, changed_items in (
            ({"kind": "independent", "item": None}, "view-independent", ("view-independent",)),
            (
                {"kind": "prerequisite", "item": "work-c"},
                "view-prerequisite",
                ("view-prerequisite", "work-c"),
            ),
        ):
            with self.subTest(operation="create", relation=relation["kind"]):
                project, work, _store = self.initialized_state(complete_sqlite_state())
                common = ("--project-root", str(project), "--work-root", str(work))
                rebuilt, _stdout, stderr = self.run_cli(*common, "views", "rebuild")
                self.assertEqual(0, rebuilt, stderr)
                for item_id in changed_items:
                    path = work / "views" / "items" / f"{item_id}.md"
                    if path.exists():
                        path.write_bytes(b"stale projection\n")
                proposal_path = project / f"{proposal_id}.json"
                proposal_path.write_text(
                    json.dumps(
                        {
                            "schema": "pinboard-proposal/v1",
                            "proposal_id": proposal_id,
                            "created_at": SQLITE_NOW.isoformat(),
                            "source_task_id": "discovering-task",
                            "user_label": f"View {proposal_id}",
                            "trigger": "A changed item projection must be republished.",
                            "evidence": ["source:cli"],
                            "why_it_matters": "Partial refresh must agree with rebuild.",
                            "relation": relation,
                            "effect": "The proposal and its intake work are stored together.",
                            "unlock": "Readers see current item facts immediately.",
                            "urgency_evidence": "The installed path owns generated views.",
                            "freshness_assumptions": ["SQLite remains authoritative."],
                        }
                    ),
                    encoding="utf-8",
                )

                created, _stdout, stderr = self.run_cli(
                    *common,
                    "proposal",
                    "--file",
                    str(proposal_path),
                    "--task-id",
                    "discovering-task",
                    "--host-id",
                    "studio",
                )

                self.assertEqual(0, created, stderr)
                assert_partial_matches_full(
                    work,
                    common,
                    tuple(f"items/{item_id}.md" for item_id in changed_items),
                )

        for action_id, payload in (
            (
                "accept-proposal:zz-proposal-a",
                {
                    "item": "zz-proposal-a",
                    "state": "ready",
                    "next_action": "activate",
                    "timing": None,
                    "depends_on": (),
                },
            ),
            ("merge-proposal:zz-proposal-a", {"target": "work-c"}),
            ("return-proposal:zz-proposal-a", {"reason": "Clarify the evidence."}),
            ("reject-proposal:zz-proposal-a", {"reason": "The proposal is obsolete."}),
        ):
            with self.subTest(operation=action_id):
                project, work, _store = self.initialized_state(complete_sqlite_state())
                common = ("--project-root", str(project), "--work-root", str(work))
                rebuilt, _stdout, stderr = self.run_cli(*common, "views", "rebuild")
                self.assertEqual(0, rebuilt, stderr)
                item_view = work / "views" / "items" / "zz-proposal-a.md"
                item_view.write_bytes(b"stale projection\n")
                actions = self.json_list(self.run_json_cli(*common, "actions", "--role", "project")["actions"])
                action = next(
                    self.json_object(value) for value in actions if self.json_object(value)["action_id"] == action_id
                )
                payload_path = project / f"{action_id.split(':', 1)[0]}.json"
                payload_path.write_text(json.dumps(payload), encoding="utf-8")

                applied, _stdout, stderr = self.run_transition(common, action, payload_path, json_output=False)

                self.assertEqual(0, applied, stderr)
                assert_partial_matches_full(work, common, ("items/zz-proposal-a.md",))

    def test_attempt_renewal_preserves_semantically_unchanged_view_files(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        rebuilt, _stdout, stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuilt, stderr)
        selectors = ("queue.md", "history.md")

        def snapshots() -> dict[str, tuple[bytes, int, int]]:
            return {
                selector: (
                    (path := work / "views" / selector).read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for selector in selectors
            }

        before = snapshots()
        renewed, _stdout, stderr = self.run_cli(
            *common,
            "attempt",
            "renew",
            "--attempt-id",
            "work-a-1",
            "--lease-id",
            "attempt-lease-a",
            "--generation",
            "3",
            "--ttl-seconds",
            "600",
        )

        self.assertEqual(0, renewed, stderr)
        self.assertEqual(state.lifecycle.project.revision + 1, store.snapshot().lifecycle.project.revision)
        after_renewal = snapshots()
        self.assertEqual(before["queue.md"], after_renewal["queue.md"])
        self.assertNotEqual(before["history.md"][0], after_renewal["history.md"][0])
        self.assertNotEqual(before["history.md"][1], after_renewal["history.md"][1])
        rebuilt, _stdout, stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuilt, stderr)
        self.assertEqual(after_renewal, snapshots())

    def test_proposal_file_failure_is_stable(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        missing, _missing_stdout, missing_stderr = self.run_cli(
            *common,
            "proposal",
            "--file",
            str(project / "missing.json"),
            "--task-id",
            "discovering-task",
            "--host-id",
            "studio",
        )
        self.assertEqual(2, missing)
        self.assertIn("PROPOSAL_INVALID", missing_stderr)
        rejected, rejected_stdout, rejected_stderr = self.run_cli(
            *common,
            "proposal",
            "--file",
            str(project / "missing.json"),
            "--task-id",
            "discovering-task",
            "--host-id",
            "studio",
            "--json",
        )
        self.assertEqual(2, rejected)
        self.assertEqual("", rejected_stderr)
        rejected_payload = self.json_object(json.loads(rejected_stdout))
        self.assertEqual("pinboard-rejected-operation/v1", rejected_payload["schema"])
        self.assertEqual("PROPOSAL_INVALID", rejected_payload["code"])
        self.assertFalse(rejected_payload["state_changed"])

    def test_status_uses_one_snapshot_and_query_failures_are_stable(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        original_snapshot = SQLiteWorkStore.snapshot
        calls = 0

        def counted(store: SQLiteWorkStore) -> stored_state.StoredWorkState:
            nonlocal calls
            calls += 1
            return original_snapshot(store)

        with patch.object(SQLiteWorkStore, "snapshot", counted):
            status = self.run_json_cli(*common, "status")
        self.assertEqual("12", status["revision"])
        self.assertEqual(1, calls)

        result, _, stderr = self.run_cli(*common, "parallel", "preview", "--item", "missing")
        self.assertEqual(11, result)
        self.assertIn("PARALLEL_SELECTION_INVALID", stderr)

    def test_validate_uses_one_snapshot_for_authority_and_projection_diagnostics(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        initialized, _stdout, stderr = self.run_cli(*common, "init")
        self.assertEqual(0, initialized, stderr)
        original_snapshot = SQLiteWorkStore.validated_snapshot
        calls = 0

        def counted(store: SQLiteWorkStore) -> stored_state.StoredWorkState:
            nonlocal calls
            calls += 1
            return original_snapshot(store)

        with patch.object(SQLiteWorkStore, "validated_snapshot", counted):
            result, stdout, stderr = self.run_cli(*common, "validate")

        self.assertEqual(0, result, stderr)
        self.assertIn("OK WORK_STATE_VALID", stdout)
        self.assertEqual(1, calls)

    def test_installed_inspections_use_their_declared_read_capability(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        original_snapshot = SQLiteWorkStore.snapshot

        for arguments in (
            ("overview", "--json"),
            ("actions", "--role", "observer", "--json"),
            ("parallel", "preview", "--json"),
        ):
            calls = 0

            def counted(store: SQLiteWorkStore) -> stored_state.StoredWorkState:
                nonlocal calls
                calls += 1
                return original_snapshot(store)

            with self.subTest(command=arguments[0]), patch.object(SQLiteWorkStore, "snapshot", counted):
                result, _stdout, stderr = self.run_cli(*common, *arguments)
            self.assertEqual(0, result, stderr)
            self.assertEqual(1, calls)

        for arguments in (
            ("item", "status", "--item-id", "work-a", "--json"),
            ("item", "definition", "--item-id", "work-a", "--json"),
            ("item", "definition-history", "--item-id", "work-a", "--json"),
        ):
            with (
                self.subTest(command=arguments[1]),
                patch.object(SQLiteWorkStore, "snapshot", side_effect=AssertionError("complete snapshot used")),
            ):
                result, _stdout, stderr = self.run_cli(*common, *arguments)
            self.assertEqual(0, result, stderr)

        with (
            patch.object(SQLiteWorkStore, "snapshot", side_effect=AssertionError("unexpected SQLite read")),
            patch(
                "pinboard.interfaces.cli.work_state_commands.resolve_roots",
                side_effect=AssertionError("unexpected project-root read"),
            ),
        ):
            result, stdout, stderr = self.run_cli("input-contract", "inspect", "--json")
        self.assertEqual(0, result, stderr)
        self.assertIn('"action_kind": "inspect"', stdout)

    def test_rooted_command_composes_one_store_while_static_commands_compose_none(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        source = project / "source.txt"
        source.write_text("selected authority\n", encoding="utf-8")
        manifest = project / "sources.json"
        manifest.write_text(
            '{"schema":"pinboard-brief-sources/v1","sources":['
            '{"authority_id":"source","selector":"source.txt","families":["contract"]}]}\n',
            encoding="utf-8",
        )
        original_compose = work_state_commands.compose_store
        composed: list[SQLiteWorkStore] = []

        def counted_compose(durable: DurableRoots) -> SQLiteWorkStore:
            store = original_compose(durable)
            composed.append(store)
            return store

        with patch.object(work_state_commands, "compose_store", counted_compose):
            result, _stdout, stderr = self.run_cli(*common, "status")
        self.assertEqual(0, result, stderr)
        self.assertEqual(1, len(composed))

        dispatch_action = self.project_action(common, "dispatch:work-a-1")
        environment = project / "dispatch.json"
        environment.write_text(
            json.dumps(
                {
                    "schema": "pinboard-dispatch/v1",
                    "checkout": str(project),
                    "branch": "codex/work-a",
                    "starting_revision": "base-revision",
                    "permissions": ["repository-read"],
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(work_state_commands, "compose_store", return_value=_store),
            patch.object(
                action_selection,
                "select_current_action",
                wraps=action_selection.select_current_action,
            ) as select_action,
            patch.object(dispatch_brief, "prepare_dispatch", return_value="prepared prompt\n") as prepare,
        ):
            result, _stdout, stderr = self.run_cli(
                *common,
                "dispatch",
                "--action-id",
                str(dispatch_action["action_id"]),
                "--expected-revision",
                str(dispatch_action["expected_revision"]),
                "--task-id",
                "project-task",
                "--host-id",
                "local",
                "--checkpoint",
                CHECKPOINT_ID,
                "--environment",
                str(environment),
            )
        self.assertEqual(0, result, stderr)
        self.assertIs(_store, select_action.call_args.args[0])
        self.assertIs(_store, prepare.call_args.args[0])

        for arguments in (
            ("input-contract", "inspect", "--json"),
            ("tool-contract", "--json"),
            (*common, "root"),
            (*common, "brief-sources", "--file", str(manifest), "--json"),
        ):
            with (
                self.subTest(command=arguments[0]),
                patch.object(
                    work_state_commands,
                    "compose_store",
                    side_effect=AssertionError("static command composed a store"),
                ),
            ):
                result, _stdout, stderr = self.run_cli(*arguments)
            self.assertEqual(0, result, stderr)

        for arguments in (("--help",), ("--version",)):
            with (
                self.subTest(command=arguments[0]),
                patch.object(
                    work_state_commands,
                    "compose_store",
                    side_effect=AssertionError("static command composed a store"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main(arguments)
            self.assertEqual(0, raised.exception.code)

    def test_item_status_emits_exact_json_and_text_without_a_complete_snapshot(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        done_item = replace(
            state.lifecycle.work_items[2],
            state=stored_state.StoredWorkItemState.DONE,
            timing=work_models.Timing.SAFE_TO_DEFER,
            outcome_evidence="accepted completion",
            next_action=None,
            source=None,
            notes=None,
        )
        done_attempt = replace(
            active,
            attempt_id=AttemptId("work-b-1"),
            item_id=done_item.item_id,
            state=work_models.AttemptState.DONE,
            accepted_scope_digest=test_definition(done_item.item_id)[1],
            candidate_revision="candidate-b",
            candidate_recorded_at=SQLITE_NOW,
        )
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=(*state.lifecycle.work_items[:2], done_item, *state.lifecycle.work_items[3:]),
                attempts=(*state.lifecycle.attempts, done_attempt),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        with patch.object(SQLiteWorkStore, "snapshot", side_effect=AssertionError("complete snapshot used")):
            status = self.run_json_cli(*common, "item", "status", "--item-id", "work-b")
        self.assertEqual(
            {
                "schema": "pinboard-item-status/v1",
                "authority": "sqlite-v5",
                "revision": "12",
                "item_id": "work-b",
                "label": "Work work-b",
                "state": "done",
                "timing": "safe-to-defer",
                "outcome_evidence": "accepted completion",
                "next_action": None,
                "source": None,
                "notes": None,
                "queue_position": None,
                "attempts": [{"attempt_id": "work-b-1", "state": "done", "candidate_revision": "candidate-b"}],
                "preparation": None,
            },
            status,
        )
        self.assertEqual(
            {
                "schema": "pinboard-item-status/v1",
                "authority": "sqlite-v5",
                "revision": "12",
                "item_id": "work-a",
                "label": "Work work-a",
                "state": "active",
                "timing": "must-now",
                "outcome_evidence": None,
                "next_action": "continue",
                "source": "accepted requirement",
                "notes": "Current work remains bounded.",
                "queue_position": 2,
                "attempts": [{"attempt_id": "work-a-1", "state": "active", "candidate_revision": None}],
                "preparation": None,
            },
            self.run_json_cli(*common, "item", "status", "--item-id", "work-a"),
        )
        result, stdout, stderr = self.run_cli(*common, "item", "status", "--item-id", "work-b")
        self.assertEqual(0, result, stderr)
        self.assertIn("OK ITEM_STATUS item=work-b state=done revision=12 authority=sqlite-v5", stdout)
        self.assertIn("queue_position=none", stdout)
        self.assertIn("outcome_evidence=accepted completion", stdout)
        self.assertIn("source=none notes=none", stdout)
        self.assertIn("attempt=work-b-1 state=done candidate=candidate-b", stdout)

    def test_item_status_rejects_missing_and_malformed_identities(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))

        missing, _missing_stdout, missing_stderr = self.run_cli(*common, "item", "status", "--item-id", "missing-item")
        malformed_errors = (
            self.run_cli_parse_error(*common, "item", "status", "--item-id", "bad/item"),
            self.run_cli_parse_error(*common, "item", "status", "--item-id", "bad\n"),
        )

        self.assertEqual(11, missing)
        self.assertIn("ITEM_NOT_FOUND", missing_stderr)
        for malformed_stderr in malformed_errors:
            self.assertIn("pinboard item status", malformed_stderr)
            self.assertIn("$.item_id", malformed_stderr)

    def test_relational_and_bounded_cli_inputs_are_rejected_at_the_selected_leaf(self) -> None:
        cases = (
            (("actions", "--role", "worker", "--lease-id", "lease-a"), "pinboard actions"),
            (
                (
                    "transition",
                    "--action-id",
                    "pause:attempt-a",
                    "--expected-revision",
                    "1",
                    "--task-id",
                    "task-a",
                    "--host-id",
                    "host-a",
                    "--authorization",
                    "attempt",
                    "--payload",
                    "payload.json",
                ),
                "pinboard transition",
            ),
            (
                (
                    "dispatch",
                    "--action-id",
                    "dispatch:attempt-a",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--checkpoint",
                    "checkpoint-a",
                    "--environment",
                    "environment.json",
                    "--review-id",
                    "review-a",
                ),
                "pinboard dispatch",
            ),
            (
                (
                    "dispatch",
                    "--action-id",
                    "dispatch:attempt-a",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--checkpoint",
                    "checkpoint-a",
                    "--environment",
                    "environment.json",
                    "--brief-review",
                    "review.json",
                ),
                "pinboard dispatch",
            ),
            (
                (
                    "dispatch",
                    "--action-id",
                    "dispatch:attempt-a",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--checkpoint",
                    "checkpoint-a",
                    "--environment",
                    "environment.json",
                    "--brief-review",
                    "review.json",
                    "--review-id",
                    "NOT KEBAB",
                ),
                "pinboard dispatch",
            ),
            (
                ("brief-sources", "--file", "manifest.json", "--max-batch-bytes", "0", "--json"),
                "pinboard brief-sources",
            ),
            (("item", "definition-history", "--item-id", "work-a", "--limit", "0"), "pinboard item definition-history"),
            (
                ("item", "definition-history", "--item-id", "work-a", "--limit", "101"),
                "pinboard item definition-history",
            ),
            (
                ("item", "definition-history", "--item-id", "work-a", "--before-revision", "0"),
                "pinboard item definition-history",
            ),
            (("parallel", "preview", "--item", "work-a", "--item", "work-a"), "pinboard parallel preview"),
        )
        for arguments, route in cases:
            with self.subTest(arguments=arguments):
                if "--json" in arguments:
                    result, stdout, stderr = self.run_cli(*arguments)
                    self.assertEqual(2, result)
                    self.assertEqual("", stderr)
                    rejection = self.json_object(json.loads(stdout))
                    self.assertEqual("CLI_ARGUMENT_INVALID", rejection["code"])
                    message = rejection["message"]
                    self.assertIsInstance(message, str)
                    assert isinstance(message, str)
                    self.assertIn(route, message)
                else:
                    stderr = self.run_cli_parse_error(*arguments)
                    self.assertIn(route, stderr)

    def test_transition_requires_explicit_authorization(self) -> None:
        stderr = self.run_cli_parse_error(
            "transition",
            "--action-id",
            "pause:attempt-a",
            "--expected-revision",
            "1",
            "--generation",
            "1",
            "--payload",
            "payload.json",
        )

        self.assertIn("the following arguments are required: --authorization", stderr)

    def test_custom_command_decoders_preserve_identifier_constraints(self) -> None:
        cases = (
            (("actions", "--role", "observer", "--action-id", "bad/id"), "$.action_id"),
            (("actions", "--role", "observer", "--action-id", "bad\n"), "$.action_id"),
            (
                (
                    "attempt",
                    "acquire",
                    "--attempt-id",
                    "bad/id",
                    "--task-id",
                    "task-a",
                    "--host-id",
                    "host-a",
                    "--ttl-seconds",
                    "60",
                ),
                "$.attempt_id",
            ),
            (
                (
                    "transition",
                    "--action-id",
                    "bad/id",
                    "--expected-revision",
                    "1",
                    "--authorization",
                    "project",
                    "--task-id",
                    "task-a",
                    "--host-id",
                    "host-a",
                    "--payload",
                    "payload.json",
                ),
                "$.action_id",
            ),
            (
                (
                    "dispatch",
                    "--action-id",
                    "bad/id",
                    "--expected-revision",
                    "1",
                    "--task-id",
                    "task-a",
                    "--host-id",
                    "host-a",
                    "--checkpoint",
                    "checkpoint-a",
                    "--environment",
                    "environment.json",
                ),
                "$.action_id",
            ),
        )
        for arguments, field_path in cases:
            with self.subTest(arguments=arguments):
                self.assertIn(field_path, self.run_cli_parse_error(*arguments))

    def test_module_entrypoint_delegates_to_cli(self) -> None:
        with patch.object(sys, "argv", ["pinboard", "--version"]), self.assertRaises(SystemExit) as raised:
            runpy.run_module("pinboard.__main__", run_name="__main__")

        self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
