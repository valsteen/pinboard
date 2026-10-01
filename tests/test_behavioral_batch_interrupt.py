"""A controlled executor reproduces interruption before shutdown drains queued work."""

import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import TracebackType
from unittest import mock

from evals.behavioral import runner
from evals.behavioral.layout import Layout
from evals.behavioral.processes import Window
from evals.behavioral.records import RunKey, RunRecord, Scenario
from evals.behavioral.spend import Budget, Category


class BatchInterruptTest(unittest.TestCase):
    def test_interrupt_halts_queued_work_before_executor_shutdown(self) -> None:
        started: list[str] = []
        evidence: list[str] = []
        queued: list[Callable[[], None]] = []
        reservation_at_shutdown: list[float] = []
        with tempfile.TemporaryDirectory() as temporary:
            budget = Budget(Layout(Path(temporary)), 120, Window(None), False)
            plan = mock.Mock(spec=runner.RunPlan)
            plan.layout = Layout(Path(temporary))
            scenario = mock.Mock()
            keys = [RunKey(scenario_id="interrupt", variant="test", index=i) for i in range(1, 4)]
            plan.planned.return_value = [(scenario, key) for key in keys]

            class Executor:
                def __init__(self, *, max_workers: int) -> None:
                    self.count = 0
                    self.max_workers = max_workers

                def __enter__(self) -> Executor:
                    return self

                def submit(
                    self, call: Callable[[Scenario, RunKey], None], scenario: Scenario, key: RunKey
                ) -> Future[None]:
                    self.count += 1
                    future: Future[None] = Future()
                    if self.count == 1:
                        call(scenario, key)
                        future.set_exception(KeyboardInterrupt())
                    else:

                        def pending_call() -> None:
                            call(scenario, key)

                        queued.append(pending_call)
                        future.set_result(None)
                    return future

                def __exit__(
                    self,
                    _kind: type[BaseException] | None,
                    _error: BaseException | None,
                    _traceback: TracebackType | None,
                ) -> None:
                    reservation_at_shutdown.append(budget.reserved)
                    for call in queued:
                        call()

            def one(_scenario: Scenario, key: RunKey) -> RunRecord:
                started.append(key.display())
                evidence.append(key.display())
                return mock.Mock(spec=RunRecord)

            with mock.patch.object(runner, "ThreadPoolExecutor", Executor), self.assertRaises(KeyboardInterrupt):
                runner.execute(plan, budget, 1, Category.CLAUDE_AGENT_RUN, one)
            self.assertEqual([keys[0].display()], started)
            self.assertEqual(started, evidence)
            self.assertEqual([0.0], reservation_at_shutdown)
            self.assertEqual(0.0, budget.reserved)


if __name__ == "__main__":
    unittest.main()
