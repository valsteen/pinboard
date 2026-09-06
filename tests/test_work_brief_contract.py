import unittest

import msgspec

from pinboard.interfaces import work_brief_models
from pinboard.interfaces.errors import WorkBriefError
from pinboard.interfaces.work_brief_contract import describe_work_brief_contract
from pinboard.interfaces.work_briefs import decode_work_brief
from tests.work_brief_support import example_work_brief, work_c_brief

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("Expected a JSON object.")
    return value


def complete_starter(template: JsonValue, completed: JsonValue) -> JsonValue:
    if template is None:
        return completed
    if isinstance(template, dict):
        completed_object = json_object(completed)
        return {key: complete_starter(value, completed_object[key]) for key, value in template.items()}
    if isinstance(template, list):
        if not isinstance(completed, list):
            raise AssertionError("Expected a JSON array.")
        if not template:
            return template
        if len(template) != len(completed):
            return completed
        return [complete_starter(value, completed[index]) for index, value in enumerate(template)]
    return template


def json_strings(value: JsonValue) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(json_strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(json_strings(item) for item in value))
    return set()


class WorkBriefContractTest(unittest.TestCase):
    def test_contract_exposes_generated_schema_and_complete_unresolved_starter_shapes(self) -> None:
        contract = describe_work_brief_contract()

        self.assertEqual("pinboard-work-brief-contract/v1", contract.schema)
        self.assertEqual(
            msgspec.json.schema(work_brief_models.WorkBrief),
            msgspec.json.decode(bytes(contract.payload_schema)),
        )

        root_keys = {field.name for field in msgspec.structs.fields(work_brief_models.WorkBrief)}
        for starter, checkpoint_type, structural_literals in (
            (
                contract.local_starter,
                work_brief_models.LocalCheckpoint,
                {"pinboard-work-brief/v2", "local", "none", "accepted-scope"},
            ),
            (
                contract.cross_boundary_starter,
                work_brief_models.CrossBoundaryCheckpoint,
                {
                    "pinboard-work-brief/v2",
                    "cross-boundary",
                    "none",
                    "independently-buildable",
                    "accepted-scope",
                    "contract",
                    "not-applicable",
                },
            ),
        ):
            payload = json_object(msgspec.json.decode(bytes(starter)))
            self.assertEqual(root_keys, payload.keys())
            self.assertEqual(structural_literals, json_strings(payload))
            self.assertEqual(bytes(starter), msgspec.json.encode(payload, order="sorted"))
            checkpoint = json_object(payload["checkpoint"])
            checkpoint_keys = {field.name for field in msgspec.structs.fields(checkpoint_type)} | {"boundary"}
            self.assertEqual(checkpoint_keys, checkpoint.keys())
            with self.assertRaises(WorkBriefError):
                decode_work_brief(bytes(starter))

        local_payload = json_object(msgspec.json.decode(bytes(contract.local_starter)))
        local_checkpoint = json_object(local_payload["checkpoint"])
        verification = local_checkpoint["verification"]
        self.assertIsInstance(verification, list)
        assert isinstance(verification, list)
        authorization = json_object(json_object(verification[0])["authorization_basis"])
        self.assertEqual("accepted-scope", authorization["kind"])
        cross_payload = json_object(msgspec.json.decode(bytes(contract.cross_boundary_starter)))
        cross_checkpoint = json_object(cross_payload["checkpoint"])
        self.assertEqual("not-applicable", json_object(cross_checkpoint["lifecycle_partition"])["kind"])
        coverage = cross_checkpoint["coverage"]
        self.assertIsInstance(coverage, list)
        assert isinstance(coverage, list)
        self.assertEqual("contract", json_object(json_object(coverage[0])["owner"])["disposition"])

    def test_mechanically_completed_starters_pass_strict_decode(self) -> None:
        contract = describe_work_brief_contract()
        local = work_c_brief()
        local_checkpoint = local.checkpoint
        assert isinstance(local_checkpoint, work_brief_models.LocalCheckpoint)
        local = msgspec.structs.replace(
            local,
            bootstrap=(),
            compatibility=(),
            non_goals=(),
            checkpoint=msgspec.structs.replace(
                local_checkpoint,
                architecture_impact=work_brief_models.NoArchitectureImpact("No architecture change."),
            ),
        )
        cross_boundary = example_work_brief()
        cross_checkpoint = cross_boundary.checkpoint
        assert isinstance(cross_checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        accepted_scope = work_brief_models.AcceptedScopeAuthorization(
            cross_boundary.item_id,
            cross_boundary.accepted_scope.revision,
        )
        cross_boundary = msgspec.structs.replace(
            cross_boundary,
            bootstrap=(),
            compatibility=(),
            non_goals=(),
            checkpoint=msgspec.structs.replace(
                cross_checkpoint,
                architecture_impact=work_brief_models.NoArchitectureImpact("No architecture change."),
                verification=(
                    msgspec.structs.replace(
                        cross_checkpoint.verification[0],
                        authorization_basis=accepted_scope,
                    ),
                ),
                deferrals=(),
            ),
        )

        for starter, completed, checkpoint_type in (
            (contract.local_starter, local, work_brief_models.LocalCheckpoint),
            (contract.cross_boundary_starter, cross_boundary, work_brief_models.CrossBoundaryCheckpoint),
        ):
            template = msgspec.json.decode(bytes(starter))
            completed_payload = msgspec.json.decode(msgspec.json.encode(completed))
            decoded = decode_work_brief(msgspec.json.encode(complete_starter(template, completed_payload)))
            self.assertIsInstance(decoded.checkpoint, checkpoint_type)
            self.assertEqual(completed, decoded)

    def test_contract_states_every_relational_rule_needed_to_complete_a_starter(self) -> None:
        contract = describe_work_brief_contract()
        constraint_ids = {constraint.constraint_id for constraint in contract.relational_constraints}

        self.assertEqual(
            {
                "accepted-scope-identity",
                "local-verification-authorization",
                "architecture-selector",
                "unique-criteria-and-deferrals",
                "unique-authority-families",
                "unique-contracts",
                "authority-authorization",
                "complete-coverage",
                "coverage-owner",
                "prohibition-disposition",
                "unique-lifecycle-operations",
            },
            constraint_ids,
        )


if __name__ == "__main__":
    unittest.main()
