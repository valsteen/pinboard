"""Exercise a deferred action-family completeness probe outside production.

The synthetic action is not an installed Pinboard action. The intentionally
local declaration below models only the tool-contract owner's route decision.
"""

import unittest

from pinboard.interfaces.tool_contract import installed_tool_contract
from tests.prototypes.action_family_completeness import ActionName, ProjectionOwner, require_dispositions

SYNTHETIC_ACTION = ActionName("archive")
INSTALLED_ACTION_ROUTES = {
    ActionName(entry.action_kind): entry.execution_route for entry in installed_tool_contract().actions
}
SUPPORTED_ACTIONS = (*INSTALLED_ACTION_ROUTES, SYNTHETIC_ACTION)
ACTION_ROUTES = {
    **INSTALLED_ACTION_ROUTES,
    SYNTHETIC_ACTION: "transition",
}
ROUTE_OWNER = ProjectionOwner(
    decision="select the installed execution route",
    production_path="src/pinboard/interfaces/tool_contract.py",
    production_symbol="_action_execution_route",
    declaration_path="tests/prototypes/test_action_family_completeness.py",
    declaration_symbol="ACTION_ROUTES",
    allowed_values=("transition", "dispatch", "overview", "runtime-continuation", "runtime-blocker-artifact"),
    accepted_facts=(
        "the synthetic archive action is state-changing, uses a strict payload, and follows the ordinary transition "
        "route; this fixture does not define its domain legality, storage effect, or presentation"
    ),
)
FOCUSED_CHECK = "uv run --locked python -m unittest tests.prototypes.test_action_family_completeness"


class ActionFamilyCompletenessTests(unittest.TestCase):
    def test_current_family_keeps_its_real_route_split(self) -> None:
        self.assertEqual(len(INSTALLED_ACTION_ROUTES), 26)
        self.assertEqual(tuple(INSTALLED_ACTION_ROUTES.values()).count("transition"), 22)
        self.assertEqual(
            {
                ActionName("continue"): "runtime-continuation",
                ActionName("dispatch"): "dispatch",
                ActionName("inspect"): "overview",
                ActionName("report-blocker"): "runtime-blocker-artifact",
            },
            {action: route for action, route in INSTALLED_ACTION_ROUTES.items() if route != "transition"},
        )

    def test_synthetic_action_requires_one_deliberate_local_route(self) -> None:
        require_dispositions(ROUTE_OWNER, SUPPORTED_ACTIONS, ACTION_ROUTES, FOCUSED_CHECK)

    def test_failure_contains_the_complete_next_step(self) -> None:
        omitted = ActionName("omitted")
        with self.assertRaises(AssertionError) as raised:
            require_dispositions(
                ROUTE_OWNER,
                (omitted,),
                {},
                FOCUSED_CHECK,
            )
        message = str(raised.exception)
        self.assertIn("missing action 'omitted'", message)
        self.assertIn("src/pinboard/interfaces/tool_contract.py:_action_execution_route", message)
        self.assertIn("tests/prototypes/test_action_family_completeness.py:ACTION_ROUTES", message)
        self.assertIn("accepted facts:", message)
        self.assertIn(f"next check: {FOCUSED_CHECK}", message)

    def test_failure_aggregates_missing_unknown_and_invalid_dispositions(self) -> None:
        missing = ActionName("missing")
        unknown = ActionName("unknown")
        with self.assertRaises(AssertionError) as raised:
            require_dispositions(
                ROUTE_OWNER,
                (SYNTHETIC_ACTION, missing),
                {SYNTHETIC_ACTION: "not-a-route", unknown: "transition"},
                FOCUSED_CHECK,
            )
        message = str(raised.exception)
        self.assertIn("missing action 'missing'", message)
        self.assertIn("unknown action 'unknown'", message)
        self.assertIn("invalid action 'archive' value 'not-a-route'", message)

    def test_completeness_cannot_choose_between_valid_semantics(self) -> None:
        require_dispositions(
            ROUTE_OWNER,
            (SYNTHETIC_ACTION,),
            {SYNTHETIC_ACTION: "overview"},
            FOCUSED_CHECK,
        )


if __name__ == "__main__":
    unittest.main()
