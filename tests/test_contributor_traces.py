"""Contributor trace opt-in at the normal launcher and MCP boundaries."""

import asyncio
import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import MutableMapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pinboard.adapters.files import contributor_traces, git_config
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.setting_resolution import SettingResolutionError
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.cli import entrypoint
from pinboard.mcp import common, execution, server
from pinboard.mcp.contracts import JsonValue
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store

ROOT = Path(__file__).resolve().parent.parent


class ContributorTraceTest(unittest.TestCase):
    def preflight_result(self, project: Path, work_root: Path) -> dict[str, JsonValue]:
        def forbidden_callback(_token: execution.CancellationToken) -> execution.OperationResult:
            raise AssertionError("The target callback ran after trace preflight failed.")

        async def run() -> dict[str, JsonValue]:
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            try:
                return await execution._run_request(
                    executor,
                    execution.Diagnostics(io.StringIO(), event_limit=10, line_limit=300),
                    1,
                    server.ITEM_STATUS_TOOL,
                    str(project),
                    forbidden_callback,
                    arguments={
                        "request": {
                            "project_root": str(project),
                            "work_root": str(work_root),
                            "operation": "item",
                            "item_id": "one",
                        }
                    },
                    capture=execution.AutomaticCapture(common.select_capture_item),
                )
            finally:
                executor.shutdown()

        return asyncio.run(run())

    def project(self, temporary: Path) -> tuple[Path, Path]:
        primary = temporary / "project"
        primary.mkdir()
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        (primary / ".pinboard").mkdir()
        with (primary / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
            exclude.write("/.pinboard/\n")
        subprocess.run(
            [
                "git",
                "-C",
                str(primary),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "base",
            ],
            check=True,
        )
        worktree = temporary / "worktree"
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", str(worktree)], check=True)
        return primary, worktree

    def settings(self, project: Path, project_mode: str, overrides: dict[str, str]) -> None:
        path = project / ".pinboard" / contributor_traces.SETTINGS_NAME
        path.write_text(
            '[pinboard "unsafe_persist_exact_pinboard_traces"]\n'
            f"\tmode = {project_mode}\n"
            + "".join(f'[item "{item}"]\n\tmode = {mode}\n' for item, mode in overrides.items()),
            encoding="utf-8",
        )

    def choose(self, project: Path, item_id: str | None) -> Path | None:
        state = contributor_traces.read_project_trace_settings(project, None)
        assert state is not None
        return contributor_traces.automatic_trace_directory(state[0], state[1].value, item_id)

    def test_cli_selection_uses_current_mode_and_prunes_automatic_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            self.choose(primary, None)
            self.settings(primary, "off", {"one": "on"})
            arguments = ("--project-root", str(worktree), "--work-root", str(primary / ".pinboard"), "close", "one")
            selected = contributor_traces.select_cli_trace(arguments)
            assert selected is not None
            self.assertEqual((primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY).resolve(), selected.parent)
            self.assertIsNone(contributor_traces.select_cli_trace(("--project-root", str(worktree), "root")))
            self.assertIsNone(contributor_traces.select_cli_trace(("--project-root", str(worktree), "close")))
            self.assertIsNone(contributor_traces.select_cli_trace(("--project-root", str(worktree), "close", "two")))
            self.settings(primary, "on", {"one": "inherit"})
            with patch.object(Path, "cwd", return_value=worktree):
                self.assertIsNotNone(contributor_traces.select_cli_trace(("root",)))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0, entrypoint.main(("--contributor-capture-select", f"--project-root={worktree}", "root"))
                )
            self.assertTrue(output.getvalue().strip().startswith(str(selected.parent)))
            for index in range(contributor_traces.TRACE_LIMIT + 4):
                (selected.parent / f"pinboard-auto-cli-{index:04}.json").write_bytes(b"x")
            self.assertEqual(
                0,
                entrypoint.main(("--contributor-capture-prune", str(selected))),
            )
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(selected.parent.glob("pinboard-auto-*.json"))))
            self.settings(primary, "off", {})
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0, entrypoint.main(("--contributor-capture-select", "--project-root", str(primary), "root"))
                )
            self.assertEqual("off", output.getvalue().strip())
            contributor_traces.prune_cli_traces(selected)
            with patch.object(Path, "cwd", return_value=Path(temporary)):
                self.assertIsNone(contributor_traces.select_cli_trace(()))

    def test_uninitialized_unignored_and_malformed_local_settings_are_not_silent_opt_ins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertIsNone(contributor_traces.read_project_trace_settings(directory, None))
            project = directory / "unignored"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            self.assertIsNone(contributor_traces.read_project_trace_settings(project, None))
            (project / ".pinboard").mkdir()
            self.assertIsNone(contributor_traces.read_project_trace_settings(project, None))
            settings = project / ".pinboard" / contributor_traces.SETTINGS_NAME
            settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n')
            with self.assertRaisesRegex(ValueError, "Git-ignored"):
                contributor_traces.read_project_trace_settings(project, None)
            primary, _ = self.project(directory)
            self.choose(primary, None)
            settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            settings.write_text("{}")
            with self.assertRaisesRegex(ValueError, "invalid or unreadable"):
                contributor_traces.read_project_trace_settings(primary, None)
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(
                    64, entrypoint.main(("--contributor-capture-select", "--project-root", str(primary), "root"))
                )
            self.assertIn("before Pinboard ran", error.getvalue())
            settings.unlink()
            settings.symlink_to(project / ".pinboard" / contributor_traces.SETTINGS_NAME)
            with self.assertRaisesRegex(ValueError, "regular file"):
                contributor_traces.read_project_trace_settings(primary, None)

    def test_ignored_setting_alone_cannot_enable_git_visible_traces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            (primary / ".git" / "info" / "exclude").write_text("/.pinboard/contributor-traces.config\n")
            self.settings(primary, "on", {})
            result = subprocess.run(
                [str(ROOT / "scripts" / "pinboard"), "--project-root", str(primary), "root"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(64, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertFalse((primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY).exists())

    def test_first_use_race_accepts_only_an_already_published_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            setting_path = primary / ".pinboard" / contributor_traces.SETTINGS_NAME

            def concurrent_writer(path: Path, _content: bytes) -> None:
                path.write_bytes(b'[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n')
                raise FileIOError(FileIOErrorCode.FILE_ALREADY_EXISTS, "created by another caller")

            with patch.object(contributor_traces, "create_immutable", side_effect=concurrent_writer):
                state = contributor_traces.read_project_trace_settings(primary, None)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("on", state[1].value.unsafe_persist_exact_pinboard_traces)
            setting_path.unlink()
            with (
                patch.object(
                    contributor_traces,
                    "create_immutable",
                    side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "publication failed"),
                ),
                self.assertRaises(SettingResolutionError),
            ):
                contributor_traces.read_project_trace_settings(primary, None)

    def test_missing_project_mode_preserves_item_override_in_both_readers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            original = '[item "one"]\n\tmode = on\n'
            settings.write_text(original)
            state = contributor_traces.read_project_trace_settings(primary, None)
            assert state is not None
            self.assertEqual("off", state[1].value.unsafe_persist_exact_pinboard_traces)
            self.assertEqual({"one": "on"}, state[1].value.item_overrides)
            self.assertNotIsInstance(state[1].value.item_overrides, MutableMapping)
            self.assertEqual(("none", "acknowledged"), (state[1].effects.file_creation, state[1].effects.key_write))
            self.assertIn(original, settings.read_text())
            settings.write_text(original)

            launcher_root = Path(temporary) / "unprepared-plugin"
            (launcher_root / "scripts").mkdir(parents=True)
            launcher = launcher_root / "scripts" / "pinboard"
            launcher.write_bytes((ROOT / "scripts" / "pinboard").read_bytes())
            launcher.chmod(0o755)
            result = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "close", "one"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(78, result.returncode)
            self.assertEqual(
                1,
                len(
                    tuple((primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY).glob("pinboard-auto-cli-*.json"))
                ),
            )
            state = contributor_traces.read_project_trace_settings(primary, None)
            assert state is not None
            self.assertEqual("off", state[1].value.unsafe_persist_exact_pinboard_traces)
            self.assertEqual({"one": "on"}, state[1].value.item_overrides)
            self.assertEqual(("none", "none"), (state[1].effects.file_creation, state[1].effects.key_write))

            invalid = "[unknown]\n\tmode = on\n" + original
            settings.write_text(invalid)
            with self.assertRaisesRegex(ValueError, "unknown key"):
                contributor_traces.read_project_trace_settings(primary, None)
            rejected = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "close", "one"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(64, rejected.returncode)
            self.assertEqual(invalid, settings.read_text())

    def test_explicit_off_and_item_overrides_follow_two_worktrees_and_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            initial = contributor_traces.read_project_trace_settings(primary, None)
            assert initial is not None
            self.assertEqual((work_root / contributor_traces.SETTINGS_NAME).resolve(), initial[1].path)
            self.assertEqual("none", initial[1].effects.parent_creation)
            self.assertEqual(
                ("confirmed", "acknowledged"), (initial[1].effects.file_creation, initial[1].effects.key_write)
            )
            self.assertIsNone(self.choose(primary, "one"))
            settings = work_root / contributor_traces.SETTINGS_NAME
            self.assertEqual(
                '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n',
                settings.read_text(encoding="utf-8"),
            )
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))
            self.assertEqual(
                0,
                subprocess.run(
                    ["git", "-C", str(primary), "check-ignore", "-q", "--", ".pinboard/contributor-traces.config"],
                    check=False,
                ).returncode,
            )
            self.settings(primary, "on", {"one": "inherit", "two": "off"})
            self.assertIsNotNone(self.choose(primary, "one"))
            self.assertIsNone(self.choose(worktree, "two"))
            self.assertIsNotNone(self.choose(worktree, "three"))
            self.settings(primary, "off", {"one": "on", "two": "inherit"})
            directory = self.choose(worktree, "one")
            self.assertIsNotNone(directory)
            assert directory is not None
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            self.assertIsNone(self.choose(primary, "two"))
            self.assertIsNone(self.choose(primary, None))
            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "pinboard"),
                    "--project-root",
                    str(worktree),
                    "close",
                    "one",
                    "--outcome",
                    "cancelled",
                    "--reason",
                    "test",
                    "--task-id",
                    "test",
                    "--host-id",
                    "test",
                ],
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(1, len(tuple(directory.glob("pinboard-auto-cli-*.json"))))

    def test_first_use_failure_preserves_file_and_key_write_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            path = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            with (
                patch.object(
                    git_config,
                    "add",
                    return_value=git_config.WriteUnconfirmed(
                        path.resolve(), "pinboard.unsafe_persist_exact_pinboard_traces.mode", "write failed"
                    ),
                ),
                self.assertRaises(SettingResolutionError) as failed,
            ):
                contributor_traces.read_project_trace_settings(primary, None)
            self.assertEqual(path.resolve(), failed.exception.path)
            self.assertEqual(
                ("confirmed", "unconfirmed"),
                (failed.exception.effects.file_creation, failed.exception.effects.key_write),
            )
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertFalse(hasattr(failed.exception, "value"))

            path.unlink()
            with (
                patch.object(
                    git_config,
                    "list_entries",
                    side_effect=[
                        git_config.Entries(path.resolve(), ()),
                        git_config.ReadFailed(path.resolve(), "list-entries", None, "reread failed"),
                    ],
                ),
                self.assertRaises(SettingResolutionError) as failed_reread,
            ):
                contributor_traces.read_project_trace_settings(primary, None)
            self.assertIn("mode = off", path.read_text())
            self.assertEqual(
                ("confirmed", "acknowledged"),
                (failed_reread.exception.effects.file_creation, failed_reread.exception.effects.key_write),
            )
            self.assertIn("reread failed", str(failed_reread.exception))

    def test_normal_cli_and_mcp_capture_exact_values_only_when_on(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            args = ("--project-root", str(primary), "root")
            launcher = ROOT / "scripts" / "pinboard"
            work_root = primary / ".pinboard"
            initialize_database(resolve_durable_roots(primary, work_root), SQLITE_NOW)
            initialize_store(SQLiteWorkStore(work_root / "state.sqlite3"), complete_sqlite_state())
            off = subprocess.run([str(launcher), *args], capture_output=True, check=False)
            self.assertEqual(0, off.returncode)
            traces = primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY
            self.assertFalse(traces.exists())

            async def mcp_call() -> dict[str, object]:
                parameters = StdioServerParameters(command=str(launcher), args=("--mcp",), cwd=ROOT)
                async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        server.ITEM_STATUS_TOOL,
                        {
                            "request": {
                                "project_root": str(worktree),
                                "work_root": str(primary / ".pinboard"),
                                "operation": "item",
                                "item_id": "missing",
                            }
                        },
                    )
                    self.assertFalse(result.is_error)
                    assert isinstance(result.structured_content, dict)
                    self.assertEqual("ITEM_NOT_FOUND", result.structured_content.get("code"))
                    return result.structured_content

            off_mcp_result = asyncio.run(mcp_call())
            self.assertFalse(traces.exists())
            self.settings(primary, "on", {})
            on = subprocess.run([str(launcher), *args], capture_output=True, check=False)
            self.assertEqual(off.stdout, on.stdout)
            self.assertEqual(off.stderr, on.stderr)
            [cli_trace] = tuple(traces.glob("pinboard-auto-cli-*.json"))
            record = json.loads(cli_trace.read_bytes())
            self.assertEqual(0o600, stat.S_IMODE(cli_trace.stat().st_mode))
            self.assertEqual(
                [str(launcher), *args], [bytes.fromhex(value["data"]).decode() for value in record["argv"]]
            )
            self.assertEqual(on.stdout, bytes.fromhex(record["stdout"]["data"]))
            self.assertEqual(on.stderr, bytes.fromhex(record["stderr"]["data"]))
            self.assertEqual(hashlib.sha256(on.stdout).hexdigest(), record["stdout"]["sha256"])
            self.assertEqual("not-captured", record["environment"])

            mcp_result = asyncio.run(mcp_call())
            self.assertEqual(off_mcp_result, mcp_result)
            [mcp_trace] = tuple(traces.glob("pinboard-auto-mcp-*.json"))
            semantic = json.loads(mcp_trace.read_bytes())
            self.assertEqual(0o700, stat.S_IMODE(traces.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(mcp_trace.stat().st_mode))
            self.assertEqual(mcp_result, semantic["result"]["value"])
            self.assertEqual("unavailable", semantic["transport_bytes"])
            self.assertEqual("unavailable", semantic["pre_callback_events"])

            self.settings(primary, "off", {})
            subprocess.run([str(launcher), *args], capture_output=True, check=False)
            asyncio.run(mcp_call())
            self.assertEqual((cli_trace,), tuple(traces.glob("pinboard-auto-cli-*.json")))
            self.assertEqual((mcp_trace,), tuple(traces.glob("pinboard-auto-mcp-*.json")))

    def test_prepared_cli_capture_retains_traces_with_explicit_work_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = Path(temporary) / "selected-work-root"
            work_root.mkdir()
            initialize_database(resolve_durable_roots(primary, work_root), SQLITE_NOW)
            initialize_store(SQLiteWorkStore(work_root / "state.sqlite3"), complete_sqlite_state())
            settings = work_root / contributor_traces.SETTINGS_NAME
            settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n')
            command = [
                str(ROOT / "scripts" / "pinboard"),
                "--project-root",
                str(worktree),
                "--work-root",
                str(work_root),
                "root",
            ]
            off = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(0, off.returncode)
            settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n')
            directory = work_root / contributor_traces.TRACE_DIRECTORY
            directory.mkdir(mode=0o700)
            for index in range(contributor_traces.TRACE_LIMIT + 3):
                (directory / f"pinboard-auto-cli-old-{index:04}.json").write_bytes(b"x")
            on = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(0, on.returncode)
            self.assertEqual(off.stdout, on.stdout)
            self.assertEqual(off.stderr, on.stderr)
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(directory.glob("pinboard-auto-*.json"))))
            [published] = tuple(directory.glob("pinboard-auto-cli-[0-9a-f]*.json"))
            record = json.loads(published.read_bytes())
            self.assertEqual(on.stdout, bytes.fromhex(record["stdout"]["data"]))

    def test_cli_prune_failure_reports_post_target_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "invocation-traces" / "pinboard-auto-cli-example.json"
            error = io.StringIO()
            with (
                patch.object(contributor_traces, "prune_cli_traces", side_effect=OSError("retention denied")),
                redirect_stderr(error),
            ):
                self.assertEqual(64, entrypoint.main(("--contributor-capture-prune", str(selected))))
            self.assertIn("retention cleanup failed after Pinboard ran", error.getvalue())

    def test_attempt_selector_uses_its_saved_item_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = Path(temporary) / "selected-work-root"
            work_root.mkdir()
            database = work_root / "state.sqlite3"
            initialize_database(resolve_durable_roots(primary, work_root), SQLITE_NOW)
            initialize_store(SQLiteWorkStore(database), complete_sqlite_state())
            (work_root / contributor_traces.SETTINGS_NAME).write_text(
                '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = off\n[item "work-a"]\n\tmode = on\n',
                encoding="utf-8",
            )
            capture = execution.AutomaticCapture(common.select_capture_item).resolve(
                str(worktree),
                {"request": {"project_root": str(worktree), "work_root": str(work_root), "attempt_id": "work-a-1"}},
            )
            self.assertIsNotNone(capture)

    def test_existing_shared_work_root_needs_no_parent_write_for_mcp_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            mkdir = Path.mkdir

            def deny_parent_write(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
                if path.resolve() == work_root.resolve():
                    raise PermissionError("The shared repository parent is read-only.")
                mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

            capture = execution.AutomaticCapture(common.select_capture_item)
            for mode in ("off", "on"):
                with self.subTest(mode=mode), patch.object(Path, "mkdir", deny_parent_write):
                    self.settings(primary, mode, {})
                    selected = capture.resolve(str(worktree), {"work_root": str(work_root), "item_id": "missing"})
                    self.assertEqual(mode == "on", selected is not None)

    def test_explicit_work_root_never_writes_shared_trace_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            selected_root = Path(temporary) / "selected-work-root"
            selected_root.mkdir()
            arguments: dict[str, JsonValue] = {
                "request": {
                    "project_root": str(worktree),
                    "work_root": str(selected_root),
                    "operation": "item",
                    "item_id": "missing",
                }
            }
            called = 0

            def callback(_token: execution.CancellationToken) -> execution.OperationResult:
                nonlocal called
                called += 1
                return execution.OperationResult({"ok": True}, "ok", None)

            def identity_result(_operation: str, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
                return value

            original_create = contributor_traces.create_immutable

            def deny_shared(path: Path, content: bytes) -> bool:
                if path.is_relative_to(primary / ".pinboard"):
                    raise PermissionError("shared work root is read-only")
                return original_create(path, content)

            async def run() -> dict[str, JsonValue]:
                executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
                try:
                    return await execution._run_request(
                        executor,
                        execution.Diagnostics(io.StringIO(), event_limit=10, line_limit=300),
                        1,
                        server.ITEM_STATUS_TOOL,
                        str(worktree),
                        callback,
                        arguments=arguments,
                        capture=execution.AutomaticCapture(common.select_capture_item),
                    )
                finally:
                    executor.shutdown()

            with (
                patch.object(contributor_traces, "create_immutable", side_effect=deny_shared),
                patch.object(execution.contract_schemas, "validate_result", side_effect=identity_result),
            ):
                self.assertEqual({"ok": True}, asyncio.run(run()))
                self.assertEqual(1, called)

                self.assertFalse((primary / ".pinboard" / contributor_traces.SETTINGS_NAME).exists())
                selected_settings = selected_root / contributor_traces.SETTINGS_NAME
                self.assertIn("mode = off", selected_settings.read_text())
                selected_settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n')
                trace_directory = selected_root / contributor_traces.TRACE_DIRECTORY
                trace_directory.mkdir(mode=0o755)
                rejected = asyncio.run(run())
                self.assertEqual("TRACE_PREFLIGHT_FAILED", rejected["code"])
                self.assertEqual(str(trace_directory), rejected["resource"])
                self.assertEqual(False, rejected["target_ran"])
                self.assertIn("0700", str(rejected["repair"]))
                self.assertEqual("unchanged", rejected["effect"])
                self.assertEqual(1, called)

    def test_preflight_reports_settings_created_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            with patch.object(
                contributor_traces, "_decode_settings", side_effect=[None, ValueError("invalid setting")]
            ):
                result = self.preflight_result(worktree, work_root)
            self.assertEqual("TRACE_PREFLIGHT_FAILED", result["code"])
            self.assertEqual(str((work_root / contributor_traces.SETTINGS_NAME).resolve()), result["resource"])
            self.assertEqual("committed", result["effect"])
            self.assertEqual(["work-root"], result["changed_surfaces"])
            self.assertEqual("confirmed", result["settings_file_creation"])
            self.assertEqual("acknowledged", result["settings_mode_write"])
            self.assertEqual(False, result["target_ran"])

    def test_preflight_reports_directory_created_before_capture_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            self.settings(primary, "on", {})
            with patch.object(execution, "SemanticCapture", side_effect=ValueError("capture probe denied")):
                result = self.preflight_result(worktree, work_root)
            self.assertEqual("TRACE_PREFLIGHT_FAILED", result["code"])
            self.assertEqual(str(work_root / contributor_traces.TRACE_DIRECTORY), result["resource"])
            self.assertEqual("unconfirmed", result["effect"])
            self.assertEqual("confirmed", result["trace_directory_creation"])
            self.assertEqual("unconfirmed", result["capture_probe_effect"])
            self.assertEqual(False, result["target_ran"])

    def test_preflight_keeps_confirmed_creation_and_uncertain_mode_write_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            setting = work_root / contributor_traces.SETTINGS_NAME
            with patch.object(
                git_config,
                "add",
                return_value=git_config.WriteUnconfirmed(
                    setting, "pinboard.unsafe_persist_exact_pinboard_traces.mode", "write unconfirmed"
                ),
            ):
                result = self.preflight_result(worktree, work_root)
            self.assertTrue(setting.exists())
            self.assertEqual("unconfirmed", result["effect"])
            self.assertIsNone(result["state_changed"])
            self.assertEqual(["work-root"], result["changed_surfaces"])
            self.assertEqual("confirmed", result["settings_file_creation"])
            self.assertEqual("unconfirmed", result["settings_mode_write"])
            self.assertEqual(False, result["target_ran"])

    def test_preflight_names_malformed_settings_and_symlinked_work_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            setting = work_root / contributor_traces.SETTINGS_NAME
            setting.write_text("[pinboard]\n\tbroken = yes\n", encoding="utf-8")
            malformed = self.preflight_result(worktree, work_root)
            self.assertEqual(str(setting.resolve()), malformed["resource"])
            self.assertEqual("unchanged", malformed["effect"])
            self.assertEqual(False, malformed["target_ran"])
            link = Path(temporary) / "linked-work-root"
            link.symlink_to(work_root, target_is_directory=True)
            rejected = self.preflight_result(worktree, link)
            self.assertEqual(str(link), rejected["resource"])
            self.assertEqual("unchanged", rejected["effect"])
            self.assertEqual(False, rejected["target_ran"])
            self.assertIn(str(work_root.resolve()), str(rejected["repair"]))
            self.assertIn("work_root", str(rejected["repair"]))
            loop = Path(temporary) / "looped-work-root"
            loop.symlink_to(loop, target_is_directory=True)
            looped = self.preflight_result(worktree, loop)
            self.assertEqual("TRACE_PREFLIGHT_FAILED", looped["code"])
            self.assertIn("existing real directory", str(looped["repair"]))

    def test_preflight_repairs_unverifiable_git_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, worktree = self.project(Path(temporary))
            work_root = Path(temporary) / "selected-work-root"
            work_root.mkdir()
            (work_root / ".git").write_text("gitdir: /missing\n", encoding="utf-8")
            rejected = self.preflight_result(worktree, work_root)
            self.assertEqual(str(work_root / contributor_traces.SETTINGS_NAME), rejected["resource"])
            self.assertEqual("unchanged", rejected["effect"])
            self.assertEqual(False, rejected["target_ran"])
            self.assertIn(str(work_root / ".git"), str(rejected["repair"]))
            self.assertIn("git -C", str(rejected["repair"]))

    def test_unprepared_launcher_uses_explicit_trace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            selected_root = Path(temporary) / "selected-work-root"
            selected_root.mkdir()
            launcher_root = Path(temporary) / "unprepared-plugin"
            (launcher_root / "scripts").mkdir(parents=True)
            launcher = launcher_root / "scripts" / "pinboard"
            launcher.write_bytes((ROOT / "scripts" / "pinboard").read_bytes())
            launcher.chmod(0o755)
            command = [str(launcher), "--project-root", str(worktree), "--work-root", str(selected_root), "root"]
            off = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(78, off.returncode)
            self.assertIn(b"runtime-preparation-required", off.stdout)
            self.assertFalse((primary / ".pinboard" / contributor_traces.SETTINGS_NAME).exists())
            selected_settings = selected_root / contributor_traces.SETTINGS_NAME
            self.assertIn("mode = off", selected_settings.read_text())
            selected_settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n')
            trace_directory = selected_root / contributor_traces.TRACE_DIRECTORY
            trace_directory.mkdir(mode=0o755)
            on = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(64, on.returncode)
            self.assertEqual(b"", on.stdout)
            self.assertIn(str(trace_directory).encode(), on.stderr)
            self.assertIn(b"target did not run", on.stderr)
            trace_directory.chmod(0o700)
            recovered = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(78, recovered.returncode)
            self.assertIn(b"runtime-preparation-required", recovered.stdout)
            self.assertEqual(1, len(tuple(trace_directory.glob("pinboard-auto-cli-*.json"))))

            subprocess.run(["git", "init", "-q", temporary], check=True)
            (selected_root / ".git").write_text("gitdir: /missing\n")
            visible = subprocess.run(
                ["git", "-C", temporary, "status", "--short", "--untracked-files=all"],
                capture_output=True,
                check=True,
            )
            self.assertIn(b"selected-work-root/contributor-traces.config", visible.stdout)
            self.assertIn(b"selected-work-root/invocation-traces/", visible.stdout)
            rejected = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(64, rejected.returncode)
            self.assertEqual(b"", rejected.stdout)
            self.assertIn(str(selected_settings).encode(), rejected.stderr)
            self.assertIn(b"Git status could not be verified", rejected.stderr)
            self.assertIn(b"target did not run", rejected.stderr)
            self.assertEqual(1, len(tuple(trace_directory.glob("pinboard-auto-cli-*.json"))))
            (selected_root / ".git").unlink()
            shutil.rmtree(Path(temporary) / ".git")
            restored = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(78, restored.returncode)
            self.assertEqual(2, len(tuple(trace_directory.glob("pinboard-auto-cli-*.json"))))

    def test_mcp_item_attribution_covers_supported_request_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            database = work_root / "state.sqlite3"
            initialize_database(resolve_durable_roots(primary, work_root), SQLITE_NOW)
            initialize_store(SQLiteWorkStore(database), complete_sqlite_state())
            self.choose(primary, None)
            self.settings(primary, "off", {"work-a": "on"})
            examples: tuple[dict[str, JsonValue], ...] = (
                {"item_id": "work-a"},
                {"request": {"operation": "item", "item_id": "work-a"}},
                {"request": {"operation": "integration", "item_id": "work-a", "target": "HEAD"}},
                {"brief": {"item_id": "work-a"}},
                {"proposal": {"relation": {"item": "work-a"}}},
                {"request": {"attempt_id": "work-a-1"}},
                {"review": {"attempt_id": "work-a-1"}},
                {"request": {"action_id": {"subject": "work-a-1"}}},
                {"request": {"receipt": {"action_id": {"subject": "work-a-1"}}}},
                {"dispatch": {"receipt": {"action_id": {"subject": "work-a-1"}}}},
            )
            for arguments in examples:
                with self.subTest(arguments=arguments):
                    self.assertEqual("work-a", common.select_capture_item(primary, str(work_root), arguments))
            self.assertEqual(
                "unmapped", common.select_capture_item(primary, None, {"action_id": {"subject": "unmapped"}})
            )
            capture = execution.AutomaticCapture(common.select_capture_item)
            self.assertIsNone(capture.resolve(str(worktree), {"item_id": "work-a"}))
            self.assertIsNone(capture.resolve(str(worktree), {"work_root": str(work_root), "item_id": "another"}))
            self.assertIsNone(capture.resolve(str(Path(temporary)), {"work_root": str(work_root), "item_id": "work-a"}))
            self.assertIsNotNone(capture.resolve(str(worktree), {"work_root": str(work_root), "item_id": "work-a"}))
            item_leaf: dict[str, JsonValue] = {
                "request": {"work_root": str(work_root), "operation": "item", "item_id": "work-a"}
            }
            integration_leaf: dict[str, JsonValue] = {
                "request": {
                    "work_root": str(work_root),
                    "operation": "integration",
                    "item_id": "work-a",
                    "target": "HEAD",
                }
            }
            branch_leaf: dict[str, JsonValue] = {
                "request": {"work_root": str(work_root), "operation": "branch", "branch": "codex/work-a"}
            }
            self.assertIsNone(common.select_capture_item(primary, str(work_root), branch_leaf))
            self.assertIsNotNone(capture.resolve(str(worktree), item_leaf))
            self.assertIsNotNone(capture.resolve(str(worktree), integration_leaf))
            self.assertIsNone(capture.resolve(str(worktree), branch_leaf))
            self.settings(primary, "off", {"work-a": "invalid"})
            rejected = capture.resolve(str(worktree), {"work_root": str(work_root), "item_id": "work-a"})
            self.assertIsInstance(rejected, execution.OperationResult)
            self.assertEqual("TRACE_PREFLIGHT_FAILED", rejected.content["code"])

    def test_mcp_startup_uses_automatic_mode_unless_manual_capture_was_declared(self) -> None:
        async def no_transport() -> None:
            return None

        with tempfile.TemporaryDirectory() as temporary:
            captures: list[execution.SemanticCapture | execution.AutomaticCapture | None] = []

            def create_server(
                _executor: execution.BoundedExecutor,
                _diagnostics: execution.Diagnostics,
                capture: execution.SemanticCapture | execution.AutomaticCapture | None,
            ) -> SimpleNamespace:
                captures.append(capture)
                return SimpleNamespace(run_stdio_async=no_transport)

            with patch.object(server, "create_server", side_effect=create_server):
                with patch.object(sys, "argv", ["pinboard-mcp"]):
                    server.main()
                self.assertIsInstance(captures[-1], execution.AutomaticCapture)
                with patch.object(
                    sys,
                    "argv",
                    ["pinboard-mcp", "--capture-evidence-dir", temporary, "--safe-to-persist-exactly"],
                ):
                    server.main()
                self.assertIsInstance(captures[-1], execution.SemanticCapture)
                with (
                    patch.object(sys, "argv", ["pinboard-mcp", "--capture-evidence-dir"]),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as rejected,
                ):
                    server.main()
                self.assertEqual(64, rejected.exception.code)

    def test_automatic_mcp_trace_retention_warning_preserves_published_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture = execution.SemanticCapture(directory, automatic=True)
            with (
                patch.object(contributor_traces, "prune_traces", side_effect=OSError("retention unavailable")),
                self.assertRaises(ImmutableFilePublishedError) as published,
            ):
                capture.available(
                    "pinboard_item_status", {"request": {"operation": "item", "item_id": "one"}}, {"status": "ok"}
                )
            self.assertEqual(FileIOErrorCode.FILE_PUBLISH_FAILED, published.exception.code)
            self.assertTrue(published.exception.path.is_file())
            self.assertEqual(1, len(tuple(directory.glob("pinboard-auto-mcp-*.json"))))

    def test_mcp_capture_preflight_rejects_missing_unwritable_and_unsynced_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                execution.SemanticCapture(directory / "missing")
            target = directory / "file"
            target.write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "existing directory"):
                execution.SemanticCapture(target)
            with (
                patch.object(
                    execution,
                    "create_immutable",
                    side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "unwritable"),
                ),
                self.assertRaisesRegex(ValueError, "not writable"),
            ):
                execution.SemanticCapture(directory)

            def published_then_sync_failed(path: Path, _content: bytes) -> None:
                path.write_bytes(b"")
                raise ImmutableFilePublishedError(
                    path, FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "sync failed")
                )

            with (
                patch.object(execution, "create_immutable", side_effect=published_then_sync_failed),
                self.assertRaisesRegex(ValueError, "could not be synchronized"),
            ):
                execution.SemanticCapture(directory)
            self.assertEqual((), tuple(directory.glob(".pinboard-capture-probe-*")))
            original_unlink = Path.unlink

            def blocked_probe_cleanup(path: Path, *, missing_ok: bool = False) -> None:
                if path.name.startswith(".pinboard-capture-probe-"):
                    raise OSError("probe cleanup unavailable")
                original_unlink(path, missing_ok=missing_ok)

            with (
                patch.object(Path, "unlink", blocked_probe_cleanup),
                self.assertRaisesRegex(ValueError, "preflight could not be removed"),
            ):
                execution.SemanticCapture(directory)

    def test_manual_capture_stays_single_when_project_mode_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            self.choose(primary, None)
            self.settings(primary, "on", {})
            manual = primary / ".pinboard" / "manual.json"
            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "pinboard"),
                    "--capture-evidence",
                    str(manual),
                    "--safe-to-persist-exactly",
                    "--",
                    "--project-root",
                    str(primary),
                    "root",
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode)
            self.assertTrue(manual.is_file())
            self.assertFalse((primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY).exists())

    def test_invalid_settings_reject_before_cli_target_and_retention_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            self.choose(primary, None)
            settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            settings.write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [str(ROOT / "scripts" / "pinboard"), "--project-root", str(primary), "root"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(64, result.returncode)
            self.assertEqual(b"", result.stdout)
            self.assertIn(b"before Pinboard ran", result.stderr)
            self.settings(primary, "on", {})
            directory = self.choose(primary, None)
            assert directory is not None
            for index in range(contributor_traces.TRACE_LIMIT + 5):
                (directory / f"pinboard-auto-cli-{index:04}.json").write_bytes(b"x")
            contributor_traces.prune_traces(directory)
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(directory.glob("pinboard-auto-*.json"))))

    def test_unprepared_cli_capture_respects_project_and_item_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            launcher_root = Path(temporary) / "unprepared-plugin"
            (launcher_root / "scripts").mkdir(parents=True)
            launcher = launcher_root / "scripts" / "pinboard"
            launcher.write_bytes((ROOT / "scripts" / "pinboard").read_bytes())
            launcher.chmod(0o755)
            directory = primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY

            missing = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "root"], capture_output=True, check=False
            )
            self.assertEqual(78, missing.returncode)
            first_settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            self.assertIn("mode = off", first_settings.read_text())
            self.assertEqual(0o600, stat.S_IMODE(first_settings.stat().st_mode))
            self.assertFalse(directory.exists())

            self.settings(primary, "off", {})
            off = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "root"], capture_output=True, check=False
            )
            self.assertEqual(78, off.returncode)
            self.assertFalse(directory.exists())

            self.settings(primary, "on", {"one": "off"})
            on = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "root"], capture_output=True, check=False
            )
            self.assertEqual(78, on.returncode)
            traces = tuple(directory.glob("pinboard-auto-cli-*.json"))
            self.assertEqual(1, len(traces))
            record = json.loads(traces[0].read_bytes())
            self.assertEqual(on.stdout, bytes.fromhex(record["stdout"]["data"]))
            self.assertEqual(78, record["exit_status"])

            item_off = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "close", "one"], capture_output=True, check=False
            )
            self.assertEqual(78, item_off.returncode)
            self.assertEqual(1, len(tuple(directory.glob("pinboard-auto-cli-*.json"))))

            self.settings(primary, "off", {"one": "on"})
            item_on = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "close", "one"], capture_output=True, check=False
            )
            self.assertEqual(78, item_on.returncode)
            self.assertEqual(2, len(tuple(directory.glob("pinboard-auto-cli-*.json"))))

            for index in range(contributor_traces.TRACE_LIMIT + 3):
                (directory / f"pinboard-auto-cli-old-{index:04}.json").write_bytes(b"x")
            retained = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "close", "one"], capture_output=True, check=False
            )
            self.assertEqual(78, retained.returncode)
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(directory.glob("pinboard-auto-cli-*.json"))))

            settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            settings.write_text(
                '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = "off\\nitem.one.mode=on"\n',
                encoding="utf-8",
            )
            malformed = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "close", "one"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(64, malformed.returncode)
            self.assertEqual(b"", malformed.stdout)
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(directory.glob("pinboard-auto-*.json"))))

            settings.write_text(
                '[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n[unknown]\n\tmode = on\n'
            )
            invalid = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "root"], capture_output=True, check=False
            )
            self.assertEqual(64, invalid.returncode)
            self.assertEqual(b"", invalid.stdout)
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(directory.glob("pinboard-auto-cli-*.json"))))

    def test_unprepared_cli_retention_counts_existing_mcp_traces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            launcher_root = Path(temporary) / "unprepared-plugin"
            (launcher_root / "scripts").mkdir(parents=True)
            launcher = launcher_root / "scripts" / "pinboard"
            launcher.write_bytes((ROOT / "scripts" / "pinboard").read_bytes())
            launcher.chmod(0o755)
            self.settings(primary, "on", {})
            directory = primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY
            directory.mkdir(mode=0o700)
            for index in range(contributor_traces.TRACE_LIMIT):
                trace = directory / f"pinboard-auto-mcp-{index:04}.json"
                trace.write_bytes(b"x")
                trace.touch()
            result = subprocess.run(
                [str(launcher), "--project-root", str(worktree), "root"], capture_output=True, check=False
            )
            self.assertEqual(78, result.returncode)
            self.assertEqual(contributor_traces.TRACE_LIMIT, len(tuple(directory.glob("pinboard-auto-*.json"))))
            self.assertEqual(1, len(tuple(directory.glob("pinboard-auto-cli-*.json"))))

    def test_pruning_uses_selected_destination_after_mode_turns_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            self.settings(primary, "on", {})
            selected = contributor_traces.select_cli_trace(("--project-root", str(primary), "root"))
            assert selected is not None
            for index in range(contributor_traces.TRACE_LIMIT + 1):
                (selected.parent / f"pinboard-auto-cli-{index:04}.json").write_bytes(b"x")
            self.settings(primary, "off", {})
            contributor_traces.prune_cli_traces(selected)
            self.assertEqual(
                contributor_traces.TRACE_LIMIT,
                len(tuple(selected.parent.glob("pinboard-auto-*.json"))),
            )

    def test_unprivate_destination_rejects_before_cli_and_mcp_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, _ = self.project(Path(temporary))
            self.choose(primary, None)
            self.settings(primary, "on", {})
            directory = primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private directory"):
                self.choose(primary, None)
            cli = subprocess.run(
                [str(ROOT / "scripts" / "pinboard"), "--project-root", str(primary), "root"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(64, cli.returncode)
            self.assertEqual(b"", cli.stdout)

            async def mcp_call() -> tuple[bool, dict[str, JsonValue]]:
                parameters = StdioServerParameters(
                    command=str(ROOT / "scripts" / "pinboard"), args=("--mcp",), cwd=ROOT
                )
                async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        server.ITEM_STATUS_TOOL,
                        {
                            "request": {
                                "project_root": str(primary),
                                "work_root": str(primary / ".pinboard"),
                                "operation": "item",
                                "item_id": "one",
                            }
                        },
                    )
                    assert isinstance(result.structured_content, dict)
                    return result.is_error, result.structured_content

            rejected, response = asyncio.run(mcp_call())
            self.assertFalse(rejected)
            self.assertEqual("TRACE_PREFLIGHT_FAILED", response["code"])
            self.assertEqual(str(directory), response["resource"])
            self.assertEqual(False, response["target_ran"])
            self.assertEqual("unchanged", response["effect"])
            self.assertIn("private directory", str(response["message"]))
            self.assertEqual((), tuple(directory.glob("pinboard-auto-*.json")))

    def test_long_lived_mcp_process_reloads_item_mode_before_each_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            self.choose(primary, None)
            self.settings(primary, "off", {"one": "on"})
            directory = primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY

            async def scenario() -> None:
                parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"), cwd=ROOT)
                async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    arguments = {
                        "request": {
                            "project_root": str(worktree),
                            "work_root": str(primary / ".pinboard"),
                            "operation": "item",
                            "item_id": "one",
                        }
                    }
                    await session.call_tool(server.ITEM_STATUS_TOOL, arguments)
                    self.assertEqual(1, len(tuple(directory.glob("pinboard-auto-mcp-*.json"))))
                    self.settings(primary, "off", {"one": "off"})
                    await session.call_tool(server.ITEM_STATUS_TOOL, arguments)
                    self.assertEqual(1, len(tuple(directory.glob("pinboard-auto-mcp-*.json"))))
                    self.settings(primary, "on", {"one": "inherit"})
                    await session.call_tool(server.ITEM_STATUS_TOOL, arguments)
                    self.assertEqual(2, len(tuple(directory.glob("pinboard-auto-mcp-*.json"))))

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
