"""Strict decoding of blind scorer answers and isolated Claude / Antigravity Gemini one-shot scorer execution."""

import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from evals.behavioral import agy_driver, cli, oneshot, processes, scoring
from evals.behavioral.processes import Completed as ProcessResult
from evals.behavioral.processes import Window
from evals.behavioral.records import Redaction, ScoreRecord, ScoringFailure
from tests.test_behavioral_agy import turn_json


def item(verdict: str) -> dict[str, str]:
    return {"verdict": verdict, "evidence": "quote"}


def answer(label: str, turns: list[int]) -> str:
    items = {name: item("pass") for name in [f"P{n}" for n in range(1, 13)] + ["N1"]}
    items["P3"] = item("fail")
    replies = json.dumps([{"turn": turn, "items": items} for turn in turns])
    body = (
        f'{{"label": {json.dumps(label)}, "replies": {replies}, "scenario_pass": false, '
        '"failed_items": ["turn 1 P3: missing"], "permission_denial_suspected": false}'
    )
    return f'Reasoning first.\n\n```json\n{{"label": "draft"}}\n```\n\nFinal:\n```json\n{body}\n```\n'


class DecodeScoreTest(unittest.TestCase):
    def test_a_complete_answer_for_the_label_decodes_from_the_last_json_block(self) -> None:
        decoded = scoring.decode_score(answer("T1", [1, 2]), "T1", 2)
        assert isinstance(decoded, ScoreRecord)
        self.assertEqual([1, 2], [reply.turn for reply in decoded.replies])

    def test_an_unknown_field_is_a_scoring_failure(self) -> None:
        text = answer("T1", [1]).replace('"scenario_pass"', '"confidence": "high", "scenario_pass"')
        decoded = scoring.decode_score(text, "T1", 1)
        self.assertIsInstance(decoded, ScoringFailure)

    def test_a_missing_checklist_item_is_a_scoring_failure(self) -> None:
        text = answer("T1", [1]).replace('"N1": {"verdict": "pass", "evidence": "quote"}', '"N2": {}')
        self.assertIsInstance(scoring.decode_score(text, "T1", 1), ScoringFailure)

    def test_an_unknown_verdict_is_a_scoring_failure(self) -> None:
        text = answer("T1", [1]).replace('"verdict": "fail"', '"verdict": "maybe"')
        self.assertIsInstance(scoring.decode_score(text, "T1", 1), ScoringFailure)

    def test_another_label_is_a_scoring_failure(self) -> None:
        self.assertIsInstance(scoring.decode_score(answer("T2", [1]), "T1", 1), ScoringFailure)

    def test_a_skipped_or_repeated_reply_is_a_scoring_failure(self) -> None:
        self.assertIsInstance(scoring.decode_score(answer("T1", [1, 1]), "T1", 2), ScoringFailure)
        self.assertIsInstance(scoring.decode_score(answer("T1", [1]), "T1", 2), ScoringFailure)

    def test_no_answer_or_no_json_block_is_a_scoring_failure(self) -> None:
        self.assertIsInstance(scoring.decode_score(None, "T1", 1), ScoringFailure)
        self.assertIsInstance(scoring.decode_score("no block here", "T1", 1), ScoringFailure)


class RedactTest(unittest.TestCase):
    def test_known_roots_become_placeholders_and_remaining_local_paths_are_removed(self) -> None:
        text = "at /work/world-1/tally and /work/world-1b, plugin /opt/export/plugin, also /Users/someone/x y"
        redacted = scoring.redact(
            text,
            [
                Redaction(value="/work/world-1", placeholder="<world>"),
                Redaction(value="/work/world-1b", placeholder="<other>"),
                Redaction(value="/opt/export/plugin", placeholder="<plugin>"),
            ],
        )
        self.assertEqual("at <world>/tally and <other>, plugin <plugin>, also <path> y", redacted)


class OneshotAndCliScoreModelTest(unittest.TestCase):
    def test_oneshot_ask_runs_agy_gemini_in_disposable_home_with_empty_permissions_and_list_price(self) -> None:
        scored_text = answer("T1", [1])
        seen: list[tuple[processes.Tool, list[str], Path, list[Path], Path, bool, list[str], str | None]] = []

        def fake_run_tool(
            tool: processes.Tool,
            arguments: Sequence[str],
            *,
            cwd: Path,
            environment: Mapping[str, str],
            stdin: str | None,
            timeout_seconds: float,
            window: Window,
        ) -> ProcessResult:
            window.timeout(timeout_seconds)
            home = Path(environment["HOME"])
            settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
            seen.append(
                (
                    tool,
                    list(arguments),
                    cwd,
                    list(cwd.iterdir()),
                    home,
                    (home / "Library" / "Keychains").is_symlink(),
                    settings["permissions"]["allow"],
                    stdin,
                )
            )
            return ProcessResult(
                0,
                turn_json(
                    response=scored_text,
                    input_tokens=10_000,
                    output_tokens=1_000,
                    thinking_tokens=500,
                    cache_read_tokens=2_000,
                ),
                "",
                False,
            )

        with tempfile.TemporaryDirectory() as tmp:
            fake_real_home = Path(tmp) / "real-home"
            (fake_real_home / "Library" / "Keychains").mkdir(parents=True)
            with (
                mock.patch.object(Path, "home", return_value=fake_real_home),
                mock.patch.object(processes, "run_tool", side_effect=fake_run_tool),
            ):
                result = oneshot.ask("score prompt", "gemini-4-argon", Window(None))

        self.assertEqual(1, len(seen))
        tool, args, cwd, cwd_entries, home, has_keychain, allow, stdin = seen[0]
        self.assertIs(processes.Tool.AGY, tool)
        self.assertEqual(
            [
                "-p",
                "score prompt",
                "--output-format",
                "json",
                "--model",
                "gemini-4-argon",
                "--disable-slash-commands",
                "--print-timeout",
                "5m",
            ],
            args,
        )
        self.assertEqual([], cwd_entries)
        self.assertNotEqual(cwd, home)
        self.assertTrue(has_keychain)
        self.assertEqual([], allow)
        self.assertIsNone(stdin)
        self.assertFalse(home.exists())
        self.assertFalse(cwd.exists())
        self.assertIsNone(result.problem)
        self.assertEqual(scored_text, result.text)
        # gemini-4-argon short context: $2/1M input, $0.20/1M cached, $12/1M output+thinking
        expected_cost = (10_000 * 2.0 + 2_000 * 0.20 + 1_500 * 12.0) / 1_000_000
        self.assertAlmostEqual(expected_cost, result.cost_usd or 0.0, places=9)
        decoded = scoring.decode_score(result.text, "T1", 1)
        self.assertIsInstance(decoded, ScoreRecord)

    def test_oneshot_ask_handles_agy_error_and_timeout_states(self) -> None:
        cases: tuple[tuple[str, ProcessResult, bool, str], ...] = (
            ("timed-out", ProcessResult(0, turn_json(), "", True), False, "timed out"),
            (
                "print-timeout",
                ProcessResult(0, turn_json(), "[agy] print timeout after 5m0s", False),
                False,
                "timed out",
            ),
            ("malformed-json", ProcessResult(0, "{bad", "err", False), False, "no decodable agy result"),
            ("negative-tokens", ProcessResult(0, turn_json(input_tokens=-5), "", False), False, "invalid usage"),
            ("error-status", ProcessResult(0, turn_json(status="ERROR"), "", False), True, "reported an error"),
            ("empty-response", ProcessResult(0, turn_json(response="   "), "", False), True, "no text"),
        )
        for label, completed, has_cost, expected_problem in cases:
            with self.subTest(label=label), mock.patch.object(processes, "run_tool", return_value=completed):
                res = oneshot.ask("prompt", "gemini-4-argon", Window(None))
                self.assertIsNone(res.text)
                self.assertEqual(has_cost, res.cost_usd is not None)
                self.assertIn(expected_problem, res.problem or "")

    def test_oneshot_and_cli_score_reject_flash_and_unknown_gemini_models_before_subprocess(self) -> None:
        for bad_model in ("gemini-3.8-flash", "gemini-3.8-flash-high", "gemini-3-flash", "gemini-unknown-99"):
            with self.subTest(bad_model=bad_model), mock.patch.object(processes, "run_tool") as run_mock:
                with self.assertRaises(agy_driver.AgyUnavailableError):
                    oneshot.ask("prompt", bad_model, Window(None))
                run_mock.assert_not_called()
            with self.subTest(cli_model=bad_model), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                cli.decode(["score", "--out", "out", "--cap-usd", "5.0", "--model", bad_model])

        default_score = cli.decode(["score", "--out", "out", "--cap-usd", "5.0"])
        assert isinstance(default_score, cli.Score)
        self.assertEqual(scoring.SCORER_MODEL, default_score.model)

        gemini_score = cli.decode(["score", "--out", "out", "--cap-usd", "5.0", "--model", "gemini-4-argon"])
        assert isinstance(gemini_score, cli.Score)
        self.assertEqual("gemini-4-argon", gemini_score.model)


if __name__ == "__main__":
    unittest.main()
