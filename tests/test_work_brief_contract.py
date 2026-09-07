import unittest

import msgspec

from pinboard.interfaces import work_brief_models
from pinboard.interfaces.errors import WorkBriefError
from pinboard.interfaces.work_brief_contract import WorkBriefStructuralChoice, describe_work_brief_contract
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
            return completed
        if len(template) != len(completed):
            return completed
        return [complete_starter(value, completed[index]) for index, value in enumerate(template)]
    return template


def replace_at_selection_path(value: JsonValue, path: str, replacement: JsonValue) -> None:
    if not path.startswith("$."):
        raise AssertionError(f"Unsupported selection path: {path}")

    def replace(current: JsonValue, remaining: list[str]) -> None:
        current_object = json_object(current)
        segment = remaining[0]
        repeated = segment.endswith("[*]")
        key = segment.removesuffix("[*]")
        if len(remaining) == 1:
            if repeated:
                selected = current_object[key]
                if not isinstance(selected, list):
                    raise AssertionError("Expected a JSON array at repeated selection path.")
                current_object[key] = [msgspec.json.decode(msgspec.json.encode(replacement)) for _ in selected]
            else:
                current_object[key] = msgspec.json.decode(msgspec.json.encode(replacement))
            return
        selected = current_object[key]
        if repeated:
            if not isinstance(selected, list):
                raise AssertionError("Expected a JSON array at repeated selection path.")
            for item in selected:
                replace(item, remaining[1:])
        else:
            replace(selected, remaining[1:])

    replace(value, path[2:].split("."))


def select_structural_variant(
    starter: JsonValue,
    choice: WorkBriefStructuralChoice,
    selection_path: str,
    selector: str,
) -> None:
    if selection_path not in choice.selection_paths:
        raise AssertionError(f"Selection path is not advertised: {selection_path}")
    variant = next(value for value in choice.variants if value.selector == selector)
    replace_at_selection_path(starter, selection_path, msgspec.json.decode(bytes(variant.template)))


def json_strings(value: JsonValue) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(json_strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(json_strings(item) for item in value))
    return set()


class WorkBriefContractTest(unittest.TestCase):
    def test_structural_choices_cover_every_supported_union_variant(self) -> None:
        contract = describe_work_brief_contract()
        choices = {choice.choice_id: choice for choice in contract.cross_boundary_structural_choices}

        self.assertEqual(
            {
                "architecture-impact": {"none", "read-only", "update-required"},
                "authorization-basis": {
                    "accepted-scope",
                    "authority",
                    "repository-policy",
                    "existing-consumer",
                },
                "coverage-owner": {"contract", "acceptance", "deferred", "not-applicable"},
                "lifecycle-partition": {"not-applicable", "required"},
            },
            {choice_id: {variant.selector for variant in choice.variants} for choice_id, choice in choices.items()},
        )
        self.assertEqual(
            {"architecture-impact"},
            {choice.choice_id for choice in contract.local_structural_choices},
        )
        for choice in choices.values():
            self.assertTrue(choice.selection_paths)
            for variant in choice.variants:
                template = msgspec.json.decode(bytes(variant.template))
                self.assertIsInstance(template, dict)
                self.assertTrue(template)

    def test_contract_exposes_generated_schema_and_complete_unresolved_starter_shapes(self) -> None:
        contract = describe_work_brief_contract()

        self.assertEqual("pinboard-work-brief-contract/v1", contract.schema)
        self.assertEqual(
            "Encode the completed typed brief as JSON with lexicographically sorted object keys, no insignificant "
            "whitespace, and exactly one trailing newline.",
            contract.canonicalization_rule,
        )
        self.assertEqual(
            "Publication validates structure, cross-references, and canonical bytes. It does not resolve branch or "
            "base_revision against Git or prove semantic scope, authority, consumer, or verification claims; the "
            "caller and independent review own those facts.",
            contract.fact_validation_boundary,
        )
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

    def test_cross_boundary_choices_mechanically_build_non_default_structures(self) -> None:
        contract = describe_work_brief_contract()
        choices = {choice.choice_id: choice for choice in contract.cross_boundary_structural_choices}
        base = example_work_brief()
        checkpoint = base.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        required_lifecycle = work_brief_models.RequiredLifecyclePartition(
            (
                work_brief_models.LifecycleRecord(
                    "submit-review",
                    "active attempt",
                    "current attempt lease",
                    "exact candidate",
                    "review state and protected candidate",
                    "stale attempt lease rejects unchanged",
                ),
            )
        )
        owners: tuple[tuple[str, work_brief_models.CoverageOwner], ...] = (
            ("contract", work_brief_models.ContractCoverageOwner(checkpoint.contracts[0].invariant)),
            ("acceptance", work_brief_models.AcceptanceCoverageOwner(1)),
            ("deferred", work_brief_models.DeferredCoverageOwner("later-work")),
            ("not-applicable", work_brief_models.NotApplicableCoverageOwner("No separate obligation.")),
        )

        for owner_selector, owner in owners:
            with self.subTest(owner=owner_selector):
                completed = msgspec.structs.replace(
                    base,
                    checkpoint=msgspec.structs.replace(
                        checkpoint,
                        coverage=(msgspec.structs.replace(checkpoint.coverage[0], owner=owner),),
                        lifecycle_partition=required_lifecycle,
                    ),
                )
                starter = msgspec.json.decode(bytes(contract.cross_boundary_starter))
                select_structural_variant(
                    starter,
                    choices["architecture-impact"],
                    "$.checkpoint.architecture_impact",
                    "update-required",
                )
                select_structural_variant(
                    starter,
                    choices["authorization-basis"],
                    "$.checkpoint.contracts[*].authorization_basis",
                    "accepted-scope",
                )
                select_structural_variant(
                    starter,
                    choices["authorization-basis"],
                    "$.checkpoint.verification[*].authorization_basis",
                    "repository-policy",
                )
                select_structural_variant(
                    starter,
                    choices["coverage-owner"],
                    "$.checkpoint.coverage[*].owner",
                    owner_selector,
                )
                select_structural_variant(
                    starter,
                    choices["lifecycle-partition"],
                    "$.checkpoint.lifecycle_partition",
                    "required",
                )
                completed_payload = msgspec.json.decode(msgspec.json.encode(completed))
                decoded = decode_work_brief(msgspec.json.encode(complete_starter(starter, completed_payload)))
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
