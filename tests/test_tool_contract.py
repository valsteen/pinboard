import contextlib
import io
import json
import unittest
from unittest.mock import patch

import msgspec

from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.cli import cli_commands, cli_parser, tool_contract
from pinboard.cli.entrypoint import main
from pinboard.cli.errors import CommandFailure


class ToolContractTest(unittest.TestCase):
    def test_cli_only_index_and_every_returned_selector_are_static_and_exact(self) -> None:
        with patch("pinboard.cli.entrypoint.work_state_commands.resolve_roots") as roots:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(("tool-contract", "--json"))
            self.assertEqual(0, result)
            payload = json.loads(stdout.getvalue())
            self.assertEqual("pinboard-cli-tool-contract/v1", payload["schema"])
            self.assertNotIn("actions", payload)
            self.assertNotIn("brief_starters", payload)
            installed = cli_parser.installed_commands()
            self.assertEqual(
                {command.operation_id for command in installed},
                {operation["operation_id"] for operation in payload["operations"]},
            )
            self.assertEqual(len(installed), len(payload["operations"]))
            for operation in payload["operations"]:
                with self.subTest(selector=operation["detail_selector"]):
                    detail = tool_contract.describe_operation(operation["operation_id"])
                    self.assertIsInstance(detail, tool_contract.OperationContract)
                    assert isinstance(detail, tool_contract.OperationContract)
                    self.assertEqual("pinboard-cli-tool-operation/v1", detail.schema)
                    self.assertEqual(operation["mutation_class"], detail.mutation_class)
                    self.assertEqual(operation["data_scope"], detail.data_scope)
                    self.assertTrue(detail.cli_usage)
                    self.assertTrue(detail.success_postcondition)
                    self.assertTrue(json.loads(bytes(detail.input_schema)))
            for selector in payload["presentation_selectors"]:
                detail = tool_contract.describe_operation(selector)
                self.assertIsInstance(detail, tool_contract.PresentationContract)
            roots.assert_not_called()

    def test_retained_human_close_leaf_schema_rejects_unknown_and_invalid_values(self) -> None:
        detail = tool_contract.describe_operation("close")
        assert isinstance(detail, tool_contract.OperationContract)
        schema = json.loads(bytes(detail.input_schema))
        self.assertIn("CloseCommand", schema["$defs"])
        command = cli_parser.parse_invocation(
            (
                "close",
                "work-a",
                "--outcome",
                "dropped",
                "--reason",
                "No longer needed.",
                "--task-id",
                "human",
                "--host-id",
                "local",
            )
        ).command
        self.assertIsInstance(command, cli_commands.CloseCommand)
        self.assertEqual("focused", detail.data_scope)
        self.assertEqual("mutates-ledger", detail.mutation_class)
        self.assertEqual("item-without-attempt", detail.lifecycle_precondition)
        value = msgspec.to_builtins(command)
        assert isinstance(value, dict)
        with self.assertRaises(msgspec.ValidationError):
            msgspec.convert(value | {"action_id": "close:work-a"}, type=cli_commands.CloseCommand)
        for selector in ("transition", "brief/publish", "review-job", "attempt/acquire", "input-contract"):
            with self.subTest(selector=selector):
                self.assertIsInstance(tool_contract.describe_operation(selector), CommandFailure)

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
        self.assertEqual("correct-input", payload["retry"])
        self.assertEqual([{"field": "operation", "value": "not-installed"}], payload["observed"])
        self.assertEqual(
            [
                {
                    "field": "operation",
                    "expected": "selector returned by pinboard tool-contract --json",
                    "observed": "not-installed",
                }
            ],
            payload["mismatches"],
        )
        self.assertEqual(
            [{"kind": "command", "command": "pinboard tool-contract --json"}],
            payload["next_actions"],
        )

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
                "pinboard.cli.entrypoint._dispatch",
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
