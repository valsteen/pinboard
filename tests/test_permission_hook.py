import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "permission-hook.py"
LAUNCHER = "/plugin root/scripts/pinboard"


class PermissionHookTests(unittest.TestCase):
    def test_pinboard_heredoc_and_substitution_receive_bounded_recovery(self) -> None:
        for command in (
            f"cat > /tmp/input.json <<'EOF'\n{{}}\nEOF\n'{LAUNCHER}' proposal --host-id $(hostname)",
            f"'{LAUNCHER}' status --host-id $(hostname)",
        ):
            with self.subTest(command=command):
                decision = self.decision_for(command)
                self.assertIsNotNone(decision)
                assert decision is not None
                output = decision["hookSpecificOutput"]
                assert isinstance(output, dict)
                self.assertEqual("deny", output["permissionDecision"])
        self.assertIsNone(self.decision_for("cat <<'EOF'\nunrelated\nEOF"))
        self.assertIsNone(self.decision_for(f"'{LAUNCHER}-other' --value $(hostname)"))
        self.assertIsNone(self.decision_for(f"echo '{LAUNCHER}' $(hostname)"))

    def decision_for(self, command: str, *, tool_name: str = "Bash") -> dict[str, object] | None:
        result = subprocess.run(
            [sys.executable, str(HOOK), LAUNCHER],
            input=json.dumps({"tool_name": tool_name, "tool_input": {"command": command}}),
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout) if result.stdout else None

    def assert_allowed(self, command: str) -> None:
        decision = self.decision_for(command)
        self.assertIsNotNone(decision)
        assert decision is not None
        output = decision["hookSpecificOutput"]
        self.assertIsInstance(output, dict)
        assert isinstance(output, dict)
        self.assertEqual("allow", output["permissionDecision"])

    def test_allows_direct_and_documented_rooted_read_only_commands(self) -> None:
        self.assert_allowed(f"'{LAUNCHER}' status --json")
        self.assert_allowed(
            f"PINBOARD_RUNTIME=claude '{LAUNCHER}' "
            "--project-root /project --work-root /work item definition --item-id item-1 --json"
        )

    def test_leaves_mutation_and_shell_composition_in_the_permission_flow(self) -> None:
        commands = (
            f"'{LAUNCHER}' migrate-storage --json",
            f"'{LAUNCHER}' proposal --file proposal.json",
            f"'{LAUNCHER}' status --json && touch changed",
            f"'{LAUNCHER}' status --json & touch changed",
            f"'{LAUNCHER}' status --json > status.json",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(self.decision_for(command))

    def test_brief_sources_allows_reads_but_not_output_plan_writes(self) -> None:
        self.assert_allowed(f"'{LAUNCHER}' brief-sources --file sources.json --json")
        output_plan_spellings = (
            "--output-plan plan.json",
            "--output-plan=plan.json",
            "--output-p plan.json",
            "--o=plan.json",
        )
        for output_plan in output_plan_spellings:
            with self.subTest(output_plan=output_plan):
                command = f"'{LAUNCHER}' brief-sources --file sources.json {output_plan} --json"
                self.assertIsNone(self.decision_for(command))

    def test_ignores_non_bash_and_malformed_input(self) -> None:
        self.assertIsNone(self.decision_for(f"'{LAUNCHER}' status", tool_name="Read"))
        result = subprocess.run(
            [sys.executable, str(HOOK), LAUNCHER],
            input="not-json",
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", result.stdout)


if __name__ == "__main__":
    unittest.main()
