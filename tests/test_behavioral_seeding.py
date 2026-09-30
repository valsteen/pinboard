"""Launcher recovery results and MCP contract mismatches become typed seed failures, never repaired."""

import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_types import CallToolResult, TextContent

from evals.behavioral import board, export, world
from evals.behavioral.export import SeedFailure
from evals.behavioral.processes import Completed, Window

RECOVERY = (
    '{"schema":"pinboard-launcher-result/v1","status":"runtime-preparation-required","pinboard_started":false,'
    '"runtime_location":{"base":"launcher-root","relative":".pinboard-runtime"},"observations":["not ready"],'
    '"upstream_exit_code":null,"retry_disposition":"retry-original-command","effect_disposition":"unchanged",'
    '"changed_surfaces":[],"next_action":{"launcher":"self","arguments":["--prepare-runtime"],'
    '"display_command":"scripts/pinboard --prepare-runtime","requires":["uv"]}}'
)


class LauncherRecoveryTest(unittest.TestCase):
    def test_a_recovery_result_from_board_init_is_a_seed_failure(self) -> None:
        with (
            patch.object(world.processes, "launcher_init", return_value=Completed(0, RECOVERY, "", False)),
            self.assertRaises(SeedFailure),
        ):
            world.init_board(Path("launcher"), Path("project"), None, Window(None))

    def test_an_unready_runtime_preparation_is_a_seed_failure(self) -> None:
        with (
            patch.object(export.processes, "launcher_prepare_runtime", return_value=Completed(1, RECOVERY, "", False)),
            self.assertRaises(SeedFailure),
        ):
            export.prepare_runtime(Path("plugin"), Window(None))

    def test_preparation_without_a_launcher_result_is_a_seed_failure(self) -> None:
        with (
            patch.object(
                export.processes, "launcher_prepare_runtime", return_value=Completed(127, "", "not found", False)
            ),
            self.assertRaises(SeedFailure),
        ):
            export.prepare_runtime(Path("plugin"), Window(None))


class ToolResultTest(unittest.TestCase):
    def test_a_result_that_lacks_a_consumed_field_is_a_seed_failure(self) -> None:
        result = CallToolResult(content=[], structured_content={"status": "committed"}, is_error=False)
        with self.assertRaises(SeedFailure):
            board.read_result("pinboard_attempt_authority", result, board.Lease)

    def test_a_tool_error_is_a_seed_failure(self) -> None:
        result = CallToolResult(content=[TextContent(type="text", text="unknown tool")], is_error=True)
        with self.assertRaises(SeedFailure):
            board.read_result("pinboard_missing", result, board.Status)

    def test_unrelated_additional_fields_are_ignored(self) -> None:
        result = CallToolResult(content=[], structured_content={"status": "committed", "revision": "7"}, is_error=False)
        self.assertEqual("committed", board.read_result("pinboard_transition", result, board.Status).status)


if __name__ == "__main__":
    unittest.main()
