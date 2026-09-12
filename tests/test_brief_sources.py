import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
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

            rendered_batches = tuple(
                (
                    batch,
                    expect_brief_source_success(render_brief_source_batch(project, plan, batch.index)),
                )
                for batch in plan.batches
            )
            rendered = b"".join(content for _batch, content in rendered_batches)

        selected = b"## Contract\n\nSelected.\n\n"
        self.assertIn(selected, rendered)
        self.assertEqual(hashlib.sha256(selected).hexdigest(), plan.sources[0].selected_sha256)
        self.assertEqual((3, 6), (plan.sources[0].start_line, plan.sources[0].end_line))
        self.assertIn(b"first line\nsecond line\n", rendered)
        self.assertFalse(hasattr(plan.sources[0].segments[0], "content"))
        self.assertTrue(all(batch.content_byte_count <= 24 for batch in plan.batches))
        self.assertTrue(all(len(content) == batch.estimated_rendered_byte_count for batch, content in rendered_batches))
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
            expect_brief_source_failure(
                render_brief_source_batch(project, plan, 1), BriefSourceErrorCode.BATCH_NOT_FOUND
            )

            (project / "source.md").write_text("# Source\n\nChanged.\n", encoding="utf-8")
            expect_brief_source_failure(
                render_brief_source_batch(project, plan, 0), BriefSourceErrorCode.SOURCE_CHANGED
            )

    def test_cli_plans_and_emits_an_empty_selected_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "empty.md").write_bytes(b"")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("empty", "empty.md", ("contract",))),
                    order="sorted",
                )
            )

            planned_result, planned_stdout, planned_stderr = self.run_cli(
                "--project-root",
                str(project),
                "brief-sources",
                "--file",
                str(manifest_path),
                "--json",
            )

            self.assertEqual((0, ""), (planned_result, planned_stderr))
            plan = json.loads(planned_stdout)
            self.assertEqual((0, 0), (plan["sources"][0]["start_line"], plan["sources"][0]["end_line"]))
            plan_path = project / "plan.json"
            plan_path.write_text(planned_stdout, encoding="utf-8")

            emitted_result, emitted_stdout, emitted_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
            )

        self.assertEqual((0, ""), (emitted_result, emitted_stderr))
        self.assertIn("authority=empty selector=empty.md lines=0-0 segment=0", emitted_stdout)

    def test_installed_plan_to_file_preserves_canonical_bytes_and_emission_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            requests: list[BriefSourceRequest] = []
            for index in range(16):
                source_name = f"source-{index:02}.md"
                source_size = 8_000 if index < 15 else 11_985
                (project / source_name).write_bytes(b"x" * (source_size - 1) + b"\n")
                requests.append(BriefSourceRequest(f"source-{index:02}", source_name, ("contract",)))
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(msgspec.json.encode(self.manifest(*requests), order="sorted"))
            plan_path = project / "plan.json"
            common = (
                "--project-root",
                str(project),
                "brief-sources",
                "--file",
                str(manifest_path),
            )

            stdout_result, complete_plan, stdout_stderr = self.run_cli(*common, "--json")
            file_result, receipt_stdout, file_stderr = self.run_cli(
                *common,
                "--output-plan",
                str(plan_path),
                "--json",
            )

            self.assertEqual((0, ""), (stdout_result, stdout_stderr))
            self.assertEqual((0, ""), (file_result, file_stderr))
            self.assertEqual(complete_plan.encode(), plan_path.read_bytes())
            self.assertGreater(len(complete_plan.encode()), 512)
            self.assertLess(len(receipt_stdout.encode()), 512)
            receipt = json.loads(receipt_stdout)
            self.assertEqual("pinboard-brief-source-plan-output/v1", receipt["schema"])
            self.assertEqual(str(plan_path), receipt["destination"])
            self.assertTrue(receipt["created"])
            self.assertEqual(len(complete_plan.encode()), receipt["plan_byte_count"])
            self.assertEqual(hashlib.sha256(complete_plan.encode()).hexdigest(), receipt["plan_sha256"])
            plan = json.loads(complete_plan)
            self.assertEqual(16, len(plan["sources"]))
            self.assertEqual(6, len(plan["batches"]))
            self.assertEqual(131_985, sum(source["selected_byte_count"] for source in plan["sources"]))

            repeated_result, repeated_stdout, repeated_stderr = self.run_cli(
                *common,
                "--output-plan",
                str(plan_path),
                "--json",
            )
            self.assertEqual((0, ""), (repeated_result, repeated_stderr))
            self.assertFalse(json.loads(repeated_stdout)["created"])

            emitted_result, emitted_stdout, emitted_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
            )
            self.assertEqual((0, ""), (emitted_result, emitted_stderr))
            self.assertIn("authority=source-00", emitted_stdout)

            (project / "source-00.md").write_bytes(b"changed\n")
            changed_result, _, changed_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
            )
            self.assertEqual(15, changed_result)
            self.assertIn(BriefSourceErrorCode.SOURCE_CHANGED.value, changed_stderr)

    def test_plan_to_file_preserves_a_differing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_bytes(b"source\n")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    order="sorted",
                )
            )
            plan_path = project / "plan.json"
            plan_path.write_bytes(b"existing\n")

            result, stdout, stderr = self.run_cli(
                "--project-root",
                str(project),
                "brief-sources",
                "--file",
                str(manifest_path),
                "--output-plan",
                str(plan_path),
                "--json",
            )
            preserved = plan_path.read_bytes()

        self.assertEqual((12, ""), (result, stderr))
        self.assertEqual(b"existing\n", preserved)
        rejection = json.loads(stdout)
        self.assertEqual("rejected", rejection["status"])
        self.assertFalse(rejection["state_changed"])
        self.assertEqual([], rejection["changed_surfaces"])

    def test_plan_to_file_reports_bytes_visible_before_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_bytes(b"source\n")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    order="sorted",
                )
            )
            plan_path = project / "plan.json"
            sync_failure = FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "simulated sync failure")

            with patch(
                "pinboard.adapters.files.file_io._sync_directory",
                side_effect=(sync_failure, None),
            ):
                result, stdout, stderr = self.run_cli(
                    "--project-root",
                    str(project),
                    "brief-sources",
                    "--file",
                    str(manifest_path),
                    "--output-plan",
                    str(plan_path),
                    "--json",
                )

            self.assertEqual((12, ""), (result, stderr))
            self.assertTrue(plan_path.is_file())
            rejection = json.loads(stdout)
            self.assertEqual("committed-effect", rejection["status"])
            self.assertTrue(rejection["state_changed"])
            self.assertEqual(["selected-output"], rejection["changed_surfaces"])
            self.assertEqual("do-not-retry", rejection["retry"])
            self.assertIn(
                {"field": "selected_output_path", "value": str(plan_path)},
                rejection["observed"],
            )

    def test_tool_contract_exposes_plan_to_file_as_a_selected_output_effect(self) -> None:
        result, stdout, stderr = self.run_cli(
            "tool-contract",
            "--operation",
            "brief-sources:plan-to-file",
            "--json",
        )

        self.assertEqual((0, ""), (result, stderr))
        contract = json.loads(stdout)
        self.assertEqual("plan-to-file", contract["variant"])
        self.assertEqual("publishes-selected-output", contract["mutation_class"])
        self.assertIn("--output-plan", contract["cli_usage"])
        self.assertEqual("selected-output-path", contract["required_authority"])

    def test_render_reads_only_sources_represented_in_the_selected_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = project / "first.md"
            second = project / "second.md"
            first.write_bytes(b"first\n")
            second.write_bytes(b"second\n")
            plan = expect_brief_source_success(
                plan_brief_sources(
                    project,
                    self.manifest(
                        BriefSourceRequest("first", first.name, ("contract",)),
                        BriefSourceRequest("second", second.name, ("acceptance",)),
                    ),
                    max_batch_bytes=10,
                )
            )
            read_paths: list[Path] = []
            original_read_bytes = Path.read_bytes

            def tracked_read_bytes(path: Path) -> bytes:
                read_paths.append(path)
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", autospec=True, side_effect=tracked_read_bytes):
                rendered = expect_brief_source_success(render_brief_source_batch(project, plan, 0))

        self.assertIn(b"authority=first", rendered)
        self.assertNotIn(b"authority=second", rendered)
        self.assertEqual([first], read_paths)

    def test_installed_emit_reads_the_plan_and_only_sources_in_the_selected_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = project / "first.md"
            second = project / "second.md"
            first.write_bytes(b"first\n")
            second.write_bytes(b"second\n")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(
                        BriefSourceRequest("first", first.name, ("contract",)),
                        BriefSourceRequest("second", second.name, ("acceptance",)),
                    ),
                    order="sorted",
                )
            )
            planned_result, planned_stdout, planned_stderr = self.run_cli(
                "--project-root",
                str(project),
                "brief-sources",
                "--file",
                str(manifest_path),
                "--max-batch-bytes",
                "10",
                "--json",
            )
            self.assertEqual((0, ""), (planned_result, planned_stderr))
            plan_path = project / "plan.json"
            plan_path.write_text(planned_stdout, encoding="utf-8")
            read_paths: list[Path] = []
            original_read_bytes = Path.read_bytes

            def tracked_read_bytes(path: Path) -> bytes:
                read_paths.append(path)
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", autospec=True, side_effect=tracked_read_bytes):
                emitted_result, emitted_stdout, emitted_stderr = self.run_cli(
                    "--project-root",
                    str(project),
                    "brief-sources",
                    "--plan",
                    str(plan_path),
                    "--emit-batch",
                    "0",
                )

        self.assertEqual((0, ""), (emitted_result, emitted_stderr))
        self.assertIn("authority=first", emitted_stdout)
        self.assertNotIn("authority=second", emitted_stdout)
        self.assertEqual([plan_path, first.resolve()], read_paths)

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
            common = ("--project-root", str(project), "brief-sources", "--file", str(manifest_path))

            planned_result, planned_stdout, planned_stderr = self.run_cli(*common, "--json")
            plan_path = project / "plan.json"
            plan_path.write_text(planned_stdout, encoding="utf-8")
            before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
            emitted_result, emitted_stdout, emitted_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
            )
            missing_result, _, missing_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "1"
            )
            invalid_emit_stderr = io.StringIO()
            with contextlib.redirect_stderr(invalid_emit_stderr), self.assertRaises(SystemExit) as invalid_emit:
                main(
                    (
                        "--project-root",
                        str(project),
                        "brief-sources",
                        "--plan",
                        str(plan_path),
                        "--max-batch-bytes",
                        "10",
                        "--emit-batch",
                        "0",
                    )
                )
            after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}

        self.assertEqual((0, ""), (planned_result, planned_stderr))
        plan = json.loads(planned_stdout)
        self.assertEqual("pinboard-brief-source-plan/v1", plan["schema"])
        self.assertEqual(24_000, plan["max_batch_bytes"])
        self.assertEqual((0, ""), (emitted_result, emitted_stderr))
        self.assertIn("BEGIN BRIEF SOURCE authority=source selector=source.md lines=1-3 segment=0", emitted_stdout)
        self.assertEqual(15, missing_result)
        self.assertIn(BriefSourceErrorCode.BATCH_NOT_FOUND.value, missing_stderr)
        self.assertEqual(2, invalid_emit.exception.code)
        self.assertIn(
            "--max-batch-bytes is only valid while planning with --file",
            invalid_emit_stderr.getvalue(),
        )
        self.assertEqual(before, after)

    def test_cli_rejects_a_plan_whose_source_and_batch_segments_disagree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_bytes(b"source\n")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    order="sorted",
                )
            )
            planned_result, planned_stdout, planned_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--file", str(manifest_path), "--json"
            )
            self.assertEqual((0, ""), (planned_result, planned_stderr))
            plan = json.loads(planned_stdout)
            plan["batches"][0]["segments"][0]["selector"] = "other.md"
            plan_path = project / "invalid-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            result, _stdout, stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
            )

        self.assertEqual(15, result)
        self.assertIn(BriefSourceErrorCode.PLAN_INVALID.value, stderr)

    def test_cli_rejects_plan_segments_that_do_not_exactly_partition_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_bytes(b"first\nother\nthird\n")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    order="sorted",
                )
            )
            planned_result, planned_stdout, planned_stderr = self.run_cli(
                "--project-root",
                str(project),
                "brief-sources",
                "--file",
                str(manifest_path),
                "--max-batch-bytes",
                "6",
                "--json",
            )
            self.assertEqual((0, ""), (planned_result, planned_stderr))
            original = json.loads(planned_stdout)
            cases = {
                "duplicate-and-gap": (1, 2),
                "reordered": (0, 1),
                "before-source": (0, None),
            }
            for name, (target_index, copied_index) in cases.items():
                with self.subTest(name=name):
                    plan = json.loads(json.dumps(original))
                    target = plan["sources"][0]["segments"][target_index]
                    batch_target = plan["batches"][target_index]["segments"][0]
                    if copied_index is None:
                        target["start_line"] = 0
                        target["end_line"] = 0
                        batch_target["start_line"] = 0
                        batch_target["end_line"] = 0
                    else:
                        copied = plan["sources"][0]["segments"][copied_index]
                        for field in (
                            "start_line",
                            "end_line",
                            "content_byte_count",
                            "content_sha256",
                            "ends_with_newline",
                        ):
                            target[field] = copied[field]
                            batch_target[field] = copied[field]
                    plan_path = project / f"invalid-{name}.json"
                    plan_path.write_text(json.dumps(plan), encoding="utf-8")

                    result, _stdout, stderr = self.run_cli(
                        "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
                    )

                    self.assertEqual(15, result)
                    self.assertIn(BriefSourceErrorCode.PLAN_INVALID.value, stderr)

    def test_cli_rejects_whole_file_flags_that_disagree_with_the_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_text("# Source\n\n## Contract\n\nBody.\n", encoding="utf-8")
            for name, selector, whole_file in (
                ("heading-marked-whole", "source.md#Contract", True),
                ("file-marked-section", "source.md", False),
            ):
                with self.subTest(name=name):
                    manifest_path = project / f"{name}-manifest.json"
                    manifest_path.write_bytes(
                        msgspec.json.encode(
                            self.manifest(BriefSourceRequest("source", selector, ("contract",))),
                            order="sorted",
                        )
                    )
                    planned_result, planned_stdout, planned_stderr = self.run_cli(
                        "--project-root",
                        str(project),
                        "brief-sources",
                        "--file",
                        str(manifest_path),
                        "--json",
                    )
                    self.assertEqual((0, ""), (planned_result, planned_stderr))
                    plan = json.loads(planned_stdout)
                    plan["sources"][0]["whole_file"] = whole_file
                    plan_path = project / f"{name}-plan.json"
                    plan_path.write_text(json.dumps(plan), encoding="utf-8")

                    result, _stdout, stderr = self.run_cli(
                        "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
                    )

                    self.assertEqual(15, result)
                    self.assertIn(BriefSourceErrorCode.PLAN_INVALID.value, stderr)

    def test_cli_rejects_a_plan_whose_rendered_size_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "source.md").write_bytes(b"source\n")
            manifest_path = project / "manifest.json"
            manifest_path.write_bytes(
                msgspec.json.encode(
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    order="sorted",
                )
            )
            planned_result, planned_stdout, planned_stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--file", str(manifest_path), "--json"
            )
            self.assertEqual((0, ""), (planned_result, planned_stderr))
            plan = json.loads(planned_stdout)
            plan["batches"][0]["estimated_rendered_byte_count"] += 1
            plan_path = project / "invalid-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            result, _stdout, stderr = self.run_cli(
                "--project-root", str(project), "brief-sources", "--plan", str(plan_path), "--emit-batch", "0"
            )

        self.assertEqual(15, result)
        self.assertIn(BriefSourceErrorCode.PLAN_INVALID.value, stderr)

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
