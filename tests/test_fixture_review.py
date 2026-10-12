"""Verified decoded fixture paging preserves changed data and source effects."""

import asyncio
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from mcp_types import CallToolResult

from pinboard.mcp import contract_schemas, contracts, execution, read_operations, server


class FixtureReviewTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.before = self.root / "before.json"
        self.after = self.root / "after.json"
        self.body = "cause café 音" * 1000
        self.write(self.before, {"failure": {"frames": "old\n" + self.body + "\nend", "flag": False}})
        self.write(self.after, {"failure": {"frames": "new\n" + self.body + "\nlast", "flag": True}})
        self.request: dict[str, contracts.JsonValue] = {
            "operation": "start",
            "project_root": str(self.root),
            "work_root": str(self.root / "absent"),
            "before": self.selection(self.before),
            "after": self.selection(self.after),
            "recipe": {"repeat_text": "cause café 音", "repeat_count": 1000},
            "max_batch_bytes": 4000,
        }

    def write(self, path: Path, value: contracts.JsonValue) -> None:
        path.write_text(json.dumps({"case": json.dumps(value, ensure_ascii=False)}, ensure_ascii=False))

    def selection(self, path: Path) -> dict[str, contracts.JsonValue]:
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def call(self, request: dict[str, contracts.JsonValue]) -> dict[str, contracts.JsonValue]:
        result = read_operations._fixture_review({"request": request}, execution.CancellationToken())
        content: dict[str, contracts.JsonValue] = msgspec.json.decode(
            msgspec.json.encode(contract_schemas.validate_result("pinboard_fixture_review", result.content))
        )

        return content

    def read_all(self) -> tuple[str, list[dict[str, contracts.JsonValue]]]:
        request = self.request
        chunks: list[str] = []
        pages: list[dict[str, contracts.JsonValue]] = []
        while True:
            page = self.call(request)
            self.assertEqual("ok", page["status"], page)
            budget = request["max_batch_bytes"]
            assert isinstance(budget, int)
            self.assertLessEqual(len(msgspec.json.encode(page)), budget)
            text = page["text"]
            assert isinstance(text, str)
            chunks.append(text)
            pages.append(page)
            continuation = page["continuation"]
            if continuation is None:
                return "".join(chunks), pages
            assert isinstance(continuation, dict)
            request = continuation

    def test_identical_unicode_substring_preserves_changed_frames_and_fields(self) -> None:
        text, pages = self.read_all()
        self.assertIn("unchanged-repeat", text)
        self.assertIn(hashlib.sha256(self.body.encode()).hexdigest(), text)
        self.assertNotIn(self.body, text)
        self.assertIn("old", text)
        self.assertIn("new", text)
        self.assertIn("last", text)
        self.assertIn("flag", text)
        records = [json.loads(line) for line in text.splitlines()]
        frame = next(record for record in records if record["kind"] == "unchanged-repeat")
        self.assertEqual("old\n" + self.body + "\nend", frame["before_prefix"] + self.body + frame["before_suffix"])
        self.assertEqual("new\n" + self.body + "\nlast", frame["after_prefix"] + self.body + frame["after_suffix"])
        self.assertEqual(len(self.body.encode()), frame["body"]["size_bytes"])
        self.assertEqual(text, self.read_all()[0])
        self.assertTrue(all(page["effect"] == "unchanged" for page in pages))
        self.assertFalse((self.root / "absent").exists())

    def test_source_mismatch_rejects_before_decode_without_writes(self) -> None:
        original = self.before.read_bytes()
        self.before.write_bytes(original + b" ")
        result = self.call(self.request)
        self.assertEqual("rejected", result["status"])
        self.assertIn("SHA-256", str(result["message"]))
        self.assertEqual([], result["changed_surfaces"])
        self.assertEqual(original + b" ", self.before.read_bytes())

    def test_oversized_duplicate_keys_reject_within_usable_native_budget(self) -> None:
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256))

        async def scenario() -> None:
            key = "café 音" * 4000
            duplicate = json.dumps(key, ensure_ascii=False)
            for raw in (
                json.dumps({"case": f"{{{duplicate}:1,{duplicate}:2}}"}, ensure_ascii=False).encode(),
                f'{{{duplicate}:"null",{duplicate}:"true"}}'.encode(),
            ):
                with self.subTest(envelope=raw.startswith(b'{"case"')):
                    self.before.write_bytes(raw)
                    request = {**self.request, "before": self.selection(self.before), "max_batch_bytes": 16000}
                    sources = (self.before.read_bytes(), self.after.read_bytes())
                    result = await transport.call_tool(server.FIXTURE_REVIEW_TOOL, {"request": request})
                    assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
                    rejection = result.structured_content
                    self.assertEqual("rejected", rejection["status"])
                    self.assertLessEqual(len(msgspec.json.encode(rejection)), 16000)
                    self.assertEqual(str(self.before), rejection["resource"])
                    self.assertIn("Duplicate JSON key", rejection["message"])
                    self.assertIn("operation start", rejection["recovery"])
                    self.assertEqual("correct-input", rejection["retry"])
                    self.assertEqual("unchanged", rejection["effect"])
                    self.assertFalse(rejection["state_changed"])
                    self.assertEqual([], rejection["changed_surfaces"])
                    self.assertEqual(sources, (self.before.read_bytes(), self.after.read_bytes()))
            self.assertFalse((self.root / "absent").exists())

        asyncio.run(scenario())

    def test_rejection_budget_preserves_resource_or_diagnoses_unusable_budget(self) -> None:
        path = self.root / ("音" * 300)
        request: dict[str, contracts.JsonValue] = {
            **self.request,
            "before": {"path": str(path), "sha256": "0" * 64},
            "max_batch_bytes": 1800,
        }
        result = self.call(request)
        self.assertEqual("rejected", result["status"])
        self.assertLessEqual(len(msgspec.json.encode(result)), 1800)
        self.assertEqual(str(path), result["resource"])
        self.assertIn("operation start", str(result["recovery"]))
        self.assertEqual("unchanged", result["effect"])
        self.assertEqual("correct-input", result["retry"])
        unusable = self.call({**request, "max_batch_bytes": 1})
        self.assertEqual("rejected", unusable["status"])
        self.assertEqual("max_batch_bytes", unusable["resource"])
        self.assertEqual("correct-input", unusable["retry"])
        valid_source = self.call({**self.request, "max_batch_bytes": 1})
        self.assertEqual("max_batch_bytes", valid_source["resource"])
        self.assertEqual("unchanged", valid_source["effect"])

    def test_mutated_bulk_remains_available_in_complete_bounded_pages(self) -> None:
        changed = self.body[:-1] + "改"
        self.write(self.after, {"failure": {"frames": "new\n" + changed + "\nlast", "flag": True}})
        self.request["after"] = self.selection(self.after)
        text, pages = self.read_all()
        self.assertGreater(len(pages), 2)
        self.assertNotIn("unchanged-repeat", text)
        records = [json.loads(line) for line in text.splitlines()]
        frame = next(record for record in records if record["path"][-1] == "frames")
        self.assertEqual("new\n" + changed + "\nlast", frame["after"])

    def test_changed_new_removed_values_and_json_types_are_lossless(self) -> None:
        self.write(self.before, {"removed": self.body, "null": None, "type": False, "frames": ["a", "b"]})
        self.write(self.after, {"added": self.body, "type": 0, "frames": ["a", "c", "d"]})
        self.request["before"] = self.selection(self.before)
        self.request["after"] = self.selection(self.after)
        text, _pages = self.read_all()
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            {
                ("case", "removed"),
                ("case", "added"),
                ("case", "null"),
                ("case", "type"),
                ("case", "frames", 1),
                ("case", "frames", 2),
            },
            {tuple(r["path"]) for r in records},
        )
        removed = next(r for r in records if r["path"][-1] == "removed")
        added = next(r for r in records if r["path"][-1] == "added")
        self.assertEqual(self.body, removed["before"])
        self.assertEqual(self.body, added["after"])
        self.assertFalse(removed["after_present"])
        self.assertFalse(added["before_present"])

    def test_ambiguous_repetition_and_literal_marker_shapes_cannot_hide_data(self) -> None:
        self.write(
            self.before, {"x": "before" + self.body + self.body, "marker": {"kind": "unchanged-repeat", "body": "old"}}
        )
        self.write(
            self.after, {"x": "after" + self.body + self.body, "marker": {"kind": "unchanged-repeat", "body": "new"}}
        )
        self.request["before"] = self.selection(self.before)
        self.request["after"] = self.selection(self.after)
        text, _pages = self.read_all()
        records = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(all(r["kind"] == "value-change" for r in records))
        self.assertEqual("after" + self.body + self.body, next(r for r in records if r["path"][-1] == "x")["after"])
        self.assertEqual("new", next(r for r in records if r["path"][-1] == "body")["after"])

    def test_invalid_sources_and_requests_reject_readonly(self) -> None:
        for raw in (
            b"[]",
            b'{"a":1}',
            b'{"a":"null","a":"true"}',
            b'{"a":"{\\"x\\":1,\\"x\\":2}"}',
            b'{"a":"NaN"}',
            b'{"a":"\\"\\\\ud800\\""}',
            b"\xff",
            b'{"a":"invalid"}',
        ):
            with self.subTest(raw=raw):
                self.before.write_bytes(raw)
                self.request["before"] = self.selection(self.before)
                result = self.call(self.request)
                self.assertEqual("rejected", result["status"])
                self.assertEqual("unchanged", result["effect"])
                self.assertIn(str(self.before), str(result["resource"]))
        invalid_fields: tuple[dict[str, contracts.JsonValue], ...] = (
            {"extra": 1},
            {"operation": "continue"},
            {"max_batch_bytes": 0},
            {"recipe": {"repeat_text": "", "repeat_count": 1}},
            {"max_batch_bytes": 1},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                self.assertEqual("rejected", self.call({**self.request, **fields})["status"])
        self.before.unlink()
        self.assertEqual("rejected", self.call(self.request)["status"])
        self.assertFalse((self.root / "absent").exists())

    def test_continuation_binds_sources_recipe_and_exact_view(self) -> None:
        self.request["max_batch_bytes"] = 1800
        self.request["recipe"] = {"repeat_text": "absent", "repeat_count": 1}
        first = self.call(self.request)
        continuation = first["continuation"]
        assert isinstance(continuation, dict)
        self.assertEqual("rejected", self.call({**continuation, "view_sha256": "0" * 64})["status"])
        self.assertEqual("rejected", self.call({**continuation, "start_character": 10**9})["status"])
        self.assertEqual(
            "rejected", self.call({**continuation, "recipe": {"repeat_text": "other", "repeat_count": 1}})["status"]
        )
        self.after.write_bytes(self.after.read_bytes() + b" ")
        self.assertEqual("rejected", self.call(continuation)["status"])

    def test_impossible_recipe_does_not_allocate_and_equal_values_have_no_changes(self) -> None:
        self.request["recipe"] = {"repeat_text": "音", "repeat_count": 10**18}
        text, _pages = self.read_all()
        self.assertNotIn('"kind":"unchanged-repeat"', text)
        self.after.write_bytes(self.before.read_bytes())
        self.request["after"] = self.selection(self.after)
        self.assertEqual("\n", self.read_all()[0])

    def test_native_schema_and_all_pages_skip_even_explicit_capture(self) -> None:
        self.write(self.after, {"failure": {"frames": self.body + "changed"}})
        self.request["after"] = self.selection(self.after)
        self.request["recipe"] = {"repeat_text": "absent", "repeat_count": 1}
        source_bytes = (self.before.read_bytes(), self.after.read_bytes())
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        capture = execution.SemanticCapture(self.root)
        transport = server.create_server(
            executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
        )

        async def scenario() -> None:
            tool = next(tool for tool in await transport.list_tools() if tool.name == server.FIXTURE_REVIEW_TOOL)
            assert tool.annotations is not None
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertIn("FixtureReviewContinueRequest", tool.input_schema["$defs"])
            self.assertFalse(tool.input_schema["additionalProperties"])
            request = self.request
            chunks: list[str] = []
            while True:
                result = await transport.call_tool(server.FIXTURE_REVIEW_TOOL, {"request": request})
                assert isinstance(result, CallToolResult)
                self.assertFalse(result.is_error, result)
                page = result.structured_content
                assert isinstance(page, dict)
                self.assertLessEqual(len(msgspec.json.encode(page)), 4000)
                chunks.append(page["text"])
                continuation = page["continuation"]
                if continuation is None:
                    break
                request = continuation
            self.assertIn(self.body, "".join(chunks))
            rejected = await transport.call_tool(server.FIXTURE_REVIEW_TOOL, {"request": {**self.request, "extra": 1}})
            assert isinstance(rejected, CallToolResult) and isinstance(rejected.structured_content, dict)
            self.assertEqual("rejected", rejected.structured_content["status"])

        with patch.object(capture, "_publish", side_effect=AssertionError("read-only operation published trace")):
            asyncio.run(scenario())
        self.assertEqual(source_bytes, (self.before.read_bytes(), self.after.read_bytes()))
        self.assertEqual({"before.json", "after.json"}, {path.name for path in self.root.iterdir()})

    def test_arbitrary_json_numbers_preserve_changed_digits_and_large_exponents(self) -> None:
        self.before.write_text(json.dumps({"case": '{"n":1.0000000000000001,"huge":1e1000}'}))
        self.after.write_text(json.dumps({"case": '{"n":1.0000000000000002,"huge":2e1000}'}))
        self.request["before"] = self.selection(self.before)
        self.request["after"] = self.selection(self.after)
        text, _pages = self.read_all()
        self.assertIn('"before":1.0000000000000001', text)
        self.assertIn('"after":1.0000000000000002', text)
        self.assertIn('"before":1e1000', text)
        self.assertIn('"after":2e1000', text)

    def test_automatic_capture_is_not_resolved(self) -> None:
        from pinboard.mcp import common  # noqa: PLC0415 - local collaborator for this native effect check

        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        capture = execution.AutomaticCapture(common.select_capture_item)
        transport = server.create_server(
            executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
        )

        async def scenario() -> None:
            result = await transport.call_tool(server.FIXTURE_REVIEW_TOOL, {"request": self.request})
            assert isinstance(result, CallToolResult) and isinstance(result.structured_content, dict)
            self.assertEqual("ok", result.structured_content["status"])

        with patch.object(
            capture, "resolve", side_effect=AssertionError("read-only operation resolved trace settings")
        ):
            asyncio.run(scenario())
        self.assertFalse((self.root / "absent").exists())

    def test_relative_selections_return_bound_absolute_resource_identities(self) -> None:
        relative_before = self.selection(self.before)
        relative_after = self.selection(self.after)
        relative_before["path"] = "before.json"
        relative_after["path"] = "after.json"
        self.request["before"] = relative_before
        self.request["after"] = relative_after
        self.request["recipe"] = {"repeat_text": "absent", "repeat_count": 1}
        first = self.call(self.request)
        self.assertEqual(self.selection(self.before), first["before"])
        continuation = first["continuation"]
        assert isinstance(continuation, dict)
        other = self.root / "other"
        other.mkdir()
        (other / "before.json").write_bytes(self.before.read_bytes())
        (other / "after.json").write_bytes(self.after.read_bytes())
        changed_resources: dict[str, contracts.JsonValue] = {
            **continuation,
            "project_root": str(other),
            "before": relative_before,
            "after": relative_after,
        }
        self.assertEqual("rejected", self.call(changed_resources)["status"])
        self.assertEqual("ok", self.call(continuation)["status"])
