from pinboard.domain import decision_models
from pinboard.interfaces import work_brief_models, work_inspection_models

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
    "ReviewJobView": work_inspection_models.ReviewJobView.__name__,
    "PriorCheckpointPackage": work_inspection_models.PriorCheckpointPackage.__name__,
    "CorrectionReviewRound": work_inspection_models.CorrectionReviewRound.__name__,
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
    title="One accepted target anchors an ordinary implementation and review loop",
    description=(
        "On the left, an ordinary coding harness repeatedly interprets a prose request, repository code, and review "
        "feedback. On the right, Pinboard preserves accepted semantics and stable authorities, derives concrete impact "
        "from the candidate, and binds that candidate plus caller-selected historical evidence into a read-only review "
        "job. The independent reviewer reuses unchanged evidence, revalidates changed or unclassified relationships, "
        "and widens on concrete escalation conditions. Covered completion preserves accepted checkpoint dispositions."
    ),
    width=1400,
    height=800,
    sections=(
        Section("Coding agents already iterate", "available prose + code are interpreted each pass", 28, 44),
        Section("Pinboard anchors the same loop", "accepted target + exact candidate + evidence", 730, 44),
    ),
    guides=(
        Guide((220, 40), (670, 40)),
        Guide((980, 40), (1372, 40)),
        Guide((700, 28), (700, 730)),
    ),
    connectors=(
        Connector(((230, 160), (260, 160)), "prompt", "implement", "derive", (245, 146)),
        Connector(((490, 160), (510, 160)), "implement", "review", "inspect", (500, 146)),
        Connector(((600, 210), (600, 395), (500, 395)), "review", "feedback", "feedback", (558, 312)),
        Connector(((390, 340), (390, 260), (375, 260), (375, 210)), "feedback", "implement", "revise", (426, 248)),
        Connector(((875, 210), (875, 290)), "brief", "implementer", "same target", (928, 256)),
        Connector(
            ((1120, 150), (1380, 150), (1380, 570), (1350, 570)),
            "brief",
            "reviewer",
            "same target",
            (1245, 136),
        ),
        Connector(((1000, 350), (1050, 350)), "implementer", "candidate", "submit", (1025, 336)),
        Connector(((1200, 410), (1200, 510)), "candidate", "reviewer", "inspect", (1240, 466)),
        Connector(
            ((1150, 630), (1150, 700), (875, 700), (875, 410)),
            "reviewer",
            "implementer",
            "correct against brief",
            (1012, 688),
        ),
    ),
    boxes=(
        Box(
            "prompt",
            "Starting context",
            "Prose request",
            ("human intent in words",),
            ("coding-agent input",),
            50,
            110,
            180,
            100,
            "muted",
        ),
        Box(
            "implement",
            "Implementation pass",
            "Derive code",
            ("interpret prose + repository",),
            ("LLM judgment",),
            260,
            110,
            230,
            100,
        ),
        Box(
            "review",
            "Review pass",
            "Inspect result",
            ("interpret request + diff",),
            ("LLM judgment",),
            510,
            110,
            180,
            100,
        ),
        Box(
            "feedback",
            "Review feedback",
            "Revise the result",
            ("findings become new prose", "then implement another pass"),
            ("ordinary harness loop",),
            280,
            340,
            220,
            120,
        ),
        Box(
            "brief",
            "Accepted target",
            "Structured work brief",
            ("intent · scope · non-goals", "stable owners · uncertainty"),
            ("pinboard-work-brief/v2",),
            820,
            90,
            300,
            120,
        ),
        Box(
            "implementer",
            "Implementer",
            "Derive changed surface",
            ("reread the accepted target", "read outward when exposed"),
            ("attempt identity",),
            750,
            290,
            250,
            120,
        ),
        Box(
            "candidate",
            "Read-only review job",
            "Current + prior evidence",
            ("exact revision + result digest", "selected package + correction"),
            ("pinboard-review-job/v2",),
            1050,
            290,
            300,
            120,
        ),
        Box(
            "reviewer",
            "Independent reviewer",
            "Classify, reuse, or widen",
            ("changed + unclassified surfaces", "reuse unchanged · widen on trigger"),
            ("completion package when covered",),
            1050,
            510,
            300,
            120,
        ),
    ),
    notes=(
        Note(
            "WITHOUT AN EXTERNALIZED TARGET, USEFUL FEEDBACK CAN QUIETLY BECOME NEW SCOPE",
            350,
            535,
            12,
            "middle",
            True,
        ),
        Note(
            "CHANGED OR UNCERTAIN RELATIONSHIPS DRIVE THE ASSURANCE SURFACE",
            1050,
            745,
            12,
            "middle",
            True,
        ),
        Note(
            "THEY DO NOT MAKE MODEL JUDGMENT INFALLIBLE OR GUARANTEE CONVERGENCE",
            1050,
            763,
            12,
            "middle",
            True,
        ),
    ),
)
