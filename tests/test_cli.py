"""Installed human-maintenance CLI behavior and exact recovery diagnostics."""

import contextlib
import io
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import persistence as sqlite_persistence
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state, work_brief_models
from pinboard.application.artifacts import NewArtifact
from pinboard.application.work_briefs import (
    canonical_work_brief_bytes,
)
from pinboard.cli import work_state_commands
from pinboard.cli.entrypoint import main
from pinboard.domain import authority_models, work_models
from pinboard.domain.identifiers import HostId, ItemId, LeaseId, TaskId
from tests.artifact_support import write_revision

from .support import (
    SQLITE_NOW,
    JsonObject,
    JsonValue,
    complete_sqlite_state,
    initialize_store,
)
from .work_brief_support import (
    work_a_brief,
)


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

    def run_git(self, cwd: Path, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True)

    def json_list(self, value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            self.fail("JSON value must be a list")
        return value

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def initialized_state(
        self,
        state: stored_state.StoredWorkState | None = None,
        accepted_brief: work_brief_models.WorkBrief | None = None,
    ) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        if state is not None:
            reference = state.artifact_references[0]
            if reference.selector.endswith(".opaque"):
                value = work_a_brief(project) if accepted_brief is None else accepted_brief
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

    def assert_readonly_human_closure(
        self,
        common: tuple[str, ...],
        work: Path,
        store: SQLiteWorkStore,
        permission_work_root: str,
    ) -> None:
        before = store.validated_snapshot()
        original_open_database = sqlite_store.open_database

        def open_query_only_database(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open_database(path, mode)
            if mode == OpenMode.READ_WRITE:
                connection.execute("PRAGMA query_only = ON")
            return connection

        with (
            patch.object(sqlite_persistence, "open_database", side_effect=open_query_only_database),
            patch("pinboard.cli.transitions.datetime") as clock,
        ):
            clock.now.return_value = SQLITE_NOW
            result, stdout, stderr = self.run_cli(
                *common,
                "close",
                "intake-work",
                "--outcome",
                "done",
                "--reason",
                "The accepted intake is complete.",
                "--task-id",
                "project-task",
                "--host-id",
                "studio",
                "--json",
            )

        self.assertEqual(12, result)
        self.assertEqual("", stderr)
        failure = self.json_object(json.loads(stdout))
        self.assertEqual("rejected", failure["status"])
        self.assertEqual("SQLITE_READONLY", failure["code"])
        self.assertFalse(failure["state_changed"])
        self.assertEqual([], failure["changed_surfaces"])
        self.assertEqual("do-not-retry", failure["retry"])
        self.assertEqual(
            {
                "database_path": str(work / "state.sqlite3"),
                "operation": "close",
                "sqlite_error_code": "SQLITE_READONLY",
                "permission_recovery": (
                    "For routine Pinboard commands, select a Codex permission profile extending ':workspace' whose "
                    f"narrow filesystem write rule grants access to '{permission_work_root}', the effective work root "
                    "for this command. A normal checkout uses the relative '.pinboard' rule; a linked worktree "
                    "uses only the resolved absolute shared-repository '.pinboard' directory; an explicit "
                    "'--work-root' uses that exact directory. Remove legacy 'sandbox_mode' and "
                    "'sandbox_workspace_write' settings because they override permission profiles. For fresh default "
                    "initialization, approve the exact 'pinboard init' command once so it can also update "
                    "'.git/info/exclude'; do not grant persistent '.git' access."
                ),
            },
            {
                str(observation["field"]): observation["value"]
                for value in self.json_list(failure["observed"])
                if (observation := self.json_object(value))
            },
        )
        self.assertEqual(before, store.validated_snapshot())

    def test_readonly_mutation_at_default_root_reports_relative_permission_and_unchanged_ledger(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())

        self.assert_readonly_human_closure(("--project-root", str(project)), work, store, ".pinboard")

    def test_readonly_mutation_at_explicit_work_root_reports_exact_permission_and_unchanged_ledger(self) -> None:
        project, default_work, _store = self.initialized_state(complete_sqlite_state())
        work = project / ".codex" / "custom-pinboard"
        work.parent.mkdir()
        default_work.rename(work)

        self.assert_readonly_human_closure(
            ("--project-root", str(project), "--work-root", str(work)),
            work,
            SQLiteWorkStore(work / "state.sqlite3"),
            str(work),
        )

    def test_readonly_mutation_from_linked_worktree_reports_exact_shared_permission(self) -> None:
        repository, work, store = self.initialized_state(complete_sqlite_state())
        linked = repository.parent / f"{repository.name}-linked"
        self.run_git(repository, "init", "-b", "main")
        (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
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
        self.run_git(repository, "worktree", "add", "-b", "linked", str(linked))

        self.assert_readonly_human_closure(("--project-root", str(linked)), work, store, str(work))

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

        with patch("pinboard.cli.work_state_commands.datetime") as clock:
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
            initialized_at, SQLiteWorkStore(work / "state.sqlite3").validated_snapshot().lifecycle.project.updated_at
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
                with patch("pinboard.cli.work_state_commands.datetime") as clock:
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
                self.assertFalse((work / "views" / "queue.md").exists())
                self.assertFalse((work / "views" / "history.md").exists())

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

    def test_initialization_and_view_rebuild_read_only_declared_projection_facts(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        with patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")):
            initialized, _stdout, stderr = self.run_cli(*common, "init")
        self.assertEqual(0, initialized, stderr)

        statements: list[str] = []
        original_open_database = sqlite_store.open_database

        def traced_open_database(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open_database(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("complete snapshot used")),
            patch("pinboard.adapters.sqlite.store.open_database", side_effect=traced_open_database),
        ):
            rebuilt, _stdout, stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuilt, stderr)
        reads = "\n".join(
            statement.lower() for statement in statements if statement.lstrip().lower().startswith("select")
        )
        for excluded in (
            "attempt_lease_generations",
            "preparation_lease_generations",
            "attempt_lease_counters",
            "preparation_lease_counters",
            "work_item_state_counts",
            "proposal_evidence",
            "proposal_freshness",
        ):
            self.assertNotIn(excluded, reads)

    def test_module_entrypoint_delegates_to_cli(self) -> None:
        with patch.object(sys, "argv", ["pinboard", "--version"]), self.assertRaises(SystemExit) as raised:
            runpy.run_module("pinboard.__main__", run_name="__main__")

        self.assertEqual(0, raised.exception.code)

    def test_status_uses_selected_project_facts_without_complete_state(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        with patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("Complete snapshot used")):
            result = self.run_json_cli("--project-root", str(project), "--work-root", str(work), "status")
        self.assertEqual("12", result["revision"])
        self.assertEqual({"active": 1, "ready": 3, "superseded": 1}, result["counts"])

    def test_status_composes_one_store_and_static_commands_compose_none(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        with patch.object(work_state_commands, "compose_store", wraps=work_state_commands.compose_store) as compose:
            result, _stdout, stderr = self.run_cli(*common, "status")
        self.assertEqual(0, result, stderr)
        compose.assert_called_once()
        for arguments in (("tool-contract", "--json"), (*common, "root")):
            with (
                self.subTest(arguments=arguments),
                patch.object(
                    work_state_commands, "compose_store", side_effect=AssertionError("Static command composed a store")
                ),
            ):
                result, _stdout, stderr = self.run_cli(*arguments)
            self.assertEqual(0, result, stderr)
        for arguments in (("--help",), ("--version",)):
            with (
                self.subTest(arguments=arguments),
                patch.object(
                    work_state_commands, "compose_store", side_effect=AssertionError("Static command composed a store")
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main(arguments)
            self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
