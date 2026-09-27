"""The advertised field formats preserve current canonical request and result rules."""

import asyncio
import io
import re
import unittest
from collections.abc import Iterator
from unittest.mock import patch

from mcp_types import Tool

from pinboard.mcp import contract_schemas, execution, server
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
    guards = field.get("allOf", [])
    assert isinstance(guards, list)
    for guard in guards:
        assert isinstance(guard, dict)
        excluded = guard["not"]
        assert isinstance(excluded, dict)
        forbidden = excluded["pattern"]
        assert isinstance(forbidden, str)
        if re.search(forbidden, value) is not None:
            return False
    return True


class McpSchemaProjectionTest(unittest.TestCase):
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
        self.assertEqual(21, len(advertised_tools))
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
