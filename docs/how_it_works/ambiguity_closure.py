from pinboard.application import work_brief_models
from pinboard.domain import decision_models

from .model import Box, Connector, Diagram, Guide, Note, Section

WORKFLOW_ACTIONS = frozenset(
    {
        decision_models.ActionKind.SUBMIT_REVIEW,
        decision_models.ActionKind.RETURN_FOR_CORRECTION,
        decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
        decision_models.ActionKind.COMPLETE,
    }
)


def validate() -> None:
    if work_brief_models.WorkBrief.__name__ != "WorkBrief":
        raise ValueError("ambiguity-closure visual references a renamed work brief")
    if not WORKFLOW_ACTIONS.issubset(decision_models.ActionKind):
        raise ValueError("ambiguity-closure visual references a missing workflow action")


DIAGRAM = Diagram(
    slug="ambiguity-closure",
    title="Ambiguity closes around one accepted brief",
    description=(
        "Structured intake and discussion become a reviewed, accepted brief. That exact brief anchors implementation "
        "and independent candidate review. Implementation defects return to the same attempt; discoveries outside "
        "delegated product or architecture authority return to a human and revise the agreement before work resumes."
    ),
    width=1200,
    height=820,
    sections=(
        Section("Resolve enough to begin", "intake → discussion → inspectable agreement", 28, 42),
        Section("Deliver exact work", "implement and review against that agreement", 840, 42),
    ),
    guides=(
        Guide((235, 38), (810, 38)),
        Guide((1030, 38), (1172, 38)),
    ),
    connectors=(
        Connector(((250, 300), (300, 300)), "discussion", "structured-brief"),
        Connector(((520, 300), (570, 300)), "structured-brief", "brief-review"),
        Connector(
            ((680, 230), (680, 205), (805, 205), (805, 135), (850, 135)),
            "brief-review",
            "accepted-brief",
            "accepted",
            (742, 194),
        ),
        Connector(
            ((680, 370), (680, 405), (410, 405), (410, 370)),
            "brief-review",
            "structured-brief",
            "correct brief",
            (545, 395),
        ),
        Connector(((790, 330), (820, 330)), "brief-review", None, arrow=False),
        Connector(((930, 190), (930, 230)), "accepted-brief", "implementer"),
        Connector(
            ((1160, 135), (1190, 135), (1190, 490), (1160, 490)),
            "accepted-brief",
            "candidate-reviewer",
            "same accepted brief",
            (1100, 212),
        ),
        Connector(
            ((940, 350), (940, 430)),
            "implementer",
            "candidate-reviewer",
            "candidate + evidence",
            (885, 396),
        ),
        Connector(
            ((1070, 430), (1070, 350)),
            "candidate-reviewer",
            "implementer",
            "implementation defect",
            (1110, 396),
        ),
        Connector(((850, 290), (820, 290), (820, 580)), "implementer", None, arrow=False),
        Connector(((850, 490), (820, 490)), "candidate-reviewer", None, arrow=False),
        Connector(
            ((820, 580), (650, 580), (650, 620)),
            None,
            "human-decision",
            "explain discovery",
            (735, 570),
        ),
        Connector(
            ((500, 680), (270, 680), (270, 330), (300, 330)),
            "human-decision",
            "structured-brief",
            "revise agreement",
            (385, 670),
        ),
        Connector(
            ((1000, 550), (1000, 650)),
            "candidate-reviewer",
            "accepted-candidate",
            "ready",
            (1035, 610),
        ),
    ),
    boxes=(
        Box(
            "discussion",
            "Intake + discussion",
            "Bound the problem",
            ("outcome · constraints", "evidence · material unknowns"),
            ("bounded enough to begin",),
            30,
            230,
            220,
            140,
        ),
        Box(
            "structured-brief",
            "Structured brief",
            "Make choices explicit",
            ("scope · exclusions", "authority · evidence", "verification · deferrals"),
            ("one inspectable agreement",),
            300,
            230,
            220,
            140,
        ),
        Box(
            "brief-review",
            "Brief review",
            "Challenge the brief",
            ("check against the project", "return bounded findings"),
            ("local work skips this step",),
            570,
            230,
            220,
            140,
        ),
        Box(
            "accepted-brief",
            "Shared anchor",
            "One accepted brief",
            ("the exact current agreement",),
            ("WorkBrief · immutable bytes",),
            850,
            80,
            310,
            110,
        ),
        Box(
            "implementer",
            "Implementation",
            "Build from the brief",
            ("implement · verify · record",),
            ("exact candidate",),
            850,
            230,
            310,
            120,
        ),
        Box(
            "candidate-reviewer",
            "Independent candidate review",
            "Review against the brief",
            ("evaluate exact candidate + evidence",),
            ("same agreement", "unmet obligation → correction"),
            850,
            430,
            310,
            120,
        ),
        Box(
            "human-decision",
            "Outside delegated authority",
            "Human decides",
            ("observer explains consequences", "scope · guarantees · architecture"),
            ("change the agreement explicitly",),
            500,
            620,
            300,
            120,
        ),
        Box(
            "accepted-candidate",
            "Reviewed result",
            "Candidate is ready",
            ("human owns repository disposition",),
            (),
            850,
            650,
            310,
            90,
        ),
    ),
    notes=(
        Note(
            "STRUCTURE PRESERVES THE AGREEMENT · PEOPLE AND AGENTS STILL JUDGE ITS MEANING",
            600,
            785,
            12,
            "middle",
            True,
        ),
    ),
)
