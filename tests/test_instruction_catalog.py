"""Startup accounting follows each supported entry and its native instruction reads."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import instruction_catalog


class StartupCompositionTest(unittest.TestCase):
    def test_generated_documents_reject_unresolvable_relative_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("worker-codex.md", "documents/action.md"):
                with self.subTest(document=relative):
                    document = root / relative
                    document.parent.mkdir(parents=True, exist_ok=True)
                    body = b"Follow [recovery](missing.md#recovery).\n"
                    document.write_bytes(body)
                    with (
                        patch.object(instruction_catalog, "expected_files", return_value={document: body}),
                        self.assertRaisesRegex(ValueError, "missing.md"),
                    ):
                        instruction_catalog.validate_generated()

    def test_intake_and_transitive_reads_count_toward_each_runtime_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "src/pinboard/instructions"
            output.mkdir(parents=True)
            for runtime in instruction_catalog.RUNTIMES:
                (output / f"coordinator-{runtime}.md").write_text("coordinator")
            (root / "src/pinboard/mcp").mkdir()
            (root / "src/pinboard/mcp/server.py").write_text("server(instructions='mcp')\n")
            for hook in ("claude_hook.py", "antigravity_hook.py"):
                (root / "src/pinboard" / hook).write_text("def session_start_main():\n    context = 'hook'\n")
            for entry in instruction_catalog.COORDINATOR_ENTRIES:
                (root / "skills" / entry).mkdir(parents=True)
                (root / "skills" / entry / "SKILL.md").write_text(
                    "Read `../../src/pinboard/instructions/coordinator-{codex|claude-code|antigravity}.md`."
                )
            intake = root / "skills/pinboard-intake/SKILL.md"
            intake.write_text("Read [Pinboard](../pinboard/SKILL.md) and [extra](extra.md).")
            extra = intake.parent / "extra.md"
            extra.write_text("extra startup rules")
            with patch.object(instruction_catalog, "ROOT", root), patch.object(instruction_catalog, "OUTPUT", output):
                sizes = instruction_catalog.validate_startup_composition()
                for runtime in instruction_catalog.RUNTIMES:
                    self.assertEqual(
                        intake.stat().st_size + extra.stat().st_size,
                        sizes[f"pinboard-intake:{runtime}"] - sizes[f"pinboard:{runtime}"],
                    )
                extra.write_text("x" * instruction_catalog.ROLE_BUDGETS["coordinator"])
                with self.assertRaisesRegex(ValueError, "pinboard-intake:codex"):
                    instruction_catalog.validate_startup_composition()
