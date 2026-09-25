import asyncio
import hashlib
import inspect
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.mcp import execution, server

ROOT = Path(__file__).resolve().parent.parent


class CapturedBytes(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    encoding: Literal["hex"]
    sha256: str
    size_bytes: int
    data: str


class CliInvocationCapture(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cli-invocation-capture/v1"]
    selector: CapturedBytes
    argv: tuple[CapturedBytes, ...]
    outcome: Literal["completed", "interrupted"]
    exit_status: int
    stdout: CapturedBytes
    stderr: CapturedBytes
    environment: Literal["not-captured"]
    presentation: Literal["outer-presentation-unavailable"]
    accepted_evidence: bool


def _decode_bytes(value: CapturedBytes) -> bytes:
    if value.encoding != "hex":
        raise AssertionError(value)
    content = bytes.fromhex(value.data)
    if value.size_bytes != len(content) or value.sha256 != hashlib.sha256(content).hexdigest():
        raise AssertionError(value)
    return content


class LauncherCaptureTest(unittest.TestCase):
    def copy_launcher(self, root: Path) -> Path:
        (root / "scripts").mkdir()
        launcher = root / "scripts" / "pinboard"
        shutil.copyfile(ROOT / "scripts" / "pinboard", launcher)
        launcher.chmod(0o755)
        return launcher

    def write_entry(self, root: Path, name: str, body: str) -> Path:
        entry = root / ".venv" / "bin" / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        entry.chmod(0o755)
        return entry

    def run_launcher(
        self,
        launcher: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([str(launcher), *arguments], capture_output=True, check=False, env=env)

    def read_capture(self, path: Path) -> CliInvocationCapture:
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        capture = msgspec.json.decode(path.read_bytes(), type=CliInvocationCapture, strict=True)
        self.assertFalse(capture.accepted_evidence)
        return capture

    def test_capture_preserves_read_and_authority_changing_process_results_once(self) -> None:
        for exit_status, marker_text in ((0, "read"), (12, "authority")):
            with self.subTest(exit_status=exit_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                launcher = self.copy_launcher(root)
                marker = root / "effect"
                self.write_entry(
                    root,
                    "pinboard",
                    f'printf "%s\\n" "$*"\nprintf "stderr-{marker_text}\\n" >&2\nprintf x >> "{marker}"\nexit {exit_status}\n',
                )
                capture = root / "private" / "capture.json"
                capture.parent.mkdir()

                result = self.run_launcher(
                    launcher,
                    "--capture-evidence",
                    str(capture),
                    "--safe-to-persist-exactly",
                    "--",
                    "status",
                    "--json",
                )

                self.assertEqual(exit_status, result.returncode)
                self.assertEqual(b"status --json\n", result.stdout)
                self.assertEqual(f"stderr-{marker_text}\n".encode(), result.stderr)
                self.assertEqual(b"x", marker.read_bytes())
                evidence = self.read_capture(capture)
                self.assertEqual(exit_status, evidence.exit_status)
                self.assertEqual("completed", evidence.outcome)
                self.assertEqual(result.stdout, _decode_bytes(evidence.stdout))
                self.assertEqual(result.stderr, _decode_bytes(evidence.stderr))
                invoked = tuple(_decode_bytes(value).decode() for value in evidence.argv)
                self.assertEqual(
                    (
                        str(launcher),
                        "--capture-evidence",
                        str(capture),
                        "--safe-to-persist-exactly",
                        "--",
                        "status",
                        "--json",
                    ),
                    invoked,
                )
                self.assertEqual(str(capture).encode(), _decode_bytes(evidence.selector))

    def test_capture_rejects_incomplete_or_invalid_safety_input_before_target(self) -> None:
        cases = (
            ("--capture-evidence",),
            ("--capture-evidence", "capture.json", "status"),
            ("--capture-evidence", "capture.json", "--safe-to-persist-exactly", "status"),
            ("status", "--capture-evidence=capture.json", "--safe-to-persist-exactly"),
            ("--safe-to-persist-exactly", "status"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                launcher = self.copy_launcher(root)
                marker = root / "effect"
                self.write_entry(root, "pinboard", f'printf x >> "{marker}"\n')

                result = self.run_launcher(launcher, *arguments)

                self.assertEqual(64, result.returncode)
                self.assertFalse(marker.exists())
                self.assertFalse((root / "capture.json").exists())
                self.assertEqual("invalid-capture-arguments", json.loads(result.stdout)["status"])

    def test_cli_capture_rejects_protocol_and_hook_startup_before_target(self) -> None:
        for selector, entry_name in (
            ("--mcp", "pinboard-mcp"),
            ("--claude-session-start", "pinboard-claude-session-start"),
            ("--claude-subagent-start", "pinboard-claude-subagent-start"),
        ):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                launcher = self.copy_launcher(root)
                marker = root / "effect"
                self.write_entry(root, entry_name, f'printf x >> "{marker}"\n')
                capture = root / "capture.json"

                result = self.run_launcher(
                    launcher,
                    "--capture-evidence",
                    str(capture),
                    "--safe-to-persist-exactly",
                    "--",
                    selector,
                )

                self.assertEqual(64, result.returncode)
                self.assertFalse(marker.exists())
                self.assertFalse(capture.exists())
                self.assertEqual("invalid-capture-arguments", json.loads(result.stdout)["status"])

    def test_capture_reserves_writable_publication_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            marker = root / "effect"
            self.write_entry(root, "pinboard", f'printf x >> "{marker}"\n')
            capture = root / "private" / "capture.json"
            capture.parent.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            real_mktemp = shutil.which("mktemp")
            assert real_mktemp is not None
            fake_mktemp = fake_bin / "mktemp"
            fake_mktemp.write_text(
                f'#!/bin/sh\nif [ "$1" = "-d" ]; then exec "{real_mktemp}" "$@"; fi\nexit 1\n',
                encoding="utf-8",
            )
            fake_mktemp.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

            result = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(capture),
                "--safe-to-persist-exactly",
                "--",
                "status",
                env=environment,
            )

            self.assertEqual(64, result.returncode)
            self.assertFalse(marker.exists())
            self.assertFalse(capture.exists())
            self.assertEqual("invalid-capture-arguments", json.loads(result.stdout)["status"])

    def test_capture_rejects_an_existing_read_only_destination_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            marker = root / "effect"
            self.write_entry(root, "pinboard", f'printf x >> "{marker}"\n')
            capture = root / "capture.json"
            capture.write_bytes(b"existing")
            capture.chmod(0o400)

            result = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(capture),
                "--safe-to-persist-exactly",
                "--",
                "--version",
            )

            self.assertEqual(64, result.returncode)
            self.assertFalse(marker.exists())
            self.assertEqual(b"existing", capture.read_bytes())
            self.assertEqual("invalid-capture-arguments", json.loads(result.stdout)["status"])

    def test_capture_reports_post_target_publication_failure_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            capture = root / "private" / "capture.json"
            capture.parent.mkdir()
            self.write_entry(root, "pinboard", 'printf "target-output\\n"\nprintf occupied > "$1"\n')

            result = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(capture),
                "--safe-to-persist-exactly",
                "--",
                str(capture),
            )

            self.assertEqual(74, result.returncode)
            self.assertEqual(b"target-output\n", result.stdout)
            self.assertEqual(b"occupied", capture.read_bytes())
            failure = json.loads(result.stderr)
            self.assertEqual("capture-publication-failed", failure["status"])
            self.assertEqual(0, failure["upstream_exit_code"])
            self.assertEqual("potentially-changed", failure["effect_disposition"])
            self.assertEqual("do-not-retry", failure["retry_disposition"])
            self.assertEqual((), tuple(capture.parent.glob(".pinboard-capture-stage.*")))

    def test_capture_records_prestart_failure_interruption_and_large_output_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            prestart = root / "prestart.json"
            result = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(prestart),
                "--safe-to-persist-exactly",
                "--",
                "status",
                "--json",
            )
            self.assertEqual(78, result.returncode)
            self.assertEqual(result.stdout, _decode_bytes(self.read_capture(prestart).stdout))

            self.write_entry(root, "pinboard", 'printf "partial\\n"\nprintf "interrupted\\n" >&2\nexit 143\n')
            interrupted = root / "interrupted.json"
            result = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(interrupted),
                "--safe-to-persist-exactly",
                "--",
                "status",
            )
            evidence = self.read_capture(interrupted)
            self.assertEqual(143, result.returncode)
            self.assertEqual("interrupted", evidence.outcome)
            self.assertEqual(b"partial\n", _decode_bytes(evidence.stdout))
            self.assertEqual(b"interrupted\n", _decode_bytes(evidence.stderr))

            self.write_entry(root, "pinboard", "head -c 20000 /dev/zero | tr '\\000' x\n")
            large = root / "large.json"
            result = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(large),
                "--safe-to-persist-exactly",
                "--",
                "status",
            )
            self.assertEqual(20_000, len(result.stdout))
            self.assertEqual(result.stdout, _decode_bytes(self.read_capture(large).stdout))

    def test_real_signal_preserves_capture_and_propagates_signal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            marker = root / "started"
            scratch = root / "scratch"
            scratch.mkdir()
            self.write_entry(
                root,
                "pinboard",
                f'printf "partial\\n"\nprintf "interrupted\\n" >&2\n: > "{marker}"\nwhile :; do sleep 1; done\n',
            )
            capture = root / "private" / "capture.json"
            capture.parent.mkdir()
            environment = os.environ.copy()
            environment["TMPDIR"] = str(scratch)
            process = subprocess.Popen(
                [
                    str(launcher),
                    "--capture-evidence",
                    str(capture),
                    "--safe-to-persist-exactly",
                    "--",
                    "status",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "target did not reach its deterministic signal barrier")
                self.assertEqual((), tuple(scratch.iterdir()))
                self.assertTrue(tuple(capture.parent.glob(".pinboard-capture.*")))
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

            self.assertEqual(128 + signal.SIGTERM, process.returncode)
            self.assertEqual(b"partial\n", stdout)
            self.assertEqual(b"interrupted\n", stderr)
            evidence = self.read_capture(capture)
            self.assertEqual("interrupted", evidence.outcome)
            self.assertEqual(128 + signal.SIGTERM, evidence.exit_status)
            self.assertEqual(stdout, _decode_bytes(evidence.stdout))
            self.assertEqual(stderr, _decode_bytes(evidence.stderr))
            self.assertEqual((), tuple(capture.parent.glob(".pinboard-capture-stage.*")))
            self.assertEqual((), tuple(capture.parent.glob(".pinboard-capture.*")))
            self.assertEqual((), tuple(scratch.iterdir()))

    def test_d222_rejection_and_d223_publication_remain_separate_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_entry(
                root,
                "pinboard",
                'if [ "$1" = "--checkpoint-history-id" ]; then\n'
                '  printf \'%s\\n\' \'{"schema":"pinboard-rejected-operation/v1","status":"rejected","state_changed":false,"changed_surfaces":[]}\'\n'
                "  exit 2\n"
                "fi\n"
                'printf \'%s\\n\' \'{"schema":"pinboard-dispatch-ready/v2","status":"ready","changed_surfaces":["immutable-artifact","accepted-artifact-reference","ledger"]}\'\n',
            )
            rejection = root / "d222.json"
            publication = root / "d223.json"

            first = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(rejection),
                "--safe-to-persist-exactly",
                "--",
                "--checkpoint-history-id",
                "3348",
            )
            second = self.run_launcher(
                launcher,
                "--capture-evidence",
                str(publication),
                "--safe-to-persist-exactly",
                "--",
                "dispatch",
            )

            self.assertEqual((2, 0), (first.returncode, second.returncode))
            self.assertNotEqual(rejection.read_bytes(), publication.read_bytes())
            self.assertFalse(json.loads(_decode_bytes(self.read_capture(rejection).stdout))["state_changed"])
            self.assertEqual(
                ["immutable-artifact", "accepted-artifact-reference", "ledger"],
                json.loads(_decode_bytes(self.read_capture(publication).stdout))["changed_surfaces"],
            )

    def test_dedicated_mcp_capture_startup_forwards_only_the_complete_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_entry(root, "pinboard-mcp", 'printf "%s\\n" "$*"\n')
            capture_directory = root / "private"
            capture_directory.mkdir()

            valid = self.run_launcher(
                launcher,
                "--mcp",
                "--capture-evidence-dir",
                str(capture_directory),
                "--safe-to-persist-exactly",
            )
            invalid = self.run_launcher(launcher, "--mcp", "--capture-evidence-dir", str(capture_directory))

            self.assertEqual(0, valid.returncode)
            self.assertEqual(
                f"--capture-evidence-dir {capture_directory} --safe-to-persist-exactly\n".encode(), valid.stdout
            )
            self.assertEqual(64, invalid.returncode)
            self.assertEqual("invalid-capture-arguments", json.loads(invalid.stderr)["status"])


class McpCaptureTest(unittest.TestCase):
    def capture_files(self, directory: Path) -> tuple[Path, ...]:
        return tuple(sorted(directory.glob("pinboard-mcp-*.json")))

    def test_shared_execution_capture_preserves_exact_semantics_and_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture = execution.SemanticCapture(directory)
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics = execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
            request: dict[str, execution.JsonValue] = {
                "project_root": "/project",
                "work_root": "/work",
                "item_id": "missing",
                "heterogeneous": [True, 3, None, {"nested": "value"}],
            }
            original_request = json.loads(json.dumps(request))
            rejected: dict[str, execution.JsonValue] = {
                "schema": "pinboard-mcp-item-status-result/v1",
                "status": "rejected",
                "code": "ITEM_NOT_FOUND",
                "message": "Missing.",
                "state_changed": False,
                "effect": "unchanged",
                "retry": "correct-input",
                "changed_surfaces": [],
                "observed": [],
                "mismatches": [],
            }

            def mutate_after_admission(_token: execution.CancellationToken) -> execution.OperationResult:
                request["item_id"] = "changed-inside-callback"
                return execution.OperationResult(rejected, "rejected", None)

            result = asyncio.run(
                execution._run_request(
                    executor,
                    diagnostics,
                    1,
                    server.ITEM_STATUS_TOOL,
                    "/project",
                    mutate_after_admission,
                    arguments=request,
                    capture=capture,
                )
            )
            self.assertEqual(rejected, result)
            first = self.capture_files(directory)
            self.assertEqual(1, len(first))
            self.assertEqual(0o600, stat.S_IMODE(first[0].stat().st_mode))
            record = json.loads(first[0].read_bytes())
            self.assertEqual(original_request, record["request"])
            self.assertEqual(rejected, record["result"]["value"])
            self.assertEqual("unavailable", record["transport_bytes"])
            self.assertEqual("unavailable", record["pre_callback_events"])
            digest, size = first[0].stem.rsplit("-", 2)[-2:]
            self.assertEqual(hashlib.sha256(first[0].read_bytes()).hexdigest(), digest)
            self.assertEqual(len(first[0].read_bytes()), int(size))

            with self.assertRaises(ToolError):
                asyncio.run(
                    execution._run_request(
                        executor,
                        diagnostics,
                        2,
                        server.ITEM_STATUS_TOOL,
                        "/project",
                        lambda _token: (_ for _ in ()).throw(execution.OperationCancelled("stopped")),
                        arguments=request,
                        capture=capture,
                    )
                )
            files = self.capture_files(directory)
            self.assertEqual(2, len(files))
            interrupted = next(
                json.loads(path.read_bytes())
                for path in files
                if json.loads(path.read_bytes())["result"]["availability"] == "unavailable"
            )
            self.assertEqual("unavailable", interrupted["result"]["availability"])
            self.assertEqual("interrupted", interrupted["result"]["reason"])

    def test_capture_write_failures_do_not_obscure_a_validated_committed_result(self) -> None:
        committed: dict[str, execution.JsonValue] = {
            "schema": "test-result/v1",
            "status": "committed",
            "state_changed": True,
            "effect": "committed",
            "retry": "do-not-retry",
            "changed_surfaces": ["immutable-artifact", "ledger"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            capture = execution.SemanticCapture(Path(temporary))
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics_stream = io.StringIO()
            diagnostics = execution.Diagnostics(diagnostics_stream, event_limit=4, line_limit=512)

            def callback(_token: execution.CancellationToken) -> execution.OperationResult:
                return execution.OperationResult(committed, "committed", "revision-7")

            with (
                patch.object(execution.contract_schemas, "validate_result", return_value=committed),
                patch.object(
                    execution,
                    "create_immutable",
                    side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "Capture directory is not writable."),
                ),
            ):
                result = asyncio.run(
                    execution._run_request(
                        executor,
                        diagnostics,
                        3,
                        server.ATTEMPT_AUTHORITY_TOOL,
                        "/project",
                        callback,
                        arguments={"request": {"operation": "acquire"}},
                        capture=capture,
                    )
                )

            self.assertEqual(committed, result)
            self.assertIn("event=capture-unavailable", diagnostics_stream.getvalue())
            self.assertIn("classification=committed", diagnostics_stream.getvalue())
            self.assertIn("commit=revision-7", diagnostics_stream.getvalue())

    def test_result_validation_failure_preserves_unavailable_capture_context(self) -> None:
        invalid: dict[str, execution.JsonValue] = {"schema": "pinboard-item-status/v1"}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture = execution.SemanticCapture(directory)
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)

            def callback(_token: execution.CancellationToken) -> execution.OperationResult:
                return execution.OperationResult(invalid, "committed", "revision-7")

            with self.assertRaisesRegex(msgspec.ValidationError, "missing required field"):
                asyncio.run(
                    execution._run_request(
                        executor,
                        execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=512),
                        4,
                        server.ITEM_STATUS_TOOL,
                        "/project",
                        callback,
                        arguments={"project_root": "/project", "item_id": "item"},
                        capture=capture,
                    )
                )

            [published] = self.capture_files(directory)
            record = json.loads(published.read_bytes())
            self.assertEqual({"project_root": "/project", "item_id": "item"}, record["request"])
            self.assertEqual(
                {
                    "availability": "unavailable",
                    "reason": "result-validation-error",
                    "classification": "committed",
                    "commit_reference": "revision-7",
                },
                record["result"],
            )

    def test_capture_failure_diagnostics_have_a_reserved_bounded_budget(self) -> None:
        committed: dict[str, execution.JsonValue] = {
            "schema": "test-result/v1",
            "status": "committed",
            "state_changed": True,
            "effect": "committed",
            "retry": "do-not-retry",
            "changed_surfaces": ["ledger"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture = execution.SemanticCapture(directory)
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics_stream = io.StringIO()
            diagnostics = execution.Diagnostics(diagnostics_stream, event_limit=2, line_limit=512)
            for request_id in (1, 2):
                diagnostics.emit(
                    event="result",
                    request_id=request_id,
                    operation=server.ITEM_STATUS_TOOL,
                    project_id="project",
                    duration_ms=1,
                    classification="unchanged",
                    commit_reference=None,
                    capture_selector=None,
                )

            def callback(_token: execution.CancellationToken) -> execution.OperationResult:
                return execution.OperationResult(committed, "committed", "revision-7")

            def publish_then_warn(path: Path, content: bytes) -> bool:
                path.write_bytes(content)
                path.chmod(0o600)
                raise ImmutableFilePublishedError(
                    path,
                    FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "Capture directory sync failed."),
                )

            publication_attempt = 0

            def fail_each_way(path: Path, content: bytes) -> bool:
                nonlocal publication_attempt
                publication_attempt += 1
                if publication_attempt == 1:
                    raise FileIOError(
                        FileIOErrorCode.FILE_PUBLISH_FAILED,
                        "Capture directory is not writable.",
                    )
                return publish_then_warn(path, content)

            with (
                patch.object(execution.contract_schemas, "validate_result", return_value=committed),
                patch.object(execution, "create_immutable", side_effect=fail_each_way),
            ):
                for request_id in (3, 4):
                    asyncio.run(
                        execution._run_request(
                            executor,
                            diagnostics,
                            request_id,
                            server.ATTEMPT_AUTHORITY_TOOL,
                            "/project",
                            callback,
                            arguments={"request": {"operation": "acquire"}},
                            capture=capture,
                        )
                    )

            diagnostics_value = diagnostics_stream.getvalue()
            self.assertEqual(1, diagnostics_value.count("event=capture-unavailable"))
            self.assertEqual(1, diagnostics_value.count("event=capture-committed-with-warning"))
            self.assertEqual(4, len(diagnostics_value.splitlines()))

    def test_published_capture_sync_failure_preserves_committed_identity(self) -> None:
        committed: dict[str, execution.JsonValue] = {
            "schema": "test-result/v1",
            "status": "committed",
            "state_changed": True,
            "effect": "committed",
            "retry": "do-not-retry",
            "changed_surfaces": ["immutable-artifact", "ledger"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            capture = execution.SemanticCapture(Path(temporary))
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics_stream = io.StringIO()
            diagnostics = execution.Diagnostics(diagnostics_stream, event_limit=4, line_limit=512)

            def callback(_token: execution.CancellationToken) -> execution.OperationResult:
                return execution.OperationResult(committed, "committed", "revision-7")

            def publish_then_warn(path: Path, content: bytes) -> bool:
                path.write_bytes(content)
                path.chmod(0o600)
                raise ImmutableFilePublishedError(
                    path,
                    FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "Capture directory sync failed."),
                )

            with (
                patch.object(execution.contract_schemas, "validate_result", return_value=committed),
                patch.object(execution, "create_immutable", side_effect=publish_then_warn),
            ):
                result = asyncio.run(
                    execution._run_request(
                        executor,
                        diagnostics,
                        4,
                        server.ATTEMPT_AUTHORITY_TOOL,
                        "/project",
                        callback,
                        arguments={"request": {"operation": "acquire"}},
                        capture=capture,
                    )
                )

            self.assertEqual(committed, result)
            [published] = self.capture_files(Path(temporary))
            digest, size = published.stem.rsplit("-", 2)[-2:]
            self.assertEqual(hashlib.sha256(published.read_bytes()).hexdigest(), digest)
            self.assertEqual(len(published.read_bytes()), int(size))
            diagnostics_value = diagnostics_stream.getvalue()
            self.assertIn("event=capture-committed-with-warning", diagnostics_value)
            self.assertIn(f"capture_selector={published.name}", diagnostics_value)
            self.assertNotIn("event=capture-unavailable", diagnostics_value)
            self.assertIn("classification=committed", diagnostics_value)
            self.assertIn("commit=revision-7", diagnostics_value)

    def test_every_registered_callback_passes_its_decoded_argument_object_to_shared_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capture = execution.SemanticCapture(Path(temporary))
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            transport = server.create_server(
                executor,
                execution.Diagnostics(io.StringIO(), event_limit=1, line_limit=256),
                capture,
                omit_regex_lookarounds=True,
            )
            observed: dict[str, dict[str, execution.JsonValue]] = {}

            async def record(
                _executor: execution.BoundedExecutor,
                _diagnostics: execution.Diagnostics,
                _request_id: int,
                operation: str,
                _project_root: str,
                _callback: object,
                *,
                arguments: dict[str, execution.JsonValue],
                capture: execution.SemanticCapture | None,
            ) -> dict[str, execution.JsonValue]:
                self.assertIs(capture, globals_capture)
                observed[operation] = arguments
                return {}

            globals_capture = capture
            with patch.object(execution, "_run_request", side_effect=record):
                for tool in asyncio.run(transport.list_tools()):
                    registered = transport._tool_manager.get_tool(tool.name)
                    assert registered is not None
                    kwargs: dict[str, object] = {}
                    for name in inspect.signature(registered.fn).parameters:
                        kwargs[name] = (
                            {"flag": True, "count": 3, "nested": [None, "value"]}
                            if name in {"request", "proposal", "brief", "dispatch", "review"}
                            else 7
                            if name in {"artifact_ref_id", "size_bytes"}
                            else f"decoded-{name}"
                        )
                    asyncio.run(registered.fn(**kwargs))
                    expected = {"request": kwargs["request"]} if tuple(kwargs) == ("request",) else kwargs
                    self.assertEqual(expected, observed[tool.name])
            self.assertEqual(20, len(observed))
            for tool in asyncio.run(transport.list_tools()):
                self.assertNotIn("capture_evidence", tool.input_schema["properties"])

    def test_server_startup_accepts_only_an_existing_directory_and_complete_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertIsNone(server._capture_from_arguments(()))
            self.assertIsInstance(
                server._capture_from_arguments(("--capture-evidence-dir", str(directory), "--safe-to-persist-exactly")),
                execution.SemanticCapture,
            )
            for arguments in (
                ("--capture-evidence-dir", str(directory)),
                ("--safe-to-persist-exactly",),
                ("--capture-evidence-dir", str(directory / "missing"), "--safe-to-persist-exactly"),
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    server._capture_from_arguments(arguments)

            with (
                patch.object(
                    execution,
                    "create_immutable",
                    side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "Capture directory is not writable."),
                ),
                self.assertRaises(ValueError),
            ):
                server._capture_from_arguments(("--capture-evidence-dir", str(directory), "--safe-to-persist-exactly"))

    def test_dedicated_stdio_process_captures_one_callback_without_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            arguments = {
                "project_root": "/missing/project",
                "work_root": "/missing/work",
                "item_id": "item",
            }

            async def scenario() -> dict[str, object]:
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=(
                        "-m",
                        "pinboard.mcp",
                        "--capture-evidence-dir",
                        str(directory),
                        "--safe-to-persist-exactly",
                    ),
                    cwd=Path.cwd(),
                )
                async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(server.ITEM_STATUS_TOOL, arguments)
                    self.assertFalse(result.is_error)
                    self.assertIsInstance(result.structured_content, dict)
                    return result.structured_content

            result = asyncio.run(scenario())
            files = self.capture_files(directory)
            self.assertEqual(1, len(files))
            record = json.loads(files[0].read_bytes())
            self.assertEqual(arguments, record["request"])
            self.assertEqual(result, record["result"]["value"])


if __name__ == "__main__":
    unittest.main()
