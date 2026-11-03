"""Recorded route facts survive native intake, retained reads and explicit revision."""

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import msgspec
from mcp import Client
from mcp.types import Implementation
from msgspec.structs import replace as replace_struct

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import dispatch_models, project_export, stored_state, work_brief_models, work_briefs
from pinboard.domain import history, work_models
from pinboard.mcp import instruction_models, tool_names
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import native_tools
from tests.support import JsonObject, complete_sqlite_state
from tests.test_proposals import proposal
from tests.work_brief_support import CHECKPOINT_ID, work_a_brief


class RecordedRouteNativeTest(CheckpointPackageSupport):
    def test_small_cross_boundary_checkpoint_accepts_and_reloads_without_brief_review(self) -> None:
        state = complete_sqlite_state()
        current = next(row for row in state.lifecycle.definition_revisions if row.item_id == "work-a")
        assert isinstance(current.definition, work_models.WorkItemDefinition)
        assert current.definition.route_facts is not None
        definition = replace(
            current.definition,
            checkpoints=(work_models.CheckpointId(CHECKPOINT_ID),),
            route_facts=replace(
                current.definition.route_facts, size_class=work_models.SizeClass.SMALL, declares_checkpoints=True
            ),
        )
        digest = history.work_item_definition_digest(definition)
        assert isinstance(digest, str)
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                definition_revisions=tuple(
                    replace(row, definition=definition, digest=digest, after_digest=digest) if row == current else row
                    for row in state.lifecycle.definition_revisions
                ),
                attempts=tuple(
                    replace(row, accepted_scope_digest=digest) if row.item_id == "work-a" else row
                    for row in state.lifecycle.attempts
                ),
            ),
        )

        def brief(project: Path) -> work_brief_models.WorkBrief:
            return replace_struct(work_a_brief(project), accepted_scope=work_brief_models.AcceptedScope(1, digest))

        with (
            patch("tests.checkpoint_support.complete_sqlite_state", return_value=state),
            patch("tests.checkpoint_support.work_a_brief", side_effect=brief),
        ):
            fixture = self.checkpoint_fixture(review_condition="missing")
        payload = self.json_object(msgspec.json.decode(fixture.payload.read_bytes()))
        self.record_commissioned_review(fixture, fixture.candidate_revision, "separate-reviewer")
        accepted = fixture.call_advertised_tool(
            tool_names.TRANSITION_TOOL,
            self.native_transition_request(
                fixture, self.project_action(fixture, "accept-checkpoint:work-a-1"), payload
            ),
        )
        self.assertEqual("committed", accepted["status"], accepted)
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        reference = next(
            row
            for row in reloaded.artifact_references
            if row.artifact_ref_id
            == next(
                receipt.artifact_ref_id
                for receipt in reloaded.transition_receipts
                if receipt.outcome_schema == "checkpoint-acceptance/v2"
            )
        )
        package = work_briefs.decode_canonical_checkpoint_review_package(
            (fixture.work / reference.selector).read_bytes()
        )
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        basis = package.review_basis
        assert isinstance(basis, work_brief_models.SmallWorkReviewBasis)
        self.assertEqual(
            digest, hashlib.sha256(msgspec.json.encode(basis.definition, order="sorted") + b"\n").hexdigest()
        )
        with self.assertRaises(ValueError):
            replace_struct(
                package,
                review_basis=replace_struct(
                    basis, definition=replace_struct(basis.definition, title="Unaccepted definition")
                ),
            )
        with self.assertRaises(ValueError):
            replace_struct(
                basis,
                definition=replace_struct(
                    basis.definition,
                    route_facts=replace_struct(basis.definition.route_facts, size_class=work_models.SizeClass.STANDARD),
                ),
            )
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        code, exported, error = self.run_cli(*fixture.common, "export", "--json")
        self.assertEqual(0, code, error)
        projected = msgspec.json.decode(exported, type=project_export.ProjectExport)
        self.assertEqual(basis, projected.checkpoint_packages[0].review_basis)

    def test_instruction_advisories_are_native_read_only_before_their_effects(self) -> None:
        fixture = self.checkpoint_fixture()
        before = fixture.store.validated_snapshot()

        async def scenario() -> None:
            with native_tools() as native:
                async with Client(
                    native.server, client_info=Implementation(name="codex-mcp-client", version="test")
                ) as client:
                    for kind, subject in (
                        ("create-proposal", "ledger"),
                        ("preview-parallel", "ledger"),
                        ("order", "ledger"),
                        ("inspect-initiative", "ledger"),
                        ("consolidate", "ledger"),
                        ("review-candidate", "work-a-1"),
                    ):
                        result = await client.call_tool(
                            tool_names.ACTIONS_TOOL,
                            {
                                "request": {
                                    "project_root": str(fixture.project),
                                    "work_root": str(fixture.work),
                                    "role": "project",
                                    "action_id": {"kind": kind, "subject": subject},
                                }
                            },
                        )
                        self.assertFalse(result.is_error)
                        content = self.json_object(result.structured_content)
                        self.assertEqual("ok", content["status"], content)
                        self.assertEqual("unchanged", content["effect"])
                        action = self.json_object(self.json_array(content["actions"])[0])
                        instruction = msgspec.convert(
                            action["instructions"], type=instruction_models.ActionInstructions
                        )
                        assert instruction.procedure is not None
                        body = Path(instruction.procedure.path).read_bytes()
                        self.assertEqual(len(body), instruction.procedure.size_bytes)
                        self.assertEqual(hashlib.sha256(body).hexdigest(), instruction.procedure.sha256)
                    pr_result = await client.call_tool(
                        tool_names.PR_REVIEW_TOOL,
                        {
                            "request": {
                                "operation": "actions",
                                "project_root": str(fixture.project),
                                "work_root": str(fixture.work),
                                "item_id": "work-c",
                            }
                        },
                    )
                    pr_content = self.json_object(pr_result.structured_content)
                    self.assertEqual(["start"], pr_content["available_actions"])
                    instruction = msgspec.convert(
                        pr_content["instructions"], type=instruction_models.ActionInstructions
                    )
                    assert instruction.procedure is not None
                    reviewer = msgspec.convert(
                        pr_content["reviewer_startup_document"], type=dispatch_models.NativeInstruction
                    )
                    self.assertEqual("reviewer-codex.md", Path(reviewer.path).name)
                    self.assertEqual(hashlib.sha256(Path(reviewer.path).read_bytes()).hexdigest(), reviewer.sha256)
                    absent_board = fixture.project / "unsaved-board"
                    result = await client.call_tool(
                        tool_names.ACTIONS_TOOL,
                        {
                            "request": {
                                "project_root": str(fixture.project),
                                "work_root": str(absent_board),
                                "role": "project",
                                "action_id": {"kind": "create-proposal", "subject": "ledger"},
                            }
                        },
                    )
                    content = self.json_object(result.structured_content)
                    self.assertEqual("ok", content["status"], content)
                    self.assertFalse(absent_board.exists())

        asyncio.run(scenario())
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_classified_intake_reloads_exact_route_and_rejects_unknown_vocabulary(self) -> None:
        fixture = self.active_fixture()
        incoming = proposal()
        incoming.update(
            schema="pinboard-proposal/v4",
            proposal_id="recorded-route",
            checkpoints=[],
            route_facts={
                "size_class": "small",
                "architecture_impact": "none",
                "external_effects": "none",
                "declares_checkpoints": False,
            },
        )
        arguments: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "actor_task_id": "coordinator",
            "actor_host_id": "local",
            "proposal": incoming,
        }
        created = fixture.call_advertised_tool(tool_names.PROPOSAL_CREATE_TOOL, arguments)
        self.assertEqual("committed", created["status"], created)

        current = fixture.call_advertised_tool(
            tool_names.ITEM_DEFINITION_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "current",
                    "item_id": "recorded-route",
                }
            },
        )
        definition = self.json_object(current["definition"])
        self.assertEqual(incoming["route_facts"], definition["route_facts"])
        self.assertEqual("pinboard-work-item-definition/v4", definition["schema"])
        prepared = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "prepare", "subject": "recorded-route"}}
        )
        self.assertEqual("ok", prepared["status"], prepared)

        async def verify_native_delivery() -> None:
            for client_name, runtime in (
                ("codex-mcp-client", "codex"),
                ("claude-code", "claude-code"),
                ("antigravity-client", "antigravity"),
            ):
                with native_tools() as native:
                    async with Client(
                        native.server, client_info=Implementation(name=client_name, version="test")
                    ) as client:
                        result = await client.call_tool(
                            tool_names.ACTIONS_TOOL,
                            {
                                "request": {
                                    "project_root": str(fixture.project),
                                    "work_root": str(fixture.work),
                                    "role": "project",
                                    "action_id": {"kind": "prepare", "subject": "recorded-route"},
                                }
                            },
                        )
                self.assertFalse(result.is_error)
                content = self.json_object(result.structured_content)
                action = self.json_object(self.json_array(content["actions"])[0])
                instruction = msgspec.convert(action["instructions"], type=instruction_models.ActionInstructions)
                self.assertEqual(runtime, instruction.runtime)
                self.assertEqual(incoming["route_facts"], msgspec.to_builtins(instruction.route_facts))
                assert instruction.procedure is not None
                body = Path(instruction.procedure.path).read_bytes()
                self.assertEqual(len(body), instruction.procedure.size_bytes)
                self.assertEqual(hashlib.sha256(body).hexdigest(), instruction.procedure.sha256)
                self.assertLessEqual(len(body), 46080)

        asyncio.run(verify_native_delivery())
        forbidden = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "activate", "subject": "recorded-route"}}
        )
        self.assertEqual("ACTION_NOT_AVAILABLE", forbidden["code"])
        self.assertEqual("unchanged", forbidden["effect"])

        malformed = deepcopy(incoming)
        malformed["proposal_id"] = "bad-route"
        malformed["route_facts"] = {
            "size_class": "tiny",
            "architecture_impact": "none",
            "external_effects": "none",
            "declares_checkpoints": False,
        }
        rejected = fixture.call_advertised_tool(tool_names.PROPOSAL_CREATE_TOOL, {**arguments, "proposal": malformed})
        self.assertEqual("PROPOSAL_INVALID", rejected["code"])
        self.assertEqual("unchanged", rejected["effect"])
        after = fixture.call_advertised_tool(
            tool_names.ITEM_DEFINITION_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "current",
                    "item_id": "recorded-route",
                }
            },
        )
        self.assertEqual(current, after)

    def test_retained_definition_stays_readable_until_explicit_route_revision(self) -> None:
        state = complete_sqlite_state()
        definitions: list[stored_state.ItemDefinitionRevision] = []
        for value in state.lifecycle.definition_revisions:
            if value.item_id == "work-c":
                assert isinstance(value.definition, work_models.WorkItemDefinition)
                retained_definition = replace(value.definition, route_facts=None)
                digest = history.work_item_definition_digest(retained_definition)
                assert isinstance(digest, str)
                value = replace(value, definition=retained_definition, digest=digest, after_digest=digest)
            definitions.append(value)
        state = replace(state, lifecycle=replace(state.lifecycle, definition_revisions=tuple(definitions)))
        with patch("tests.checkpoint_support.complete_sqlite_state", return_value=state):
            fixture = self.checkpoint_fixture()
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "operation": "current",
            "item_id": "work-c",
        }
        before = fixture.call_advertised_tool(tool_names.ITEM_DEFINITION_TOOL, {"request": request})
        original = self.json_object(before["definition"])
        self.assertNotIn("route_facts", original)
        original_bytes = msgspec.json.encode(original, order="sorted")
        pr_actions = fixture.call_advertised_tool(
            tool_names.PR_REVIEW_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "actions",
                    "item_id": "work-c",
                }
            },
        )
        self.assertEqual([], pr_actions["available_actions"])

        refused = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "prepare", "subject": "work-c"}}
        )
        self.assertEqual("ROUTE_CLASSIFICATION_REQUIRED", refused["code"])
        self.assertEqual("unchanged", refused["effect"])
        action = self.native_actions(fixture, "revise-item", "work-c")
        revised: JsonObject = {
            **original,
            "schema": "pinboard-work-item-definition/v4",
            "checkpoints": [],
            "route_facts": {
                "size_class": "small",
                "architecture_impact": "none",
                "external_effects": "none",
                "declares_checkpoints": False,
            },
        }
        committed = self.transition_result(
            fixture,
            action,
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": before["definition_revision"],
                "expected_digest": before["definition_digest"],
                "source_task": "coordinator",
                "reason": "Record the explicitly chosen route.",
                "definition": revised,
            },
        )
        self.assertEqual("committed", committed["status"], committed)
        after = fixture.call_advertised_tool(tool_names.ITEM_DEFINITION_TOOL, {"request": request})
        self.assertEqual(revised, after["definition"])
        recorded_history = fixture.call_advertised_tool(
            tool_names.ITEM_DEFINITION_TOOL,
            {"request": {**request, "operation": "history", "limit": 10, "before_revision": None}},
        )
        revisions = self.json_array(recorded_history["revisions"])
        retained = next(
            self.json_object(row)["definition"]
            for row in revisions
            if self.json_object(row)["revision"] == before["definition_revision"]
        )
        self.assertEqual(original_bytes, msgspec.json.encode(retained, order="sorted"))
        prepared = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "prepare", "subject": "work-c"}}
        )
        self.assertEqual("ok", prepared["status"], prepared)
