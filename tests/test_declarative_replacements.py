import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

from codegen.replacements.compiler import (
    APPLICATION_OUTPUT,
    GENERATED_HEADER,
    VALIDATION_OUTPUT,
    DeclarationError,
    _validate_private_row_imports,
    _validated_invariant_operation,
    _validated_projection_operation,
    _write_or_check,
    render_application,
    render_validation,
)
from codegen.replacements.declaration import (
    REPLACEMENTS,
    Copy,
    ExactCost,
    KnownItems,
)

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId, TaskId
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


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
    def test_generated_ownership_convention_is_mechanically_visible(self) -> None:
        root = Path(__file__).parents[1]
        outputs = (APPLICATION_OUTPUT, VALIDATION_OUTPUT)
        self.assertEqual(
            (
                Path("src/pinboard/application/_generated/replacement_projection.py"),
                Path("src/pinboard/adapters/sqlite/_generated/replacement_validation.py"),
            ),
            outputs,
        )
        for output in outputs:
            with self.subTest(output=output):
                self.assertEqual(GENERATED_HEADER, (root / output).read_text(encoding="utf-8").splitlines()[0])
                self.assertTrue((root / output.parent / "AGENTS.md").is_file())
        attributes = (root / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("src/pinboard/application/_generated/*.py linguist-generated", attributes)
        self.assertIn("src/pinboard/adapters/sqlite/_generated/*.py linguist-generated", attributes)

    def test_current_declaration_covers_source_shape_and_generated_output_is_current(self) -> None:
        self.assertEqual(
            APPLICATION_OUTPUT.read_text(encoding="utf-8"),
            render_application(REPLACEMENTS),
        )

    def _replacement_records(self) -> stored_state.ReplacementRecords:
        replacement = stored_state.StoredPlannedReplacement(
            ItemId("work-a"),
            1,
            ItemId("work-c"),
            "replace cost",
            work_models.PlannedReplacementStatus.CURRENT,
            TaskId("coordinator"),
            SQLITE_NOW,
            12,
        )
        disposition = stored_state.StoredReplacementDisposition(
            ItemId("work-a"),
            1,
            "retain temporarily",
            replacement.replacement_cost,
            TaskId("coordinator"),
            SQLITE_NOW,
            12,
        )
        return stored_state.ReplacementRecords((replacement,), (disposition,))

    def _assert_rejected_without_commit(
        self,
        records: stored_state.ReplacementRecords,
        message: str,
    ) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = replace(complete_sqlite_state(), replacements=records)
        with self.assertRaisesRegex(StorageError, message):
            initialize_store(store, state)
        self.assertEqual(0, store.validated_snapshot().lifecycle.project.revision)

    def test_generated_validator_preserves_all_six_diagnostics_and_rollback(self) -> None:
        valid = self._replacement_records()
        replacement = valid.planned_replacements[0]
        disposition = valid.dispositions[0]
        cases = (
            (
                "unknown item",
                replace(valid, planned_replacements=(replace(replacement, replacement_item_id=ItemId("missing")),)),
                "A planned replacement names an unknown work item.",
            ),
            (
                "noncontiguous revision",
                replace(valid, planned_replacements=(replace(replacement, relation_revision=2),)),
                "Planned replacement revisions must be contiguous from revision 1.",
            ),
            (
                "unknown replacement",
                replace(valid, planned_replacements=()),
                "A temporary-retention disposition names an unknown replacement revision.",
            ),
            (
                "withdrawn replacement",
                replace(
                    valid,
                    planned_replacements=(replace(replacement, status=work_models.PlannedReplacementStatus.WITHDRAWN),),
                ),
                "A temporary-retention disposition names a withdrawn replacement revision.",
            ),
            (
                "wrong cost",
                replace(valid, dispositions=(replace(disposition, accepted_cost="wrong"),)),
                "A temporary-retention disposition must accept the exact replacement cost.",
            ),
            (
                "duplicate disposition",
                replace(valid, dispositions=(disposition, disposition)),
                "A replacement revision has duplicate temporary-retention dispositions.",
            ),
        )
        for name, records, message in cases:
            with self.subTest(name=name):
                self._assert_rejected_without_commit(records, message)

    def test_generated_validator_preserves_first_failure_precedence(self) -> None:
        valid = self._replacement_records()
        replacement = valid.planned_replacements[0]
        disposition = valid.dispositions[0]
        self._assert_rejected_without_commit(
            replace(
                valid,
                planned_replacements=(
                    replace(replacement, replacement_item_id=ItemId("missing"), relation_revision=2),
                ),
                dispositions=(replace(disposition, accepted_cost="wrong"), disposition),
            ),
            "A planned replacement names an unknown work item.",
        )
        self._assert_rejected_without_commit(
            replace(
                valid,
                planned_replacements=(replace(replacement, status=work_models.PlannedReplacementStatus.WITHDRAWN),),
                dispositions=(replace(disposition, accepted_cost="wrong"), disposition),
            ),
            "A temporary-retention disposition names a withdrawn replacement revision.",
        )

    def test_invariant_role_removal_and_field_drift_fail_generation(self) -> None:
        first, second = REPLACEMENTS.collections
        missing_role = replace(
            REPLACEMENTS,
            collections=(first, replace(second, invariants=second.invariants[:-1])),
        )
        with self.assertRaisesRegex(DeclarationError, "invariant roles must be"):
            render_validation(missing_role)

        operation = second.invariants[2]
        assert isinstance(operation, ExactCost)
        invalid_field = replace(
            REPLACEMENTS,
            collections=(
                first,
                replace(
                    second,
                    invariants=(
                        *second.invariants[:2],
                        replace(operation, disposition_field="missing"),
                        second.invariants[3],
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(DeclarationError, "names unknown fields.*missing"):
            render_validation(invalid_field)

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

    def test_collection_target_controls_generated_alias_and_result_field(self) -> None:
        first, second = REPLACEMENTS.collections
        renamed = replace(
            REPLACEMENTS,
            collections=(replace(first, target="plans"), second),
        )

        generated = render_application(renamed)

        self.assertIn("type ProjectedPlans = tuple[", generated)
        self.assertIn("    plans: ProjectedPlans", generated)
        self.assertIn("        plans=tuple(", generated)

    def test_private_generated_row_access_is_rejected(self) -> None:
        source_root = Path(tempfile.mkdtemp()) / "src"
        application = source_root / "pinboard" / "application"
        application.mkdir(parents=True)
        cases = {
            "absolute.py": (
                "from pinboard.application._generated.replacement_projection import _HandoverPlannedReplacement\n"
            ),
            "qualified.py": (
                "import pinboard.application._generated.replacement_projection as projection\n"
                "row = projection._HandoverPlannedReplacement\n"
            ),
            "qualified_unaliased.py": (
                "import pinboard.application._generated.replacement_projection\n"
                "row = pinboard.application._generated.replacement_projection._HandoverPlannedReplacement\n"
            ),
            "relative.py": ("from ._generated.replacement_projection import _HandoverPlannedReplacement\n"),
            "relative_module.py": (
                "from ._generated import replacement_projection as projection\n"
                "row = projection._HandoverPlannedReplacement\n"
            ),
        }
        for filename, source in cases.items():
            with self.subTest(filename=filename):
                path = application / filename
                path.write_text(source, encoding="utf-8")
                with self.assertRaisesRegex(
                    DeclarationError,
                    f"private generated row symbol.*_HandoverPlannedReplacement.*{filename}",
                ):
                    _validate_private_row_imports(REPLACEMENTS, source_root)
                path.unlink()

    def test_unknown_projection_and_invariant_operations_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            DeclarationError,
            "unsupported projection operation.*ReplacementRecords.planned_replacements.affected_item_id",
        ):
            _validated_projection_operation(
                KnownItems((), "unused diagnostic"),
                "ReplacementRecords.planned_replacements.affected_item_id",
            )

        with self.assertRaisesRegex(
            DeclarationError,
            "unsupported invariant operation.*ReplacementRecords.planned_replacements.*role 1",
        ):
            _validated_invariant_operation(
                Copy(),
                "ReplacementRecords.planned_replacements",
                1,
            )

    def test_stale_generated_bytes_fail_the_check(self) -> None:
        path = Path(tempfile.mkdtemp()) / "generated.py"
        path.write_text("stale\n", encoding="utf-8")

        self.assertFalse(_write_or_check(path, "current\n", True))
        self.assertEqual("stale\n", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
