from pinboard.adapters import review_operations
from pinboard.application import work_brief_models
from pinboard.domain import decision_models
from pinboard.mcp import contracts

from .model import Box, Connector, Diagram, Guide, Note, Section

REVIEW_LOOP_ACTIONS = frozenset(
    {
        decision_models.ActionKind.SUBMIT_REVIEW,
        decision_models.ActionKind.RETURN_FOR_CORRECTION,
        decision_models.ActionKind.ACCEPT_CHECKPOINT,
        decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
        decision_models.ActionKind.COMPLETE,
    }
)

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "WorkBrief": work_brief_models.WorkBrief.__name__,
    "WorkBriefReview": work_brief_models.WorkBriefReview.__name__,
    "ReviewJobReady": contracts.ReviewJobReady.__name__,
    "PriorCheckpointPackage": review_operations.PriorCheckpointPackage.__name__,
    "CorrectionReviewRound": review_operations.CorrectionReviewRound.__name__,
    "CompletionReviewPackage": work_brief_models.CompletionReviewPackage.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if renamed:
        raise ValueError(f"review-loop visual references renamed source symbols: {', '.join(renamed)}")
    if not REVIEW_LOOP_ACTIONS.issubset(decision_models.ActionKind):
        raise ValueError("review-loop visual references a missing review action")


DIAGRAM = Diagram(
    slug="review-loop",
    title="Review the intended work, then review its implementation",
    description=(
        "A published brief receives independent review and bounded corrections before cross-boundary implementation. "
        "Local checkpoints skip the separate brief review. After implementation, the exact candidate and evidence "
        "are bound with caller-selected checkpoint or correction evidence into context for a separate, "
        "candidate-read-only implementation reviewer. Publication may change prompt artifacts, accepted references, "
        "and the ledger, but leaves lifecycle, candidate, and authority unchanged. The reviewer reuses unchanged "
        "evidence, revalidates changed or unclassified relationships, and widens on concrete escalation conditions. "
        "Implementation defects return to the same attempt for correction; brief gaps and product decisions return "
        "to their owner. A favorable review informs the outcome owner's acceptance decision. Covered completion "
        "preserves accepted checkpoint dispositions."
    ),
    width=1200,
    height=750,
    sections=(
        Section("Before implementation", "one accepted direction becomes an inspectable brief", 28, 42),
        Section("After implementation", "prepare the evidence, then commission the independent review", 28, 420),
    ),
    guides=(
        Guide((258, 38), (1172, 38)),
        Guide((252, 416), (1172, 416)),
    ),
    connectors=(
        Connector(((360, 220), (440, 220)), "brief", "brief-review"),
        Connector(((760, 220), (840, 220)), "brief-review", "implementer", "ready", (800, 201)),
        Connector(
            ((210, 150), (210, 110), (1000, 110), (1000, 150)),
            "brief",
            "implementer",
            "local checkpoint · no separate brief review",
            (600, 96),
        ),
        Connector(
            ((550, 285), (550, 335), (210, 335), (210, 285)),
            "brief-review",
            "brief",
            "correct brief · republish · recheck",
            (380, 322),
        ),
        Connector(((990, 285), (990, 480)), "implementer", "context", "submit candidate", (1055, 365)),
        Connector(((840, 550), (760, 550)), "context", "reviewer"),
        Connector(((440, 550), (360, 550)), "reviewer", "outcome", "ready", (400, 533)),
        Connector(
            ((600, 480), (600, 380), (800, 380), (800, 255), (840, 255)),
            "reviewer",
            "implementer",
            "correct implementation",
            (700, 366),
        ),
    ),
    boxes=(
        Box(
            "brief",
            "Accepted direction",
            "Publish structured brief",
            ("outcome · scope · verification", "authorities for cross-boundary work"),
            ("pinboard-work-brief/v2",),
            60,
            150,
            300,
            135,
        ),
        Box(
            "brief-review",
            "Cross-boundary work",
            "Independent brief review",
            ("challenge scope and source roles", "check contracts and evidence"),
            ("ready evidence binds checkpoint",),
            440,
            150,
            320,
            135,
        ),
        Box(
            "implementer",
            "Implementation",
            "Build and verify",
            ("follow the accepted brief", "record candidate and result"),
            ("same attempt through correction",),
            840,
            150,
            300,
            135,
        ),
        Box(
            "context",
            "Review preparation",
            "Bind candidate + evidence",
            ("brief · result · selected history", "publish exact reviewer prompt"),
            ("pinboard-mcp-review-job-result/v1",),
            840,
            480,
            300,
            140,
        ),
        Box(
            "reviewer",
            "Implementation review",
            "Independent reviewer",
            ("inspect the candidate against brief", "reuse evidence or revalidate it"),
            ("candidate-read-only · widen on trigger",),
            440,
            480,
            320,
            140,
        ),
        Box(
            "outcome",
            "Outcome owner",
            "Apply the reviewed result",
            ("accept checkpoint → pause", "accept + continue · complete"),
            ("human owns repository disposition",),
            60,
            480,
            300,
            140,
        ),
    ),
    notes=(
        Note("LOCAL WORK STILL RECEIVES INDEPENDENT IMPLEMENTATION REVIEW", 600, 675, 12, "middle", True),
        Note(
            "Publication may change prompt artifacts, references, and ledger; it neither reviews nor accepts the candidate.",
            600,
            703,
            12,
            "middle",
        ),
        Note("The accepted brief anchors both reviews. Model judgment can still be wrong.", 600, 725, 12, "middle"),
    ),
)
