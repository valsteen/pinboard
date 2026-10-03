"""Preparation contracts, selected-source wiring and truthful immutable-output aftermath."""

import asyncio
import hashlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from mcp_types import CallToolResult

from pinboard.adapters.files import file_io
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.application import (
    brief_source_codec,
    brief_source_models,
    brief_sources,
    work_brief_contract,
    work_briefs,
)
from pinboard.mcp import common as mcp_common
from pinboard.mcp import contract_schemas, contracts, server
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import read_operations as mcp_reads
from tests import test_mcp
from tests.test_work_brief_contract import complete_starter, json_object, select_structural_variant
from tests.work_brief_support import example_work_brief, work_c_brief


class McpBriefPreparationTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(self.project)), check=True)
        self.roots = {"project_root": str(self.project), "work_root": str(self.project / "absent-state")}
        (self.project / "a.md").write_bytes(b"# A\r\nfirst\r\n## B\r\nsecond")
        (self.project / "empty").write_bytes(b"")
        self.manifest: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-brief-sources/v1",
            "sources": [
                {"authority_id": "a", "selector": "a.md#A", "families": ["contract"]},
                {"authority_id": "empty", "selector": "empty", "families": ["contract"]},
            ],
        }

    def sources(self, operation: str, **fields: contracts.JsonValue) -> dict[str, contracts.JsonValue]:
        owner = mcp_reads._brief_source_plan_output if operation == "plan-to-file" else mcp_reads._brief_sources
        tool = server.BRIEF_SOURCE_PLAN_OUTPUT_TOOL if operation == "plan-to-file" else server.BRIEF_SOURCES_TOOL
        result = owner({"request": {**self.roots, "operation": operation, **fields}}, mcp_execution.CancellationToken())
        return contract_schemas.validate_result(tool, result.content)

    def test_negotiated_construction_outputs_complete_canonical_starters_and_publish(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = server.create_server(
            executor,
            mcp_execution.Diagnostics(io.StringIO(), event_limit=8, line_limit=256),
        )
        temporary, project, roots = test_mcp.McpTransportTest()._project()
        self.addCleanup(temporary.cleanup)

        async def scenario() -> None:
            full = await transport.call_tool(
                server.BRIEF_CONTRACT_TOOL, {"request": {**self.roots, "operation": "full"}}
            )
            assert isinstance(full, CallToolResult)
            self.assertEqual(
                msgspec.json.decode(msgspec.json.encode(work_brief_contract.describe_work_brief_contract())),
                full.structured_content,
            )
            for boundary, completed in (("local", work_c_brief()), ("cross-boundary", example_work_brief())):
                result = await transport.call_tool(
                    server.BRIEF_CONTRACT_TOOL,
                    {"request": {**self.roots, "operation": "starter", "boundary": boundary}},
                )
                assert isinstance(result, CallToolResult)
                contract = msgspec.json.decode(
                    msgspec.json.encode(result.structured_content), type=work_brief_contract.WorkBriefStarterContract
                )
                template = msgspec.json.decode(bytes(contract.starter))
                choices = {choice.choice_id: choice for choice in contract.structural_choices}
                select_structural_variant(
                    template, choices["architecture-impact"], "$.checkpoint.architecture_impact", "update-required"
                )
                if boundary == "cross-boundary":
                    select_structural_variant(
                        template,
                        choices["authorization-basis"],
                        "$.checkpoint.verification[*].authorization_basis",
                        "repository-policy",
                    )
                payload = json_object(complete_starter(template, msgspec.json.decode(msgspec.json.encode(completed))))
                self.assertEqual(completed, work_briefs.decode_work_brief(msgspec.json.encode(payload)))
                if boundary == "local":
                    published = await transport.call_tool(
                        server.BRIEF_PUBLISH_TOOL,
                        {
                            "project_root": str(project),
                            "work_root": str(roots.work_root),
                            "brief": payload,
                        },
                    )
                    assert isinstance(published, CallToolResult) and isinstance(published.structured_content, dict)
                    self.assertEqual("committed", published.structured_content["status"])

        with patch.object(mcp_common, "_resolve_durable", side_effect=AssertionError("construction touched state")):
            # Construction uses nonexistent roots as context, not a store capability.
            result = mcp_reads._brief_contract(
                {"request": {**self.roots, "operation": "full"}}, mcp_execution.CancellationToken()
            )
            contract_schemas.validate_result(server.BRIEF_CONTRACT_TOOL, result.content)
        asyncio.run(scenario())

    def test_strict_leaf_matrix_rejects_before_root_source_or_output_effects(self) -> None:
        plan = self.sources("plan", manifest=self.manifest, max_batch_bytes=500)
        valid = (
            {"operation": "plan", "manifest": self.manifest, "max_batch_bytes": 500},
            {
                "operation": "plan-to-file",
                "manifest": self.manifest,
                "max_batch_bytes": 500,
                "destination": str(self.project / "plan"),
            },
            {"operation": "emit", "plan": plan, "batch_index": 0},
            {"operation": "emit-file", "plan_path": str(self.project / "plan"), "batch_index": 0},
        )
        invalid = [{**leaf, "unexpected": True} for leaf in valid] + [
            {**valid[0], "max_batch_bytes": 0},
            valid[1],
            {**valid[2], "batch_index": -1},
            {**valid[2], "plan_path": "mixed"},
            {**valid[3], "plan": plan},
            {**valid[0], "actor_task_id": "forbidden"},
            {**valid[0], "manifest": {**self.manifest, "extra": True}},
        ]
        with patch.object(
            mcp_reads, "resolve_source_checkout_root", side_effect=AssertionError("invalid request read roots")
        ):
            for leaf in invalid:
                with self.subTest(leaf=leaf):
                    result = mcp_reads._brief_sources(
                        {"request": {**self.roots, **leaf}}, mcp_execution.CancellationToken()
                    ).content
                    self.assertEqual("BRIEF_SOURCES_REQUEST_INVALID", result["code"])
                    contract_schemas.validate_result(server.BRIEF_SOURCES_TOOL, result)
            for root in ("", "bad\x00root"):
                self.assertEqual(
                    "BRIEF_SOURCES_REQUEST_INVALID",
                    self.sources("plan", manifest=self.manifest, max_batch_bytes=500, project_root=root)["code"],
                )
        for fields in (
            {"operation": "full", "boundary": "local"},
            {"operation": "starter", "boundary": "invalid"},
            {"operation": "starter"},
        ):
            result = mcp_reads._brief_contract(
                {"request": {**self.roots, **fields}}, mcp_execution.CancellationToken()
            ).content
            self.assertEqual("BRIEF_CONTRACT_REQUEST_INVALID", result["code"])
            contract_schemas.validate_result(server.BRIEF_CONTRACT_TOOL, result)

    def test_read_tool_rejects_plan_publication_at_the_negotiated_boundary(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )
        destination = self.project / "forbidden.json"

        async def call() -> CallToolResult:
            result = await transport.call_tool(
                server.BRIEF_SOURCES_TOOL,
                {
                    "request": {
                        **self.roots,
                        "operation": "plan-to-file",
                        "manifest": self.manifest,
                        "max_batch_bytes": 500,
                        "destination": str(destination),
                    }
                },
            )
            assert isinstance(result, CallToolResult)
            return result

        result = asyncio.run(call())
        assert isinstance(result.structured_content, dict)
        self.assertEqual("BRIEF_SOURCES_REQUEST_INVALID", result.structured_content["code"])
        self.assertFalse(destination.exists())

    def test_inline_saved_and_selected_checkout_batches_preserve_facts(self) -> None:
        with patch.object(
            mcp_common, "_resolve_durable", side_effect=AssertionError("source preparation touched state")
        ):
            plan = self.sources("plan", manifest=self.manifest, max_batch_bytes=500)
            first = self.sources("emit", plan=plan, batch_index=0)
            saved = self.project / "saved.json"
            saved.write_text(json.dumps(plan, indent=1), encoding="utf-8")
            self.assertEqual(first, self.sources("emit-file", plan_path=str(saved), batch_index=0))
            self.assertEqual("BRIEF_SOURCE_BATCH_NOT_FOUND", self.sources("emit", plan=plan, batch_index=100)["code"])
            with patch.object(
                mcp_reads, "select_checkout_brief_source", wraps=mcp_reads.select_checkout_brief_source
            ) as reads:
                self.sources("emit", plan=plan, batch_index=0)
                self.assertEqual(
                    [Path("a.md"), Path("empty")],
                    [Path(*call.args[1].relative_path.parts) for call in reads.call_args_list],
                )
            text = str(first["text"])
            self.assertIn("# A\nfirst\n## B\n", text)
            self.assertNotIn("\r", text)
            (self.project / "a.md").write_text("# A\nchanged\n", encoding="utf-8")
            self.assertEqual("BRIEF_SOURCE_CHANGED", self.sources("emit", plan=plan, batch_index=0)["code"])
            self.assertEqual(
                "BRIEF_SOURCE_PLAN_INVALID",
                self.sources("emit-file", plan_path=str(self.project / "missing"), batch_index=0)["code"],
            )
        self.assertFalse(Path(self.roots["work_root"]).exists())

    def test_escaped_batches_are_complete_bounded_and_resumable_from_a_saved_plan(self) -> None:
        content = (b'"\\\n' * 1_500) + b"last\n"
        (self.project / "a.md").write_bytes(content)
        manifest: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-brief-sources/v1",
            "sources": [{"authority_id": "a", "selector": "a.md", "families": ["contract"]}],
        }
        destination = self.project / "source-plan.json"
        receipt = self.sources("plan-to-file", manifest=manifest, max_batch_bytes=4_000, destination=str(destination))
        self.assertTrue(receipt["created"])
        plan = brief_source_codec.decode_brief_source_plan(destination.read_bytes())
        assert not isinstance(plan, brief_source_models.BriefSourceFailure)
        self.assertGreater(len(plan.batches), 1)
        batches = [
            self.sources("emit-file", plan_path=str(destination), batch_index=i) for i in range(len(plan.batches))
        ]
        self.assertEqual(batches[1], self.sources("emit-file", plan_path=str(destination), batch_index=1))
        for batch in batches:
            size = batch["presented_byte_count"]
            assert isinstance(size, int)
            self.assertEqual(len(msgspec.json.encode(batch)), size)
            self.assertLessEqual(size, 4_000)
        selected = b"".join(
            match.group(1).encode()
            for batch in batches
            for match in re.finditer(r"===== BEGIN[^\n]*=====\n(.*?)===== END", str(batch["text"]), re.DOTALL)
        )
        self.assertEqual(content, selected)
        self.assertEqual(hashlib.sha256(content).hexdigest(), plan.sources[0].selected_sha256)
        self.assertFalse(Path(self.roots["work_root"]).exists())

    def test_oversized_line_and_older_plan_reject_before_emission(self) -> None:
        (self.project / "a.md").write_bytes(b"x" * 8_000 + b"\n")
        manifest: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-brief-sources/v1",
            "sources": [{"authority_id": "a", "selector": "a.md", "families": ["contract"]}],
        }
        line = self.sources("plan", manifest=manifest, max_batch_bytes=8_000)
        self.assertEqual("BRIEF_SOURCE_LINE_TOO_LARGE", line["code"])
        self.assertRegex(str(line["message"]), r"requires 8\d{3} presented bytes; limit is 8000")
        (self.project / "second.md").write_bytes(b"y" * 8_000 + b"\n")
        manifest["sources"] = [
            {"authority_id": "a", "selector": "a.md", "families": ["contract"]},
            {"authority_id": "second", "selector": "second.md", "families": ["contract"]},
        ]
        planned = brief_sources.plan_brief_sources(
            lambda selector, require_utf8: mcp_reads.select_checkout_brief_source(self.project, selector, require_utf8),
            msgspec.convert(manifest, type=brief_source_models.BriefSourceManifest),
            24_000,
        )
        assert not isinstance(planned, brief_source_models.BriefSourceFailure)
        segments = tuple(segment for source in planned.sources for segment in source.segments)
        older = replace(
            planned,
            max_batch_bytes=50_000,
            batches=(
                brief_source_models.BriefSourceBatch(
                    0,
                    sum(segment.content_byte_count for segment in segments),
                    sum(batch.estimated_rendered_byte_count for batch in planned.batches),
                    segments,
                ),
            ),
        )
        saved = self.project / "older.json"
        saved.write_bytes(brief_source_codec.encode_brief_source_plan(older))
        with patch.object(
            mcp_reads, "select_checkout_brief_source", side_effect=AssertionError("oversized plan read source")
        ):
            failure = self.sources("emit-file", plan_path=str(saved), batch_index=0)
        self.assertEqual("BRIEF_SOURCE_PLAN_INVALID", failure["code"])
        self.assertIn("Create a smaller plan", str(failure["message"]))

    def test_native_transport_envelope_stays_below_the_observed_truncation_size(self) -> None:
        (self.project / "a.md").write_bytes((b"\\" * 20 + b"\n") * 400)
        manifest: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-brief-sources/v1",
            "sources": [{"authority_id": "a", "selector": "a.md", "families": ["contract"]}],
        }
        plan = self.sources("plan", manifest=manifest, max_batch_bytes=50_000)
        self.assertEqual(brief_sources.MAX_PRESENTED_BATCH_BYTES, plan["max_batch_bytes"])
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        capture = mcp_execution.AutomaticCapture(mcp_common.select_capture_item)
        transport = server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
        )

        async def emit() -> CallToolResult:
            result = await transport.call_tool(
                server.BRIEF_SOURCES_TOOL,
                {"request": {**self.roots, "operation": "emit", "plan": plan, "batch_index": 0}},
            )
            assert isinstance(result, CallToolResult)
            return result

        with patch.object(capture, "resolve", side_effect=AssertionError("read tool started write preflight")):
            result = asyncio.run(emit())
        assert isinstance(result.structured_content, dict)
        self.assertLessEqual(result.structured_content["presented_byte_count"], 16_000)
        self.assertLess(len(msgspec.json.encode(result.model_dump(mode="json", by_alias=True))), 50_000)

    def test_many_source_inline_plan_rejects_with_complete_size_and_read_only_route(self) -> None:
        sources: list[contracts.JsonValue] = []
        for index in range(100):
            name = f"source-{index}.md"
            (self.project / name).write_text(f"# Source {index}\nbody\n", encoding="utf-8")
            sources.append({"authority_id": f"source-{index}", "selector": name, "families": ["contract"]})
        manifest: dict[str, contracts.JsonValue] = {"schema": "pinboard-brief-sources/v1", "sources": sources}
        complete = brief_sources.plan_brief_sources(
            lambda selector, require_utf8: mcp_reads.select_checkout_brief_source(self.project, selector, require_utf8),
            msgspec.convert(manifest, type=brief_source_models.BriefSourceManifest),
            16_000,
        )
        assert not isinstance(complete, brief_source_models.BriefSourceFailure)
        expected_size = len(
            msgspec.json.encode(msgspec.to_builtins(brief_source_codec.project_brief_source_plan(complete)))
        )
        self.assertGreater(expected_size, 16_000)
        rejected = self.sources("plan", manifest=manifest, max_batch_bytes=16_000)
        self.assertEqual("BRIEF_SOURCE_PLAN_INVALID", rejected["code"])
        self.assertIn(f"{expected_size} presented bytes; limit is 16000", str(rejected["message"]))
        self.assertIn("smaller source selections", str(rejected["message"]))
        for start in range(0, len(sources), 20):
            subset = sources[start : start + 20]
            smaller = self.sources("plan", manifest={**manifest, "sources": subset}, max_batch_bytes=16_000)
            smaller_sources = smaller["sources"]
            assert isinstance(smaller_sources, (list, tuple))
            self.assertEqual(len(subset), len(smaller_sources))
            self.assertLessEqual(len(msgspec.json.encode(smaller)), 16_000)
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256)
        )

        async def plan_subset() -> CallToolResult:
            result = await transport.call_tool(
                server.BRIEF_SOURCES_TOOL,
                {
                    "request": {
                        **self.roots,
                        "operation": "plan",
                        "manifest": {**manifest, "sources": sources[:20]},
                        "max_batch_bytes": 16_000,
                    }
                },
            )
            assert isinstance(result, CallToolResult)
            return result

        native_plan = asyncio.run(plan_subset())
        assert isinstance(native_plan.structured_content, dict)
        self.assertLessEqual(len(msgspec.json.encode(native_plan.structured_content)), 16_000)
        self.assertLess(len(msgspec.json.encode(native_plan.model_dump(mode="json", by_alias=True))), 50_000)

    def test_native_read_only_plan_and_all_batches_complete_the_source(self) -> None:
        content = (b'"\\\n' * 1_500) + b"last\n"
        (self.project / "a.md").write_bytes(content)
        manifest: dict[str, contracts.JsonValue] = {
            "schema": "pinboard-brief-sources/v1",
            "sources": [{"authority_id": "a", "selector": "a.md", "families": ["contract"]}],
        }
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        capture = mcp_execution.AutomaticCapture(mcp_common.select_capture_item)
        transport = server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
        )

        async def read() -> tuple[CallToolResult, list[CallToolResult]]:
            planned = await transport.call_tool(
                server.BRIEF_SOURCES_TOOL,
                {"request": {**self.roots, "operation": "plan", "manifest": manifest, "max_batch_bytes": 4_000}},
            )
            assert isinstance(planned, CallToolResult) and isinstance(planned.structured_content, dict)
            batches: list[CallToolResult] = []
            for index in range(len(planned.structured_content["batches"])):
                batch = await transport.call_tool(
                    server.BRIEF_SOURCES_TOOL,
                    {
                        "request": {
                            **self.roots,
                            "operation": "emit",
                            "plan": planned.structured_content,
                            "batch_index": index,
                        }
                    },
                )
                assert isinstance(batch, CallToolResult)
                batches.append(batch)
            return planned, batches

        with patch.object(capture, "resolve", side_effect=AssertionError("read-only review started capture preflight")):
            planned, batches = asyncio.run(read())
        assert isinstance(planned.structured_content, dict)
        self.assertGreater(len(batches), 1)
        self.assertLessEqual(len(msgspec.json.encode(planned.structured_content)), 16_000)
        self.assertLess(len(msgspec.json.encode(planned.model_dump(mode="json", by_alias=True))), 50_000)
        selected = b"".join(
            match.group(1).encode()
            for batch in batches
            for match in re.finditer(
                r"===== BEGIN[^\n]*=====\n(.*?)===== END", str(batch.structured_content["text"]), re.DOTALL
            )
        )
        self.assertEqual(content, selected)
        self.assertEqual(
            hashlib.sha256(content).hexdigest(), planned.structured_content["sources"][0]["selected_sha256"]
        )
        self.assertFalse(Path(self.roots["work_root"]).exists())

    def test_plan_publication_with_capture_enabled_writes_only_selected_output(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        capture = mcp_execution.AutomaticCapture(mcp_common.select_capture_item)
        transport = server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
        )
        destination = self.project / "published-plan.json"

        async def publish() -> CallToolResult:
            result = await transport.call_tool(
                server.BRIEF_SOURCE_PLAN_OUTPUT_TOOL,
                {
                    "request": {
                        **self.roots,
                        "operation": "plan-to-file",
                        "manifest": self.manifest,
                        "max_batch_bytes": 500,
                        "destination": str(destination),
                    }
                },
            )
            assert isinstance(result, CallToolResult)
            return result

        with patch.object(capture, "resolve", side_effect=AssertionError("plan publication started capture preflight")):
            result = asyncio.run(publish())
        assert isinstance(result.structured_content, dict)
        self.assertEqual("committed", result.structured_content["effect"])
        self.assertEqual(
            {"a.md", "empty", "published-plan.json"}, {path.name for path in self.project.iterdir() if path.is_file()}
        )
        self.assertFalse(Path(self.roots["work_root"]).exists())

    def test_selected_output_create_reuse_collision_and_sync_failure(self) -> None:
        destination = self.project / "output.json"
        fields = {"manifest": self.manifest, "max_batch_bytes": 500, "destination": str(destination)}
        created = self.sources("plan-to-file", **fields)
        self.assertEqual(
            (True, "committed", ["selected-output"]),
            (created["created"], created["effect"], created["changed_surfaces"]),
        )
        plan = brief_source_codec.decode_brief_source_plan(destination.read_bytes())
        assert not isinstance(plan, brief_source_models.BriefSourceFailure)
        self.assertEqual(destination.read_bytes(), brief_source_codec.encode_brief_source_plan(plan))
        self.assertEqual("unchanged", self.sources("plan-to-file", **fields)["effect"])
        collision = self.sources("plan-to-file", **{**fields, "max_batch_bytes": 400})
        self.assertEqual("FILE_ALREADY_EXISTS", collision["code"])
        failing = self.project / "visible.json"
        with patch.object(
            file_io,
            "_sync_directory",
            side_effect=FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "controlled sync failure"),
        ):
            failure = self.sources("plan-to-file", **{**fields, "destination": str(failing)})
        self.assertEqual(
            ("committed-effect", "do-not-retry", str(failing)),
            (failure["status"], failure["retry"], failure["destination"]),
        )
        self.assertEqual(destination.read_bytes(), failing.read_bytes())

    def test_cancellation_before_publication_and_after_visibility_is_truthful(self) -> None:
        destination = self.project / "cancelled.json"
        token = mcp_execution.CancellationToken()
        original = mcp_reads.brief_sources.plan_brief_sources

        def cancel_after_plan(
            select_source: brief_sources.BriefSourceSelector,
            manifest: brief_source_models.BriefSourceManifest,
            max_batch_bytes: int,
        ) -> brief_source_models.BriefSourceResult[brief_source_models.BriefSourcePlan]:
            plan = original(select_source, manifest, max_batch_bytes)
            token.cancel()
            return plan

        request: dict[str, contracts.JsonValue] = {
            "request": {
                **self.roots,
                "operation": "plan-to-file",
                "manifest": self.manifest,
                "max_batch_bytes": 500,
                "destination": str(destination),
            }
        }
        with (
            patch.object(mcp_reads.brief_sources, "plan_brief_sources", side_effect=cancel_after_plan),
            self.assertRaises(mcp_execution.OperationCancelled),
        ):
            mcp_reads._brief_source_plan_output(request, token)
        self.assertFalse(destination.exists())
        token = mcp_execution.CancellationToken()
        publish = mcp_reads.create_immutable

        def cancel_after_visibility(path: Path, content: bytes) -> bool:
            created = publish(path, content)
            token.cancel()
            return created

        with patch.object(mcp_reads, "create_immutable", side_effect=cancel_after_visibility):
            result = mcp_reads._brief_source_plan_output(request, token).content
        self.assertEqual("committed", result["effect"])
        self.assertTrue(destination.exists())
