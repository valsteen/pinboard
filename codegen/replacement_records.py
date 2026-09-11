"""Compile the replacement declaration into explicit, statically checked Python."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec.inspect

from codegen.replacement_spec import (
    REPLACEMENTS,
    Copy,
    Isoformat,
    ProjectedField,
    ProjectionOperation,
    ReplacementFamily,
    Text,
)
from pinboard.domain import work_models

APPLICATION_OUTPUT = Path("src/pinboard/application/generated_replacements.py")

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
    return discovered


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
        content = render_application(REPLACEMENTS)
    except DeclarationError as error:
        print(error, file=sys.stderr)
        return 1
    return 0 if _write_or_check(APPLICATION_OUTPUT, content, args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
