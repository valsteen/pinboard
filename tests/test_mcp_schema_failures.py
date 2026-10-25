"""Schema refusal reaches the advertised native caller without changing board facts."""

import asyncio
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from mcp import Client

from pinboard.adapters.sqlite import database
from pinboard.application import stored_state
from pinboard.cli.entrypoint import main
from pinboard.mcp import common, execution, server
from tests.native_support import NativeTools
from tests.support import SQLITE_NOW


class NativeSchemaFailureTest(unittest.TestCase):
    def world(self, version: int) -> tuple[Path, Path, NativeTools]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        project = Path(directory.name).resolve()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        work = project / ".pinboard"
        work.mkdir()
        (project / ".git/info/exclude").write_text("/.pinboard/\n")
        with closing(sqlite3.connect(work / "state.sqlite3")) as connection, connection:
            connection.executescript(
                Path(database.__file__).with_name(f"schema_compatibility_v{version}.sql").read_text()
            )
            timestamp = SQLITE_NOW.isoformat()
            connection.execute(
                "INSERT INTO project_meta VALUES (1, 'pinboard', ?, 0, 1, ?, ?)", (version, timestamp, timestamp)
            )
            connection.executemany(
                "INSERT INTO work_item_state_counts VALUES (?, 0)",
                tuple((state.value,) for state in stored_state.StoredWorkItemState),
            )
        for child in ("artifacts/accepted.json", "views/board.md", "migration/retained.json"):
            destination = work / child
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(b"retained evidence\n")
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        native = NativeTools(
            server.create_server(
                executor,
                execution.Diagnostics(io.StringIO(), event_limit=8, line_limit=256),
                execution.AutomaticCapture(common.select_capture_item),
            )
        )
        return project, work, native

    def board_files(self, work: Path) -> dict[Path, tuple[bytes, int]]:
        return {
            p.relative_to(work): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in work.rglob("*")
            if p.is_file() and p.name != "contributor-traces.config" and "invocation-traces" not in p.parts
        }

    def preview(self, mismatch: dict[str, common.JsonValue]) -> None:
        arguments = mismatch["preview_arguments"]
        assert isinstance(arguments, list)
        cli_arguments: list[str] = []
        for argument in arguments:
            assert isinstance(argument, str)
            cli_arguments.append(argument)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(cli_arguments), output.getvalue())
        preview = json.loads(output.getvalue())
        self.assertIn("plan_id", preview)

    def test_exact_predecessors_reach_sdk_reads_mutations_and_preflight_with_executable_preview(self) -> None:
        for version in (6, 7, 8, 9):
            with self.subTest(version=version):
                project, work, native = self.world(version)
                path = work / "state.sqlite3"
                preserved = self.board_files(work)
                inspect_arguments: dict[str, common.JsonValue] = {
                    "project_root": str(project),
                    "work_root": str(work),
                    "attempt_id": "one-1",
                    "reconciliation": None,
                }
                result = native.call_advertised_tool("pinboard_attempt_inspect", inspect_arguments)
                self.assertEqual("SCHEMA_UNSUPPORTED", result["code"])
                self.assertEqual("unchanged", result["effect"])
                self.assertEqual("do-not-retry", result["retry"])
                self.assertEqual([], result["changed_surfaces"])
                effects = result["trace_preflight_effects"]
                assert isinstance(effects, dict)
                self.assertEqual("confirmed", effects["settings_file_creation"])
                mismatch = result["schema_mismatch"]
                assert isinstance(mismatch, dict)
                self.assertEqual(str(path), result["resource"])
                self.assertEqual(f"sqlite-v{version}", mismatch["observed_schema"])
                self.assertEqual(database.SCHEMA_ID, mismatch["required_schema"])
                self.assertEqual(preserved, self.board_files(work))
                self.preview(mismatch)
                self.assertEqual(preserved, self.board_files(work))

                (work / "contributor-traces.config").write_text(
                    '[pinboard "unsafe_persist_exact_pinboard_traces"]\nmode = on\n'
                )
                traced = native.call_advertised_tool("pinboard_attempt_inspect", inspect_arguments)
                auxiliary = traced["trace_preflight_effects"]
                assert isinstance(auxiliary, dict)
                self.assertEqual("confirmed", auxiliary["trace_directory_creation"])
                captures = list((work / "invocation-traces").glob("*.json"))
                self.assertEqual(1, len(captures))
                self.assertEqual(traced, json.loads(captures[0].read_text())["result"]["value"])
                self.assertEqual(preserved, self.board_files(work))

                (work / "contributor-traces.config").write_text(
                    '[pinboard "unsafe_persist_exact_pinboard_traces"]\nmode = off\n'
                )

                refused = native.call_advertised_tool(
                    "pinboard_attempt_authority",
                    {
                        "request": {
                            "operation": "acquire",
                            "project_root": str(project),
                            "work_root": str(work),
                            "attempt_id": "one-1",
                            "host_id": "test-host",
                            "task_id": "test-worker",
                            "ttl_seconds": 3600,
                        }
                    },
                )
                self.assertEqual("SCHEMA_UNSUPPORTED", refused["code"])
                self.assertEqual("unchanged", refused["effect"])
                self.assertEqual(preserved, self.board_files(work))

                (work / "contributor-traces.config").write_text('[item "one"]\nmode = off\n')
                preflight = native.call_advertised_tool("pinboard_attempt_inspect", inspect_arguments)
                self.assertEqual("TRACE_PREFLIGHT_FAILED", preflight["code"])
                self.assertFalse(preflight["target_ran"])
                self.assertEqual(str(path), preflight["resource"])
                self.assertEqual("committed", preflight["effect"])
                self.assertEqual(["work-root"], preflight["changed_surfaces"])
                self.assertEqual("acknowledged", preflight["settings_mode_write"])
                self.assertEqual(mismatch, preflight["schema_mismatch"])
                self.assertEqual(preserved, self.board_files(work))
                self.preview(mismatch)
                self.assertEqual(preserved, self.board_files(work))

    def test_unsupported_schema_has_exact_diagnosis_without_migration_and_corruption_keeps_diagnostics(self) -> None:
        project, work, native = self.world(9)
        path = work / "state.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE project_meta SET schema_version = 999")
        before = self.board_files(work)
        result = native.call_advertised_tool(
            "pinboard_overview", {"project_root": str(project), "work_root": str(work)}
        )
        mismatch = result["schema_mismatch"]
        assert isinstance(mismatch, dict)
        self.assertEqual("sqlite-v999", mismatch["observed_schema"])
        self.assertIsNone(mismatch["preview_arguments"])
        self.assertIn("No supported migration", str(result["repair"]))
        self.assertEqual(before, self.board_files(work))

        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE project_meta SET schema_version = 9")
            name = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index' AND sql IS NOT NULL LIMIT 1"
            ).fetchone()[0]
            connection.execute(f'DROP INDEX "{name}"')
        before = self.board_files(work)

        async def corrupt_read() -> None:
            async with Client(native.server) as client:
                failed = await client.call_tool(
                    "pinboard_overview", {"project_root": str(project), "work_root": str(work)}
                )
            self.assertTrue(failed.is_error)
            self.assertIsNone(failed.structured_content)

        with self.assertLogs(level="ERROR") as diagnostics:
            asyncio.run(corrupt_read())
        self.assertIn("exact v9 schema", "\n".join(diagnostics.output))
        self.assertEqual(before, self.board_files(work))
