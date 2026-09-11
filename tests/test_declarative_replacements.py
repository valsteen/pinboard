import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from codegen.replacement_records import (
    APPLICATION_OUTPUT,
    DeclarationError,
    _write_or_check,
    render_application,
)
from codegen.replacement_spec import REPLACEMENTS

from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId, TaskId


@dataclass(frozen=True, slots=True)
class RootWithExtraCollection:
    planned_replacements: tuple[stored_state.StoredPlannedReplacement, ...]
    dispositions: tuple[stored_state.StoredReplacementDisposition, ...]
    extra: tuple[stored_state.StoredReplacementDisposition, ...]


@dataclass(frozen=True, slots=True)
class PlannedReplacementWithExtraLeaf:
    affected_item_id: ItemId
    relation_revision: int
    replacement_item_id: ItemId
    replacement_cost: str
    status: work_models.PlannedReplacementStatus
    recorded_by: TaskId
    recorded_at: str
    accepted_project_revision: int
    extra: str


@dataclass(frozen=True, slots=True)
class RootWithExtraLeaf:
    planned_replacements: tuple[PlannedReplacementWithExtraLeaf, ...]
    dispositions: tuple[stored_state.StoredReplacementDisposition, ...]


@dataclass(frozen=True, slots=True)
class RootWithChangedTupleMember:
    planned_replacements: tuple[str, ...]
    dispositions: tuple[stored_state.StoredReplacementDisposition, ...]


class DeclarativeReplacementTest(unittest.TestCase):
    def test_current_declaration_covers_source_shape_and_generated_output_is_current(self) -> None:
        self.assertEqual(
            APPLICATION_OUTPUT.read_text(encoding="utf-8"),
            render_application(REPLACEMENTS),
        )

    def test_new_root_collection_is_discovered_without_a_class_or_site_inventory(self) -> None:
        family = replace(REPLACEMENTS, source=RootWithExtraCollection)

        with self.assertRaisesRegex(
            DeclarationError,
            "collection dispositions differ.*extra",
        ):
            render_application(family)

    def test_new_leaf_is_discovered_without_a_class_or_site_inventory(self) -> None:
        family = replace(REPLACEMENTS, source=RootWithExtraLeaf)

        with self.assertRaisesRegex(
            DeclarationError,
            "field dispositions differ.*extra",
        ):
            render_application(family)

    def test_changed_tuple_member_type_is_rejected(self) -> None:
        family = replace(REPLACEMENTS, source=RootWithChangedTupleMember)

        with self.assertRaisesRegex(
            DeclarationError,
            "planned_replacements must be a tuple of dataclasses",
        ):
            render_application(family)

    def test_missing_field_and_invariant_dispositions_are_rejected(self) -> None:
        first, second = REPLACEMENTS.collections
        missing_field = replace(
            REPLACEMENTS,
            collections=(replace(first, fields=first.fields[:-1]), second),
        )
        with self.assertRaisesRegex(DeclarationError, "field dispositions differ"):
            render_application(missing_field)

        missing_invariant = replace(
            REPLACEMENTS,
            collections=(replace(first, invariants=()), second),
        )
        with self.assertRaisesRegex(DeclarationError, "invariant-role disposition missing"):
            render_application(missing_invariant)

    def test_stale_generated_bytes_fail_the_check(self) -> None:
        path = Path(tempfile.mkdtemp()) / "generated.py"
        path.write_text("stale\n", encoding="utf-8")

        self.assertFalse(_write_or_check(path, "current\n", True))
        self.assertEqual("stale\n", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
