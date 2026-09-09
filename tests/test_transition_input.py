import json
import unittest

from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailureCode, RetryDisposition
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, CandidateId, ItemId, ProposalId
from pinboard.interfaces.errors import TransitionInputFailure
from pinboard.interfaces.transition_input import (
    INPUT_CONTRACT_ACTION_KINDS,
    encoded_transition_input_schema,
    parse_transition_command,
)
from tests.domain_support import action
from tests.support import JsonObject, JsonValue


def expect_transition_command(
    value: decision_models.TransitionCommand | TransitionInputFailure,
) -> decision_models.TransitionCommand:
    if isinstance(value, TransitionInputFailure):
        raise AssertionError(str(value))
    return value


def expect_schema(value: bytes | TransitionInputFailure) -> bytes:
    if isinstance(value, TransitionInputFailure):
        raise AssertionError(str(value))
    return value


def revise_item_payload() -> JsonObject:
    return {
        "schema": "pinboard-item-revision/v1",
        "item_id": "work-a",
        "expected_revision": 1,
        "expected_digest": "a" * 64,
        "source_task": "owner-task",
        "reason": "Clarify the accepted outcome.",
        "definition": {
            "schema": "pinboard-work-item-definition/v1",
            "title": "Work A",
            "objective": "Make the outcome explicit.",
            "hypothesis": "Explicit outcomes reduce coordination mistakes.",
            "evidence": [],
            "scope": ["Record the outcome."],
            "non_scope": [],
            "acceptance_criteria": ["The outcome is queryable."],
            "dependencies": [],
            "effect": "The outcome is explicit.",
            "unlock": "Work can continue.",
        },
    }


def revise_item_payload_with_definition(*, omit_schema: bool = False, **changes: JsonValue) -> JsonObject:
    payload = revise_item_payload()
    definition = payload["definition"]
    assert isinstance(definition, dict)
    changed_definition: JsonObject = {**definition, **changes}
    if omit_schema:
        del changed_definition["schema"]
    payload["definition"] = changed_definition
    return payload


class TransitionInputTest(unittest.TestCase):
    def test_selected_action_decodes_directly_to_its_exact_command(self) -> None:
        submit = action(decision_models.SubmitReviewAction, AttemptId("attempt-1"))

        command = parse_transition_command(submit, '{"candidate":"candidate-1"}')

        self.assertEqual(
            decision_models.SubmitReviewCommand(
                submit,
                work_models.SubmitReviewInput(CandidateId("candidate-1")),
            ),
            command,
        )

    def test_complete_decodes_exact_direct_and_covered_leaves(self) -> None:
        complete = action(decision_models.CompleteAction, AttemptId("attempt-1"))
        direct = expect_transition_command(parse_transition_command(complete, '{"evidence":"accepted"}'))
        covered_payload = {
            "schema": "pinboard-covered-completion/v1",
            "candidate": "candidate-1",
            "evidence": "all checkpoints remain covered",
            "reviewer_task_id": "reviewer-task",
            "result_sha256": "a" * 64,
            "review_sha256": "b" * 64,
            "packages": [
                {
                    "history_id": 7,
                    "package_sha256": "c" * 64,
                    "disposition": "revalidated",
                    "evidence": "shared assumptions were rechecked",
                }
            ],
        }
        covered = expect_transition_command(parse_transition_command(complete, json.dumps(covered_payload)))

        self.assertIsInstance(direct, decision_models.DirectCompleteCommand)
        self.assertIsInstance(covered, decision_models.CoveredCompleteCommand)

        invalid_payloads = (
            covered_payload | {"unknown": True},
            covered_payload | {"schema": "unknown"},
            covered_payload | {"packages": list[JsonValue]()},
            covered_payload | {"packages": [covered_payload["packages"][0], covered_payload["packages"][0]]},
            {"candidate": "candidate-1", "evidence": "accepted"},
            {"evidence": "accepted", "schema": "pinboard-covered-completion/v1"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                rejected = parse_transition_command(complete, json.dumps(payload))
                self.assertIsInstance(rejected, TransitionInputFailure)

    def test_input_contract_describes_every_action_kind(self) -> None:
        self.assertEqual(
            tuple(kind.value for kind in decision_models.ActionKind),
            INPUT_CONTRACT_ACTION_KINDS,
        )

    def test_current_inputs_decode_exact_models(self) -> None:
        activation_action = action(decision_models.ActivateAction, ItemId("item-1"))
        activation = expect_transition_command(
            parse_transition_command(
                activation_action,
                json.dumps(
                    {
                        "attempt": "attempt-1",
                        "branch": "codex/attempt-1",
                        "base_revision": "abc123",
                        "owner": "worker",
                        "brief_artifact_ref_id": 7,
                    }
                ),
            )
        )
        self.assertEqual(
            decision_models.ActivateCommand(
                activation_action,
                work_models.ActivateInput(
                    AttemptId("attempt-1"), "codex/attempt-1", "abc123", "worker", ArtifactRefId(7)
                ),
            ),
            activation,
        )
        resume_action = action(decision_models.ResumeAction, ItemId("item-1"))
        self.assertEqual(
            decision_models.ResumeCommand(resume_action, work_models.ResumeInput()),
            expect_transition_command(parse_transition_command(resume_action, "{}")),
        )
        self.assertEqual(
            decision_models.ResumeCommand(resume_action, work_models.ResumeInput(ArtifactRefId(8))),
            expect_transition_command(parse_transition_command(resume_action, '{"brief_artifact_ref_id":8}')),
        )
        rebind_action = action(decision_models.RebindAttemptAction, AttemptId("attempt-1"))
        self.assertEqual(
            decision_models.RebindAttemptCommand(
                rebind_action,
                work_models.RebindAttemptInput(
                    AttemptId("attempt-1"), "codex/corrected", "correct-base", ArtifactRefId(9)
                ),
            ),
            expect_transition_command(
                parse_transition_command(
                    rebind_action,
                    '{"attempt":"attempt-1","branch":"codex/corrected","base_revision":"correct-base",'
                    '"brief_artifact_ref_id":9}',
                )
            ),
        )

        checkpoint_action = action(decision_models.AcceptCheckpointAction, AttemptId("attempt-1"))
        checkpoint = expect_transition_command(
            parse_transition_command(
                checkpoint_action,
                '{"checkpoint":"design-accepted","candidate":"sha256:candidate","evidence":"accepted"}',
            )
        )
        self.assertIsInstance(checkpoint, decision_models.AcceptCheckpointCommand)

        continuation_action = action(decision_models.AcceptReviewAndContinueAction, AttemptId("attempt-1"))
        continuation = expect_transition_command(
            parse_transition_command(
                continuation_action,
                '{"candidate":"sha256:candidate","evidence":"review accepted"}',
            )
        )
        self.assertEqual(
            decision_models.AcceptReviewAndContinueCommand(
                continuation_action,
                work_models.AcceptReviewAndContinueInput(CandidateId("sha256:candidate"), "review accepted"),
            ),
            continuation,
        )

    def test_invalid_closed_choices_report_native_paths(self) -> None:
        revise = action(decision_models.ReviseItemAction, ItemId("work-a"))
        cases: tuple[tuple[decision_models.Action, JsonObject], ...] = (
            (
                action(decision_models.ActivateAction, ItemId("item-1")),
                {
                    "attempt": "bad\nvalue",
                    "branch": "branch",
                    "base_revision": "base",
                    "owner": "task",
                    "brief_artifact_ref_id": 1,
                },
            ),
            (
                action(decision_models.AcceptCheckpointAction, AttemptId("attempt-1")),
                {"checkpoint": "Bad Checkpoint", "candidate": "candidate", "evidence": "accepted"},
            ),
            (
                action(decision_models.AcceptCheckpointAction, AttemptId("attempt-1")),
                {"checkpoint": "checkpoint-a\n", "candidate": "candidate", "evidence": "accepted"},
            ),
            (
                action(decision_models.SubmitReviewAction, AttemptId("attempt-1")),
                {"candidate": "candidate\n"},
            ),
            (
                action(decision_models.AcceptReviewAndContinueAction, AttemptId("attempt-1")),
                {"candidate": "candidate", "evidence": ""},
            ),
            (
                action(decision_models.AcceptReviewAndContinueAction, AttemptId("attempt-1")),
                {"candidate": "candidate", "evidence": "accepted", "unexpected": True},
            ),
            (
                action(decision_models.RebindAttemptAction, AttemptId("attempt-1")),
                {
                    "attempt": "attempt-1",
                    "branch": "codex/corrected",
                    "base_revision": "correct-base",
                    "brief_artifact_ref_id": 1,
                    "unexpected": True,
                },
            ),
            (
                action(decision_models.RebindAttemptAction, AttemptId("attempt-1")),
                {
                    "attempt": "attempt-1",
                    "branch": "bad\nbranch",
                    "base_revision": "correct-base",
                    "brief_artifact_ref_id": 1,
                },
            ),
            (action(decision_models.SubmitReviewAction, AttemptId("attempt-1")), {"candidate": 1}),
            (revise, revise_item_payload() | {"source_task": ""}),
            (revise, revise_item_payload() | {"reason": ""}),
            (revise, revise_item_payload() | {"expected_digest": "a" * 64 + "\n"}),
            (revise, revise_item_payload_with_definition(omit_schema=True)),
            (revise, revise_item_payload_with_definition(title="Work A\n")),
            (revise, revise_item_payload_with_definition(scope=[])),
            (
                revise,
                revise_item_payload_with_definition(evidence=["same", "same"]),
            ),
            (
                revise,
                revise_item_payload_with_definition(dependencies=["Bad Identity"]),
            ),
            (
                action(decision_models.BlockItemAction, ItemId("work-a")),
                {"reason": "blocked", "depends_on": ["work-b", "work-b"]},
            ),
            (
                action(decision_models.AcceptProposalAction, ProposalId("proposal-a")),
                {
                    "item": "work-a",
                    "state": "intake",
                    "next_action": "review",
                    "depends_on": ["work-b", "work-b"],
                },
            ),
            (
                action(decision_models.AcceptProposalAction, ProposalId("proposal-a")),
                {
                    "item": "work-a",
                    "state": "intake",
                    "next_action": "review",
                    "depends_on": ["work-a"],
                },
            ),
        )
        for selected_action, value in cases:
            with self.subTest(kind=selected_action.kind):
                rejected = parse_transition_command(selected_action, json.dumps(value))
                self.assertIsInstance(rejected, TransitionInputFailure)
                assert isinstance(rejected, TransitionInputFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        advisory = parse_transition_command(action(decision_models.ReportBlockerAction, AttemptId("attempt-1")), "{}")
        self.assertIsInstance(advisory, TransitionInputFailure)
        assert isinstance(advisory, TransitionInputFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_MUTATING, advisory.code)

    def test_every_current_kind_decodes_and_has_a_schema(self) -> None:
        cases: tuple[tuple[decision_models.Action, JsonObject], ...] = (
            (
                action(decision_models.AcceptCheckpointAction, AttemptId("attempt-1")),
                {
                    "checkpoint": "checkpoint-a",
                    "candidate": "candidate",
                    "evidence": "accepted",
                },
            ),
            (
                action(decision_models.AcceptReviewAndContinueAction, AttemptId("attempt-1")),
                {"candidate": "candidate", "evidence": "accepted"},
            ),
            (
                action(decision_models.AcceptProposalAction, ProposalId("proposal-1")),
                {
                    "item": "work-a",
                    "state": "intake",
                    "next_action": "review",
                    "timing": "must-now",
                    "depends_on": [],
                },
            ),
            (
                action(decision_models.ActivateAction, ItemId("work-a")),
                {
                    "attempt": "work-a-1",
                    "branch": "codex/work-a",
                    "base_revision": "base",
                    "owner": "task",
                    "brief_artifact_ref_id": 1,
                },
            ),
            (
                action(decision_models.BlockAttemptAction, AttemptId("attempt-1")),
                {"reason": "blocked", "depends_on": ["work-b"]},
            ),
            (action(decision_models.BlockItemAction, ItemId("work-a")), {"reason": "blocked", "depends_on": []}),
            (action(decision_models.CloseAction, ItemId("work-a")), {"outcome": "done", "reason": "complete"}),
            (action(decision_models.CompleteAction, AttemptId("attempt-1")), {"evidence": "complete"}),
            (
                action(decision_models.DeferAction, ItemId("work-a")),
                {"timing": "safe-to-defer", "reopen_condition": "when needed"},
            ),
            (action(decision_models.MarkReadyAction, ItemId("work-a")), {"reason": "ready"}),
            (action(decision_models.MergeProposalAction, ProposalId("proposal-1")), {"target": "work-a"}),
            (action(decision_models.PauseAction, AttemptId("attempt-1")), {"reason": "pause"}),
            (action(decision_models.RejectProposalAction, ProposalId("proposal-1")), {"reason": "reject"}),
            (action(decision_models.ReopenAction, ItemId("work-a")), {"evidence": "reopen"}),
            (
                action(decision_models.RebindAttemptAction, AttemptId("attempt-1")),
                {
                    "attempt": "attempt-1",
                    "branch": "codex/corrected",
                    "base_revision": "correct-base",
                    "brief_artifact_ref_id": 2,
                },
            ),
            (action(decision_models.ResumeAction, ItemId("work-a")), {}),
            (action(decision_models.ReturnForCorrectionAction, AttemptId("attempt-1")), {"reason": "correct"}),
            (action(decision_models.ReturnProposalAction, ProposalId("proposal-1")), {"reason": "more evidence"}),
            (
                action(decision_models.ReviseItemAction, ItemId("work-a")),
                revise_item_payload(),
            ),
            (action(decision_models.SubmitReviewAction, AttemptId("attempt-1")), {"candidate": "candidate"}),
        )
        for selected_action, payload in cases:
            with self.subTest(kind=selected_action.kind):
                expect_transition_command(parse_transition_command(selected_action, json.dumps(payload)))
                schema = expect_schema(encoded_transition_input_schema(selected_action.kind))
                self.assertIn(b'"type":"object"', schema)

    def test_activate_rejects_non_string_attempt(self) -> None:
        invalid_values: tuple[JsonValue, ...] = (None, 1, [], {})
        for invalid in invalid_values:
            value: dict[str, JsonValue] = {
                "attempt": invalid,
                "branch": "codex/reveal-core",
                "base_revision": "abc123",
                "owner": "worker",
                "brief_artifact_ref_id": 1,
            }
            with self.subTest(invalid=invalid):
                rejected = parse_transition_command(
                    action(decision_models.ActivateAction, ItemId("item-1")), json.dumps(value)
                )
                self.assertIsInstance(rejected, TransitionInputFailure)
                assert isinstance(rejected, TransitionInputFailure)
                assert rejected.details is not None
                self.assertEqual(RetryDisposition.CORRECT_INPUT, rejected.details.retry)


if __name__ == "__main__":
    unittest.main()
