import hashlib
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.application.brief_source_models import (
    AuthoritySelector,
    BriefSourceErrorCode,
    BriefSourceFailure,
    BriefSourceManifest,
    BriefSourceRequest,
    BriefSourceResult,
    SelectedBriefSource,
)
from pinboard.application.brief_sources import (
    BriefSourceSelector,
    plan_brief_sources,
    render_brief_source_batch,
    select_brief_source_bytes,
)
from pinboard.mcp import server
from tests.native_support import call_native_tool
from tests.support import JsonObject, JsonValue


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


def source_selector(project: Path) -> BriefSourceSelector:
    return partial(select_checkout_brief_source, project)


class BriefSourcesTest(unittest.TestCase):
    def sources(self, project: Path, operation: str, **fields: JsonValue) -> JsonObject:
        return call_native_tool(
            server.BRIEF_SOURCES_TOOL,
            {
                "request": {
                    "project_root": str(project.resolve()),
                    "work_root": str(project.resolve() / "absent-state"),
                    "operation": operation,
                    **fields,
                }
            },
        )

    def manifest_value(self, *sources: BriefSourceRequest) -> JsonObject:
        return {
            "schema": "pinboard-brief-sources/v1",
            "sources": [
                {"authority_id": source.authority_id, "selector": source.selector, "families": list(source.families)}
                for source in sources
            ],
        }

    def manifest(self, *sources: BriefSourceRequest) -> BriefSourceManifest:
        return BriefSourceManifest(schema="pinboard-brief-sources/v1", sources=sources)

    def run_git(self, cwd: Path, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)

    def test_application_plans_from_an_injected_byte_capability(self) -> None:
        selections: list[tuple[AuthoritySelector, bool]] = []

        def select_source(selector: AuthoritySelector, require_utf8: bool) -> BriefSourceResult[SelectedBriefSource]:
            selections.append((selector, require_utf8))
            return select_brief_source_bytes(selector, b"# Authority\n\nReviewed.\n", require_utf8)

        plan = expect_brief_source_success(
            plan_brief_sources(
                select_source,
                self.manifest(BriefSourceRequest("authority", "authority.md", ("contract",))),
                128,
            )
        )

        rendered = expect_brief_source_success(render_brief_source_batch(select_source, plan, 0))

        self.assertEqual(2, len(selections))
        self.assertTrue(selections[0][1])
        self.assertEqual("authority.md", str(selections[0][0].relative_path))
        self.assertIn(b"# Authority\n\nReviewed.\n", rendered)

    def test_manifest_boundary_rejects_unknown_fields_duplicates_and_unsafe_selectors(self) -> None:
        cases = (
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md","families":["contract"],"extra":true}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md","families":["contract"]},{"authority_id":"a","selector":"b.md","families":["acceptance"]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"../a.md","families":["contract"]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md","families":[]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a\\n","selector":"a.md","families":["contract"]}]}',
            b'{"schema":"pinboard-brief-sources/v1","sources":[{"authority_id":"a","selector":"a.md\\n","families":["contract"]}]}',
        )

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for raw in cases:
                with self.subTest(raw=raw):
                    manifest: JsonObject = json.loads(raw)
                    rejected = self.sources(
                        project,
                        "plan",
                        manifest=manifest,
                        max_batch_bytes=128,
                    )
                    self.assertEqual("BRIEF_SOURCES_REQUEST_INVALID", rejected["code"])
                    self.assertFalse(rejected["state_changed"])

    def test_plan_normalizes_heading_bytes_and_batches_every_selected_byte_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "architecture.md").write_bytes(
                b"# Architecture\r\n\r\n## Contract\r\n\r\nSelected.\r\n\r\n## Sibling\r\nExcluded.\r\n"
            )
            (project / "acceptance.txt").write_bytes(b"first line\nsecond line\n")
            plan = expect_brief_source_success(
                plan_brief_sources(
                    source_selector(project),
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
                    expect_brief_source_success(render_brief_source_batch(source_selector(project), plan, batch.index)),
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
                plan_brief_sources(source_selector(project), overlapping, max_batch_bytes=128),
                BriefSourceErrorCode.SELECTOR_OVERLAP,
            )

            (project / "binary.dat").write_bytes(b"\xff\xfe")
            expect_brief_source_failure(
                plan_brief_sources(
                    source_selector(project),
                    self.manifest(BriefSourceRequest("binary", "binary.dat", ("contract",))),
                    max_batch_bytes=128,
                ),
                BriefSourceErrorCode.SOURCE_NOT_UTF8,
            )

            expect_brief_source_failure(
                plan_brief_sources(
                    source_selector(project),
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    max_batch_bytes=8,
                ),
                BriefSourceErrorCode.LINE_TOO_LARGE,
            )

            plan = expect_brief_source_success(
                plan_brief_sources(
                    source_selector(project),
                    self.manifest(BriefSourceRequest("source", "source.md", ("contract",))),
                    max_batch_bytes=128,
                )
            )
            expect_brief_source_failure(
                render_brief_source_batch(source_selector(project), plan, 1), BriefSourceErrorCode.BATCH_NOT_FOUND
            )

            (project / "source.md").write_text("# Source\n\nChanged.\n", encoding="utf-8")
            expect_brief_source_failure(
                render_brief_source_batch(source_selector(project), plan, 0), BriefSourceErrorCode.SOURCE_CHANGED
            )

    def test_native_plans_and_emits_an_empty_selected_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.run_git(project, "init", "--quiet")
            (project / "empty.md").write_bytes(b"")
            plan = self.sources(
                project,
                "plan",
                manifest=self.manifest_value(BriefSourceRequest("empty", "empty.md", ("contract",))),
                max_batch_bytes=24_000,
            )
            sources = plan["sources"]
            assert isinstance(sources, list) and isinstance(sources[0], dict)
            self.assertEqual((0, 0), (sources[0]["start_line"], sources[0]["end_line"]))
            emitted = self.sources(project, "emit", plan=plan, batch_index=0)
            self.assertIn("authority=empty selector=empty.md lines=0-0 segment=0", str(emitted["text"]))
            self.assertFalse((project / "absent-state").exists())

    def test_native_plan_to_file_preserves_canonical_bytes_compact_receipt_and_emission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.run_git(project, "init", "--quiet")
            requests: list[BriefSourceRequest] = []
            for index in range(16):
                name = f"source-{index:02}.md"
                size = 8_000 if index < 15 else 11_985
                (project / name).write_bytes(b"x" * (size - 1) + b"\n")
                requests.append(BriefSourceRequest(f"source-{index:02}", name, ("contract",)))
            manifest = self.manifest_value(*requests)
            plan = self.sources(project, "plan", manifest=manifest, max_batch_bytes=24_000)
            plan_path = project / "plan.json"
            receipt = self.sources(
                project, "plan-to-file", manifest=manifest, max_batch_bytes=24_000, destination=str(plan_path)
            )
            canonical = msgspec.json.format(msgspec.json.encode(plan, order="sorted"), indent=2) + b"\n"
            self.assertEqual(canonical, plan_path.read_bytes())
            self.assertGreater(len(canonical), 512)
            self.assertLess(len(msgspec.json.encode(receipt)), 512)
            self.assertEqual("pinboard-brief-source-plan-output/v1", receipt["schema"])
            self.assertEqual(str(plan_path), receipt["destination"])
            self.assertTrue(receipt["created"])
            self.assertEqual(len(canonical), receipt["plan_byte_count"])
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), receipt["plan_sha256"])
            sources, batches = plan["sources"], plan["batches"]
            assert isinstance(sources, list) and isinstance(batches, list)
            self.assertEqual((16, 6), (len(sources), len(batches)))
            self.assertEqual(
                131_985,
                sum(
                    source["selected_byte_count"]
                    for source in sources
                    if isinstance(source, dict) and isinstance(source["selected_byte_count"], int)
                ),
            )
            reused = self.sources(
                project, "plan-to-file", manifest=manifest, max_batch_bytes=24_000, destination=str(plan_path)
            )
            self.assertEqual((False, "unchanged"), (reused["created"], reused["effect"]))
            emitted = self.sources(project, "emit-file", plan_path=str(plan_path), batch_index=0)
            self.assertIn("authority=source-00", str(emitted["text"]))
            (project / "source-00.md").write_bytes(b"changed\n")
            changed = self.sources(project, "emit-file", plan_path=str(plan_path), batch_index=0)
            self.assertEqual(BriefSourceErrorCode.SOURCE_CHANGED.value, changed["code"])
            self.assertFalse((project / "absent-state").exists())

    def test_native_selected_output_collision_and_post_visibility_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.run_git(project, "init", "--quiet")
            (project / "source.md").write_bytes(b"source\n")
            manifest = self.manifest_value(BriefSourceRequest("source", "source.md", ("contract",)))
            occupied = project / "occupied.json"
            occupied.write_bytes(b"existing\n")
            collision = self.sources(
                project, "plan-to-file", manifest=manifest, max_batch_bytes=24_000, destination=str(occupied)
            )
            self.assertEqual(
                ("FILE_ALREADY_EXISTS", "rejected", "correct-input", False, []),
                (
                    collision["code"],
                    collision["status"],
                    collision["retry"],
                    collision["state_changed"],
                    collision["changed_surfaces"],
                ),
            )
            self.assertEqual(b"existing\n", occupied.read_bytes())
            visible = project / "visible.json"
            with patch(
                "pinboard.adapters.files.file_io._sync_directory",
                side_effect=FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "controlled sync failure"),
            ):
                failure = self.sources(
                    project, "plan-to-file", manifest=manifest, max_batch_bytes=24_000, destination=str(visible)
                )
            self.assertTrue(visible.is_file())
            self.assertEqual(
                ("DIRECTORY_SYNC_FAILED", "committed-effect", "do-not-retry", True, ["selected-output"], str(visible)),
                (
                    failure["code"],
                    failure["status"],
                    failure["retry"],
                    failure["state_changed"],
                    failure["changed_surfaces"],
                    failure["destination"],
                ),
            )
            self.assertFalse((project / "absent-state").exists())

    def test_render_reads_only_sources_represented_in_the_selected_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = project / "first.md"
            second = project / "second.md"
            first.write_bytes(b"first\n")
            second.write_bytes(b"second\n")
            plan = expect_brief_source_success(
                plan_brief_sources(
                    source_selector(project),
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
                rendered = expect_brief_source_success(render_brief_source_batch(source_selector(project), plan, 0))

        self.assertIn(b"authority=first", rendered)
        self.assertNotIn(b"authority=second", rendered)
        self.assertEqual([first], read_paths)

    def test_native_saved_emit_reads_only_the_plan_and_selected_batch_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.run_git(project, "init", "--quiet")
            first, second = project / "first.md", project / "second.md"
            first.write_bytes(b"first\n")
            second.write_bytes(b"second\n")
            plan = self.sources(
                project,
                "plan",
                manifest=self.manifest_value(
                    BriefSourceRequest("first", first.name, ("contract",)),
                    BriefSourceRequest("second", second.name, ("acceptance",)),
                ),
                max_batch_bytes=10,
            )
            plan_path = project / "plan.json"
            plan_path.write_bytes(msgspec.json.encode(plan))
            before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
            paths: list[Path] = []
            original_read = Path.read_bytes

            def tracked(path: Path) -> bytes:
                paths.append(path)
                return original_read(path)

            with patch.object(Path, "read_bytes", autospec=True, side_effect=tracked):
                emitted = self.sources(project, "emit-file", plan_path=str(plan_path), batch_index=0)
            self.assertIn("authority=first", str(emitted["text"]))
            self.assertNotIn("authority=second", str(emitted["text"]))
            self.assertEqual([plan_path, first.resolve()], paths)
            missing = self.sources(project, "emit-file", plan_path=str(plan_path), batch_index=100)
            self.assertEqual(BriefSourceErrorCode.BATCH_NOT_FOUND.value, missing["code"])
            after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((project / "absent-state").exists())

    def test_native_saved_plan_rejects_inconsistent_segments_flags_and_rendered_sizes(self) -> None:  # noqa: PLR0915 - one independent native plan-integrity matrix
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            self.run_git(project, "init", "--quiet")
            (project / "source.md").write_bytes(b"first\nother\nthird\n")
            original = self.sources(
                project,
                "plan",
                manifest=self.manifest_value(BriefSourceRequest("source", "source.md", ("contract",))),
                max_batch_bytes=6,
            )
            cases: list[tuple[str, JsonObject]] = []
            selector = deepcopy(original)
            batches = selector["batches"]
            assert isinstance(batches, list) and isinstance(batches[0], dict)
            segments = batches[0]["segments"]
            assert isinstance(segments, list) and isinstance(segments[0], dict)
            segments[0]["selector"] = "other.md"
            cases.append(("source-batch-disagreement", selector))
            for name, target_index, copied_index in (
                ("duplicate-and-gap", 1, 2),
                ("reordered", 0, 1),
                ("before-source", 0, None),
            ):
                plan = deepcopy(original)
                sources, batches = plan["sources"], plan["batches"]
                assert isinstance(sources, list) and isinstance(sources[0], dict) and isinstance(batches, list)
                source_segments = sources[0]["segments"]
                batch = batches[target_index]
                assert isinstance(source_segments, list) and isinstance(batch, dict)
                batch_segments = batch["segments"]
                assert isinstance(batch_segments, list)
                target, batch_target = source_segments[target_index], batch_segments[0]
                assert isinstance(target, dict) and isinstance(batch_target, dict)
                if copied_index is None:
                    target["start_line"] = target["end_line"] = 0
                    batch_target["start_line"] = batch_target["end_line"] = 0
                else:
                    copied = source_segments[copied_index]
                    assert isinstance(copied, dict)
                    for field in (
                        "start_line",
                        "end_line",
                        "content_byte_count",
                        "content_sha256",
                        "ends_with_newline",
                    ):
                        target[field] = batch_target[field] = copied[field]
                cases.append((name, plan))
            size = deepcopy(original)
            batches = size["batches"]
            assert isinstance(batches, list) and isinstance(batches[0], dict)
            count = batches[0]["estimated_rendered_byte_count"]
            assert isinstance(count, int)
            batches[0]["estimated_rendered_byte_count"] = count + 1
            cases.append(("false-rendered-size", size))
            (project / "heading.md").write_bytes(b"# Source\n\n## Contract\n\nBody.\n")
            for name, selector, whole_file in (
                ("heading-marked-whole", "heading.md#Contract", True),
                ("file-marked-section", "heading.md", False),
            ):
                plan = self.sources(
                    project,
                    "plan",
                    manifest=self.manifest_value(BriefSourceRequest("source", selector, ("contract",))),
                    max_batch_bytes=24_000,
                )
                sources = plan["sources"]
                assert isinstance(sources, list) and isinstance(sources[0], dict)
                sources[0]["whole_file"] = whole_file
                cases.append((name, plan))
            for name, plan in cases:
                with self.subTest(name=name):
                    path = project / f"invalid-{name}.json"
                    path.write_bytes(msgspec.json.encode(plan))
                    failure = self.sources(project, "emit-file", plan_path=str(path), batch_index=0)
                    self.assertEqual(BriefSourceErrorCode.PLAN_INVALID.value, failure["code"])
                    self.assertEqual(
                        ("rejected", False, []),
                        (failure["status"], failure["state_changed"], failure["changed_surfaces"]),
                    )
            self.assertFalse((project / "absent-state").exists())

    def test_native_reads_authority_bytes_from_the_selected_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, linked = root / "repository", root / "linked"
            repository.mkdir()
            self.run_git(repository, "init", "-b", "main")
            (repository / "authority.md").write_bytes(b"linked authority\n")
            self.run_git(repository, "add", "authority.md")
            self.run_git(
                repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial"
            )
            self.run_git(repository, "worktree", "add", "-b", "linked", str(linked))
            (repository / "authority.md").write_bytes(b"dirty primary authority\n")
            plan = self.sources(
                linked,
                "plan",
                manifest=self.manifest_value(BriefSourceRequest("authority", "authority.md", ("contract",))),
                max_batch_bytes=24_000,
            )
            sources = plan["sources"]
            assert isinstance(sources, list) and isinstance(sources[0], dict)
            self.assertEqual(hashlib.sha256(b"linked authority\n").hexdigest(), sources[0]["selected_sha256"])
            emitted = self.sources(linked, "emit", plan=plan, batch_index=0)
            self.assertIn("linked authority", str(emitted["text"]))
            self.assertNotIn("dirty primary", str(emitted["text"]))


if __name__ == "__main__":
    unittest.main()
