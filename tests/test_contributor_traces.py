"""Contributor trace opt-in at the normal launcher and MCP boundaries."""

import asyncio
import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from pinboard.adapters.files import contributor_traces, git_config
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.setting_resolution import SettingEffects, SettingResolution, SettingResolutionError
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.cli import entrypoint
from pinboard.mcp import common, execution, server
from pinboard.mcp.contracts import JsonValue
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store

ROOT = Path(__file__).resolve().parent.parent


class ContributorTraceTest(unittest.TestCase):
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
        state = contributor_traces.read_project_trace_settings(project)
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
            self.assertIsNone(contributor_traces.read_project_trace_settings(directory))
            project = directory / "unignored"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            self.assertIsNone(contributor_traces.read_project_trace_settings(project))
            (project / ".pinboard").mkdir()
            self.assertIsNone(contributor_traces.read_project_trace_settings(project))
            settings = project / ".pinboard" / contributor_traces.SETTINGS_NAME
            settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\n\tmode = on\n')
            with self.assertRaisesRegex(ValueError, "Git-ignored"):
                contributor_traces.read_project_trace_settings(project)
            primary, _ = self.project(directory)
            self.choose(primary, None)
            settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            settings.write_text("{}")
            with self.assertRaisesRegex(ValueError, "invalid or unreadable"):
                contributor_traces.read_project_trace_settings(primary)
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(
                    64, entrypoint.main(("--contributor-capture-select", "--project-root", str(primary), "root"))
                )
            self.assertIn("before Pinboard ran", error.getvalue())
            settings.unlink()
            settings.symlink_to(project / ".pinboard" / contributor_traces.SETTINGS_NAME)
            with self.assertRaisesRegex(ValueError, "regular file"):
                contributor_traces.read_project_trace_settings(primary)

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
                state = contributor_traces.read_project_trace_settings(primary)
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
                contributor_traces.read_project_trace_settings(primary)

    def test_missing_project_mode_preserves_item_override_in_both_readers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            settings = primary / ".pinboard" / contributor_traces.SETTINGS_NAME
            original = '[item "one"]\n\tmode = on\n'
            settings.write_text(original)
            state = contributor_traces.read_project_trace_settings(primary)
            assert state is not None
            self.assertEqual("off", state[1].value.unsafe_persist_exact_pinboard_traces)
            self.assertEqual({"one": "on"}, state[1].value.item_overrides)
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
            state = contributor_traces.read_project_trace_settings(primary)
            assert state is not None
            self.assertEqual("off", state[1].value.unsafe_persist_exact_pinboard_traces)
            self.assertEqual({"one": "on"}, state[1].value.item_overrides)
            self.assertEqual(("none", "none"), (state[1].effects.file_creation, state[1].effects.key_write))

            invalid = "[unknown]\n\tmode = on\n" + original
            settings.write_text(invalid)
            with self.assertRaisesRegex(ValueError, "unknown key"):
                contributor_traces.read_project_trace_settings(primary)
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
            initial = contributor_traces.read_project_trace_settings(primary)
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
                contributor_traces.read_project_trace_settings(primary)
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
                contributor_traces.read_project_trace_settings(primary)
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
            off = subprocess.run([str(launcher), *args], capture_output=True, check=False)
            self.assertEqual(0, off.returncode)
            traces = primary / ".pinboard" / contributor_traces.TRACE_DIRECTORY
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

            async def mcp_call() -> dict[str, object]:
                parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"), cwd=ROOT)
                async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        server.ITEM_STATUS_TOOL,
                        {"project_root": str(worktree), "work_root": str(primary / ".pinboard"), "item_id": "missing"},
                    )
                    assert isinstance(result.structured_content, dict)
                    return result.structured_content

            mcp_result = asyncio.run(mcp_call())
            [mcp_trace] = tuple(traces.glob("pinboard-auto-mcp-*.json"))
            semantic = json.loads(mcp_trace.read_bytes())
            self.assertEqual(mcp_result, semantic["result"]["value"])
            self.assertEqual("unavailable", semantic["transport_bytes"])
            self.assertEqual("unavailable", semantic["pre_callback_events"])

            self.settings(primary, "off", {})
            subprocess.run([str(launcher), *args], capture_output=True, check=False)
            asyncio.run(mcp_call())
            self.assertEqual((cli_trace,), tuple(traces.glob("pinboard-auto-cli-*.json")))
            self.assertEqual((mcp_trace,), tuple(traces.glob("pinboard-auto-mcp-*.json")))

    def test_attempt_selector_uses_its_saved_item_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary, worktree = self.project(Path(temporary))
            work_root = primary / ".pinboard"
            database = work_root / "state.sqlite3"
            initialize_database(resolve_durable_roots(primary, work_root), SQLITE_NOW)
            initialize_store(SQLiteWorkStore(database), complete_sqlite_state())
            self.choose(primary, None)
            self.settings(primary, "off", {"work-a": "on"})
            capture = execution.AutomaticCapture(common.select_capture_item).resolve(
                str(worktree),
                {"request": {"project_root": str(worktree), "work_root": str(work_root), "attempt_id": "work-a-1"}},
            )
            self.assertIsNotNone(capture)

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
            self.assertIsNone(capture.resolve(str(worktree), {"item_id": "another"}))
            self.assertIsNone(capture.resolve(str(Path(temporary)), {"item_id": "work-a"}))
            self.assertIsNotNone(capture.resolve(str(worktree), {"item_id": "work-a"}))
            self.settings(primary, "off", {"work-a": "invalid"})
            with self.assertRaises(ToolError):
                capture.resolve(str(worktree), {"item_id": "work-a"})

    def test_mcp_startup_uses_automatic_mode_unless_manual_capture_was_declared(self) -> None:
        async def no_transport() -> None:
            return None

        with tempfile.TemporaryDirectory() as temporary:
            captures: list[execution.SemanticCapture | execution.AutomaticCapture | None] = []

            def create_server(
                _executor: execution.BoundedExecutor,
                _diagnostics: execution.Diagnostics,
                capture: execution.SemanticCapture | execution.AutomaticCapture | None,
                *,
                omit_regex_lookarounds: bool,
            ) -> SimpleNamespace:
                self.assertTrue(omit_regex_lookarounds)
                captures.append(capture)
                return SimpleNamespace(run_stdio_async=no_transport)

            with (
                patch.object(server, "create_server", side_effect=create_server),
                patch.object(
                    server,
                    "read_mcp_omit_regex_lookarounds",
                    return_value=SettingResolution(Path("config"), True, SettingEffects("none", "none", "none")),
                ),
            ):
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
                capture.available("pinboard_item_status", {"item_id": "one"}, {"status": "ok"})
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

            async def mcp_call() -> bool:
                parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"), cwd=ROOT)
                async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        server.ITEM_STATUS_TOOL,
                        {"project_root": str(primary), "work_root": str(primary / ".pinboard"), "item_id": "one"},
                    )
                    return result.is_error

            self.assertTrue(asyncio.run(mcp_call()))
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
                        "project_root": str(worktree),
                        "work_root": str(primary / ".pinboard"),
                        "item_id": "one",
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
