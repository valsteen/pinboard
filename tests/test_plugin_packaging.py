import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.support import JsonObject

ROOT = Path(__file__).resolve().parent.parent


def subagent_start_event() -> dict[str, str]:
    return {
        "agent_id": "native-worker",
        "agent_type": "general-purpose",
        "cwd": "/sensitive/native-project",
        "hook_event_name": "SubagentStart",
        "session_id": "sensitive-parent-session",
        "transcript_path": "/sensitive/native-transcript.jsonl",
    }


def copied_repository_payload(source_root: Path, destination: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=source_root,
        check=True,
        capture_output=True,
    ).stdout
    deleted = set(
        subprocess.run(
            ["git", "ls-files", "-z", "--deleted"],
            cwd=source_root,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
    )
    for raw_path in listed.split(b"\0"):
        if not raw_path or raw_path in deleted:
            continue
        relative_path = Path(os.fsdecode(raw_path))
        source = source_root / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        else:
            shutil.copy2(source, target)


def tree_fingerprint(root: Path) -> tuple[tuple[str, str, int, str, str], ...]:
    entries: list[tuple[str, str, int, str, str]] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind, target, content = "symlink", str(path.readlink()), ""
        elif stat.S_ISREG(metadata.st_mode):
            kind, target, content = "file", "", hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISDIR(metadata.st_mode):
            kind, target, content = "directory", "", ""
        else:
            raise ValueError(f"unsupported copied payload entry: {path}")
        entries.append((path.relative_to(root).as_posix(), kind, stat.S_IMODE(metadata.st_mode), target, content))
    return tuple(entries)


class PluginPackagingTests(unittest.TestCase):
    def test_installed_native_hook_delivers_only_own_identity_and_rejects_invalid_events(self) -> None:
        event = subagent_start_event()
        invalid: list[dict[str, str | int] | list[str]] = [
            [],
            {**event, "unexpected": "sensitive-extra"},
            {**event, "hook_event_name": "SubagentStop"},
            {**event, "agent_id": ""},
            {**event, "agent_id": "parent/worker"},
            {**event, "agent_id": "worker\nspoofed context"},
            {**event, "agent_id": "worker\u0000"},
            {**event, "agent_id": " worker"},
            {**event, "agent_id": 47},
        ]
        invalid.extend({key: value for key, value in event.items() if key != missing} for missing in event)
        invalid.extend({**event, key: 47} for key in event if key != "agent_id")
        payloads = [json.dumps(event), *(json.dumps(value) for value in invalid), "{"]
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                result = subprocess.run(
                    [str(ROOT / "scripts" / "pinboard"), "--claude-subagent-start"],
                    input=payload,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if index == 0:
                    self.assertEqual(0, result.returncode, result.stderr)
                    output = json.loads(result.stdout)
                    self.assertEqual({"hookSpecificOutput"}, set(output))
                    context = output["hookSpecificOutput"]
                    self.assertEqual({"hookEventName", "additionalContext"}, set(context))
                    self.assertEqual("SubagentStart", context["hookEventName"])
                    self.assertIn(json.dumps(event["agent_id"]), context["additionalContext"])
                    self.assertEqual("", result.stderr)
                else:
                    self.assertEqual(1, result.returncode, result.stderr)
                    self.assertEqual("", result.stdout)
                    self.assertLess(len(result.stderr), 256)
                for key in ("cwd", "session_id", "transcript_path"):
                    self.assertNotIn(event[key], result.stdout + result.stderr)
                self.assertNotIn("sensitive-extra", result.stdout + result.stderr)

    def assert_configured_mcp_reads(
        self, sandbox: Path, plugin_root: Path, project: Path, environment: dict[str, str], proposal: JsonObject
    ) -> None:
        async def scenario() -> None:
            for manifest_index, manifest_path in enumerate((".codex-plugin/plugin.json", ".claude-plugin/plugin.json")):
                manifest = json.loads((plugin_root / manifest_path).read_bytes())
                config_path = plugin_root / manifest["mcpServers"]
                self.assertEqual((ROOT / config_path.relative_to(plugin_root)).read_bytes(), config_path.read_bytes())
                config = json.loads(config_path.read_bytes())["mcpServers"]["pinboard"]
                command = config["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                cwd = plugin_root / config["cwd"] if "cwd" in config else None
                parameters = StdioServerParameters(command=command, args=config["args"], cwd=cwd, env=environment)
                with chdir(sandbox):
                    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        self.assertEqual("pinboard", initialized.server_info.name)
                        discovered = await session.list_tools()
                        self.assertEqual(19, len(discovered.tools))
                        if manifest_index == 0:
                            created = await session.call_tool(
                                "pinboard_proposal_create",
                                {
                                    "project_root": str(project),
                                    "work_root": str(project / ".codex" / "pinboard"),
                                    "proposal": proposal,
                                    "actor_task_id": "claude-session",
                                    "actor_host_id": "local",
                                },
                            )
                            self.assertFalse(created.is_error)
                            assert created.structured_content is not None
                            self.assertEqual("committed", created.structured_content["status"])
                        result = await session.call_tool(
                            "pinboard_item_status",
                            {
                                "project_root": str(project),
                                "work_root": str(project / ".codex" / "pinboard"),
                                "item_id": "packaged-proposal",
                            },
                        )
                        self.assertFalse(result.is_error)
                        self.assertIsNotNone(result.structured_content)
                        assert result.structured_content is not None
                        self.assertEqual("intake", result.structured_content["state"])

        asyncio.run(scenario())
        origin = subprocess.run(
            [
                str(plugin_root / ".pinboard-runtime" / "environment" / "bin" / "python"),
                "-c",
                "import importlib.metadata, pinboard, sys; print(pinboard.__file__); "
                "print(importlib.metadata.distribution('pinboard').locate_file('')); "
                "print(f'prefix={sys.prefix!r} executable={sys.executable!r} sys.path={sys.path!r}', file=sys.stderr)",
            ],
            env={**environment, "PYTHONDONTWRITEBYTECODE": "1"},
            check=True,
            capture_output=True,
            text=True,
        )
        module_origin, installation_origin = map(Path, origin.stdout.splitlines())
        provenance = (
            f"module_origin={module_origin!s} installation_origin={installation_origin!s} "
            f"expected_module_root={plugin_root / 'src'!s} "
            f"expected_runtime_root={plugin_root / '.pinboard-runtime' / 'environment'!s}; stderr={origin.stderr!r}"
        )
        self.assertTrue(module_origin.is_relative_to(plugin_root / "src"), provenance)
        self.assertTrue(
            installation_origin.is_relative_to(plugin_root / ".pinboard-runtime" / "environment"), provenance
        )
        self.assertEqual(
            (ROOT / "scripts" / "pinboard").read_bytes(), (plugin_root / "scripts" / "pinboard").read_bytes()
        )

    def test_metadata_rejects_invalid_mcp_configuration_and_missing_assets(self) -> None:
        manifest_path = ".claude-plugin/plugin.json"
        manifest = json.loads((ROOT / manifest_path).read_bytes())
        missing_hooks = {key: value for key, value in manifest.items() if key != "hooks"}
        changes = (
            (manifest_path, None),
            (manifest_path, json.dumps(missing_hooks)),
            (manifest_path, json.dumps({**manifest, "hooks": "./hooks/hooks.json"})),
            (manifest_path, json.dumps({**manifest, "hooks": "./hooks/missing.json"})),
            (manifest_path, json.dumps({**manifest, "hooks": {}})),
            (manifest_path, json.dumps({**manifest, "hooks": None})),
            ("hooks/hooks.json", (ROOT / manifest["hooks"]).read_text(encoding="utf-8")),
            ("mcp-codex.json", '{"mcpServers":{"pinboard":{"command":"sh","args":["--mcp"],"cwd":"."}}}'),
            (
                "mcp-codex.json",
                '{"mcpServers":{"pinboard":{"command":"sh","args":["./scripts/pinboard","--mcp"],"cwd":".","unknown":true}}}',
            ),
            (
                "mcp-codex.json",
                '{"mcpServers":{"pinboard":{"command":"sh","args":["./scripts/pinboard","--mcp"],"cwd":".."}}}',
            ),
            (
                "mcp-codex.json",
                '{"mcpServers":{"pinboard":{"command":"sh","args":["./scripts/pinboard","--mcp"],"cwd":"."},"extra":{}}}',
            ),
            ("mcp-claude.json", '{"mcpServers":{"pinboard":{"command":"scripts/pinboard","args":["--mcp"]}}}'),
            (
                "mcp-claude.json",
                '{"mcpServers":{"pinboard":{"command":"${CLAUDE_PLUGIN_ROOT}/scripts/pinboard","args":["--mcp","--version"]}}}',
            ),
            (
                "mcp-claude.json",
                '{"mcpServers":{"pinboard":{"command":"${CLAUDE_PLUGIN_ROOT}/scripts/pinboard","args":["--mcp"],"cwd":"."}}}',
            ),
            ("mcp-claude.json", "[]"),
            ("mcp-codex.json", "{"),
            ("mcp-codex.json", None),
            ("mcp-claude.json", None),
            ("scripts/pinboard", None),
            ("hooks/claude-hooks.json", None),
            ("hooks/claude-hooks.json", '{"hooks":{"SubagentStart":[]}}'),
            ("hooks/claude-hooks.json", '{"hooks":{"SubagentStop":[]}}'),
            (
                "hooks/claude-hooks.json",
                '{"hooks":{"SubagentStart":[{"matcher":".*","hooks":[{"type":"command",'
                '"command":"scripts/pinboard --claude-subagent-start"}]}]}}',
            ),
            (
                "hooks/claude-hooks.json",
                '{"hooks":{"SubagentStart":[{"matcher":".*","hooks":[{"type":"command",'
                '"command":"\\"${CLAUDE_PLUGIN_ROOT}/scripts/pinboard\\" --claude-subagent-start",'
                '"async":true}]}]}}',
            ),
        )
        for relative, content in changes:
            with self.subTest(relative=relative, content=content), tempfile.TemporaryDirectory() as directory:
                plugin = Path(directory)
                copied_repository_payload(ROOT, plugin)
                target = plugin / relative
                if content is None:
                    target.unlink()
                else:
                    target.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(plugin / "scripts" / "validate-metadata.py")],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)

    def test_copy_and_fingerprint_cover_only_tracked_payload_entry_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            source = sandbox / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True, text=True)
            tracked = source / "tracked.txt"
            tracked.write_text("tracked", encoding="utf-8")
            executable = source / "tool"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            link = source / "tracked-link"
            link.symlink_to(tracked.name)
            removed = source / "removed.txt"
            removed.write_text("retired tracked payload", encoding="utf-8")
            (source / "untracked.txt").write_text("untracked", encoding="utf-8")
            subprocess.run(
                ["git", "add", tracked.name, executable.name, link.name, removed.name],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            removed.unlink()

            destination = sandbox / "destination"
            destination.mkdir()
            copied_repository_payload(source, destination)
            self.assertFalse((destination / "untracked.txt").exists())
            self.assertFalse((destination / removed.name).exists())
            self.assertTrue((destination / link.name).is_symlink())
            self.assertEqual(Path(tracked.name), (destination / link.name).readlink())

            baseline = tree_fingerprint(destination)
            (destination / tracked.name).chmod(0o600)
            mode_changed = tree_fingerprint(destination)
            self.assertNotEqual(baseline, mode_changed)
            (destination / tracked.name).write_text("changed", encoding="utf-8")
            content_changed = tree_fingerprint(destination)
            self.assertNotEqual(mode_changed, content_changed)
            (destination / link.name).unlink()
            (destination / link.name).symlink_to(executable.name)
            self.assertNotEqual(content_changed, tree_fingerprint(destination))

    def prepare_copied_launcher(
        self, sandbox: Path, plugin_root: Path, project: Path
    ) -> tuple[Path, dict[str, str], tuple[tuple[str, str, int, str, str], ...]]:
        environment = {**os.environ, "PINBOARD_RUNTIME": "claude"}
        launcher = plugin_root / "scripts" / "pinboard"
        metadata_validation = subprocess.run(
            [sys.executable, str(plugin_root / "scripts" / "validate-metadata.py")],
            cwd=sandbox,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Codex and Claude plugins", metadata_validation.stdout)

        missing_runtime = subprocess.run(
            [str(launcher), "--project-root", str(project), "status", "--json"],
            cwd=sandbox,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(78, missing_runtime.returncode)
        self.assertEqual("", missing_runtime.stderr)
        missing_payload = json.loads(missing_runtime.stdout)
        self.assertEqual("pinboard-launcher-result/v1", missing_payload["schema"])
        self.assertEqual("runtime-preparation-required", missing_payload["status"])
        self.assertEqual("run-preparation", missing_payload["retry_disposition"])
        self.assertEqual("unchanged", missing_payload["effect_disposition"])
        self.assertEqual(["--prepare-runtime"], missing_payload["next_action"]["arguments"])
        self.assertEqual("scripts/pinboard --prepare-runtime", missing_payload["next_action"]["display_command"])

        prepared = subprocess.run(
            [str(launcher), *missing_payload["next_action"]["arguments"]],
            cwd=sandbox,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        self.assertEqual("", prepared.stderr)
        prepared_payload = json.loads(prepared.stdout)
        self.assertEqual("runtime-ready", prepared_payload["status"])
        self.assertEqual([".pinboard-runtime"], prepared_payload["changed_surfaces"])
        self.assertTrue((plugin_root / ".pinboard-runtime" / ".pinboard-ready").is_file())

        unusable_cache = sandbox / "unusable-cache"
        unusable_cache.write_text("not a directory", encoding="utf-8")
        environment = {**environment, "PATH": "/usr/bin:/bin", "UV_CACHE_DIR": str(unusable_cache)}
        version = subprocess.run(
            [str(launcher), "--version"],
            cwd=sandbox,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, version.returncode, version.stderr)
        self.assertEqual("0.1.0\n", version.stdout)
        self.assertEqual("", version.stderr)
        return launcher, environment, tree_fingerprint(plugin_root)

    def test_copied_plugin_launcher_runs_complete_no_model_workflow_without_mutating_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            plugin_root = sandbox / "copied plugin"
            plugin_root.mkdir()
            copied_repository_payload(ROOT, plugin_root)

            project = sandbox / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True, text=True)
            decoy_executable = project / ".venv" / "bin" / "pinboard"
            decoy_executable.parent.mkdir(parents=True)
            decoy_executable.write_text(
                '#!/bin/sh\nprintf "managed-project-runtime-used\\n"\nexit 99\n', encoding="utf-8"
            )
            decoy_executable.chmod(0o755)
            managed_pyproject = project / "pyproject.toml"
            managed_pyproject.write_text("[project]\nname = 'managed-project'\n", encoding="utf-8")
            managed_lock = project / "uv.lock"
            managed_lock.write_text("managed project lock\n", encoding="utf-8")
            managed_dependency_bytes = (
                decoy_executable.read_bytes(),
                managed_pyproject.read_bytes(),
                managed_lock.read_bytes(),
            )
            proposal: JsonObject = {
                "schema": "pinboard-proposal/v1",
                "proposal_id": "packaged-proposal",
                "created_at": "2026-09-06T12:00:00Z",
                "source_task_id": "claude-session",
                "user_label": "Packaged proposal",
                "trigger": "Exercise the copied launcher through a persisted mutation.",
                "evidence": ["source:packaging-smoke"],
                "why_it_matters": "The packaged workflow must retain one shared SQLite authority.",
                "relation": {"kind": "independent", "item": None},
                "effect": "One intake item is stored by the existing engine.",
                "unlock": "The copied plugin can run the supported workflow.",
                "urgency_evidence": "This is the packaging compatibility boundary.",
                "freshness_assumptions": ["The disposable repository began empty."],
            }
            launcher, environment, before = self.prepare_copied_launcher(sandbox, plugin_root, project)
            claude_manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_bytes())
            hook_configuration = json.loads((plugin_root / claude_manifest["hooks"]).read_bytes())
            hook_command = hook_configuration["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
            hook = subprocess.run(
                hook_command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)),
                shell=True,
                input=json.dumps(subagent_start_event()),
                cwd=project,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, hook.returncode, hook.stderr)
            context = json.loads(hook.stdout)["hookSpecificOutput"]
            self.assertEqual("SubagentStart", context["hookEventName"])
            self.assertIn(json.dumps(subagent_start_event()["agent_id"]), context["additionalContext"])
            self.assertEqual("", hook.stderr)

            def run(*arguments: str) -> subprocess.CompletedProcess[str]:
                result = subprocess.run(
                    [str(launcher), "--project-root", str(project), *arguments],
                    cwd=sandbox,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                return result

            initialized = run("init")
            self.assertNotIn("model_auto_compact_token_limit_scope", initialized.stdout)
            self.assert_configured_mcp_reads(sandbox, plugin_root, project, environment, proposal)
            validation = json.loads(run("validate", "--json").stdout)
            reopened = run("init")
            self.assertTrue(validation["valid"])
            self.assertNotIn("Optional next steps", reopened.stdout)
            self.assertTrue((project / ".codex" / "pinboard" / "state.sqlite3").is_file())
            self.assertEqual(before, tree_fingerprint(plugin_root))
            self.assertEqual(
                managed_dependency_bytes,
                (decoy_executable.read_bytes(), managed_pyproject.read_bytes(), managed_lock.read_bytes()),
            )


if __name__ == "__main__":
    unittest.main()
