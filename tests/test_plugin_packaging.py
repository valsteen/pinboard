import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def copied_repository_payload(destination: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    for raw_path in listed.split(b"\0"):
        if not raw_path:
            continue
        relative_path = Path(os.fsdecode(raw_path))
        source = ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def tree_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


class PluginPackagingTests(unittest.TestCase):
    def test_copied_plugin_launcher_runs_complete_no_model_workflow_without_mutating_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            plugin_root = sandbox / "copied-plugin"
            plugin_root.mkdir()
            copied_repository_payload(plugin_root)
            before = tree_fingerprint(plugin_root)

            project = sandbox / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True, text=True)
            proposal_path = sandbox / "proposal.json"
            proposal_path.write_text(
                json.dumps(
                    {
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
                ),
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "PINBOARD_RUNTIME": "claude",
            }
            launcher = plugin_root / "scripts" / "pinboard"

            metadata_validation = subprocess.run(
                [sys.executable, str(plugin_root / "scripts" / "validate-metadata.py")],
                cwd=sandbox,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Codex and Claude plugins", metadata_validation.stdout)

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
            run(
                "proposal",
                "--file",
                str(proposal_path),
                "--task-id",
                "claude-session",
                "--host-id",
                "local",
            )
            item = json.loads(run("item", "status", "--item-id", "packaged-proposal", "--json").stdout)
            validation = json.loads(run("validate", "--json").stdout)
            reopened = run("init")

            self.assertEqual("intake", item["state"])
            self.assertTrue(validation["valid"])
            self.assertNotIn("Optional next steps", reopened.stdout)
            self.assertTrue((project / ".codex" / "pinboard" / "state.sqlite3").is_file())
            self.assertEqual(before, tree_fingerprint(plugin_root))


if __name__ == "__main__":
    unittest.main()
