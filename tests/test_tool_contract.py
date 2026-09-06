import contextlib
import io
import json
import unittest
from unittest.mock import patch

from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.domain import decision_models
from pinboard.interfaces import cli_parser, tool_contract
from pinboard.interfaces.cli import main


class ToolContractTest(unittest.TestCase):
    def test_installed_index_covers_every_parser_variant_and_action_once(self) -> None:
        contract = tool_contract.installed_tool_contract()

        parser_variants = cli_parser.installed_command_variants()
        self.assertEqual(
            {(variant.operation_id, variant.variant) for variant in parser_variants},
            {(operation.operation_id, operation.variant) for operation in contract.operations},
        )
        self.assertEqual(
            {kind.value for kind in decision_models.ActionKind},
            {action.action_kind for action in contract.actions},
        )
        self.assertEqual(len(parser_variants), len(contract.operations))
        self.assertEqual(len(decision_models.ActionKind), len(contract.actions))
        self.assertEqual(
            {"root", "help", "version"},
            {presentation.presentation for presentation in contract.presentations},
        )
        for operation in contract.operations:
            with self.subTest(operation=operation.detail_selector):
                detail = tool_contract.describe_operation(operation.operation_id, operation.variant)
                self.assertEqual("pinboard-agent-tool-operation/v1", detail.schema)
                self.assertTrue(detail.purpose)
                self.assertTrue(detail.success_postcondition)
        for action in decision_models.ActionKind:
            with self.subTest(action=action.value):
                detail = tool_contract.describe_action(action)
                self.assertTrue(detail.purpose)
                self.assertTrue(detail.success_postcondition)

    def test_selected_command_and_action_expose_bounded_execution_facts(self) -> None:
        command = tool_contract.describe_operation("transition", "attempt")
        self.assertIsInstance(command, tool_contract.OperationContract)
        assert isinstance(command, tool_contract.OperationContract)
        self.assertEqual("pinboard-agent-tool-operation/v1", command.schema)
        self.assertEqual("transition", command.operation_id)
        self.assertEqual("attempt", command.variant)
        self.assertEqual("mutates-ledger", command.mutation_class)
        self.assertEqual(("worker",), command.permitted_roles)
        self.assertEqual("attempt-lease", command.required_authority)
        self.assertEqual("action-subject", command.subject_kind)
        self.assertIsNotNone(command.input_schema)
        self.assertEqual("never-retry-with-stale-action-facts", command.retry_semantics)

        action = tool_contract.describe_action(decision_models.ActionKind.SUBMIT_REVIEW)
        self.assertEqual("pinboard-agent-tool-action/v1", action.schema)
        self.assertEqual("submit-review", action.action_kind)
        self.assertEqual("mutates-ledger", action.mutation_class)
        self.assertEqual(("worker",), action.permitted_roles)
        self.assertEqual("attempt", action.subject_kind)
        self.assertEqual("active-attempt-current-scope", action.lifecycle_precondition)
        self.assertIsNotNone(action.input_schema)
        self.assertEqual("reselect-after-any-rejection", action.retry_semantics)

        brief = tool_contract.describe_operation("brief/publish")
        self.assertIsInstance(brief, tool_contract.OperationContract)
        assert isinstance(brief, tool_contract.OperationContract)
        self.assertIsNone(brief.artifact_schema)
        self.assertIsNotNone(brief.work_brief)
        assert brief.work_brief is not None
        self.assertEqual("pinboard-work-brief-contract/v1", brief.work_brief.schema)

        proposal = tool_contract.describe_operation("proposal")
        self.assertIsInstance(proposal, tool_contract.OperationContract)
        assert isinstance(proposal, tool_contract.OperationContract)
        assert proposal.artifact_schema is not None
        proposal_schema = json.loads(bytes(proposal.artifact_schema))
        relation_variants = proposal_schema["$defs"]["Proposal"]["properties"]["relation"]
        self.assertIn("anyOf", relation_variants)
        self.assertEqual(
            {"type": "null"},
            proposal_schema["$defs"]["IndependentProposalRelation"]["properties"]["item"],
        )
        self.assertEqual(
            {"type": "null"},
            proposal_schema["$defs"]["ClarificationProposalRelation"]["properties"]["item"],
        )
        self.assertEqual(
            "string",
            proposal_schema["$defs"]["FollowUpProposalRelation"]["properties"]["item"]["type"],
        )

    def test_cli_index_and_selected_detail_do_not_resolve_project_roots(self) -> None:
        for arguments in (
            ("tool-contract", "--json"),
            ("tool-contract", "--operation", "attempt/inspect", "--json"),
            ("tool-contract", "--action-kind", "activate", "--json"),
        ):
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    patch(
                        "pinboard.interfaces.cli.work_state_commands.resolve_roots",
                        side_effect=AssertionError("unexpected project-root read"),
                    ),
                ):
                    result = main(arguments)
                self.assertEqual(0, result, stderr.getvalue())
                payload = json.loads(stdout.getvalue())
                self.assertIsInstance(payload, dict)
                self.assertIn("schema", payload)

    def test_completeness_rejects_missing_duplicate_and_unknown_classification(self) -> None:
        installed = cli_parser.installed_command_variants()
        operation_keys = tuple((variant.operation_id, variant.variant) for variant in installed)
        actions = tuple(kind.value for kind in decision_models.ActionKind)
        with self.assertRaisesRegex(ValueError, "missing operation classification"):
            tool_contract.validate_contract_inventory(operation_keys, operation_keys[1:], actions, actions)
        with self.assertRaisesRegex(ValueError, "duplicate operation classification"):
            tool_contract.validate_contract_inventory(
                operation_keys, (*operation_keys, operation_keys[0]), actions, actions
            )
        with self.assertRaisesRegex(ValueError, "unknown action classification"):
            tool_contract.validate_contract_inventory(operation_keys, operation_keys, actions[:-1], actions)

    def test_unknown_selected_operation_is_an_expected_rejection(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(("tool-contract", "--operation", "not-installed", "--json"))
        self.assertEqual(11, result)
        self.assertEqual("", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual("pinboard-rejected-operation/v1", payload["schema"])
        self.assertEqual("rejected", payload["status"])
        self.assertEqual("tool-contract", payload["operation"])
        self.assertEqual("TRANSITION_INPUT_INVALID", payload["code"])
        self.assertFalse(payload["state_changed"])

    def test_json_parse_and_infrastructure_failures_are_structured(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            parse_result = main(("tool-contract", "--not-installed", "value", "--json"))
        self.assertEqual(2, parse_result)
        self.assertEqual("", stderr.getvalue())
        parse_payload = json.loads(stdout.getvalue())
        self.assertEqual("CLI_ARGUMENT_INVALID", parse_payload["code"])
        self.assertEqual("correct-input", parse_payload["retry"])
        self.assertEqual(
            [{"kind": "command", "command": "pinboard tool-contract --json"}],
            parse_payload["next_actions"],
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch(
                "pinboard.interfaces.cli._dispatch",
                side_effect=StorageError(StorageErrorCode.BUSY, "held by another process", retryable=True),
            ),
        ):
            storage_result = main(("tool-contract", "--json"))
        self.assertEqual(12, storage_result)
        self.assertEqual("", stderr.getvalue())
        storage_payload = json.loads(stdout.getvalue())
        self.assertEqual("STORAGE_BUSY", storage_payload["code"])
        self.assertEqual("retry-same-input", storage_payload["retry"])
        self.assertFalse(storage_payload["state_changed"])


if __name__ == "__main__":
    unittest.main()
