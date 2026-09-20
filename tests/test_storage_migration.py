import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.legacy_storage import StorageLocation, observe_storage_location
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, JsonObject, JsonValue


class StorageMigrationTests(unittest.TestCase):
    def run_cli(self, project: Path, *arguments: str) -> tuple[int, JsonObject]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(("--project-root", str(project), *arguments, "--json"))
        return code, json.loads(output.getvalue())

    def initialize_legacy(self, project: Path) -> Path:
        legacy = project / ".codex" / "pinboard"
        self.assertEqual(0, self.run_cli(project, "--work-root", str(legacy), "init")[0])
        return legacy

    def test_default_root_and_migration_state_matrix(self) -> None:
        for state in ("fresh", "legacy", "current", "alias", "conflict", "wrong-alias"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
                legacy = project / ".codex" / "pinboard"
                current = project / ".pinboard"
                if state == "legacy":
                    self.initialize_legacy(project)
                elif state in {"current", "alias", "conflict", "wrong-alias"}:
                    self.assertEqual(0, self.run_cli(project, "init")[0])
                    if state == "alias":
                        legacy.parent.mkdir()
                        legacy.symlink_to("../.pinboard", target_is_directory=True)
                    elif state == "conflict":
                        legacy.mkdir(parents=True)
                    elif state == "wrong-alias":
                        legacy.parent.mkdir()
                        legacy.symlink_to("../elsewhere", target_is_directory=True)
                self.assertEqual(
                    {
                        "fresh": StorageLocation.FRESH,
                        "legacy": StorageLocation.LEGACY,
                        "current": StorageLocation.CURRENT,
                        "alias": StorageLocation.ALIASED,
                        "conflict": StorageLocation.CONFLICT,
                        "wrong-alias": StorageLocation.CONFLICT,
                    }[state],
                    observe_storage_location(project),
                )
                code, result = self.run_cli(project, "migrate-work-root")
                if state in {"legacy", "current", "alias"}:
                    self.assertEqual(0, code, result)
                    self.assertEqual("pinboard-work-root-migration/v1", result["schema"])
                    self.assertEqual(str(current), result["work_root"])
                    self.assertEqual(Path("../.pinboard"), legacy.readlink())
                else:
                    self.assertEqual(11, code, result)
                    self.assertEqual("rejected", result["status"])
                    self.assertFalse(result["state_changed"])

    def test_fresh_init_and_legacy_recovery_use_the_neutral_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            code, created = self.run_cli(project, "init")
            self.assertEqual(0, code, created)
            self.assertEqual(str(project / ".pinboard"), created["work_root"])
            self.assertFalse((project / ".codex").exists())
            self.assertIn("/.pinboard/", (project / ".git" / "info" / "exclude").read_text().splitlines())

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            self.initialize_legacy(project)
            for command in ("init", "status"):
                code, result = self.run_cli(project, command)
                self.assertEqual(11, code, result)
                self.assertEqual("WORK_ROOT_MIGRATION_REQUIRED", result["code"])
                observed = result["observed"]
                assert isinstance(observed, list)
                observations: dict[str, JsonValue] = {}
                for value in observed:
                    assert isinstance(value, dict)
                    observations[str(value["field"])] = value["value"]
                self.assertEqual("pinboard migrate-work-root", observations["recovery_command"])
                self.assertFalse((project / ".pinboard").exists())

            contract_code, contract = self.run_cli(project, "tool-contract", "--operation", "migrate-work-root")
            self.assertEqual(0, contract_code, contract)
            self.assertEqual("migrates-work-root", contract["mutation_class"])
            self.assertEqual("inspect-current-state-before-retry", contract["retry_semantics"])

    def test_migration_preserves_populated_state_artifacts_and_unrelated_codex_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            (legacy.parent / "config.toml").write_text("unrelated\n")
            exclude = project / ".git" / "info" / "exclude"
            with exclude.open("ab") as stream:
                stream.write(b"/.codex/pinboard/\n")
            roots = resolve_durable_roots(project, legacy)
            publication = ArtifactRepository(roots).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "kept", 1, ".txt", b"immutable\n")
            )
            store = SQLiteWorkStore(roots.database_path)
            expect_success(store.accept_artifact_reference(legacy, publication.reference, SQLITE_NOW))
            before = store.validated_snapshot()
            database_bytes = roots.database_path.read_bytes()
            code, result = self.run_cli(project, "migrate-work-root")
            self.assertEqual(0, code, result)
            self.assertEqual(["repository-git-exclude", "work-root", "compatibility-alias"], result["changed_surfaces"])
            current = project / ".pinboard"
            self.assertEqual(database_bytes, (current / "state.sqlite3").read_bytes())
            self.assertEqual(before, SQLiteWorkStore(current / "state.sqlite3").validated_snapshot())
            self.assertEqual(b"immutable\n", (current / publication.reference.selector).read_bytes())
            self.assertEqual("unrelated\n", (legacy.parent / "config.toml").read_text())
            for path in (".pinboard/state.sqlite3", ".codex/pinboard"):
                self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", path], cwd=project).returncode)
            exclude_lines = exclude.read_text().splitlines()
            self.assertIn("/.codex/pinboard/", exclude_lines)
            self.assertIn("/.pinboard/", exclude_lines)

    def test_partial_move_and_alias_failures_report_exact_effects_and_repair(self) -> None:
        for operation, expected in (
            ("rename", ["repository-git-exclude"]),
            ("symlink_to", ["repository-git-exclude", "work-root"]),
        ):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
                legacy = self.initialize_legacy(project)
                before = (legacy / "state.sqlite3").read_bytes()
                with patch.object(Path, operation, side_effect=PermissionError("injected failure")):
                    code, failure = self.run_cli(project, "migrate-work-root")
                self.assertEqual(11, code, failure)
                self.assertEqual(expected, failure["changed_surfaces"])
                self.assertEqual(("committed-effect", "do-not-retry"), (failure["status"], failure["retry"]))
                self.assertEqual(0, self.run_cli(project, "migrate-work-root")[0])
                self.assertEqual(before, (project / ".pinboard" / "state.sqlite3").read_bytes())

    def test_target_only_alias_failure_leaves_no_alias_effect_or_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])

            with patch.object(Path, "symlink_to", side_effect=PermissionError("injected failure")):
                code, failure = self.run_cli(project, "migrate-work-root")

            self.assertEqual(11, code, failure)
            self.assertEqual([], failure["changed_surfaces"])
            self.assertEqual(("rejected", "correct-input"), (failure["status"], failure["retry"]))
            self.assertFalse((project / ".codex").exists())

    def test_exact_alias_is_canonical_and_arbitrary_symlink_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, external = root / "project", root / "external"
            project.mkdir()
            external.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            legacy = project / ".codex" / "pinboard"
            legacy.parent.mkdir()
            legacy.symlink_to("../.pinboard", target_is_directory=True)
            self.assertEqual(project / ".pinboard", resolve_durable_roots(project, legacy).work_root)
            linked_parent = project / "linked-parent"
            linked_parent.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(Exception, "DIRECTORY_INVALID"):
                resolve_durable_roots(project, linked_parent / "work")

    def test_repository_alias_preserves_explicit_legacy_root_through_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real_parent = root / "real-parent"
            spelled_parent = root / "spelled-parent"
            real_parent.mkdir()
            spelled_parent.symlink_to(real_parent, target_is_directory=True)
            project = real_parent / "project"
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            spelled_project = spelled_parent / "project"
            spelled_legacy = spelled_project / ".codex" / "pinboard"

            init_code, init_result = self.run_cli(
                spelled_project,
                "--work-root",
                str(spelled_legacy),
                "init",
            )
            self.assertEqual(0, init_code, init_result)
            migration_code, migration = self.run_cli(spelled_project, "migrate-work-root")
            self.assertEqual(0, migration_code, migration)

            current = project / ".pinboard"
            legacy = project / ".codex" / "pinboard"
            self.assertTrue((current / "state.sqlite3").is_file())
            self.assertEqual(Path("../.pinboard"), legacy.readlink())
            for path in (".pinboard/state.sqlite3", ".codex/pinboard"):
                self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", path], cwd=project).returncode)
            reopen_code, reopen = self.run_cli(spelled_project, "status")
            self.assertEqual(0, reopen_code, reopen)


if __name__ == "__main__":
    unittest.main()
