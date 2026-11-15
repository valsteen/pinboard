"""Held work names what it waits for, and every surface says so in one plain sentence."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from pinboard.application import unblockers
from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    DecisionId,
    LeaseId,
    WorkItemId,
)
from pinboard.domain.ledger import LedgerSnapshot
from tests.domain_support import action, attempt_record, expect_success

NOW = datetime(2026, 11, 15, tzinfo=UTC)
DIGEST = "a" * 64
ITEMS = work_models.WaitingForItemsToFinish((WorkItemId("survey"),))
QUESTION = work_models.WaitingForHumanDecision("Which audience should the guide address first?")
CONDITION = work_models.WaitingForOutsideCondition("The vendor publishes its v2 API.")


def _item(
    item_id: str,
    state: work_models.WorkState,
    *,
    attempt: str | None = None,
    depends_on: tuple[work_models.Dependency, ...] = (),
    unblocker: work_models.Unblocker | None = None,
) -> work_models.WorkItem:
    return work_models.WorkItem(
        WorkItemId(item_id),
        state,
        None,
        depends_on,
        None if attempt is None else AttemptId(attempt),
        None,
        None,
        None,
        1,
        unblocker=unblocker,
    )


def _definition(item_id: str, dependencies: tuple[work_models.Dependency, ...]) -> work_models.DefinitionAnchor:
    return work_models.DefinitionAnchor(
        WorkItemId(item_id),
        1,
        DIGEST,
        work_models.WorkItemDefinition(
            "Build the map",
            "Add navigable routes",
            "The party cannot travel",
            (),
            ("Add navigable routes",),
            (),
            ("Reach the next area",),
            dependencies,
            "Add navigable routes",
            "Reach the next area",
            work_models.CheckoutPolicy.COORDINATOR_SELECTED,
            (
                work_models.WorkObligation(
                    work_models.ObligationId("reach-next-area"),
                    "Reach the next area",
                    work_models.ObligationDeferralPolicy.FORBIDDEN,
                ),
            ),
            (),
            route_facts=None,
        ),
    )


def _snapshot(*items: work_models.WorkItem, attempts: tuple[work_models.AttemptRecord, ...] = ()) -> LedgerSnapshot:
    return LedgerSnapshot(
        "revision",
        items,
        attempts=attempts,
        definitions=(_definition("build", (WorkItemId("survey"),)),),
        attempt_authorities=(
            work_models.AttemptAuthority(AttemptId("build-1"), WorkItemId("build"), LeaseId("worker-lease"), 3),
        ),
        accepted_checkpoints=frozenset(),
        dependency_facts=(),
        dropped_item_ids=frozenset(),
    )


def _held_change_unblocker(decision: decision_models.Decision) -> work_models.Unblocker | None:
    match decision.change:
        case (
            decision_models.BlockWorkItemChange(unblocker=unblocker)
            | decision_models.BlockAttemptChange(unblocker=unblocker)
            | decision_models.AttemptStateChange(unblocker=unblocker)
            | decision_models.CheckpointAcceptanceChange(unblocker=unblocker)
        ):
            return unblocker
        case decision_models.RebindAttemptChange(carried_unblocker=carried):
            return carried
        case _:
            return None


class HoldingDecisionTest(unittest.TestCase):
    def test_every_holding_transition_records_each_kind_of_unblocker(self) -> None:
        ready = _snapshot(_item("build", work_models.WorkState.READY, depends_on=(WorkItemId("survey"),)))
        active = _snapshot(
            _item("build", work_models.WorkState.ACTIVE, attempt="build-1", depends_on=(WorkItemId("survey"),)),
            attempts=(attempt_record("build-1", "build", work_models.AttemptState.ACTIVE, 1, DIGEST),),
        )
        for unblocker in (ITEMS, QUESTION, CONDITION):
            for snapshot, command in (
                (
                    ready,
                    decision_models.BlockWorkItemCommand(
                        action(decision_models.BlockWorkItemAction, WorkItemId("build")),
                        work_models.BlockInput(unblocker),
                    ),
                ),
                (
                    active,
                    decision_models.BlockCommand(
                        action(decision_models.BlockAttemptAction, AttemptId("build-1")),
                        work_models.BlockInput(unblocker),
                    ),
                ),
                (
                    active,
                    decision_models.PauseCommand(
                        action(decision_models.PauseAction, AttemptId("build-1")),
                        work_models.PlainPauseInput("Stop at a stable boundary.", unblocker),
                    ),
                ),
            ):
                with self.subTest(kind=command.action.kind.value, unblocker=type(unblocker).__name__):
                    decision = expect_success(decision_outcome(snapshot, command, NOW))
                    self.assertEqual(unblocker, _held_change_unblocker(decision))

    def test_unnamed_empty_or_unaccepted_unblockers_are_rejected_unchanged(self) -> None:
        ready = _snapshot(_item("build", work_models.WorkState.READY, depends_on=(WorkItemId("survey"),)))
        block_item = action(decision_models.BlockWorkItemAction, WorkItemId("build"))
        for unblocker, named in (
            (work_models.WaitingForItemsToFinish((WorkItemId("elsewhere"),)), "elsewhere"),
            (work_models.WaitingForItemsToFinish(()), "distinct items"),
            (work_models.WaitingForItemsToFinish((WorkItemId("survey"), WorkItemId("survey"))), "distinct items"),
            (work_models.WaitingForHumanDecision("   "), "state its text"),
            (work_models.WaitingForOutsideCondition(""), "state its text"),
        ):
            with self.subTest(unblocker=unblocker):
                rejected = decision_outcome(
                    ready, decision_models.BlockWorkItemCommand(block_item, work_models.BlockInput(unblocker)), NOW
                )
                assert isinstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)
                self.assertIn(named, rejected.message)
                self.assertIn("ledger is unchanged", rejected.message)

    def test_derived_unblockers_for_a_decision_batch_and_a_checkpoint_need_no_new_input(self) -> None:
        active = _snapshot(
            _item("build", work_models.WorkState.ACTIVE, attempt="build-1", depends_on=(WorkItemId("survey"),)),
            attempts=(attempt_record("build-1", "build", work_models.AttemptState.ACTIVE, 1, DIGEST),),
        )
        batch = work_models.DecisionBatchInput(
            "Two choices remain.",
            (
                work_models.PauseDecision(
                    DecisionId("audience"),
                    "Who reads it?",
                    ("Users", "Admins"),
                    "Users",
                    "Scope",
                    "Delay",
                ),
            ),
        )
        paused = expect_success(
            decision_outcome(
                active,
                decision_models.PauseCommand(action(decision_models.PauseAction, AttemptId("build-1")), batch),
                NOW,
            )
        )
        derived = _held_change_unblocker(paused)
        assert isinstance(derived, work_models.WaitingForHumanDecision)
        self.assertIn("audience", derived.question)
        review = _snapshot(
            _item("build", work_models.WorkState.REVIEW, attempt="build-1", depends_on=(WorkItemId("survey"),)),
            attempts=(attempt_record("build-1", "build", work_models.AttemptState.REVIEW, 1, DIGEST, "candidate"),),
        )
        accepted = expect_success(
            decision_outcome(
                review,
                decision_models.AcceptCheckpointCommand(
                    action(decision_models.AcceptCheckpointAction, AttemptId("build-1")),
                    work_models.AcceptCheckpointInput(CheckpointId("routes"), CandidateId("candidate"), "Verified."),
                ),
                NOW,
            )
        )
        checkpoint = _held_change_unblocker(accepted)
        assert isinstance(checkpoint, work_models.WaitingForHumanDecision)
        self.assertIn("routes", checkpoint.question)
        self.assertIn("remaining work", checkpoint.question)

    def test_paused_rebind_copies_the_current_unblocker_forward(self) -> None:
        paused = _snapshot(
            _item(
                "build",
                work_models.WorkState.PAUSED,
                attempt="build-1",
                depends_on=(WorkItemId("survey"),),
                unblocker=QUESTION,
            ),
            attempts=(attempt_record("build-1", "build", work_models.AttemptState.PAUSED, 1, DIGEST),),
        )
        paused = replace(
            paused, artifacts=(work_models.ArtifactRecord(ArtifactRefId(7), work_models.ArtifactKind.BRIEF),)
        )
        rebound = expect_success(
            decision_outcome(
                paused,
                decision_models.RebindAttemptCommand(
                    action(decision_models.RebindAttemptAction, AttemptId("build-1")),
                    work_models.RebindAttemptInput("build-branch", "base", ArtifactRefId(7)),
                ),
                NOW,
            )
        )
        self.assertEqual(QUESTION, _held_change_unblocker(rebound))

    def test_setting_unstarted_work_aside_is_block_item_with_an_outside_condition(self) -> None:
        kinds = {kind.value for kind in decision_models.ActionKind}
        self.assertNotIn("defer", kinds)
        self.assertNotIn("reopen", kinds)
        self.assertNotIn("deferred", {state.value for state in work_models.WorkState})
        ready = _snapshot(_item("build", work_models.WorkState.READY, depends_on=(WorkItemId("survey"),)))
        decision = expect_success(
            decision_outcome(
                ready,
                decision_models.BlockWorkItemCommand(
                    action(decision_models.BlockWorkItemAction, WorkItemId("build")), work_models.BlockInput(CONDITION)
                ),
                NOW,
            )
        )
        self.assertEqual(CONDITION, _held_change_unblocker(decision))


AWAITING = work_models.DependencyFate.AWAITING_ITEM
FINISHED = work_models.DependencyFate.ITEM_FINISHED
CANCELLED = work_models.DependencyFate.ITEM_CANCELLED
CHECKPOINT = work_models.CheckpointDependency(WorkItemId("survey"), CheckpointId("cp-1"))


def _facts(
    unblocker: work_models.Unblocker,
    *,
    started: bool,
    dependencies: tuple[tuple[work_models.Dependency | str, work_models.DependencyFate], ...],
    replacement: str | None = None,
) -> unblockers.HoldFacts:
    return unblockers.HoldFacts(
        started,
        unblocker,
        tuple(
            unblockers.DependencyCondition(WorkItemId(target) if isinstance(target, str) else target, fate)
            for target, fate in dependencies
        ),
        None if replacement is None else WorkItemId(replacement),
    )


def _sentence(facts: unblockers.HoldFacts) -> tuple[str, unblockers.HoldDescription]:
    description = unblockers.describe_hold(facts)
    return unblockers.sentence_text(description.parts), description


class DependencyFateTest(unittest.TestCase):
    def test_each_prerequisite_outcome_gives_one_fate_and_an_accepted_checkpoint_takes_precedence(self) -> None:
        survey = WorkItemId("survey")
        accepted = frozenset({CHECKPOINT})
        for target, outcome, checkpoints, expected in (
            (survey, work_models.PrerequisiteOutcome.LIVE, frozenset(), AWAITING),
            (
                CHECKPOINT,
                work_models.PrerequisiteOutcome.LIVE,
                frozenset(),
                work_models.DependencyFate.AWAITING_CHECKPOINT,
            ),
            (survey, work_models.PrerequisiteOutcome.FINISHED, frozenset(), FINISHED),
            (CHECKPOINT, work_models.PrerequisiteOutcome.FINISHED, frozenset(), FINISHED),
            (survey, work_models.PrerequisiteOutcome.CANCELLED, frozenset(), CANCELLED),
            (CHECKPOINT, work_models.PrerequisiteOutcome.CANCELLED, frozenset(), CANCELLED),
            (
                CHECKPOINT,
                work_models.PrerequisiteOutcome.LIVE,
                accepted,
                work_models.DependencyFate.CHECKPOINT_ACCEPTED,
            ),
            (
                CHECKPOINT,
                work_models.PrerequisiteOutcome.FINISHED,
                accepted,
                work_models.DependencyFate.CHECKPOINT_ACCEPTED,
            ),
            (
                CHECKPOINT,
                work_models.PrerequisiteOutcome.CANCELLED,
                accepted,
                work_models.DependencyFate.CHECKPOINT_ACCEPTED,
            ),
        ):
            with self.subTest(target=target, outcome=outcome, accepted=bool(checkpoints)):
                fate = work_models.dependency_fate(target, outcome, checkpoints)
                self.assertEqual(expected, fate)
                self.assertEqual(
                    fate.satisfied,
                    work_models.dependency_satisfied(
                        target, outcome == work_models.PrerequisiteOutcome.FINISHED, checkpoints
                    ),
                )
        self.assertTrue(FINISHED.satisfied)
        self.assertTrue(work_models.DependencyFate.CHECKPOINT_ACCEPTED.satisfied)
        self.assertFalse(CANCELLED.satisfied)


class HoldSentenceTest(unittest.TestCase):
    def test_sentence_says_whether_work_started_whether_anyone_works_and_what_it_waits_for(self) -> None:
        unstarted = unblockers.describe_hold(_facts(ITEMS, started=False, dependencies=(("survey", AWAITING),)))
        started = unblockers.describe_hold(_facts(ITEMS, started=True, dependencies=(("survey", AWAITING),)))
        self.assertIn("not started", unblockers.sentence_text(unstarted.parts))
        self.assertIn("started", unblockers.sentence_text(started.parts))
        self.assertIn("nobody is working on it now", unblockers.sentence_text(started.parts))
        for description in (unstarted, started):
            self.assertEqual((WorkItemId("survey"),), unblockers.sentence_items(description.parts))
            self.assertIn("survey to finish", unblockers.sentence_text(description.parts))
            self.assertFalse(description.resume_needed)

    def test_finished_waited_items_say_someone_needs_to_resume_only_when_resume_is_offered(self) -> None:
        done = unblockers.describe_hold(_facts(ITEMS, started=True, dependencies=(("survey", FINISHED),)))
        self.assertTrue(done.resume_needed)
        self.assertIn("can continue once someone resumes it", unblockers.sentence_text(done.parts))
        other = unblockers.describe_hold(
            _facts(ITEMS, started=False, dependencies=(("survey", FINISHED), ("legal-review", AWAITING)))
        )
        self.assertFalse(other.resume_needed)
        self.assertIn("still waits for legal-review to finish", unblockers.sentence_text(other.parts))
        self.assertEqual((WorkItemId("survey"), WorkItemId("legal-review")), unblockers.sentence_items(other.parts))
        replaced = unblockers.describe_hold(
            _facts(ITEMS, started=False, dependencies=(("survey", FINISHED),), replacement="new-map")
        )
        self.assertFalse(replaced.resume_needed)
        self.assertIn("planned replacement by new-map", unblockers.sentence_text(replaced.parts))

    def test_a_named_item_dropped_from_the_dependencies_is_never_called_finished(self) -> None:
        for others in ((), (("legal-review", FINISHED),)):
            with self.subTest(others=others):
                text, description = _sentence(_facts(ITEMS, started=True, dependencies=others))
                self.assertIn("survey is no longer one of this item's dependencies", text)
                self.assertNotIn("has finished", text.replace("legal-review has finished", ""))
                self.assertNotIn("survey to finish", text)
                self.assertTrue(description.resume_needed)
                self.assertIn("can continue once someone resumes it", text)
        text, description = _sentence(_facts(ITEMS, started=False, dependencies=(("legal-review", AWAITING),)))
        self.assertIn("survey is no longer one of this item's dependencies", text)
        self.assertIn("still waits for legal-review to finish", text)
        self.assertFalse(description.resume_needed)
        self.assertNotIn("can continue", text)

    def test_a_cancelled_waited_for_item_is_not_awaited_and_needs_the_dependencies_revised(self) -> None:
        text, description = _sentence(_facts(ITEMS, started=True, dependencies=(("survey", CANCELLED),)))
        self.assertIn("survey was cancelled and will not finish", text)
        self.assertIn("dependencies need revising", text)
        self.assertNotIn("survey to finish", text)
        self.assertNotIn("has finished", text)
        self.assertNotIn("can continue", text)
        self.assertFalse(description.resume_needed)
        both = work_models.WaitingForItemsToFinish((WorkItemId("survey"), WorkItemId("legal-review")))
        text, description = _sentence(
            _facts(both, started=False, dependencies=(("survey", CANCELLED), ("legal-review", FINISHED)))
        )
        self.assertIn("survey was cancelled and will not finish", text)
        self.assertIn("legal-review has finished", text)
        self.assertFalse(description.resume_needed)
        text, _ = _sentence(
            _facts(both, started=False, dependencies=(("survey", CANCELLED), ("legal-review", AWAITING)))
        )
        self.assertIn("It waits for legal-review to finish", text)
        self.assertIn("survey was cancelled and will not finish", text)
        text, _ = _sentence(_facts(QUESTION, started=False, dependencies=(("survey", CANCELLED),)))
        self.assertIn("survey was cancelled and will not finish", text)
        self.assertNotIn("survey to finish", text)

    def test_checkpoint_dependencies_name_the_checkpoint_not_the_item_finishing(self) -> None:
        text, description = _sentence(
            _facts(ITEMS, started=True, dependencies=((CHECKPOINT, work_models.DependencyFate.AWAITING_CHECKPOINT),))
        )
        self.assertIn("checkpoint cp-1 of survey to be accepted", text)
        self.assertNotIn("survey to finish", text)
        self.assertFalse(description.resume_needed)
        text, description = _sentence(
            _facts(ITEMS, started=True, dependencies=((CHECKPOINT, work_models.DependencyFate.CHECKPOINT_ACCEPTED),))
        )
        self.assertIn("Checkpoint cp-1 of survey was accepted", text)
        self.assertNotIn("has finished", text)
        self.assertTrue(description.resume_needed)
        self.assertIn("can continue once someone resumes it", text)
        self.assertEqual((WorkItemId("survey"),), unblockers.sentence_items(description.parts))

    def test_human_decisions_and_outside_conditions_never_resolve_on_their_own(self) -> None:
        for unblocker, text in (
            (QUESTION, "human decision: Which audience should the guide address first?"),
            (CONDITION, "outside condition: The vendor publishes its v2 API."),
        ):
            with self.subTest(unblocker=type(unblocker).__name__):
                description = unblockers.describe_hold(
                    _facts(unblocker, started=False, dependencies=(("survey", FINISHED),))
                )
                self.assertFalse(description.resume_needed)
                self.assertIn(text, unblockers.sentence_text(description.parts))
                pending = unblockers.describe_hold(
                    _facts(unblocker, started=True, dependencies=(("survey", AWAITING),))
                )
                self.assertIn("Resuming also waits for survey to finish", unblockers.sentence_text(pending.parts))

    def test_stored_unblockers_round_trip_through_their_canonical_record(self) -> None:
        for unblocker in (ITEMS, QUESTION, CONDITION):
            with self.subTest(unblocker=type(unblocker).__name__):
                self.assertEqual(unblocker, unblockers.decode_unblocker(unblockers.encode_unblocker(unblocker)))


if __name__ == "__main__":
    unittest.main()
