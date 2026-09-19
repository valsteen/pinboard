"""Present bounded current work facts without complete-state acquisition."""

from pathlib import Path

import msgspec

from pinboard.application import ports, query_models
from pinboard.cli import cli_commands
from pinboard.cli.cli_output import write_json
from pinboard.domain import work_models


class StatusView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    stored_state_opened: bool = msgspec.field(name="valid")
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    revision: str
    active_attempts: tuple[str, ...]
    counts: dict[str, int]
    intake_item_count: int
    authority: str


def compose_status(
    facts: query_models.ProjectStatusFacts,
    work: Path,
    source_checkout: Path,
    shared_repository: Path,
) -> StatusView:
    counts = dict(facts.counts)
    return StatusView(
        stored_state_opened=True,
        source_checkout_root=str(source_checkout),
        shared_repository_root=str(shared_repository),
        work_root=str(work),
        revision=str(facts.project_revision),
        active_attempts=tuple(str(value) for value in facts.active_attempts),
        counts=counts,
        intake_item_count=counts.get(work_models.WorkState.INTAKE.value, 0),
        authority="sqlite-v6",
    )


def show_status(roots: cli_commands.ResolvedRoots, store: ports.WorkStore, command: cli_commands.StatusCommand) -> int:
    projection = compose_status(store.read_project_status(), roots.work, roots.source_checkout, roots.shared_repository)
    if command.json:
        write_json(projection)
    else:
        print(f"OK WORK_STATE_VALID revision={projection.revision}")
        print(f"active_attempts={','.join(projection.active_attempts) or 'none'}")
        print(f"intake_items={projection.intake_item_count}")
    return 0
