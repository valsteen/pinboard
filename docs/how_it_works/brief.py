from pinboard.interfaces import work_brief_contract, work_brief_models, work_briefs

from .model import Box, Diagram, Guide, Note, Section

IDENTITY_FIELDS = frozenset(
    {
        "schema",
        "artifact_revision",
        "item_id",
        "attempt_id",
        "owner_task_id",
        "base_revision",
        "branch",
        "title",
    }
)

WORK_DEFINITION_FIELDS = frozenset(
    {
        "outcome",
        "scope",
        "non_goals",
        "compatibility",
        "supported_production_roots",
        "product_decision_and_provenance",
        "testing_strategy",
        "remaining_work",
        "bootstrap",
    }
)

CHECKPOINT_HEADER_FIELDS = frozenset(
    {
        "checkpoint_id",
        "title",
        "outcome",
        "outcome_description",
    }
)

CHECKPOINT_DETAIL_FIELDS = frozenset(
    {
        "acceptance_criteria",
        "architecture_impact",
        "reviewed_authorities",
        "contracts",
        "coverage",
        "lifecycle_partition",
        "verification",
        "deferrals",
    }
)

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "WorkBrief": work_brief_models.WorkBrief.__name__,
    "CrossBoundaryCheckpoint": work_brief_models.CrossBoundaryCheckpoint.__name__,
    "decode_work_brief": work_briefs.decode_work_brief.__name__,
    "canonical_work_brief_bytes": work_briefs.canonical_work_brief_bytes.__name__,
    "WorkBriefContract": work_brief_contract.WorkBriefContract.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if renamed:
        raise ValueError(f"brief visual references renamed source symbols: {', '.join(renamed)}")

    visual_brief_fields = IDENTITY_FIELDS | WORK_DEFINITION_FIELDS | {"accepted_scope", "checkpoint"}
    model_brief_fields = frozenset(work_brief_models.WorkBrief.__struct_fields__)
    visual_checkpoint_fields = CHECKPOINT_HEADER_FIELDS | CHECKPOINT_DETAIL_FIELDS
    model_checkpoint_fields = frozenset(work_brief_models.CrossBoundaryCheckpoint.__struct_fields__)
    if visual_brief_fields != model_brief_fields or visual_checkpoint_fields != model_checkpoint_fields:
        drifted = sorted(visual_brief_fields ^ model_brief_fields | visual_checkpoint_fields ^ model_checkpoint_fields)
        raise ValueError(f"brief visual and work-brief information architecture differ: {', '.join(drifted)}")

    if frozenset(work_brief_models.AcceptedScope.__struct_fields__) != {"revision", "digest"}:
        raise ValueError("brief visual and accepted-scope identity differ")


DIAGRAM = Diagram(
    slug="brief",
    title="The canonical work brief turns one accepted decision into structured implementation and review attention",
    description=(
        "A document-anatomy view of the canonical work brief. Artifact identity and accepted-scope identity anchor the "
        "whole-work definition. A cross-boundary checkpoint then names its boundary and outcome before expanding into "
        "acceptance criteria, architecture impact, reviewed authorities, contracts, coverage, lifecycle distinctions, "
        "verification, and deferrals. Static discovery provides complete unresolved starters plus exact templates for "
        "every structural choice without choosing their meaning. Humans and language models select and interpret those "
        "named relationships, while Pinboard code validates the completed envelope and stable artifact identity."
    ),
    width=1200,
    height=1040,
    sections=(
        Section("Canonical work brief", "one strict hierarchy travels with implementation, review, and resume", 28, 42),
        Section("Anchor the whole job", "identity, accepted scope, and the complete work definition", 28, 192),
        Section("Checkpoint", "one reviewable boundary expands into explicit reasoning obligations", 28, 390),
        Section(
            "The schema disciplines attention",
            "the structure is enforced; the meaning still requires judgment",
            28,
            868,
        ),
    ),
    guides=(
        Guide((255, 38), (1172, 38)),
        Guide((214, 188), (1172, 188)),
        Guide((132, 386), (1172, 386)),
        Guide((300, 864), (1172, 864)),
    ),
    connectors=(),
    boxes=(
        Box(
            "brief",
            "pinboard-work-brief/v2",
            "One accepted artifact",
            ("starter → structural choices → completed brief",),
            ("WorkBriefContract · WorkBrief",),
            400,
            70,
            400,
            105,
        ),
        Box(
            "identity",
            "Identity + versioning",
            "Which exact artifact is this?",
            ("item · attempt · owner", "revision · base · branch"),
            ("schema · title",),
            35,
            220,
            300,
            145,
        ),
        Box(
            "accepted-scope",
            "Accepted scope reference",
            "Which decision was accepted?",
            ("revision + digest",),
            ("accepted_scope",),
            355,
            220,
            280,
            145,
        ),
        Box(
            "work-definition",
            "Overall work definition",
            "What is the whole job?",
            (
                "outcome · scope · non-goals",
                "compatibility · roots · provenance",
                "testing · bootstrap · remaining work",
            ),
            ("definition-bound semantics",),
            655,
            220,
            510,
            145,
        ),
        Box(
            "checkpoint",
            "Checkpoint identity + boundary",
            "What can be built and reviewed together?",
            ("checkpoint id · title · boundary", "outcome + outcome description"),
            ("CrossBoundaryCheckpoint",),
            35,
            418,
            1130,
            125,
        ),
        Box(
            "criteria",
            "Acceptance criteria",
            "What earns acceptance?",
            ("numbered requirements",),
            ("number · requirement",),
            35,
            553,
            275,
            120,
        ),
        Box(
            "architecture",
            "Architecture impact",
            "Is architecture affected?",
            ("reason + exact selector",),
            ("kind · reason · selector",),
            320,
            553,
            275,
            120,
        ),
        Box(
            "authorities",
            "Reviewed authorities",
            "Which sources were examined?",
            ("identity · selector · digest",),
            ("families[]",),
            605,
            553,
            275,
            120,
        ),
        Box(
            "contracts",
            "Contracts",
            "What must remain true?",
            ("authority → consumer", "invariant · failure · revalidation"),
            ("verification · authorization basis",),
            890,
            553,
            275,
            120,
        ),
        Box(
            "coverage",
            "Coverage",
            "Who owns each distinction?",
            ("consumer + counterexample",),
            ("criterion · contract · deferral",),
            35,
            703,
            275,
            120,
        ),
        Box(
            "lifecycle",
            "Lifecycle partition",
            "Which sibling is illegal?",
            ("operation · source · authority", "effects + evidence"),
            ("illegal_sibling",),
            320,
            703,
            275,
            120,
        ),
        Box(
            "verification",
            "Verification",
            "How is each obligation proved?",
            ("obligation + authorization",),
            ("accepted scope or authority",),
            605,
            703,
            275,
            120,
        ),
        Box(
            "deferrals",
            "Deferrals",
            "What remains outside?",
            ("why it waits",),
            ("deferral id · reopen when",),
            890,
            703,
            275,
            120,
        ),
        Box(
            "interpretation",
            "Human + LLM responsibility",
            "Interpret what the work means",
            ("labels + hierarchy guide attention",),
            ("semantic judgment",),
            35,
            900,
            550,
            105,
        ),
        Box(
            "validation",
            "Pinboard tool + code responsibility",
            "Guide and verify the envelope",
            ("starter · variants · schema · decode",),
            ("canonicalize · bind bytes",),
            615,
            900,
            550,
            105,
            "muted",
        ),
    ),
    notes=(
        Note(
            "THE SAME NAMED RELATIONSHIPS STEER IMPLEMENTATION · REVIEW · CORRECTION · RESUME",
            600,
            1022,
            12,
            "middle",
            True,
        ),
    ),
)
