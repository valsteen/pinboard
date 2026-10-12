"""Decode the external JSON-string envelope and render a non-authoritative field view.

The serialized values have arbitrary JSON grammar, not a fixed Bitwig schema.
This module owns that grammar, duplicate rejection and exact string representation;
numeric tokens remain exact Raw values so float rounding cannot hide changed digits.
All comparison and rendering operates on validated JSON values without file effects.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, assert_never  # noqa: TID251 - stdlib JSON decoder boundary, narrowed immediately

import msgspec

type JsonValue = bool | str | msgspec.Raw | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class FixtureReviewFailure:
    resource: str
    message: str


class RepeatSummary(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    sha256: str
    size_bytes: int
    repeat_count: int


class ValueChange(msgspec.Struct, tag="value-change", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    path: tuple[str | int, ...]
    before_present: bool
    after_present: bool
    before: JsonValue
    after: JsonValue


class RepeatedStringChange(
    msgspec.Struct, tag="unchanged-repeat", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    path: tuple[str | int, ...]
    body: RepeatSummary
    before_prefix: str
    after_prefix: str
    before_suffix: str
    after_suffix: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = child
    return value


def _json_value(value: Any) -> JsonValue:  # noqa: ANN401 - recursive arbitrary-JSON decoding boundary
    if isinstance(value, str):
        value.encode("utf-8")
        return value
    if value is None or isinstance(value, (bool, msgspec.Raw)):
        return value
    if isinstance(value, list):
        children: list[Any] = value
        return [_json_value(child) for child in children]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        members: dict[Any, Any] = value
        for key, child in members.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            key.encode("utf-8")
            result[key] = _json_value(child)
        return result
    raise ValueError("Unsupported JSON value")


def _decode_fixture(raw: bytes, resource: str) -> dict[str, JsonValue] | FixtureReviewFailure:
    try:
        envelope = _json_value(
            json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_int=msgspec.Raw, parse_float=msgspec.Raw
            )
        )
        if not isinstance(envelope, dict) or not all(isinstance(value, str) for value in envelope.values()):
            raise ValueError("Expected a JSON object of serialized JSON strings")
        decoded: dict[str, JsonValue] = {}
        for key, serialized in envelope.items():
            assert isinstance(serialized, str)
            decoded[key] = _json_value(
                json.loads(serialized, object_pairs_hook=_unique_object, parse_int=msgspec.Raw, parse_float=msgspec.Raw)
            )
        return decoded
    except (ValueError, RecursionError) as error:
        return FixtureReviewFailure(resource, f"Cannot decode fixture {resource!r}: {error}")


def _repeated_body(value: JsonValue, pattern: re.Pattern[str], character_count: int) -> str | None:
    """Take the recipe body only from a proven run in actual selected content."""
    match value:
        case str():
            if len(value) >= character_count:
                for run in pattern.finditer(value):
                    if run.end() - run.start() >= character_count:
                        return value[run.start() : run.start() + character_count]
        case list():
            for child in value:
                if (body := _repeated_body(child, pattern, character_count)) is not None:
                    return body
        case dict():
            for child in value.values():
                if (body := _repeated_body(child, pattern, character_count)) is not None:
                    return body
        case bool() | msgspec.Raw() | None:
            return None
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _render_change(
    path: tuple[str | int, ...],
    before: JsonValue,
    after: JsonValue,
    before_present: bool,
    after_present: bool,
    repeated: str | None,
    repeat_count: int,
) -> str:
    if isinstance(before, str) and isinstance(after, str) and repeated is not None:
        left = before.find(repeated)
        right = after.find(repeated)
        if left >= 0 and right >= 0 and before.find(repeated, left + 1) == -1 and after.find(repeated, right + 1) == -1:
            raw_body = repeated.encode("utf-8")
            return msgspec.json.encode(
                RepeatedStringChange(
                    path,
                    RepeatSummary(hashlib.sha256(raw_body).hexdigest(), len(raw_body), repeat_count),
                    before[:left],
                    after[:right],
                    before[left + len(repeated) :],
                    after[right + len(repeated) :],
                )
            ).decode("utf-8")
    return msgspec.json.encode(ValueChange(path, before_present, after_present, before, after), order="sorted").decode(
        "utf-8"
    )


def _changes(
    before: JsonValue, after: JsonValue, path: tuple[str | int, ...], repeated: str | None, repeat_count: int
) -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[str] = []
        for key in sorted(before.keys() | after.keys()):
            if key in before and key in after:
                changes.extend(_changes(before[key], after[key], (*path, key), repeated, repeat_count))
            else:
                changes.append(
                    _render_change(
                        (*path, key), before.get(key), after.get(key), key in before, key in after, None, repeat_count
                    )
                )
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        for index in range(max(len(before), len(after))):
            left_present, right_present = index < len(before), index < len(after)
            if left_present and right_present:
                changes.extend(_changes(before[index], after[index], (*path, index), repeated, repeat_count))
            else:
                changes.append(
                    _render_change(
                        (*path, index),
                        before[index] if left_present else None,
                        after[index] if right_present else None,
                        left_present,
                        right_present,
                        None,
                        repeat_count,
                    )
                )
        return changes
    if msgspec.json.encode(before, order="sorted") == msgspec.json.encode(after, order="sorted"):
        return []
    return [_render_change(path, before, after, True, True, repeated, repeat_count)]


def render_fixture_review(
    before_raw: bytes, after_raw: bytes, repeat_text: str, repeat_count: int
) -> str | FixtureReviewFailure:
    before = _decode_fixture(before_raw, "before")
    if isinstance(before, FixtureReviewFailure):
        return before
    after = _decode_fixture(after_raw, "after")
    if isinstance(after, FixtureReviewFailure):
        return after
    pattern = re.compile(f"(?:{re.escape(repeat_text)})+")
    repeated = _repeated_body(before, pattern, len(repeat_text) * repeat_count)
    return "\n".join(_changes(before, after, (), repeated, repeat_count)) + "\n"
