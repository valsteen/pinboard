"""Compile the replacement declaration into explicit, statically checked Python."""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec.inspect

from codegen.replacement_spec import (
    REPLACEMENTS,
    ContiguousRevisions,
    Copy,
    CurrentReplacement,
    ExactCost,
    InvariantOperation,
    Isoformat,
    KnownItems,
    KnownReplacement,
    ProjectedField,
    ProjectionOperation,
    ReplacementFamily,
    Text,
    UniqueDisposition,
)
from pinboard.domain import work_models

APPLICATION_OUTPUT = Path("src/pinboard/application/generated_replacements.py")
VALIDATION_OUTPUT = Path("src/pinboard/adapters/sqlite/generated_replacement_validation.py")

type SourceLeafType = (
    msgspec.inspect.IntType | msgspec.inspect.StrType | msgspec.inspect.EnumType | msgspec.inspect.DateTimeType
)


class DeclarationError(ValueError):
    """The replacement declaration does not cover its discovered source shape."""


@dataclass(frozen=True, slots=True)
class SourceField:
    name: str
    type: SourceLeafType


@dataclass(frozen=True, slots=True)
class SourceCollection:
    source: str
    fields: tuple[SourceField, ...]


def _source_collections(family: ReplacementFamily) -> tuple[SourceCollection, ...]:
    root = msgspec.inspect.type_info(family.source)
    if not isinstance(root, msgspec.inspect.DataclassType):
        raise DeclarationError("replacement family: source root must be a dataclass")
    collections: list[SourceCollection] = []
    for field in root.fields:
        if not isinstance(field.type, msgspec.inspect.VarTupleType):
            raise DeclarationError(
                f"replacement family: source path {family.source.__name__}.{field.name} must be a tuple of dataclasses"
            )
        row = field.type.item_type
        if not isinstance(row, msgspec.inspect.DataclassType):
            raise DeclarationError(
                f"replacement family: source path {family.source.__name__}.{field.name} must be a tuple of dataclasses"
            )
        leaves: list[SourceField] = []
        for leaf in row.fields:
            if not isinstance(
                leaf.type,
                (
                    msgspec.inspect.IntType,
                    msgspec.inspect.StrType,
                    msgspec.inspect.EnumType,
                    msgspec.inspect.DateTimeType,
                ),
            ):
                raise DeclarationError(
                    f"replacement family: unsupported source leaf {family.source.__name__}.{field.name}.{leaf.name}"
                )
            leaves.append(SourceField(leaf.name, leaf.type))
        collections.append(SourceCollection(field.name, tuple(leaves)))
    return tuple(collections)


def _validated_collections(family: ReplacementFamily) -> tuple[SourceCollection, ...]:
    discovered = _source_collections(family)
    discovered_roots = tuple(value.source for value in discovered)
    declared_roots = tuple(value.source for value in family.collections)
    if declared_roots != discovered_roots:
        raise DeclarationError(
            "replacement family: collection dispositions differ from discovered fields; "
            f"expected {discovered_roots!r}, declared {declared_roots!r}"
        )
    for source, declaration in zip(discovered, family.collections, strict=True):
        discovered_fields = tuple(value.name for value in source.fields)
        declared_fields = tuple(value.source for value in declaration.fields)
        if declared_fields != discovered_fields:
            raise DeclarationError(
                f"replacement family: field dispositions differ at "
                f"{family.source.__name__}.{source.source}; "
                f"expected {discovered_fields!r}, declared {declared_fields!r}"
            )
        if not declaration.invariants:
            raise DeclarationError(
                f"replacement family: invariant-role disposition missing at {family.source.__name__}.{source.source}"
            )
        tuple(_operation_name(operation) for operation in declaration.invariants)
    return discovered


def _operation_name(operation: InvariantOperation) -> str:
    match operation:
        case KnownItems():
            return "known-items"
        case ContiguousRevisions():
            return "contiguous-revisions"
        case KnownReplacement():
            return "known-replacement"
        case CurrentReplacement():
            return "current-replacement"
        case ExactCost():
            return "exact-cost"
        case UniqueDisposition():
            return "unique-disposition"
        case _ as unreachable:
            assert_never(unreachable)


def _require_fields(path: str, available: tuple[str, ...], required: tuple[str, ...]) -> None:
    missing = tuple(field for field in required if field not in available)
    if missing:
        raise DeclarationError(f"replacement family: invariant at {path} names unknown fields {missing!r}")


def _annotation(operation: ProjectionOperation, source: SourceField) -> str:
    match operation:
        case Text() | Isoformat():
            return "str"
        case Copy():
            if isinstance(source.type, msgspec.inspect.IntType):
                return "int"
            if isinstance(source.type, msgspec.inspect.StrType):
                return "str"
            if (
                isinstance(source.type, msgspec.inspect.EnumType)
                and source.type.cls is work_models.PlannedReplacementStatus
            ):
                return "work_models.PlannedReplacementStatus"
            raise DeclarationError(
                f"replacement family: Copy has no generated annotation for source field {source.name}"
            )
        case _ as unreachable:
            assert_never(unreachable)


def _expression(operation: ProjectionOperation, source_field: str) -> str:
    match operation:
        case Copy():
            return f"value.{source_field}"
        case Text():
            return f"str(value.{source_field})"
        case Isoformat():
            return f"value.{source_field}.isoformat()"
        case _ as unreachable:
            assert_never(unreachable)


def _row_class(
    name: str,
    fields: tuple[ProjectedField, ...],
    source_fields: tuple[SourceField, ...],
) -> list[str]:
    lines = [f"class {name}(msgspec.Struct, frozen=True, forbid_unknown_fields=True):"]
    lines.extend(
        f"    {field.target}: {_annotation(field.operation, source)}"
        for field, source in zip(fields, source_fields, strict=True)
    )
    return lines


def render_application(family: ReplacementFamily) -> str:
    discovered = _validated_collections(family)
    first, second = family.collections
    first_source, second_source = discovered
    lines = [
        '"""Generated by ``python -m codegen.replacement_records``; do not edit."""',
        "",
        "from dataclasses import dataclass",
        "",
        "import msgspec",
        "",
        "from pinboard.application import stored_state",
        "from pinboard.domain import work_models",
        "",
        "",
        *_row_class(first.target_row, first.fields, first_source.fields),
        "",
        "",
        *_row_class(second.target_row, second.fields, second_source.fields),
        "",
        "",
        f"type ProjectedPlannedReplacements = tuple[{first.target_row}, ...]",
        f"type ProjectedReplacementDispositions = tuple[{second.target_row}, ...]",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class ProjectedReplacements:",
        "    planned_replacements: ProjectedPlannedReplacements",
        "    replacement_dispositions: ProjectedReplacementDispositions",
        "",
        "",
        "def project_replacements(records: stored_state.ReplacementRecords) -> ProjectedReplacements:",
        "    return ProjectedReplacements(",
    ]
    for collection in family.collections:
        lines.extend(
            [
                "        tuple(",
                f"            {collection.target_row}(",
                *(f"                {_expression(field.operation, field.source)}," for field in collection.fields),
                "            )",
                f"            for value in records.{collection.source}",
                "        ),",
            ]
        )
    lines.extend(["    )", ""])
    return "\n".join(lines)


def render_validation(family: ReplacementFamily) -> str:
    discovered = _validated_collections(family)
    planned, dispositions = family.collections
    planned_source, disposition_source = discovered
    planned_fields = tuple(field.name for field in planned_source.fields)
    disposition_fields = tuple(field.name for field in disposition_source.fields)
    match planned.invariants:
        case (
            KnownItems(fields=item_fields, message=unknown_item_message),
            ContiguousRevisions(
                item_field=item_field,
                revision_field=revision_field,
                message=revision_message,
            ),
        ):
            _require_fields(planned.source, planned_fields, (*item_fields, item_field, revision_field))
        case _:
            roles = tuple(_operation_name(operation) for operation in planned.invariants)
            raise DeclarationError(
                f"replacement family: {planned.source} invariant roles must be "
                f"('known-items', 'contiguous-revisions'), declared {roles!r}"
            )
    match dispositions.invariants:
        case (
            KnownReplacement(
                item_field=disposition_item_field,
                revision_field=disposition_revision_field,
                message=unknown_replacement_message,
            ),
            CurrentReplacement(status_field=status_field, message=withdrawn_message),
            ExactCost(
                disposition_field=disposition_cost_field,
                replacement_field=replacement_cost_field,
                message=cost_message,
            ),
            UniqueDisposition(message=duplicate_message),
        ):
            _require_fields(
                dispositions.source,
                disposition_fields,
                (disposition_item_field, disposition_revision_field, disposition_cost_field),
            )
            _require_fields(planned.source, planned_fields, (status_field, replacement_cost_field))
        case _:
            roles = tuple(_operation_name(operation) for operation in dispositions.invariants)
            raise DeclarationError(
                f"replacement family: {dispositions.source} invariant roles must be "
                "('known-replacement', 'current-replacement', 'exact-cost', 'unique-disposition'), "
                f"declared {roles!r}"
            )
    lines = [
        '"""Generated by ``python -m codegen.replacement_records``; do not edit."""',
        "",
        "from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode",
        "from pinboard.application import stored_state",
        "from pinboard.domain import work_models",
        "from pinboard.domain.identifiers import ItemId",
        "",
        "",
        "def _replacement_revision(value: stored_state.StoredPlannedReplacement) -> int:",
        f"    return value.{revision_field}",
        "",
        "",
        "def validate_replacements(",
        "    records: stored_state.ReplacementRecords,",
        "    item_ids: set[ItemId],",
        "    error_code: StorageErrorCode,",
        ") -> None:",
        "    replacements_by_item: dict[ItemId, list[stored_state.StoredPlannedReplacement]] = {}",
        f"    for replacement in records.{planned.source}:",
        "        " + "if " + " or ".join(f"replacement.{field} not in item_ids" for field in item_fields) + ":",
        f"            raise StorageError(error_code, {json.dumps(unknown_item_message)})",
        f"        replacements_by_item.setdefault(replacement.{item_field}, []).append(replacement)",
        "    indexed_replacements: dict[tuple[ItemId, int], stored_state.StoredPlannedReplacement] = {}",
        "    for affected_item, replacements in replacements_by_item.items():",
        "        ordered = sorted(replacements, key=_replacement_revision)",
        f"        if [value.{revision_field} for value in ordered] != list(range(1, len(ordered) + 1)):",
        f"            raise StorageError(error_code, {json.dumps(revision_message)})",
        f"        indexed_replacements.update(((affected_item, value.{revision_field}), value) for value in ordered)",
        "    disposition_keys: set[tuple[ItemId, int]] = set()",
        f"    for disposition in records.{dispositions.source}:",
        f"        key = (disposition.{disposition_item_field}, disposition.{disposition_revision_field})",
        "        replacement = indexed_replacements.get(key)",
        "        if replacement is None:",
        f"            raise StorageError(error_code, {json.dumps(unknown_replacement_message)})",
        f"        if replacement.{status_field} != work_models.PlannedReplacementStatus.CURRENT:",
        f"            raise StorageError(error_code, {json.dumps(withdrawn_message)})",
        f"        if disposition.{disposition_cost_field} != replacement.{replacement_cost_field}:",
        f"            raise StorageError(error_code, {json.dumps(cost_message)})",
        "        if key in disposition_keys:",
        f"            raise StorageError(error_code, {json.dumps(duplicate_message)})",
        "        disposition_keys.add(key)",
        "",
    ]
    return "\n".join(lines)


def _write_or_check(path: Path, content: str, check: bool) -> bool:
    if check:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return True
        print(f"replacement family: stale generated output {path}", file=sys.stderr)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        application_content = render_application(REPLACEMENTS)
        validation_content = render_validation(REPLACEMENTS)
    except DeclarationError as error:
        print(error, file=sys.stderr)
        return 1
    application_ok = _write_or_check(APPLICATION_OUTPUT, application_content, args.check)
    validation_ok = _write_or_check(VALIDATION_OUTPUT, validation_content, args.check)
    return 0 if application_ok and validation_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
