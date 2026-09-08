import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import ArtifactRepository, write_revision
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_facts, refresh_facts
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.interfaces.errors import WorkBriefErrorCode, WorkBriefFailure, WorkBriefResult
from pinboard.interfaces.work_briefs import (
    build_attempt_brief_views,
    build_selected_attempt_brief_views,
    canonical_work_brief_bytes,
)
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.work_brief_support import work_a_brief


def expect_work_brief_success[T](result: WorkBriefResult[T]) -> T:
    if isinstance(result, WorkBriefFailure):
        raise AssertionError(str(result))
    return result


class GeneratedViewsTest(unittest.TestCase):
    def _state(self) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        return roots.work_root, store

    def test_generated_views_are_stable_across_unrelated_project_revisions(self) -> None:
        work_root, store = self._state()
        state = store.validated_snapshot()
        facts = store.read_all_generated_view_facts(SQLITE_NOW)

        result = rebuild_facts(facts, work_root, {})

        self.assertIsNone(result.warning)
        for selector in (
            "views/items/work-a.md",
            "views/attempts/work-a-1.md",
            f"views/history/{state.transition_receipts[0].history_id}.md",
        ):
            text = (work_root / selector).read_text(encoding="utf-8")
            self.assertNotIn("database_revision:", text)
        self.assertFalse((work_root / "views" / "queue.md").exists())
        self.assertFalse((work_root / "views" / "history.md").exists())
        sparse_item = (work_root / "views" / "items" / "intake-work.md").read_text(encoding="utf-8")
        self.assertIn("- Source: none", sparse_item)
        self.assertIn("- Notes: none", sparse_item)
        populated_item = (work_root / "views" / "items" / "work-a.md").read_text(encoding="utf-8")
        self.assertIn("- Source: accepted requirement", populated_item)
        self.assertIn("- Notes: Current work remains bounded.", populated_item)
        terminal_item = (work_root / "views" / "items" / "work-b.md").read_text(encoding="utf-8")
        self.assertIn("- Queue position: none", terminal_item)
        advanced = replace(
            state,
            lifecycle=replace(state.lifecycle, project=replace(state.lifecycle.project, revision=13)),
        )
        self.assertEqual(
            derive_expected_view_bytes(state, {}, now=SQLITE_NOW),
            derive_expected_view_bytes(advanced, {}, now=SQLITE_NOW),
        )

    def test_post_commit_refresh_failure_is_a_repairable_warning(self) -> None:
        work_root, store = self._state()
        with patch(
            "pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "disk full"),
        ):
            receipt = store.validated_snapshot().transition_receipts[0]
            result = refresh_facts(
                store.read_generated_view_facts((), (), (receipt.history_id,), SQLITE_NOW),
                work_root,
                {},
            )

        self.assertEqual(12, result.database_revision)
        self.assertIsNotNone(result.warning)
        assert result.warning is not None
        self.assertIn("generated views need repair", result.warning.message)
        self.assertIn("pinboard views rebuild", result.warning.repair)

    def test_rebuild_removes_legacy_aggregates_and_preserves_equal_projection_metadata(self) -> None:
        work_root, store = self._state()
        view_root = work_root / "views"
        view_root.mkdir(parents=True)
        (view_root / "queue.md").write_text("legacy queue\n", encoding="utf-8")
        (view_root / "history.md").write_text("legacy history\n", encoding="utf-8")

        first = rebuild_facts(store.read_all_generated_view_facts(SQLITE_NOW), work_root, {})

        self.assertIsNone(first.warning)
        self.assertFalse((view_root / "queue.md").exists())
        self.assertFalse((view_root / "history.md").exists())
        paths = tuple(path for path in view_root.rglob("*.md") if path.is_file())
        before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}

        second = rebuild_facts(store.read_all_generated_view_facts(SQLITE_NOW), work_root, {})

        self.assertIsNone(second.warning)
        self.assertEqual(
            before, {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
        )

    def test_live_v2_attempt_view_is_a_complete_rebuildable_projection(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        value = work_a_brief(project)
        published = write_revision(
            roots,
            NewArtifact(
                work_models.ArtifactKind.BRIEF, value.attempt_id, 1, ".json", canonical_work_brief_bytes(value)
            ),
        )
        state = complete_sqlite_state()
        reference = replace(
            state.artifact_references[0],
            key=published.key,
            revision=published.revision,
            selector=published.selector,
            content_sha256=published.content_sha256,
            size_bytes=published.size_bytes,
        )
        state = replace(state, artifact_references=(reference, *state.artifact_references[1:]))
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        facts = store.read_all_generated_view_facts(SQLITE_NOW)
        attempt_briefs = expect_work_brief_success(
            build_selected_attempt_brief_views(facts.attempts, ArtifactRepository(roots))
        )

        result = rebuild_facts(facts, roots.work_root, attempt_briefs)

        self.assertIsNone(result.warning)
        path = roots.work_root / "views" / "attempts" / "work-a-1.md"
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("database_revision:", text)
        self.assertIn("typed-json-cutover", text)
        path.unlink()
        facts = store.read_all_generated_view_facts(SQLITE_NOW)
        rebuild_facts(
            facts,
            roots.work_root,
            expect_work_brief_success(build_selected_attempt_brief_views(facts.attempts, ArtifactRepository(roots))),
        )
        self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_live_attempt_rejects_a_non_json_accepted_brief(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)

        failure = build_attempt_brief_views(complete_sqlite_state(), ArtifactRepository(roots))
        self.assertIsInstance(failure, WorkBriefFailure)
        assert isinstance(failure, WorkBriefFailure)
        self.assertEqual(WorkBriefErrorCode.BRIEF_INVALID, failure.code)
        self.assertIn("work-a-1", failure.message)


if __name__ == "__main__":
    unittest.main()
