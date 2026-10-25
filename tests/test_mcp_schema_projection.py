"""The advertised field formats preserve current canonical request and result rules."""

import asyncio
import io
import re
import unittest
from collections.abc import Iterator
from unittest.mock import patch

from mcp_types import Tool

from pinboard.application import action_models
from pinboard.domain import decision_models
from pinboard.mcp import contract_schemas, contracts, execution, server
from pinboard.mcp.contracts import JsonSchemaValue


def pattern_pairs(
    raw: JsonSchemaValue, advertised: JsonSchemaValue
) -> Iterator[tuple[str, dict[str, JsonSchemaValue]]]:
    if isinstance(raw, dict) and isinstance(advertised, dict):
        if isinstance(pattern := raw.get("pattern"), str):
            yield pattern, advertised
        for key, child in raw.items():
            if key in advertised and key not in {"const", "enum", "default", "examples"}:
                yield from pattern_pairs(child, advertised[key])
    elif isinstance(raw, list) and isinstance(advertised, list):
        for before, after in zip(raw, advertised, strict=True):
            yield from pattern_pairs(before, after)


def advertised_matches(field: dict[str, JsonSchemaValue], value: str) -> bool:
    pattern = field["pattern"]
    assert isinstance(pattern, str)
    if re.search(pattern, value) is None:
        return False
    excluded = field.get("not")
    if excluded is not None:
        assert isinstance(excluded, dict)
        forbidden = excluded["pattern"]
        assert isinstance(forbidden, str)
        if re.search(forbidden, value) is not None:
            return False
    return True


RECORD_TEXT = "Lead sentence.\n\nProblem:\n- `views` drops a line.\n1. Indent it.\n\nReferences: review.md"
RECORD_TEXT_VALUES = (RECORD_TEXT, "x", "  surrounding whitespace  ", "carriage\rreturn", "a\n")


def advertised_tools() -> dict[str, Tool]:
    executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
    try:
        created = server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256))
        return {tool.name: tool for tool in asyncio.run(created.list_tools())}
    finally:
        executor.shutdown()


def schema_field(schema: dict[str, JsonSchemaValue], definition: str, field: str) -> dict[str, JsonSchemaValue]:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    record = definitions[definition]
    assert isinstance(record, dict)
    properties = record["properties"]
    assert isinstance(properties, dict)
    value = properties[field]
    assert isinstance(value, dict)
    return value


def inline_records(schema: JsonSchemaValue, title: str) -> Iterator[dict[str, JsonSchemaValue]]:
    if isinstance(schema, dict):
        if schema.get("title") == title:
            yield schema
        for key, child in schema.items():
            if key not in {"const", "enum", "default", "examples"}:
                yield from inline_records(child, title)
    elif isinstance(schema, list):
        for child in schema:
            yield from inline_records(child, title)


def accepts_record_text(field: dict[str, JsonSchemaValue]) -> bool:
    """Decide JSON Schema acceptance of every record-text value for a field without a pattern."""

    minimum = field.get("minLength")
    return (
        set(field) == {"type", "minLength"}
        and field["type"] == "string"
        and isinstance(minimum, int)
        and all(len(value) >= minimum for value in RECORD_TEXT_VALUES)
        and minimum == 1
    )


class McpSchemaProjectionTest(unittest.TestCase):
    def test_record_text_fields_advertise_line_breaks_and_other_fields_stay_single_line(self) -> None:
        tools = advertised_tools()
        transition = tools[server.TRANSITION_TOOL].input_schema
        dispatch = tools[server.DISPATCH_TOOL].input_schema
        close = tools[server.CLOSE_TOOL].input_schema
        for schema, definition, field in (
            (transition, "ReasonInputPayload", "reason"),
            (transition, "AcceptCheckpointInputPayload", "evidence"),
            (transition, "AcceptReviewAndContinueInputPayload", "evidence"),
            (transition, "ReviseItemInputPayload", "reason"),
        ):
            with self.subTest(definition=definition, field=field):
                self.assertTrue(accepts_record_text(schema_field(schema, definition, field)))
        for review in ("CorrectionSourceReview", "ReusedCoverageCorrectionReview", "LocalCorrectionSourceReview"):
            with self.subTest(review=review):
                records = tuple(inline_records(dispatch, review))
                self.assertTrue(records)
                for record in records:
                    properties = record["properties"]
                    assert isinstance(properties, dict)
                    correction = properties["correction_input"]
                    assert isinstance(correction, dict)
                    fields = correction["properties"]
                    assert isinstance(fields, dict)
                    reason = fields["reason"]
                    assert isinstance(reason, dict)
                    self.assertTrue(accepts_record_text(reason))
        for schema, definition, field in (
            (transition, "RejectProposalInputPayload", "reason"),
            (transition, "BlockInputPayload", "reason"),
            (transition, "EvidenceInputPayload", "evidence"),
            (transition, "DeferInputPayload", "reopen_condition"),
            (transition, "AcceptCheckpointInputPayload", "candidate"),
            (close, "CloseInputPayload", "reason"),
            (close, "CloseInputPayload", "human_decision"),
        ):
            with self.subTest(definition=definition, field=field):
                single_line = schema_field(schema, definition, field)
                self.assertTrue(advertised_matches(single_line, "one line"))
                self.assertFalse(advertised_matches(single_line, RECORD_TEXT))
                self.assertFalse(advertised_matches(single_line, "a\n"))
        for leaf, payload in (
            ("pause", "ReasonInputPayload"),
            ("return-for-correction", "ReasonInputPayload"),
            ("reject-proposal", "RejectProposalInputPayload"),
        ):
            with self.subTest(leaf=leaf):
                request = schema_field(
                    transition, f"ProjectTransitionRequest_typing.Literal__{leaf}____{payload}_", "payload"
                )
                self.assertEqual({"$ref": f"#/$defs/{payload}"}, request)

    def test_dispatch_inlines_the_complete_strict_contract_without_losing_metadata(self) -> None:
        canonical = contract_schemas.schema_for(contracts.DispatchRequest)
        contract_schemas.project_regex_patterns(canonical)
        advertised = advertised_tools()[server.DISPATCH_TOOL].input_schema
        definitions = canonical["$defs"]
        assert isinstance(definitions, dict)

        def compare(expected: JsonSchemaValue, actual: JsonSchemaValue) -> None:
            if isinstance(expected, dict):
                assert isinstance(actual, dict)
                if "$ref" in expected:
                    reference = expected["$ref"]
                    assert isinstance(reference, str)
                    self.assertEqual({"$ref"}, set(expected))
                    compare(definitions[reference.removeprefix("#/$defs/")], actual)
                else:
                    keys = set(expected) - {"$defs", "discriminator"}
                    self.assertEqual(keys, set(actual))
                    for key in keys:
                        compare(expected[key], actual[key])
            elif isinstance(expected, list):
                assert isinstance(actual, list)
                self.assertEqual(len(expected), len(actual))
                for first, second in zip(expected, actual, strict=True):
                    compare(first, second)
            else:
                self.assertEqual(expected, actual)

        compare(canonical, advertised)

    def test_actions_payload_schemas_accept_record_text_only_for_record_fields(self) -> None:
        for kind, definition, field in (
            (decision_models.ActionKind.PAUSE, "ReasonInputPayload", "reason"),
            (decision_models.ActionKind.PAUSE, "PauseDecisionsInput", "reason"),
            (decision_models.ActionKind.RETURN_FOR_CORRECTION, "ReasonInputPayload", "reason"),
            (decision_models.ActionKind.ACCEPT_CHECKPOINT, "AcceptCheckpointInputPayload", "evidence"),
            (decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, "AcceptReviewAndContinueInputPayload", "evidence"),
            (decision_models.ActionKind.REVISE_ITEM, "ReviseItemInputPayload", "reason"),
        ):
            with self.subTest(kind=kind):
                schema = action_models.action_payload_schema(kind)
                assert schema is not None
                self.assertTrue(accepts_record_text(schema_field(schema, definition, field)))
        for kind, definition, field in (
            (decision_models.ActionKind.REJECT_PROPOSAL, "RejectProposalInputPayload", "reason"),
            (decision_models.ActionKind.BLOCK, "BlockInputPayload", "reason"),
            (decision_models.ActionKind.DEFER, "DeferInputPayload", "reopen_condition"),
        ):
            with self.subTest(kind=kind):
                schema = action_models.action_payload_schema(kind)
                assert schema is not None
                pattern = schema_field(schema, definition, field)["pattern"]
                assert isinstance(pattern, str)
                self.assertIsNotNone(re.search(pattern, "one line"))
                self.assertIsNone(re.search(pattern, RECORD_TEXT))

    def test_all_registered_field_patterns_preserve_representative_values(self) -> None:
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        diagnostics = execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)

        def retain_canonical(_schema: dict[str, JsonSchemaValue]) -> None:
            return None

        with patch.object(contract_schemas, "project_regex_patterns", side_effect=retain_canonical):
            raw_server = server.create_server(executor, diagnostics)
        advertised_server = server.create_server(executor, diagnostics)

        async def tools() -> tuple[list[Tool], list[Tool]]:
            return await raw_server.list_tools(), await advertised_server.list_tools()

        raw_tools, advertised_tools = asyncio.run(tools())
        self.assertEqual({tool.name for tool in raw_tools}, {tool.name for tool in advertised_tools})
        samples = (
            "",
            ".",
            "..",
            "...",
            ". a",
            ".. a",
            ". ",
            ".. ",
            ".a.",
            "a",
            "a-b",
            "a_b",
            "a/b",
            "a\\b",
            " a",
            "a ",
            "a\tb",
            "a\n",
            "a\r",
            "a\r\n",
            "a\x00",
            "a|b",
            "0" * 40,
            "0" * 64,
            "0" * 65,
            "working-tree-state-sha256:" + "a" * 64,
            "é",
            "a\u2028",
            "\u0085a",
            "a\u0085",
            "\ufeffa",
            "a\ufeff",
        )
        seen: set[str] = set()
        for raw_tool, advertised_tool in zip(raw_tools, advertised_tools, strict=True):
            self.assertEqual(raw_tool.name, advertised_tool.name)
            for original, projected in (
                (raw_tool.input_schema, advertised_tool.input_schema),
                (raw_tool.output_schema, advertised_tool.output_schema),
            ):
                assert projected is not None
                for canonical, field in pattern_pairs(original, projected):
                    seen.add(canonical)
                    portable = field["pattern"]
                    assert isinstance(portable, str)
                    self.assertFalse(any(marker in portable for marker in (r"\A", r"\z", "(?=", "(?!", "(?<=", "(?<!")))
                    for sample in samples:
                        with self.subTest(tool=raw_tool.name, canonical=canonical, sample=sample):
                            self.assertEqual(
                                re.search(canonical, sample) is not None, advertised_matches(field, sample)
                            )
        self.assertTrue(seen)
