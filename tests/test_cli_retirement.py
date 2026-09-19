import contextlib
import io
import json
import unittest
from unittest.mock import patch

from pinboard.cli.entrypoint import main


class CliRetirementTest(unittest.TestCase):
    def test_agent_routes_and_options_reject_before_resources(self) -> None:
        retired = (
            ("overview",),
            ("item", "status"),
            ("parallel", "preview"),
            ("actions",),
            ("transition",),
            ("proposal",),
            ("order",),
            ("brief", "publish"),
            ("brief-sources",),
            ("review-job",),
            ("preparation", "start"),
            ("attempt", "acquire"),
            ("dispatch",),
            ("artifact", "verify"),
            ("input-contract",),
            ("tool-contract", "--action-kind", "complete"),
            ("tool-contract", "--brief-starter", "local"),
        )
        for arguments in retired:
            with (
                self.subTest(arguments=arguments),
                patch("pinboard.cli.entrypoint.work_state_commands.resolve_roots") as roots,
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(2, main((*arguments, "--json")))
                roots.assert_not_called()
                rejection = json.loads(output.getvalue())
                self.assertEqual("CLI_ARGUMENT_INVALID", rejection["code"])
                self.assertFalse(rejection["state_changed"])
                self.assertEqual([], rejection["changed_surfaces"])
                self.assertEqual(
                    [{"kind": "command", "command": "pinboard tool-contract --json"}],
                    rejection["next_actions"],
                )
