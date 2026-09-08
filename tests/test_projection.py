import unittest

from pinboard.domain.identifiers import ItemId, LeaseId
from tests.decision_support import project_decision_snapshot
from tests.support import SQLITE_NOW, complete_sqlite_state


class DecisionProjectionTest(unittest.TestCase):
    def test_stored_state_projects_current_decision_facts(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)

        self.assertEqual("12", snapshot.revision)
        self.assertEqual(
            (ItemId("intake-work"), ItemId("work-a"), ItemId("work-c"), ItemId("zz-proposal-a")),
            tuple(item.item for item in snapshot.items),
        )
        self.assertEqual((ItemId("work-c"),), snapshot.items_by_id()[ItemId("work-a")].depends_on)
        self.assertEqual((1, 2, 3, 4), tuple(item.queue_position for item in snapshot.items))
        sparse_item = snapshot.items_by_id()[ItemId("intake-work")]
        self.assertIsNone(sparse_item.source)
        self.assertIsNone(sparse_item.notes)
        self.assertEqual(LeaseId("attempt-lease-a"), snapshot.attempt_authorities[0].lease_id)
        self.assertEqual(2, snapshot.host_epoch)

    def test_terminal_work_is_history_not_live_work(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)

        self.assertNotIn(ItemId("work-b"), snapshot.items_by_id())
        self.assertIn(ItemId("work-b"), snapshot.history_items)


if __name__ == "__main__":
    unittest.main()
