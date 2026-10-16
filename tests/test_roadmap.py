"""Disposable versions of the reviewed 15-appearance/12-member and 4→3→2 examples.

The grouping is hypothetical; the original read-only Bitwig observations were at
revision 1084. These scenarios never open that project or claim its current state.
"""

import unittest
from dataclasses import replace

from pinboard.adapters.files import roadmap as renderer
from pinboard.application import roadmap, stored_state
from pinboard.domain import initiatives
from pinboard.domain.identifiers import HostId, TaskId, WorkItemId
from tests.support import SQLITE_NOW


class RoadmapTest(unittest.TestCase):
    def initiative(self, name: str, members: tuple[str, ...]) -> initiatives.Initiative:
        identity = initiatives.InitiativeId(name)
        provenance = initiatives.Provenance(
            initiatives.HumanReason("Owner", SQLITE_NOW.date(), "Observed workflow"),
            TaskId("test"),
            HostId("local"),
            SQLITE_NOW,
            1,
        )
        revision = initiatives.Revision(
            identity, 1, 1, initiatives.Definition(name, "Goal", "Rationale", "Success signal", ()), provenance
        )
        return initiatives.Initiative(
            identity,
            1,
            (revision,),
            tuple(initiatives.Membership(identity, WorkItemId(member), 1, provenance, None) for member in members),
            (),
            (),
        )

    def facts(self, completed: frozenset[str]) -> roadmap.Facts:
        # Exact independently reviewed graph, including search's stored prerequisite order.
        dependencies = {
            "o": (),
            "p": (),
            "a": (),
            "k": (),
            "c": (),
            "t": (),
            "r": ("o",),
            "d": ("o",),
            "v": ("d",),
            "m": ("o",),
            "s": ("v", "r", "k"),
            "b": ("v",),
        }
        items = tuple(
            roadmap.Item(
                WorkItemId(identity),
                identity,
                stored_state.StoredWorkItemState.DONE
                if identity in completed
                else stored_state.StoredWorkItemState.BLOCKED
                if identity == "b"
                else stored_state.StoredWorkItemState.READY,
                tuple(WorkItemId(value) for value in prerequisites),
                None if identity in completed else position,
            )
            for position, (identity, prerequisites) in enumerate(dependencies.items(), start=1)
        )
        return roadmap.Facts(
            1084,
            (
                self.initiative("find-and-revisit", ("o", "r", "d", "v", "s", "b")),
                self.initiative("control-targets", ("o", "d", "m", "t")),
                self.initiative("keyboard-navigation", ("p", "a", "k", "c", "s")),
            ),
            items,
        )

    def test_shared_unique_counts_and_global_remaining_waves_reflow_without_assessment(self) -> None:
        for completed, remaining_waves in ((frozenset(), 4), (frozenset({"o"}), 3), (frozenset({"o", "d"}), 2)):
            with self.subTest(completed=completed):
                facts = self.facts(completed)
                value = roadmap.project(facts)
                self.assertEqual(15, sum(len(lane.memberships) for lane in facts.initiatives))
                self.assertEqual(12, len(value.current_members))
                self.assertEqual(roadmap.Counts(len(completed), 0, 0, 12 - len(completed)), value.totals)
                self.assertEqual(
                    remaining_waves,
                    max(
                        placement.number
                        for placement in value.placements.values()
                        if isinstance(placement, roadmap.Wave)
                    ),
                )
                self.assertEqual(roadmap.Wave(remaining_waves), value.placements[WorkItemId("s")])
                self.assertEqual(roadmap.Wave(remaining_waves), value.placements[WorkItemId("b")])
                markdown, html = renderer.render(value)
                self.assertEqual(2, markdown.count(b"[s](items/s.md)"))
                self.assertIn(b"Not assessed", html)
                self.assertTrue(all(not lane.assessments for lane in facts.initiatives))
                self.assertEqual(stored_state.StoredWorkItemState.BLOCKED, facts.items[-1].state)

    def test_supersession_satisfies_without_delivery_and_dropped_paths_stay_blocked(self) -> None:
        facts = self.facts(frozenset())
        superseded = replace(
            facts,
            items=tuple(
                replace(item, state=stored_state.StoredWorkItemState.SUPERSEDED, queue_position=None)
                if item.item_id == "d"
                else item
                for item in facts.items
            ),
        )
        value = roadmap.project(superseded)
        self.assertEqual(roadmap.Counts(0, 0, 1, 11), value.totals)
        self.assertEqual(roadmap.Wave(1), value.placements[WorkItemId("v")])
        dropped = replace(
            facts,
            items=tuple(
                replace(item, state=stored_state.StoredWorkItemState.DROPPED, queue_position=None)
                if item.item_id == "o"
                else item
                for item in facts.items
            ),
        )
        value = roadmap.project(dropped)
        for identity in ("r", "d", "v", "m", "s", "b"):
            placement = value.placements[WorkItemId(identity)]
            self.assertIsInstance(placement, roadmap.Blocked)
            assert isinstance(placement, roadmap.Blocked)
            self.assertEqual("o", placement.path[-1])
        self.assertEqual(roadmap.Counts(0, 1, 0, 11), value.totals)

    def test_completed_member_removal_and_readdition_preserve_episodes_without_other_lane_effect(self) -> None:
        facts = self.facts(frozenset({"o", "d"}))
        first, second, third = facts.initiatives
        original = first.memberships[0]
        removed = replace(original, removed=original.added)
        first = replace(first, memberships=(removed, *first.memberships[1:]))
        value = roadmap.project(replace(facts, initiatives=(first, second, third)))
        self.assertEqual(2, value.totals.delivered)
        html = renderer.render(value)[1]
        self.assertIn(b"Removed membership earns no delivery credit", html)
        self.assertEqual(
            1,
            sum(
                member.item_id == "o" and member.removed is None
                for lane in value.facts.initiatives
                for member in lane.memberships
            ),
        )
        readded = replace(original, episode=2)
        first = replace(first, memberships=(*first.memberships, readded))
        value = roadmap.project(replace(facts, initiatives=(first, second, third)))
        self.assertEqual(2, value.totals.delivered)
        self.assertEqual(
            2,
            sum(
                member.item_id == "o" and member.removed is None
                for lane in value.facts.initiatives
                for member in lane.memberships
            ),
        )
        self.assertIn(b"episode 2", renderer.render(value)[1])

    def test_removed_only_terminal_history_is_not_external_prerequisite_context(self) -> None:
        facts = self.facts(frozenset())
        first, *others = facts.initiatives
        ended = replace(first.memberships[0], item_id=WorkItemId("retired-history"), removed=first.memberships[0].added)
        first = replace(first, memberships=(*first.memberships, ended))
        historical = roadmap.Item(
            WorkItemId("retired-history"), "Retired history", stored_state.StoredWorkItemState.DONE, (), None
        )
        value = roadmap.project(replace(facts, initiatives=(first, *others), items=(*facts.items, historical)))
        self.assertNotIn(WorkItemId("retired-history"), value.external_prerequisites)
        self.assertEqual(roadmap.Counts(0, 0, 0, 12), value.totals)
        self.assertIn(b"retired-history", renderer.render(value)[1])

    def test_external_dependency_closure_context_unassigned_and_unknown_graph_are_explicit(self) -> None:
        facts = self.facts(frozenset())
        keyboard = replace(facts, initiatives=(facts.initiatives[-1],))
        value = roadmap.project(keyboard)
        self.assertEqual(roadmap.Wave(4), value.placements[WorkItemId("s")])
        self.assertEqual(7, len(value.unassigned))
        self.assertEqual(12, value.totals.unfinished)
        missing = replace(facts.items[0], dependencies=(WorkItemId("missing"),))
        cyclic = replace(facts.items[1], dependencies=(WorkItemId("a"),))
        other_cycle = replace(facts.items[2], dependencies=(WorkItemId("p"),))
        invalid = replace(facts, items=(missing, cyclic, other_cycle, *facts.items[3:]))
        value = roadmap.project(invalid)
        for identity in ("o", "r", "d", "v", "s", "b", "p", "a"):
            self.assertIsInstance(value.placements[WorkItemId(identity)], roadmap.Unknown)
        self.assertIn(b"Missing prerequisite", renderer.render(value)[1])
        self.assertIn(b"Cyclic or invalid", renderer.render(value)[1])
        self.assertEqual(facts.items, keyboard.items)
