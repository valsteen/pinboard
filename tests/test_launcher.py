import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LauncherTest(unittest.TestCase):
    def copy_launcher(self, root: Path) -> Path:
        (root / "scripts").mkdir()
        launcher = root / "scripts" / "pinboard"
        shutil.copyfile(ROOT / "scripts" / "pinboard", launcher)
        launcher.chmod(0o755)
        return launcher

    def run_launcher(
        self, launcher: Path, *arguments: str, path: str, extra_environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(launcher), *arguments],
            env={**os.environ, "PATH": path, **(extra_environment or {})},
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_result(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        status: str,
        retry: str,
        effect: str,
        changed_surfaces: list[str],
        upstream_exit_code: int | None,
        next_action_requires: list[str] | None,
        returncode: int = 78,
    ) -> None:
        self.assertEqual(returncode, result.returncode)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(1, result.stdout.count("\n"))
        payload = json.loads(result.stdout)
        self.assertEqual(
            {
                "schema": "pinboard-launcher-result/v1",
                "status": status,
                "pinboard_started": False,
                "runtime_location": {"base": "launcher-root", "relative": ".pinboard-runtime"},
                "observations": payload["observations"],
                "upstream_exit_code": upstream_exit_code,
                "retry_disposition": retry,
                "effect_disposition": effect,
                "changed_surfaces": changed_surfaces,
                "next_action": (
                    None
                    if next_action_requires is None
                    else {
                        "launcher": "self",
                        "arguments": ["--prepare-runtime"],
                        "display_command": "scripts/pinboard --prepare-runtime",
                        "requires": next_action_requires,
                    }
                ),
            },
            payload,
        )
        self.assertTrue(payload["observations"])
        self.assertTrue(all(observation and "\n" not in observation for observation in payload["observations"]))

    def write_uv(self, root: Path, body: str) -> Path:
        uv = root / "uv"
        uv.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        uv.chmod(0o755)
        return uv

    def test_native_startup_selects_requested_source_or_private_entry_without_uv(self) -> None:
        for selector, entry in (
            ("--mcp", "pinboard-mcp"),
            ("--claude-subagent-start", "pinboard-claude-subagent-start"),
        ):
            for runtime in (".venv", ".pinboard-runtime/environment"):
                with self.subTest(selector=selector, runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    launcher = self.copy_launcher(root)
                    executable = root / runtime / "bin" / entry
                    executable.parent.mkdir(parents=True)
                    executable.write_text('#!/bin/sh\nprintf "native:%s\\n" "$*"\n', encoding="utf-8")
                    executable.chmod(0o755)
                    if runtime != ".venv":
                        (root / ".pinboard-runtime" / ".pinboard-ready").touch()
                    result = self.run_launcher(launcher, selector, path="/usr/bin:/bin")
                    self.assertEqual(0, result.returncode)
                    self.assertEqual("native:\n", result.stdout)
                    self.assertEqual("", result.stderr)

    def test_hook_context_entry_does_not_enable_missing_mcp_or_accept_extra_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            executable = root / ".pinboard-runtime" / "environment" / "bin" / "pinboard-claude-subagent-start"
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nprintf "identity-context\\n"\n', encoding="utf-8")
            executable.chmod(0o755)
            (root / ".pinboard-runtime" / ".pinboard-ready").touch()
            hook = self.run_launcher(launcher, "--claude-subagent-start", path="/usr/bin:/bin")
            self.assertEqual(0, hook.returncode)
            self.assertEqual("identity-context\n", hook.stdout)
            result = self.run_launcher(launcher, "--mcp", path="/usr/bin:/bin")
            self.assertEqual(78, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual("runtime-preparation-required", json.loads(result.stderr)["status"])
            for argument in ("--prepare-runtime", "--mcp", "--version", ""):
                extra = self.run_launcher(launcher, "--claude-subagent-start", argument, path="/usr/bin:/bin")
                self.assertEqual(64, extra.returncode)
                self.assertEqual("", extra.stdout)
                self.assertEqual("unchanged", json.loads(extra.stderr)["effect_disposition"])

    def test_preparation_rejects_old_ready_runtime_without_hook_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            runtime = root / ".pinboard-runtime" / "environment" / "bin"
            runtime.mkdir(parents=True)
            for entry in ("pinboard", "pinboard-mcp"):
                executable = runtime / entry
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            (root / ".pinboard-runtime" / ".pinboard-ready").touch()
            self.write_uv(
                root,
                'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                "printf '#!/bin/sh\\nexit 0\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/pinboard\"\n"
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n'
                'cp "$UV_PROJECT_ENVIRONMENT/bin/pinboard" "$UV_PROJECT_ENVIRONMENT/bin/pinboard-mcp"\n',
            )
            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")
            self.assertEqual(78, result.returncode)
            self.assertEqual("runtime-entrypoint-invalid", json.loads(result.stdout)["status"])
            self.assertIn("pinboard-claude-subagent-start", result.stderr)
            self.assertFalse((root / ".pinboard-runtime" / ".pinboard-ready").exists())

    def test_mcp_missing_or_partial_entry_keeps_protocol_stdout_empty(self) -> None:
        for state in ("missing", "cli-only", "marker-only", "entry-only"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                launcher = self.copy_launcher(root)
                private = root / ".pinboard-runtime"
                private.mkdir()
                if state in ("cli-only", "marker-only"):
                    (private / ".pinboard-ready").touch()
                if state in ("cli-only", "entry-only"):
                    executable = (
                        private / "environment" / "bin" / ("pinboard" if state == "cli-only" else "pinboard-mcp")
                    )
                    executable.parent.mkdir(parents=True)
                    executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                    executable.chmod(0o755)
                before = tuple(private.rglob("*"))
                sentinel = root / "uv-was-called"
                self.write_uv(root, f'touch "{sentinel}"\n')
                result = self.run_launcher(launcher, "--mcp", path=f"{root}:/usr/bin:/bin")
                self.assertEqual("", result.stdout)
                self.assertLess(len(result.stderr), 2048)
                self.assert_result(
                    subprocess.CompletedProcess(result.args, result.returncode, result.stderr, ""),
                    status="runtime-preparation-required",
                    retry="run-preparation",
                    effect="unchanged",
                    changed_surfaces=[],
                    upstream_exit_code=None,
                    next_action_requires=["uv and write access to launcher-root .pinboard-runtime"],
                )
                self.assertFalse(sentinel.exists())
                self.assertEqual(before, tuple(private.rglob("*")))

    def test_mcp_rejects_every_additional_startup_argument_before_effect(self) -> None:
        for argument in ("--version", "--prepare-runtime", "status", "--project-root", "--mcp", ""):
            with self.subTest(argument=argument), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                launcher = self.copy_launcher(root)
                result = self.run_launcher(launcher, "--mcp", argument, path="/usr/bin:/bin")
                self.assertEqual(64, result.returncode)
                self.assertEqual("", result.stdout)
                payload = json.loads(result.stderr)
                self.assertEqual("invalid-startup-arguments", payload["status"])
                self.assertEqual("unchanged", payload["effect_disposition"])
                self.assertEqual([], payload["changed_surfaces"])
                self.assertFalse(payload["pinboard_started"])
                self.assertFalse((root / ".pinboard-runtime").exists())

    def test_preparation_does_not_mark_cli_only_runtime_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_uv(
                root,
                'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                "printf '#!/bin/sh\\nexit 0\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/pinboard\"\n"
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n',
            )
            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")
            self.assertEqual(78, result.returncode)
            payload = json.loads(result.stdout)
            self.assertEqual("runtime-entrypoint-invalid", payload["status"])
            self.assertIn("pinboard-mcp", result.stderr)
            self.assertFalse((root / ".pinboard-runtime" / ".pinboard-ready").exists())

    def test_ready_source_environment_bypasses_uv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            executable = root / ".venv" / "bin" / "pinboard"
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nprintf "source:%s\\n" "$*"\n', encoding="utf-8")
            executable.chmod(0o755)
            self.write_uv(root, 'printf "uv:%s\\n" "$*"\n')

            result = self.run_launcher(launcher, "status", "--json", path=f"{root}:/usr/bin:/bin")

            self.assertEqual(0, result.returncode)
            self.assertEqual("source:status --json\n", result.stdout)
            self.assertEqual("", result.stderr)

    def test_normal_launch_requires_explicit_preparation_without_invoking_uv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            sentinel = root / "uv-was-called"
            self.write_uv(root, f'touch "{sentinel}"\n')

            result = self.run_launcher(launcher, "status", "--json", path=f"{root}:/usr/bin:/bin")

            self.assert_result(
                result,
                status="runtime-preparation-required",
                retry="run-preparation",
                effect="unchanged",
                changed_surfaces=[],
                upstream_exit_code=None,
                next_action_requires=["uv and write access to launcher-root .pinboard-runtime"],
            )
            self.assertEqual("", result.stderr)
            self.assertFalse(sentinel.exists())

    def test_partial_private_runtime_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            executable = root / ".pinboard-runtime" / "environment" / "bin" / "pinboard"
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nprintf "partial\\n"\n', encoding="utf-8")
            executable.chmod(0o755)

            result = self.run_launcher(launcher, "status", path="/usr/bin:/bin")

            self.assert_result(
                result,
                status="runtime-preparation-required",
                retry="run-preparation",
                effect="unchanged",
                changed_surfaces=[],
                upstream_exit_code=None,
                next_action_requires=["uv and write access to launcher-root .pinboard-runtime"],
            )
            self.assertEqual("", result.stderr)

    def test_preparation_reports_missing_uv_without_changing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)

            result = self.run_launcher(launcher, "--prepare-runtime", path="/usr/bin:/bin")

            self.assert_result(
                result,
                status="uv-unavailable",
                retry="correct-environment",
                effect="unchanged",
                changed_surfaces=[],
                upstream_exit_code=None,
                next_action_requires=["uv"],
            )
            self.assertEqual("", result.stderr)
            self.assertFalse((root / ".pinboard-runtime").exists())

    def test_preparation_reports_unavailable_runtime_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            (root / ".pinboard-runtime").mkdir()
            self.write_uv(root, "exit 0\n")
            denied_rm = root / "rm"
            denied_rm.write_text('#!/bin/sh\nprintf "runtime write denied\\n" >&2\nexit 41\n', encoding="utf-8")
            denied_rm.chmod(0o755)

            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")

            self.assert_result(
                result,
                status="runtime-write-unavailable",
                retry="authorize-runtime-write",
                effect="potentially-changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=41,
                next_action_requires=["write access to launcher-root .pinboard-runtime"],
            )
            self.assertEqual("runtime write denied\n", result.stderr)

    def test_preparation_keeps_diagnostics_inside_private_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            (root / ".pinboard-runtime-preparation-output").mkdir()
            self.write_uv(
                root,
                'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                "printf '#!/bin/sh\\nexit 0\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/pinboard\"\n"
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n'
                'cp "$UV_PROJECT_ENVIRONMENT/bin/pinboard" "$UV_PROJECT_ENVIRONMENT/bin/pinboard-mcp"\n'
                'cp "$UV_PROJECT_ENVIRONMENT/bin/pinboard" "$UV_PROJECT_ENVIRONMENT/bin/pinboard-claude-subagent-start"\n',
            )

            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")

            self.assert_result(
                result,
                status="runtime-ready",
                retry="retry-original-command",
                effect="changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=None,
                next_action_requires=None,
                returncode=0,
            )
            self.assertEqual("", result.stderr)
            self.assertTrue((root / ".pinboard-runtime-preparation-output").is_dir())

    def test_preparation_reports_ready_marker_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_uv(
                root,
                'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                'mkdir "$READY_MARKER"\n'
                "printf '#!/bin/sh\\nexit 0\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/pinboard\"\n"
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n'
                'cp "$UV_PROJECT_ENVIRONMENT/bin/pinboard" "$UV_PROJECT_ENVIRONMENT/bin/pinboard-mcp"\n'
                'cp "$UV_PROJECT_ENVIRONMENT/bin/pinboard" "$UV_PROJECT_ENVIRONMENT/bin/pinboard-claude-subagent-start"\n',
            )

            result = self.run_launcher(
                launcher,
                "--prepare-runtime",
                path=f"{root}:/usr/bin:/bin",
                extra_environment={"READY_MARKER": str(root / ".pinboard-runtime" / ".pinboard-ready")},
            )
            upstream_exit_code = json.loads(result.stdout)["upstream_exit_code"]

            self.assertIsInstance(upstream_exit_code, int)
            self.assertGreaterEqual(upstream_exit_code, 1)
            self.assertLessEqual(upstream_exit_code, 255)
            self.assert_result(
                result,
                status="runtime-write-unavailable",
                retry="authorize-runtime-write",
                effect="potentially-changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=upstream_exit_code,
                next_action_requires=["write access to launcher-root .pinboard-runtime"],
            )

    def test_preparation_reports_failed_locked_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_uv(root, 'printf "sync stdout\\n"\nprintf "sync stderr\\n" >&2\nexit 23\n')

            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")

            self.assert_result(
                result,
                status="runtime-sync-failed",
                retry="correct-environment",
                effect="potentially-changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=23,
                next_action_requires=["a corrected uv environment/cache"],
            )
            self.assertEqual("sync stdout\nsync stderr\n", result.stderr)
            self.assertFalse((root / ".pinboard-runtime" / ".pinboard-ready").exists())

    def test_preparation_reports_invalid_private_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_uv(
                root,
                'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                'printf \'#!/bin/sh\\nprintf "invalid stdout\\n"\\nprintf "invalid stderr\\n" >&2\\nexit 37\\n\' '
                '> "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n'
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n',
            )

            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")

            self.assert_result(
                result,
                status="runtime-entrypoint-invalid",
                retry="correct-environment",
                effect="potentially-changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=37,
                next_action_requires=["a valid locked runtime"],
            )
            self.assertEqual("invalid stdout\ninvalid stderr\n", result.stderr)
            self.assertFalse((root / ".pinboard-runtime" / ".pinboard-ready").exists())

    def test_preparation_preserves_missing_private_entrypoint_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            self.write_uv(root, "exit 0\n")

            result = self.run_launcher(launcher, "--prepare-runtime", path=f"{root}:/usr/bin:/bin")
            executable = root / ".pinboard-runtime" / "environment" / "bin" / "pinboard"
            upstream_exit_code = json.loads(result.stdout)["upstream_exit_code"]

            self.assertIsInstance(upstream_exit_code, int)
            self.assertGreaterEqual(upstream_exit_code, 1)
            self.assertLessEqual(upstream_exit_code, 255)
            self.assert_result(
                result,
                status="runtime-entrypoint-invalid",
                retry="correct-environment",
                effect="potentially-changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=upstream_exit_code,
                next_action_requires=["a valid locked runtime"],
            )
            self.assertIn(str(executable), result.stderr)
            self.assertNotIn("prepared Pinboard entry point is not executable", result.stderr)
            self.assertFalse((root / ".pinboard-runtime" / ".pinboard-ready").exists())

    def test_preparation_creates_and_reuses_private_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self.copy_launcher(root)
            trace = root / "uv-trace"
            self.write_uv(
                root,
                'printf "%s\\n%s\\n" "$*" "$UV_PROJECT_ENVIRONMENT" > "$TRACE_FILE"\n'
                'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
                'printf \'#!/bin/sh\\nif [ "$1" = "--version" ]; then printf "pinboard 0.1.0\\n"; exit 0; fi\\n'
                'printf "private:%%s\\n" "$*"\\n\' > "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n'
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard"\n'
                "printf '#!/bin/sh\\nexit 99\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/pinboard-mcp\"\n"
                'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/pinboard-mcp"\n'
                'cp "$UV_PROJECT_ENVIRONMENT/bin/pinboard-mcp" "$UV_PROJECT_ENVIRONMENT/bin/pinboard-claude-subagent-start"\n',
            )
            environment = {"TRACE_FILE": str(trace)}

            prepared = self.run_launcher(
                launcher,
                "--prepare-runtime",
                path=f"{root}:/usr/bin:/bin",
                extra_environment=environment,
            )

            self.assert_result(
                prepared,
                status="runtime-ready",
                retry="retry-original-command",
                effect="changed",
                changed_surfaces=[".pinboard-runtime"],
                upstream_exit_code=None,
                next_action_requires=None,
                returncode=0,
            )
            self.assertEqual("", prepared.stderr)
            self.assertEqual(
                f"sync --locked --no-dev --project {root}\n{root / '.pinboard-runtime' / 'environment'}\n",
                trace.read_text(encoding="utf-8"),
            )
            self.assertTrue((root / ".pinboard-runtime" / ".pinboard-ready").is_file())
            self.assertFalse((root / ".pinboard-runtime" / ".preparation-output").exists())

            launched = self.run_launcher(
                launcher,
                "status",
                "--json",
                path="/usr/bin:/bin",
                extra_environment={"UV_CACHE_DIR": str(root / "unusable-cache")},
            )
            self.assertEqual(0, launched.returncode)
            self.assertEqual("private:status --json\n", launched.stdout)
            self.assertEqual("", launched.stderr)

            already_ready = self.run_launcher(launcher, "--prepare-runtime", path="/usr/bin:/bin")
            self.assert_result(
                already_ready,
                status="runtime-already-ready",
                retry="retry-original-command",
                effect="unchanged",
                changed_surfaces=[],
                upstream_exit_code=None,
                next_action_requires=None,
                returncode=0,
            )
            self.assertEqual("", already_ready.stderr)


if __name__ == "__main__":
    unittest.main()
