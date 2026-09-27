import unittest

from pinboard.domain.identifiers import LeaseId, WorkItemId
from tests.decision_support import project_decision_snapshot
from tests.support import SQLITE_NOW, complete_sqlite_state


class DecisionProjectionTest(unittest.TestCase):
    def test_stored_state_projects_current_decision_facts(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)

        self.assertEqual("12", snapshot.revision)
        self.assertEqual(
            (WorkItemId("intake-work"), WorkItemId("work-a"), WorkItemId("work-c"), WorkItemId("zz-proposal-a")),
            tuple(item.work_item_id for item in snapshot.items),
        )
        self.assertEqual((WorkItemId("work-c"),), snapshot.work_items_by_id()[WorkItemId("work-a")].depends_on)
        self.assertEqual((1, 2, 3, 4), tuple(item.queue_position for item in snapshot.items))
        sparse_item = snapshot.work_items_by_id()[WorkItemId("intake-work")]
        self.assertIsNone(sparse_item.source)
        self.assertIsNone(sparse_item.notes)
        self.assertEqual(LeaseId("attempt-lease-a"), snapshot.attempt_authorities[0].lease_id)
        self.assertEqual(2, snapshot.host_epoch)

    def test_terminal_work_is_history_not_live_work(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)

        self.assertNotIn(WorkItemId("work-b"), snapshot.work_items_by_id())
        self.assertIn(WorkItemId("work-b"), snapshot.history_items)


if __name__ == "__main__":
    unittest.main()
