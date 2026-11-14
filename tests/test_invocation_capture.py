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
import unittest
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from evals.behavioral.export import LauncherResult
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.domain import authority_models
from pinboard.mcp import contracts, execution, server

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


def _bytes_json(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f'{{"encoding":"hex","sha256":"{digest}","size_bytes":{len(content)},"data":"{content.hex()}"}}'


def _record_bytes(selector: str, argv: tuple[str, ...], exit_status: int, stdout: bytes, stderr: bytes) -> bytes:
    """The capture record laid out field by field, as the v1 format has always been written."""
    outcome = "interrupted" if exit_status >= 128 else "completed"
    return (
        '{"schema":"pinboard-cli-invocation-capture/v1","selector":'
        + _bytes_json(selector.encode())
        + ',"argv":['
        + ",".join(_bytes_json(argument.encode()) for argument in argv)
        + f'],"outcome":"{outcome}","exit_status":{exit_status},"stdout":'
        + _bytes_json(stdout)
        + ',"stderr":'
        + _bytes_json(stderr)
        + ',"environment":"not-captured","presentation":"outer-presentation-unavailable","accepted_evidence":false}\n'
    ).encode()


class LauncherCaptureTest(unittest.TestCase):
    """CLI capture through a copied launcher whose prepared entry runs an injected target in Python."""

    def copy_launcher(self, root: Path) -> Path:
        (root / "scripts").mkdir()
        launcher = root / "scripts" / "pinboard"
        shutil.copyfile(ROOT / "scripts" / "pinboard", launcher)
        launcher.chmod(0o755)
        return launcher

    def plugin_with_target(self, root: Path) -> Path:
        launcher = self.copy_launcher(root)
        entry = root / ".venv" / "bin" / "pinboard"
        entry.parent.mkdir(parents=True)
        entry.write_text(
            f'#!/bin/sh\ncd "{ROOT}" || exit 70\nexec "{sys.executable}" -m tests.cli_capture_support "$@"\n',
            encoding="utf-8",
        )
        entry.chmod(0o755)
        return launcher

    def run_launcher(
        self,
        launcher: Path,
        *arguments: str,
        target: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(launcher), *arguments],
            capture_output=True,
            check=False,
            cwd=launcher.parent.parent,
            env={**os.environ, "PINBOARD_TEST_TARGET": target, **(environment or {})},
        )

    def write_entry(self, root: Path, name: str, body: str) -> Path:
        entry = root / ".venv" / "bin" / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        entry.chmod(0o755)
        return entry

    def manual(self, capture: Path, *arguments: str) -> tuple[str, ...]:
        return ("--capture-evidence", str(capture), "--safe-to-persist-exactly", "--", *arguments)

    def read_capture(self, path: Path) -> CliInvocationCapture:
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        capture = msgspec.json.decode(path.read_bytes(), type=CliInvocationCapture, strict=True)
        self.assertFalse(capture.accepted_evidence)
        return capture

    def launcher_result(self, output: bytes) -> LauncherResult:
        return msgspec.json.decode(output, type=LauncherResult, strict=True)

    def test_capture_preserves_read_and_authority_changing_process_results_once(self) -> None:
        for exit_status in (0, 12):
            with self.subTest(exit_status=exit_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                launcher = self.plugin_with_target(root)
                marker = root / "effect"
                scratch = root / "scratch"
                scratch.mkdir()
                capture = root / "private" / "capture.json"
                capture.parent.mkdir()

                result = self.run_launcher(
                    launcher,
                    *self.manual(capture, "status", "--json"),
                    target=f"status:{exit_status}",
                    environment={"PINBOARD_TEST_EFFECT": str(marker), "TMPDIR": str(scratch)},
                )

                self.assertEqual(exit_status, result.returncode, result.stderr)
                self.assertEqual(b"target-stdout\n", result.stdout)
                self.assertEqual(b"target-stderr\n", result.stderr)
                self.assertEqual(b"x", marker.read_bytes())
                self.assertEqual((capture,), tuple(capture.parent.iterdir()))
                self.assertEqual((), tuple(scratch.iterdir()))
                evidence = self.read_capture(capture)
                self.assertEqual(exit_status, evidence.exit_status)
                self.assertEqual("completed", evidence.outcome)
                self.assertEqual(result.stdout, _decode_bytes(evidence.stdout))
                self.assertEqual(result.stderr, _decode_bytes(evidence.stderr))
                invoked = tuple(_decode_bytes(value).decode() for value in evidence.argv)
                self.assertEqual((str(launcher), *self.manual(capture, "status", "--json")), invoked)
                self.assertEqual(str(capture).encode(), _decode_bytes(evidence.selector))
                self.assertEqual(
                    _record_bytes(str(capture), invoked, exit_status, result.stdout, result.stderr),
                    capture.read_bytes(),
                )

    def test_capture_rejects_incomplete_or_invalid_safety_input_before_target(self) -> None:
        cases = (
            ("--capture-evidence",),
            ("--capture-evidence", "capture.json", "status"),
            ("--capture-evidence", "capture.json", "--safe-to-persist-exactly", "status"),
            ("--capture-evidence", "capture.json", "--safe-to-persist-exactly", "--"),
            ("status", "--capture-evidence=capture.json", "--safe-to-persist-exactly"),
            ("--safe-to-persist-exactly", "status"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                launcher = self.plugin_with_target(root)
                marker = root / "effect"

                result = self.run_launcher(
                    launcher, *arguments, target="status:0", environment={"PINBOARD_TEST_EFFECT": str(marker)}
                )

                self.assertEqual(64, result.returncode)
                self.assertFalse(marker.exists())
                self.assertFalse((root / "capture.json").exists())
                rejected = self.launcher_result(result.stdout)
                self.assertEqual("invalid-capture-arguments", rejected.status)
                self.assertFalse(rejected.pinboard_started)

    def test_cli_capture_rejects_launcher_only_targets_before_target(self) -> None:
        for target_arguments in (
            ("--prepare-runtime",),
            ("--mcp",),
            ("--claude-session-start",),
            ("--claude-subagent-start",),
            ("--claude-pre-tool-use",),
            ("--antigravity-session-start",),
            ("--antigravity-pre-tool-use",),
            ("--capture-evidence", "nested.json", "--safe-to-persist-exactly", "--", "status"),
            ("status", "--capture-evidence-dir", "."),
        ):
            with self.subTest(target_arguments=target_arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                launcher = self.plugin_with_target(root)
                marker = root / "effect"
                capture = root / "capture.json"

                result = self.run_launcher(
                    launcher,
                    *self.manual(capture, *target_arguments),
                    target="status:0",
                    environment={"PINBOARD_TEST_EFFECT": str(marker)},
                )

                self.assertEqual(64, result.returncode)
                self.assertFalse(marker.exists())
                self.assertEqual((), tuple(root.glob("*.json")))
                self.assertEqual("invalid-capture-arguments", self.launcher_result(result.stdout).status)

    def test_capture_refuses_dot_named_unavailable_and_existing_destinations_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.plugin_with_target(root)
            marker = root / "effect"
            existing = root / "existing.json"
            existing.write_bytes(b"existing")
            existing.chmod(0o400)
            for destination in (f"{root}/.", f"{root}/..", f"{root}/missing/capture.json", str(existing)):
                with self.subTest(destination=destination):
                    result = self.run_launcher(
                        launcher,
                        "--capture-evidence",
                        destination,
                        "--safe-to-persist-exactly",
                        "--",
                        "status",
                        target="status:0",
                        environment={"PINBOARD_TEST_EFFECT": str(marker)},
                    )

                    self.assertEqual(64, result.returncode)
                    self.assertFalse(marker.exists())
                    self.assertEqual("invalid-capture-arguments", self.launcher_result(result.stdout).status)
            self.assertEqual(b"existing", existing.read_bytes())
            self.assertEqual((), tuple(root.glob(".pinboard-capture-stage.*")))

    def test_capture_reserves_its_stage_in_the_selected_parent_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.plugin_with_target(root)
            marker = root / "effect"
            capture = root / "private" / "capture.json"
            capture.parent.mkdir()
            capture.parent.chmod(0o500)
            try:
                result = self.run_launcher(
                    launcher,
                    *self.manual(capture, "status"),
                    target="status:0",
                    environment={"PINBOARD_TEST_EFFECT": str(marker)},
                )
            finally:
                capture.parent.chmod(0o700)

            self.assertEqual(64, result.returncode)
            self.assertFalse(marker.exists())
            self.assertEqual((), tuple(capture.parent.iterdir()))
            rejected = self.launcher_result(result.stdout)
            self.assertEqual("invalid-capture-arguments", rejected.status)
            self.assertIn("write access", rejected.observations[0])

    def test_capture_reports_post_target_publication_failure_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.plugin_with_target(root)
            capture = root / "private" / "capture.json"
            capture.parent.mkdir()

            result = self.run_launcher(launcher, *self.manual(capture, "status"), target=f"occupy:{capture}")

            self.assertEqual(74, result.returncode)
            self.assertEqual(b"target-output\n", result.stdout)
            self.assertEqual(b"occupied", capture.read_bytes())
            self.assertEqual((capture,), tuple(capture.parent.iterdir()))
            failure = self.launcher_result(result.stderr)
            self.assertEqual("capture-publication-failed", failure.status)
            self.assertTrue(failure.pinboard_started)
            self.assertEqual(0, failure.upstream_exit_code)
            self.assertEqual("potentially-changed", failure.effect_disposition)
            self.assertEqual("do-not-retry", failure.retry_disposition)
            self.assertEqual(["target"], failure.changed_surfaces)
            self.assertIn(str(capture), failure.observations[0])

    def test_captured_exit_and_output_match_an_uncaptured_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.plugin_with_target(root)
            ordinary = ("--project-root", str(root), "status")
            for target, exit_status in (("raise", 1), ("system-exit-text", 1), ("large", 3), ("status:143", 143)):
                with self.subTest(target=target):
                    capture = root / f"{target.replace(':', '-')}.json"
                    uncaptured = self.run_launcher(launcher, *ordinary, target=target)
                    captured = self.run_launcher(launcher, *self.manual(capture, *ordinary), target=target)

                    self.assertEqual(exit_status, uncaptured.returncode)
                    self.assertEqual(
                        (uncaptured.returncode, uncaptured.stdout, uncaptured.stderr),
                        (captured.returncode, captured.stdout, captured.stderr),
                    )
                    evidence = self.read_capture(capture)
                    self.assertEqual(captured.returncode, evidence.exit_status)
                    self.assertEqual("interrupted" if exit_status >= 128 else "completed", evidence.outcome)
                    self.assertEqual(captured.stdout, _decode_bytes(evidence.stdout))
                    self.assertEqual(captured.stderr, _decode_bytes(evidence.stderr))
            self.assertIn(b"RuntimeError: target failed", self.run_launcher(launcher, *ordinary, target="raise").stderr)

    def test_real_cli_exits_and_output_match_an_uncaptured_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = ROOT / "scripts" / "pinboard"
            for arguments in (
                ("--version",),
                ("--help",),
                ("root", "--bogus"),
                ("code-catalog", "--json"),
                ("--project-root", str(root), "status"),
            ):
                with self.subTest(arguments=arguments):
                    capture = root / "capture.json"
                    capture.unlink(missing_ok=True)
                    uncaptured = subprocess.run([str(launcher), *arguments], capture_output=True, check=False, cwd=root)
                    captured = subprocess.run(
                        [str(launcher), *self.manual(capture, *arguments)], capture_output=True, check=False, cwd=root
                    )
                    self.assertEqual(
                        (uncaptured.returncode, uncaptured.stdout, uncaptured.stderr),
                        (captured.returncode, captured.stdout, captured.stderr),
                    )
                    if arguments == ("code-catalog", "--json"):
                        self.assertGreater(len(captured.stdout), 65_536)
                    evidence = self.read_capture(capture)
                    self.assertEqual(captured.returncode, evidence.exit_status)
                    self.assertEqual(captured.stdout, _decode_bytes(evidence.stdout))
                    self.assertEqual(captured.stderr, _decode_bytes(evidence.stderr))
                    self.assertEqual(
                        (str(launcher), *self.manual(capture, *arguments)),
                        tuple(_decode_bytes(value).decode() for value in evidence.argv),
                    )

    def test_interrupt_ends_target_with_signal_status_and_publishes_record(self) -> None:
        for name in ("SIGHUP", "SIGINT", "SIGTERM"):
            with self.subTest(signal=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                launcher = self.plugin_with_target(root)
                scratch = root / "scratch"
                scratch.mkdir()
                capture = root / "private" / "capture.json"
                capture.parent.mkdir()

                result = self.run_launcher(
                    launcher,
                    *self.manual(capture, "status"),
                    target=f"signal:{name}",
                    environment={"TMPDIR": str(scratch)},
                )

                status = 128 + signal.Signals[name]
                self.assertEqual(status, result.returncode)
                self.assertEqual(b"partial\n", result.stdout)
                self.assertEqual(b"interrupted\n", result.stderr)
                evidence = self.read_capture(capture)
                self.assertEqual("interrupted", evidence.outcome)
                self.assertEqual(status, evidence.exit_status)
                self.assertEqual(result.stdout, _decode_bytes(evidence.stdout))
                self.assertEqual(result.stderr, _decode_bytes(evidence.stderr))
                self.assertEqual((capture,), tuple(capture.parent.iterdir()))
                self.assertEqual((), tuple(scratch.iterdir()))

    def test_signals_ignored_at_startup_stay_ignored_under_capture(self) -> None:
        for name in ("SIGHUP", "SIGINT"):
            with self.subTest(signal=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                launcher = self.plugin_with_target(root)
                capture = root / "capture.json"
                # Start the launcher as `nohup` or a background job would, with the signal already ignored.
                ignoring = ("/bin/sh", "-c", f'trap "" {name.removeprefix("SIG")}; exec "$0" "$@"', str(launcher))
                uncaptured, captured = (
                    subprocess.run(
                        [*ignoring, *arguments],
                        capture_output=True,
                        check=False,
                        cwd=root,
                        env={**os.environ, "PINBOARD_TEST_TARGET": f"signal:{name}"},
                    )
                    for arguments in (("status",), self.manual(capture, "status"))
                )

                self.assertEqual(0, uncaptured.returncode, uncaptured.stderr)
                self.assertEqual(b"partial\nsignal-not-delivered\n", uncaptured.stdout)
                self.assertEqual(
                    (uncaptured.returncode, uncaptured.stdout, uncaptured.stderr),
                    (captured.returncode, captured.stdout, captured.stderr),
                )
                evidence = self.read_capture(capture)
                self.assertEqual(("completed", 0), (evidence.outcome, evidence.exit_status))
                self.assertEqual(captured.stdout, _decode_bytes(evidence.stdout))
                self.assertEqual(captured.stderr, _decode_bytes(evidence.stderr))

    def test_signal_during_publication_changes_only_a_failed_publication_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.plugin_with_target(root)
            published = root / "published.json"
            result = self.run_launcher(
                launcher,
                *self.manual(published, "status"),
                target="status:0",
                environment={"PINBOARD_TEST_PUBLICATION_SIGNAL": "SIGTERM"},
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual((b"target-stdout\n", b"target-stderr\n"), (result.stdout, result.stderr))
            evidence = self.read_capture(published)
            self.assertEqual(("completed", 0), (evidence.outcome, evidence.exit_status))
            self.assertEqual(result.stdout, _decode_bytes(evidence.stdout))

            occupied = root / "occupied.json"
            failed = self.run_launcher(
                launcher,
                *self.manual(occupied, "status"),
                target=f"occupy:{occupied}",
                environment={"PINBOARD_TEST_PUBLICATION_SIGNAL": "SIGHUP"},
            )
            self.assertEqual(128 + signal.SIGHUP, failed.returncode)
            self.assertEqual(b"target-output\n", failed.stdout)
            self.assertEqual("capture-publication-failed", self.launcher_result(failed.stderr).status)
            self.assertEqual((), tuple(root.glob(".pinboard-capture-stage.*")))

    def test_signal_after_a_raising_target_still_publishes_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.plugin_with_target(root)
            published = root / "published" / "capture.json"
            published.parent.mkdir()

            result = self.run_launcher(
                launcher,
                *self.manual(published, "status"),
                target="raise",
                environment={"PINBOARD_TEST_HANDOFF_SIGNAL": "SIGTERM"},
            )

            self.assertEqual(128 + signal.SIGTERM, result.returncode, result.stderr)
            self.assertEqual(b"partial\n", result.stdout)
            evidence = self.read_capture(published)
            self.assertEqual(("interrupted", result.returncode), (evidence.outcome, evidence.exit_status))
            self.assertEqual(result.stdout, _decode_bytes(evidence.stdout))
            self.assertEqual(result.stderr, _decode_bytes(evidence.stderr))
            self.assertEqual((published,), tuple(published.parent.iterdir()))

            occupied = root / "occupied" / "capture.json"
            occupied.parent.mkdir()
            failed = self.run_launcher(
                launcher,
                *self.manual(occupied, "status"),
                target=f"occupy-raise:{occupied}",
                environment={"PINBOARD_TEST_HANDOFF_SIGNAL": "SIGINT"},
            )

            self.assertEqual(128 + signal.SIGINT, failed.returncode)
            self.assertEqual(b"partial\n", failed.stdout)
            failure = self.launcher_result(failed.stderr)
            self.assertEqual("capture-publication-failed", failure.status)
            self.assertEqual(128 + signal.SIGINT, failure.upstream_exit_code)
            self.assertEqual("do-not-retry", failure.retry_disposition)
            self.assertEqual(b"occupied", occupied.read_bytes())
            self.assertEqual((occupied,), tuple(occupied.parent.iterdir()))

    def test_rejection_and_publication_remain_separate_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = ROOT / "scripts" / "pinboard"
            rejection = root / "rejection.json"
            publication = root / "publication.json"

            first = subprocess.run(
                [str(launcher), *self.manual(rejection, "root", "--bogus")], capture_output=True, check=False, cwd=root
            )
            second = subprocess.run(
                [str(launcher), *self.manual(publication, "--version")], capture_output=True, check=False, cwd=root
            )

            self.assertEqual((2, 0), (first.returncode, second.returncode))
            self.assertNotEqual(rejection.read_bytes(), publication.read_bytes())
            self.assertEqual(first.stderr, _decode_bytes(self.read_capture(rejection).stderr))
            self.assertEqual(second.stdout, _decode_bytes(self.read_capture(publication).stdout))

    def test_manual_capture_records_the_targets_missing_project_root_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            capture = root / "capture.json"
            missing = root / "missing"
            arguments = ("--project-root", str(missing), "root")
            uncaptured = subprocess.run(
                [str(ROOT / "scripts" / "pinboard"), *arguments], capture_output=True, check=False, cwd=root
            )
            captured = subprocess.run(
                [str(ROOT / "scripts" / "pinboard"), *self.manual(capture, *arguments)],
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(2, uncaptured.returncode)
            self.assertIn(str(missing).encode(), uncaptured.stderr)
            self.assertEqual(
                (uncaptured.returncode, uncaptured.stdout, uncaptured.stderr),
                (captured.returncode, captured.stdout, captured.stderr),
            )
            evidence = self.read_capture(capture)
            self.assertEqual(("completed", 2), (evidence.outcome, evidence.exit_status))
            self.assertEqual(captured.stderr, _decode_bytes(evidence.stderr))
            self.assertFalse(missing.exists())

    def test_unprepared_runtime_and_direct_entry_do_not_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            launcher = self.copy_launcher(root)
            capture = root / "capture.json"
            unprepared = subprocess.run(
                [str(launcher), *self.manual(capture, "--version")], capture_output=True, check=False, cwd=root
            )
            self.assertEqual(78, unprepared.returncode)
            self.assertEqual("runtime-preparation-required", json.loads(unprepared.stdout)["status"])
            direct = subprocess.run(
                [str(Path(sys.executable).parent / "pinboard"), *self.manual(capture, "--version")],
                capture_output=True,
                check=False,
                cwd=root,
            )
            self.assertEqual(2, direct.returncode)
            self.assertFalse(capture.exists())

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
                target="unused",
            )
            invalid = self.run_launcher(
                launcher, "--mcp", "--capture-evidence-dir", str(capture_directory), target="unused"
            )

            self.assertEqual(0, valid.returncode)
            self.assertEqual(
                f"--capture-evidence-dir {capture_directory} --safe-to-persist-exactly\n".encode(), valid.stdout
            )
            self.assertEqual(64, invalid.returncode)
            self.assertEqual("invalid-capture-arguments", json.loads(invalid.stderr)["status"])


class McpCaptureTest(unittest.TestCase):
    def capture_files(self, directory: Path) -> tuple[Path, ...]:
        return tuple(sorted(directory.glob("pinboard-mcp-*.json")))

    def committed_authority(
        self, _token: execution.CancellationToken
    ) -> execution.TypedOperationResult[contracts.AttemptAuthorityCommitted]:
        return execution.TypedOperationResult(
            contracts.AttemptAuthorityCommitted(
                "pinboard-mcp-attempt-authority-result/v1",
                "attempt-1",
                "item-1",
                "task",
                "host",
                "lease",
                1,
                "2026-01-01T01:00:00+00:00",
                authority_models.AttemptLeaseStatus.ACTIVE,
                "2026-01-01T00:00:00+00:00",
                "committed",
                7,
                1,
                True,
                "committed",
                "do-not-retry",
                ("ledger",),
                None,
            ),
            "committed",
            "7",
        )

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
            rejected = contracts.ItemStatusUnavailable(
                "pinboard-mcp-item-status-result/v3",
                "rejected",
                "ITEM_NOT_FOUND",
                "Missing.",
                False,
                "unchanged",
                "correct-input",
                (),
                (),
                (),
            )

            def mutate_after_admission(
                _token: execution.CancellationToken,
            ) -> execution.TypedOperationResult[contracts.ItemStatusUnavailable]:
                request["item_id"] = "changed-inside-callback"
                return execution.TypedOperationResult(rejected, "rejected", None)

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
            self.assertEqual(execution.wire_content(rejected), result)
            first = self.capture_files(directory)
            self.assertEqual(1, len(first))
            self.assertEqual(0o600, stat.S_IMODE(first[0].stat().st_mode))
            record = json.loads(first[0].read_bytes())
            self.assertEqual(original_request, record["request"])
            self.assertEqual(result, record["result"]["value"])
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

    def test_capture_write_failures_do_not_obscure_a_committed_result(self) -> None:
        expected = json.loads(msgspec.json.encode(self.committed_authority(execution.CancellationToken()).record))
        with tempfile.TemporaryDirectory() as temporary:
            capture = execution.SemanticCapture(Path(temporary))
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics_stream = io.StringIO()
            diagnostics = execution.Diagnostics(diagnostics_stream, event_limit=4, line_limit=512)

            with patch.object(
                execution,
                "create_immutable",
                side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "Capture directory is not writable."),
            ):
                result = asyncio.run(
                    execution._run_request(
                        executor,
                        diagnostics,
                        3,
                        server.ATTEMPT_AUTHORITY_TOOL,
                        "/project",
                        self.committed_authority,
                        arguments={"request": {"operation": "acquire"}},
                        capture=capture,
                    )
                )

            self.assertEqual(expected, result)
            self.assertEqual("committed", result["effect"])
            self.assertEqual(["ledger"], result["changed_surfaces"])
            self.assertEqual(7, result["committed_revision"])
            self.assertIn("event=capture-unavailable", diagnostics_stream.getvalue())
            self.assertIn("classification=committed", diagnostics_stream.getvalue())
            self.assertIn("commit=7", diagnostics_stream.getvalue())

    def test_capture_failure_diagnostics_have_a_reserved_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture = execution.SemanticCapture(directory)
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics_stream = io.StringIO()
            diagnostics = execution.Diagnostics(diagnostics_stream, event_limit=2, line_limit=512)
            for request_id in (1, 2):
                diagnostics.emit(
                    event=execution.TraceEvent.RESULT,
                    request_id=request_id,
                    operation=server.ITEM_STATUS_TOOL,
                    project_id="project",
                    duration_ms=1,
                    classification="unchanged",
                    commit_reference=None,
                    capture_selector=None,
                )

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

            with patch.object(execution, "create_immutable", side_effect=fail_each_way):
                for request_id in (3, 4):
                    asyncio.run(
                        execution._run_request(
                            executor,
                            diagnostics,
                            request_id,
                            server.ATTEMPT_AUTHORITY_TOOL,
                            "/project",
                            self.committed_authority,
                            arguments={"request": {"operation": "acquire"}},
                            capture=capture,
                        )
                    )

            diagnostics_value = diagnostics_stream.getvalue()
            self.assertEqual(1, diagnostics_value.count("event=capture-unavailable"))
            self.assertEqual(1, diagnostics_value.count("event=capture-committed-with-warning"))
            self.assertEqual(4, len(diagnostics_value.splitlines()))

    def test_published_capture_sync_failure_preserves_committed_identity(self) -> None:
        expected = json.loads(msgspec.json.encode(self.committed_authority(execution.CancellationToken()).record))
        with tempfile.TemporaryDirectory() as temporary:
            capture = execution.SemanticCapture(Path(temporary))
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            diagnostics_stream = io.StringIO()
            diagnostics = execution.Diagnostics(diagnostics_stream, event_limit=4, line_limit=512)

            def publish_then_warn(path: Path, content: bytes) -> bool:
                path.write_bytes(content)
                path.chmod(0o600)
                raise ImmutableFilePublishedError(
                    path,
                    FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "Capture directory sync failed."),
                )

            with patch.object(execution, "create_immutable", side_effect=publish_then_warn):
                result = asyncio.run(
                    execution._run_request(
                        executor,
                        diagnostics,
                        4,
                        server.ATTEMPT_AUTHORITY_TOOL,
                        "/project",
                        self.committed_authority,
                        arguments={"request": {"operation": "acquire"}},
                        capture=capture,
                    )
                )

            [published] = self.capture_files(Path(temporary))
            self.assertEqual(expected, result)
            self.assertEqual("committed", result["effect"])
            self.assertEqual(result, json.loads(published.read_bytes())["result"]["value"])
            digest, size = published.stem.rsplit("-", 2)[-2:]
            self.assertEqual(hashlib.sha256(published.read_bytes()).hexdigest(), digest)
            self.assertEqual(len(published.read_bytes()), int(size))
            diagnostics_value = diagnostics_stream.getvalue()
            self.assertIn("event=capture-committed-with-warning", diagnostics_value)
            self.assertIn(f"capture_selector={published.name}", diagnostics_value)
            self.assertNotIn("event=capture-unavailable", diagnostics_value)
            self.assertIn("classification=committed", diagnostics_value)
            self.assertIn("commit=7", diagnostics_value)

    def test_every_registered_callback_passes_its_decoded_argument_object_to_shared_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capture = execution.SemanticCapture(Path(temporary))
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            self.addCleanup(executor.shutdown)
            transport = server.create_server(
                executor,
                execution.Diagnostics(io.StringIO(), event_limit=1, line_limit=256),
                capture,
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
                self.assertIs(
                    capture,
                    None
                    if operation
                    in {
                        server.BRIEF_SOURCES_TOOL,
                        server.BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
                        server.FIXTURE_REVIEW_TOOL,
                        server.STARTUP_TEXT_TOOL,
                        server.BRIEF_LOOKUP_TOOL,
                        server.OBLIGATION_COVERAGE_TOOL,
                    }
                    else globals_capture,
                )
                observed[operation] = arguments
                return {}

            globals_capture = capture
            with patch.object(execution, "_run_request", side_effect=record):
                for tool in asyncio.run(transport.list_tools()):
                    registered = transport._tool_manager.get_tool(tool.name)
                    assert registered is not None
                    kwargs: dict[str, object] = {}
                    for name in inspect.signature(registered.fn).parameters:
                        if name == registered.context_kwarg:
                            continue
                        kwargs[name] = (
                            {"flag": True, "count": 3, "nested": [None, "value"]}
                            if name in {"request", "proposal", "brief", "dispatch", "review"}
                            else 7
                            if name in {"artifact_ref_id", "size_bytes"}
                            else f"decoded-{name}"
                        )
                    context: dict[str, Context] = (
                        {} if registered.context_kwarg is None else {registered.context_kwarg: Context()}
                    )
                    asyncio.run(registered.fn(**kwargs, **context))
                    expected = {"request": kwargs["request"]} if tuple(kwargs) == ("request",) else kwargs
                    self.assertEqual(expected, observed[tool.name])
            self.assertEqual({tool.name for tool in asyncio.run(transport.list_tools())}, observed.keys())
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
                "request": {
                    "project_root": "/missing/project",
                    "work_root": "/missing/work",
                    "operation": "item",
                    "item_id": "item",
                }
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
