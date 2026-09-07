import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

import msgspec

from pinboard.interfaces.brief_source_models import BriefSourceManifest, BriefSourceRequest
from pinboard.interfaces.brief_sources import (
    decode_brief_source_manifest,
    plan_brief_sources,
    render_brief_source_batch,
)
from pinboard.interfaces.cli import main
from pinboard.interfaces.errors import BriefSourceErrorCode, BriefSourceFailure, BriefSourceResult


def expect_brief_source_success[T](result: BriefSourceResult[T]) -> T:
    if isinstance(result, BriefSourceFailure):
        raise AssertionError(str(result))
    return result


def expect_brief_source_failure[T](result: BriefSourceResult[T], code: BriefSourceErrorCode) -> BriefSourceFailure:
    if not isinstance(result, BriefSourceFailure):
        raise AssertionError(f"Expected {code.value}, received success: {result!r}")
    if result.code != code:
        raise AssertionError(f"Expected {code.value}, received {result.code.value}: {result.message}")
    return result


class BriefSourcesTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def manifest(self, *sources: BriefSourceRequest) -> BriefSourceManifest:
        return BriefSourceManifest(schema="pinboard-brief-sources/v1", sources=sources)

    def run_git(self, cwd: Path, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)

    def test_manifest_boundary_rejects_unknown_fields_duplicates_and_unsafe_selectors(self) -> None:
        cases = (
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md","families":["contract"],"extra":true}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md","families":["contract"]},{"authority_id":"a","selector":"b.md","families":["acceptance"]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"../a.md","families":["contract"]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md","families":[]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a\\n","selector":"a.md","families":["contract"]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md\\n","families":["contract"]}]}',
        )

        for raw in cases:
            with self.subTest(raw=raw):
                expect_brief_source_failure(decode_brief_source_manifest(raw), BriefSourceErrorCode.MANIFEST_INVALID)

    def test_plan_normalizes_heading_bytes_and_batches_every_selected_byte_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "architecture.md").write_bytes(
                b"# Architecture\r\n\r\n## Contract\r\n\r\nSelected.\r\n\r\n## Sibling\r\nExcluded.\r\n"
            )
            (project / "acceptance.txt").write_bytes(b"first line\nsecond line\n")
            plan = expect_brief_source_success(
                plan_brief_sources(
                    project,
                    self.manifest(
                        BriefSourceRequest("architecture", "architecture.md#Contract", ("contract",)),
                        BriefSourceRequest("acceptance", "acceptance.txt", ("acceptance",)),
                    ),
                    max_batch_bytes=24,
                )
            )

        selected = b"## Contract\n\nSelected.\n\n"
        self.assertEqual(selected, b"".join(segment.content for segment in plan.sources[0].segments))
        self.assertEqual(hashlib.sha256(selected).hexdigest(), plan.sources[0].selected_sha256)
        self.assertEqual((3, 6), (plan.sources[0].start_line, plan.sources[0].end_line))
        self.assertEqual(
            b"first line\nsecond line\n",
            b"".join(segment.content for segment in plan.sources[1].segments),
        )
        self.assertTrue(all(batch.content_byte_count <= 24 for batch in plan.batches))
        self.assertEqual(tuple(range(len(plan.batches))), tuple(batch.index for batch in plan.batches))

    def test_plan_rejects_overlap_non_utf8_oversized_lines_and_unknown_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_text("# Source\n\n## Contract\n\nSelected.\n", encoding="utf-8")
            overlapping = self.manifest(
                BriefSourceRequest("whole", "source.md", ("contract",)),
                BriefSourceRequest("section", "source.md#Contract", ("acceptance",)),
            )
            expect_brief_source_failure(
                plan_brief_sources(project, overlapping, max_batch_bytes=128),
                BriefSourceErrorCode.SELECTOR_OVERLAP,
            )

            (project / "binary.dat").write_bytes(b"\xff\xfe")
            expect_brief_source_failure(
                plan_brief_sources(
                    project,
                    self.manifest(BriefSourceRequest("binary", "binary.dat", ("contract",))),
                    max_batch_bytes=128,
                ),
                BriefSourceErrorCode.SOURCE_NOT_UTF8,
            )

            expect_brief_source_failure(
                plan_brief_sources(
                    project,
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    max_batch_bytes=8,
                ),
                BriefSourceErrorCode.LINE_TOO_LARGE,
            )

            plan = expect_brief_source_success(
                plan_brief_sources(
                    project,
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    max_batch_bytes=128,
                )
            )
            expect_brief_source_failure(render_brief_source_batch(plan, 1), BriefSourceErrorCode.BATCH_NOT_FOUND)

    def test_cli_plans_and_emits_without_work_state_or_project_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_text("# Source\n\nBody.\n", encoding="utf-8")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    order="sorted",
                )
            )
            before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
            common = ("--project-root", str(project), "brief-sources", "--file", str(manifest_path))

            planned_result, planned_stdout, planned_stderr = self.run_cli(*common, "--json")
            emitted_result, emitted_stdout, emitted_stderr = self.run_cli(*common, "--emit-batch", "0")
            missing_result, _, missing_stderr = self.run_cli(*common, "--emit-batch", "1")
            after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}

        self.assertEqual((0, ""), (planned_result, planned_stderr))
        plan = json.loads(planned_stdout)
        self.assertEqual("pinboard-brief-source-plan/v1", plan["schema"])
        self.assertEqual(24_000, plan["max_batch_bytes"])
        self.assertEqual((0, ""), (emitted_result, emitted_stderr))
        self.assertIn("BEGIN BRIEF SOURCE authority=source selector=source.md lines=1-3 segment=0", emitted_stdout)
        self.assertEqual(15, missing_result)
        self.assertIn(BriefSourceErrorCode.BATCH_NOT_FOUND.value, missing_stderr)
        self.assertEqual(before, after)

    def test_cli_reads_authority_bytes_from_the_selected_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            linked = root / "linked"
            repository.mkdir()
            self.run_git(repository, "init", "-b", "main")
            (repository / "authority.md").write_text("linked authority\n", encoding="utf-8")
            self.run_git(repository, "add", "authority.md")
            self.run_git(
                repository,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "initial",
            )
            self.run_git(repository, "worktree", "add", "-b", "linked", str(linked))
            (repository / "authority.md").write_text("dirty primary authority\n", encoding="utf-8")
            manifest_path = linked / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("authority", "authority.md", ("contract",))),
                    order="sorted",
                )
            )

            explicit_result, explicit_stdout, explicit_stderr = self.run_cli(
                "--project-root",
                str(linked),
                "brief-sources",
                "--file",
                str(manifest_path),
                "--json",
            )
            with chdir(linked):
                default_result, default_stdout, default_stderr = self.run_cli(
                    "brief-sources", "--file", str(manifest_path), "--json"
                )

        expected_digest = hashlib.sha256(b"linked authority\n").hexdigest()
        self.assertEqual((0, ""), (explicit_result, explicit_stderr))
        self.assertEqual((0, ""), (default_result, default_stderr))
        self.assertEqual(expected_digest, json.loads(explicit_stdout)["sources"][0]["selected_sha256"])
        self.assertEqual(expected_digest, json.loads(default_stdout)["sources"][0]["selected_sha256"])


if __name__ == "__main__":
    unittest.main()
