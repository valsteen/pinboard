import unittest

import msgspec

from pinboard.application import work_brief_models
from pinboard.application.work_brief_contract import WorkBriefStructuralChoice, describe_work_brief_contract
from pinboard.application.work_briefs import decode_work_brief
from tests.work_brief_support import example_work_brief, work_c_brief

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def expect_work_brief_success[T](result: work_brief_models.WorkBriefResult[T]) -> T:
    if isinstance(result, work_brief_models.WorkBriefFailure):
        raise AssertionError(str(result))
    return result


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


def fill_variant_template(value: JsonValue, key: str) -> JsonValue:
    """Independent synthetic field inputs complete construction shapes without copying their prose."""
    if value is None:
        return 1 if key in {"number", "criterion", "scope_revision"} else "fixture"
    if isinstance(value, dict):
        return {name: fill_variant_template(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [fill_variant_template(item, key) for item in value]
    return value


class WorkBriefContractTest(unittest.TestCase):
    def test_structural_choices_cover_every_supported_union_variant(self) -> None:
        contract = describe_work_brief_contract()
        choices = {choice.choice_id: choice for choice in contract.cross_boundary_structural_choices}

        shape_types = {
            "architecture-impact": work_brief_models.ArchitectureImpact,
            "authorization-basis": work_brief_models.AuthorizationBasis,
            "coverage-owner": work_brief_models.CoverageOwner,
            "lifecycle-partition": work_brief_models.LifecyclePartition,
            "checkout-selection": work_brief_models.work_models.CheckoutSelection,
            "obligation-target": work_brief_models.ObligationTarget,
            "checkpoint-disposition": work_brief_models.CheckpointDisposition,
        }
        for choice_id, shape in shape_types.items():
            definitions = msgspec.json.schema(shape).get("$defs", {})
            expected = {
                value
                for definition in definitions.values()
                for prop in definition.get("properties", {}).values()
                for value in prop.get("enum", [])
                if isinstance(value, str)
            }
            if choice_id == "checkout-selection":
                expected = {member.value for member in work_brief_models.work_models.CheckoutSelection}
            self.assertEqual(expected, {variant.selector for variant in choices[choice_id].variants})
            for variant in choices[choice_id].variants:
                payload = fill_variant_template(msgspec.json.decode(bytes(variant.template)), "")
                decoded = msgspec.json.decode(msgspec.json.encode(payload), type=shape)
                self.assertEqual(msgspec.json.encode(payload, order="sorted"), msgspec.json.encode(decoded, order="sorted"))
        self.assertTrue(contract.local_structural_choices)
        for choice in choices.values():
            self.assertTrue(choice.selection_paths)
            for variant in choice.variants:
                template = msgspec.json.decode(bytes(variant.template))
                if choice.choice_id == "checkout-selection":
                    self.assertIsInstance(template, str)
                else:
                    self.assertIsInstance(template, dict)
                    self.assertTrue(template)

    def test_contract_exposes_generated_schema_and_complete_unresolved_starter_shapes(self) -> None:
        contract = describe_work_brief_contract()

        self.assertEqual("pinboard-work-brief-contract/v1", contract.schema)
        self.assertEqual(
            msgspec.json.schema(work_brief_models.WorkBrief),
            msgspec.json.decode(bytes(contract.payload_schema)),
        )

        root_keys = {field.name for field in msgspec.structs.fields(work_brief_models.WorkBrief)}
        for starter, checkpoint_type in (
            (contract.local_starter, work_brief_models.LocalCheckpoint),
            (contract.cross_boundary_starter, work_brief_models.CrossBoundaryCheckpoint),
        ):
            payload = json_object(msgspec.json.decode(bytes(starter)))
            self.assertEqual(root_keys, payload.keys())
            self.assertEqual(bytes(starter), msgspec.json.encode(payload, order="sorted"))
            checkpoint = json_object(payload["checkpoint"])
            checkpoint_keys = {field.name for field in msgspec.structs.fields(checkpoint_type)} | {"boundary"}
            self.assertEqual(checkpoint_keys, checkpoint.keys())
            self.assertIsInstance(decode_work_brief(bytes(starter)), work_brief_models.WorkBriefFailure)

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
            decoded = expect_work_brief_success(
                decode_work_brief(msgspec.json.encode(complete_starter(template, completed_payload)))
            )
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
                decoded = expect_work_brief_success(
                    decode_work_brief(msgspec.json.encode(complete_starter(starter, completed_payload)))
                )
                self.assertEqual(completed, decoded)

    def test_relational_rules_have_unique_construction_guidance(self) -> None:
        constraints = describe_work_brief_contract().relational_constraints
        self.assertTrue(constraints)
        self.assertEqual(len(constraints), len({value.constraint_id for value in constraints}))


if __name__ == "__main__":
    unittest.main()
