"""Offline checks for the private benchmark launcher; no credentials or model calls."""

import contextlib
import io
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.behavioral import codex_driver, credentials, processes
import benchmark_limits as limits


class LauncherTests(unittest.TestCase):
    def test_invalid_or_missing_selection_cannot_start_setup(self):
        for arguments in (
            [],
            ['luna', 'implement'],
            ['luna', 'invalid-mode', '--maximum-active-seconds', '117'],
            ['luna', '--maximum-active-seconds', '117'],
            ['luna', 'implement', '--maximum-active-seconds', '0'],
            ['luna', 'implement', '--maximum-active-seconds', '-1'],
            ['luna', 'implement', '--maximum-active-seconds', 'not-a-duration'],
            ['luna', 'implement', '--maximum-active-seconds', str(sys.maxsize)],
        ):
            with self.subTest(arguments=arguments):
                with patch.object(sys, 'argv', ['run_codex.py', *arguments]), \
                     patch.object(credentials, 'exclusive_codex_session') as setup, \
                     patch.object(credentials, 'isolated_home') as isolation, \
                     patch.object(credentials, 'default_source') as source, \
                     patch.object(codex_driver, 'codex') as paid, \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as rejected:
                        runpy.run_path(str(Path(__file__).with_name('run_codex.py')))
                    self.assertNotEqual(rejected.exception.code, 0)
                    setup.assert_not_called()
                    isolation.assert_not_called()
                    source.assert_not_called()
                    paid.assert_not_called()

    def run_launcher(self, arm, mode, budget, durations, failure):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / 'private'
            private.mkdir()
            launcher = private / 'run_codex.py'
            shutil.copyfile(Path(__file__).with_name('run_codex.py'), launcher)
            shutil.copyfile(Path(__file__).with_name('benchmark_limits.py'), private / 'benchmark_limits.py')
            (private / f'prompt-{arm}.txt').write_text('Complete the implementation.')
            isolated = root / 'isolated-home'
            isolated.mkdir()
            home = credentials.IsolatedHome(
                path=isolated, source=root / 'unused-auth.json', copied=b'{}',
                settlement=credentials.CredentialSettlement.UNCHANGED,
            )
            context = codex_driver.LoadedContext(
                entries=[], sandbox_mode='workspace-write', approval_policy='on-request',
                writable_roots=[], prompt_text='',
            )
            now = [1000.0]
            calls = []

            def configure(home, base, model, effort, approval, window):
                (home / 'config.toml').write_text('')
                manifest = home / 'plugins/cache/pinboard/pinboard/test/mcp-codex.json'
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({'mcpServers': {'pinboard': {}}}))

            def execute(arguments, *, home, cwd, timeout_seconds, window):
                calls.append((arguments, timeout_seconds, window.deadline, now[0]))
                if failure is not None:
                    raise failure
                now[0] += durations[len(calls) - 1]
                events = [
                    {'type': 'thread.started', 'thread_id': 'fake-thread'},
                    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Human question'}},
                ]
                return processes.Completed(
                    returncode=0, stdout='\n'.join(json.dumps(event) for event in events),
                    stderr='', timed_out=False,
                )

            class HumanInput:
                def readline(self):
                    now[0] += 100000.0
                    return json.dumps('Continue with the approved answer.') + '\n'

            with patch.object(sys, 'argv', [str(launcher), arm, mode, '--maximum-active-seconds', str(budget)]), \
                 patch.object(sys, 'stdin', HumanInput()), \
                 patch('time.monotonic', side_effect=lambda: now[0]), \
                 patch('threading.Thread'), \
                 patch.object(credentials, 'default_source', return_value=home.source), \
                 patch.object(credentials, 'exclusive_codex_session', return_value=contextlib.nullcontext()), \
                 patch.object(credentials, 'isolated_home', return_value=contextlib.nullcontext(home)) as isolation, \
                 patch.object(codex_driver, 'write_config', side_effect=configure), \
                 patch.object(codex_driver, 'loaded_context', return_value=context), \
                 patch.object(codex_driver, 'rollout_text', return_value=''), \
                 patch.object(codex_driver, 'codex', side_effect=execute), \
                 contextlib.redirect_stdout(io.StringIO()):
                if failure is None:
                    runpy.run_path(str(launcher))
                else:
                    with self.assertRaises(type(failure)):
                        runpy.run_path(str(launcher))
                isolation.assert_called_once()
            outputs = {path.name: path.read_text() for path in (private / arm).iterdir()}
            return calls, outputs

    def test_selected_budget_survives_resume_and_excludes_human_wait(self):
        budget = 117
        spent = 27
        for arm in ('luna', 'luna-xhigh'):
            with self.subTest(arm=arm):
                calls, outputs = self.run_launcher(arm, 'implement', budget, [spent, budget - spent], None)
                self.assertEqual(len(calls), 2)
                for call, expected in zip(calls, (budget, budget - spent), strict=True):
                    arguments, timeout, deadline, started = call
                    self.assertEqual(timeout, expected)
                    self.assertEqual(deadline - started, expected)
                    self.assertIn(f'{budget} active seconds across all turns', arguments[-1])
                    self.assertIn(f'{expected:g} active seconds remain', arguments[-1])
                self.assertEqual(calls[1][0][1], 'resume')
                summary = json.loads(outputs['implementation-2-summary.json'])
                self.assertEqual(summary['maximum_active_seconds'], budget)
                self.assertEqual(summary['active_seconds'], budget)

    def test_probe_also_requires_and_uses_the_selected_budget(self):
        budget = 83
        calls, outputs = self.run_launcher('luna', 'probe', budget, [11], None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], budget)
        self.assertEqual(calls[0][2] - calls[0][3], budget)
        self.assertEqual(json.loads(outputs['probe-summary.json'])['maximum_active_seconds'], budget)

    def test_twelve_hour_endpoint_reaches_driver_offline(self):
        accepted_endpoint = limits.MAXIMUM_ACTIVE_SECONDS
        calls, outputs = self.run_launcher('luna', 'implement', accepted_endpoint, [accepted_endpoint], None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], accepted_endpoint)
        self.assertEqual(calls[0][2] - calls[0][3], accepted_endpoint)
        self.assertIn(f'{accepted_endpoint} active seconds across all turns', calls[0][0][-1])
        self.assertEqual(json.loads(outputs['implementation-1-summary.json'])['active_seconds'], accepted_endpoint)

    def test_interrupted_process_preserves_partial_evidence(self):
        failure = processes.ProcessIncomplete(b'partial output', b'partial error', TimeoutError('expired'))
        calls, outputs = self.run_launcher('luna', 'implement', 61, [], failure)
        self.assertEqual(len(calls), 1)
        self.assertEqual(outputs['implementation-1.jsonl'], failure.stdout)
        self.assertIn('implementation-1-rollout.jsonl', outputs)


if __name__ == '__main__':
    unittest.main()
