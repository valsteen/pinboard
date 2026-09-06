import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import ArtifactRepository, write_revision
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.files.views import derive_expected_view_bytes, rebuild_state, refresh_state
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.interfaces.errors import WorkBriefError, WorkBriefErrorCode
from pinboard.interfaces.work_briefs import build_attempt_brief_views, canonical_work_brief_bytes
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.work_brief_support import work_a_brief


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
        state = store.snapshot()

        result = rebuild_state(state, work_root, now=SQLITE_NOW)

        self.assertIsNone(result.warning)
        for selector in (
            "views/queue.md",
            "views/items/work-a.md",
            "views/attempts/work-a-1.md",
            "views/history.md",
        ):
            text = (work_root / selector).read_text(encoding="utf-8")
            self.assertNotIn("database_revision:", text)
        history = (work_root / "views" / "history.md").read_text(encoding="utf-8")
        self.assertIn("| Accepted test definition. | test-source |", history)
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
            derive_expected_view_bytes(state, now=SQLITE_NOW),
            derive_expected_view_bytes(advanced, now=SQLITE_NOW),
        )

    def test_post_commit_refresh_failure_is_a_repairable_warning(self) -> None:
        work_root, store = self._state()
        with patch(
            "pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "disk full"),
        ):
            result = refresh_state(store.snapshot(), work_root, AffectedViews(queue=True), now=SQLITE_NOW)

        self.assertEqual(12, result.database_revision)
        self.assertIsNotNone(result.warning)
        assert result.warning is not None
        self.assertIn("generated views need repair", result.warning.message)
        self.assertIn("pinboard views rebuild", result.warning.repair)

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
        attempt_briefs = build_attempt_brief_views(store.snapshot(), ArtifactRepository(roots))

        result = rebuild_state(store.snapshot(), roots.work_root, attempt_briefs, now=SQLITE_NOW)

        self.assertIsNone(result.warning)
        path = roots.work_root / "views" / "attempts" / "work-a-1.md"
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("database_revision:", text)
        self.assertIn("typed-json-cutover", text)
        path.unlink()
        rebuild_state(
            store.snapshot(),
            roots.work_root,
            build_attempt_brief_views(store.snapshot(), ArtifactRepository(roots)),
            now=SQLITE_NOW,
        )
        self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_live_attempt_rejects_a_non_json_accepted_brief(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)

        with self.assertRaises(WorkBriefError) as raised:
            build_attempt_brief_views(complete_sqlite_state(), ArtifactRepository(roots))

        self.assertEqual(WorkBriefErrorCode.BRIEF_INVALID, raised.exception.code)
        self.assertIn("work-a-1", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
