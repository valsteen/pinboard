import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def copied_repository_payload(source_root: Path, destination: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=source_root,
        check=True,
        capture_output=True,
    ).stdout
    for raw_path in listed.split(b"\0"):
        if not raw_path:
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
            (source / "untracked.txt").write_text("untracked", encoding="utf-8")
            subprocess.run(
                ["git", "add", tracked.name, executable.name, link.name],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )

            destination = sandbox / "destination"
            destination.mkdir()
            copied_repository_payload(source, destination)
            self.assertFalse((destination / "untracked.txt").exists())
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

    def test_copied_plugin_launcher_runs_complete_no_model_workflow_without_mutating_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            plugin_root = sandbox / "copied-plugin"
            plugin_root.mkdir()
            copied_repository_payload(ROOT, plugin_root)
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
