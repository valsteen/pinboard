"""Preparation contracts, selected-source wiring and truthful immutable-output aftermath."""

import asyncio
import io
import json
import subprocess
import tempfile
import unittest
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
        result = mcp_reads._brief_sources(
            {"request": {**self.roots, "operation": operation, **fields}}, mcp_execution.CancellationToken()
        )
        return contract_schemas.validate_result(server.BRIEF_SOURCES_TOOL, result.content)

    def test_negotiated_construction_outputs_complete_canonical_starters_and_publish(self) -> None:
        executor = mcp_execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = server.create_server(
            executor, mcp_execution.Diagnostics(io.StringIO(), event_limit=8, line_limit=256)
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
        plan = self.sources("plan", manifest=self.manifest, max_batch_bytes=18)
        valid = (
            {"operation": "plan", "manifest": self.manifest, "max_batch_bytes": 18},
            {
                "operation": "plan-to-file",
                "manifest": self.manifest,
                "max_batch_bytes": 18,
                "destination": str(self.project / "plan"),
            },
            {"operation": "emit", "plan": plan, "batch_index": 0},
            {"operation": "emit-file", "plan_path": str(self.project / "plan"), "batch_index": 0},
        )
        invalid = [{**leaf, "unexpected": True} for leaf in valid] + [
            {**valid[0], "max_batch_bytes": 0},
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
                    self.sources("plan", manifest=self.manifest, max_batch_bytes=18, project_root=root)["code"],
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

    def test_inline_saved_and_selected_checkout_batches_preserve_facts(self) -> None:
        with patch.object(
            mcp_common, "_resolve_durable", side_effect=AssertionError("source preparation touched state")
        ):
            plan = self.sources("plan", manifest=self.manifest, max_batch_bytes=18)
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
                    [Path("a.md")], [Path(*call.args[1].relative_path.parts) for call in reads.call_args_list]
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

    def test_selected_output_create_reuse_collision_and_sync_failure(self) -> None:
        destination = self.project / "output.json"
        fields = {"manifest": self.manifest, "max_batch_bytes": 100, "destination": str(destination)}
        created = self.sources("plan-to-file", **fields)
        self.assertEqual(
            (True, "committed", ["selected-output"]),
            (created["created"], created["effect"], created["changed_surfaces"]),
        )
        plan = brief_source_codec.decode_brief_source_plan(destination.read_bytes())
        assert not isinstance(plan, brief_source_models.BriefSourceFailure)
        self.assertEqual(destination.read_bytes(), brief_source_codec.encode_brief_source_plan(plan))
        self.assertEqual("unchanged", self.sources("plan-to-file", **fields)["effect"])
        collision = self.sources("plan-to-file", **{**fields, "max_batch_bytes": 18})
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
                "max_batch_bytes": 100,
                "destination": str(destination),
            }
        }
        with (
            patch.object(mcp_reads.brief_sources, "plan_brief_sources", side_effect=cancel_after_plan),
            self.assertRaises(mcp_execution.OperationCancelled),
        ):
            mcp_reads._brief_sources(request, token)
        self.assertFalse(destination.exists())
        token = mcp_execution.CancellationToken()
        publish = mcp_reads.create_immutable

        def cancel_after_visibility(path: Path, content: bytes) -> bool:
            created = publish(path, content)
            token.cancel()
            return created

        with patch.object(mcp_reads, "create_immutable", side_effect=cancel_after_visibility):
            result = mcp_reads._brief_sources(request, token).content
        self.assertEqual("committed", result["effect"])
        self.assertTrue(destination.exists())
