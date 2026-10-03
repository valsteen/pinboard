"""Present current status and an explicit complete-state diagnosis."""

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import msgspec

from pinboard import __version__
from pinboard.adapters.files import contributor_traces
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.application import ports, query_models
from pinboard.cli import cli_commands, work_state_commands
from pinboard.cli.cli_output import write_json
from pinboard.cli.work_state_models import Diagnostic, DiagnosticView, Severity, ValidationReport
from pinboard.domain import work_models


class StatusView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    stored_state_opened: bool = msgspec.field(name="valid")
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    revision: str
    active_attempts: tuple[str, ...]
    counts: Mapping[str, int]
    ready_item_count: int
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
        ready_item_count=counts.get(work_models.WorkState.READY.value, 0),
        authority="sqlite-v7",
    )


def show_status(roots: cli_commands.ResolvedRoots, store: ports.WorkStore, command: cli_commands.StatusCommand) -> int:
    projection = compose_status(store.read_project_status(), roots.work, roots.source_checkout, roots.shared_repository)
    if command.json:
        write_json(projection)
    else:
        print(f"OK WORK_STATE_VALID revision={projection.revision}")
        print(f"active_attempts={','.join(projection.active_attempts) or 'none'}")
        print(f"ready_items={projection.ready_item_count}")
    return 0


class UnfinishedAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    item_id: str
    state: str


class RecentReceipt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    project_revision: int
    action_kind: str
    subject_id: str
    committed_at: str


class DiagnosticHealth(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    status: Literal["valid", "invalid"]
    scope: Literal["explicit-project-wide"]
    diagnostics: tuple[DiagnosticView, ...]


class DiagnosisView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-diagnosis/v1"]
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    work_root_selection: Literal["default", "explicit"]
    runtime_mode: str
    trace_configuration_path: str
    trace_project_mode: Literal["off", "on", "unobserved"]
    trace_configuration_error: str | None
    runtime_version: str
    schema_version: int | None
    project_revision: int | None
    unfinished_attempts: tuple[UnfinishedAttempt, ...] | None
    pending_proposal_ids: tuple[str, ...] | None
    current_replacement_items: tuple[str, ...] | None
    recent_receipt_limit: Literal[10]
    recent_receipts: tuple[RecentReceipt, ...] | None
    validation: DiagnosticHealth


def show_diagnosis(
    roots: cli_commands.ResolvedRoots,
    durable: DurableRoots,
    store: ports.WorkStore,
    _command: cli_commands.DiagnoseCommand,
) -> int:
    report, state = work_state_commands.read_validation_report(roots.work, durable, store, now=datetime.now(UTC))
    receipts: tuple[query_models.RecentReceiptFacts, ...] | None = None
    if state is not None:
        try:
            receipts = store.read_recent_receipts(through_revision=state.lifecycle.project.revision)
        except StorageError as error:
            report = ValidationReport(
                (*report.diagnostics, Diagnostic(error.code.value, Severity.ERROR, durable.database_path, str(error)))
            )
    try:
        configured_trace_mode = contributor_traces.read_configured_project_trace_mode(roots.work)
        trace_configuration_error = None
    except ValueError as error:
        configured_trace_mode = None
        trace_configuration_error = str(error)
    view = DiagnosisView(
        schema="pinboard-diagnosis/v1",
        source_checkout_root=str(roots.source_checkout),
        shared_repository_root=str(roots.shared_repository),
        work_root=str(roots.work),
        work_root_selection="explicit" if roots.explicit_work_root else "default",
        runtime_mode=os.environ.get("PINBOARD_RUNTIME") or "unspecified",
        trace_configuration_path=str(roots.work / contributor_traces.SETTINGS_NAME),
        trace_project_mode=configured_trace_mode if configured_trace_mode is not None else "unobserved",
        trace_configuration_error=trace_configuration_error,
        runtime_version=__version__,
        schema_version=None if state is None else state.lifecycle.project.schema_version,
        project_revision=None if state is None else state.lifecycle.project.revision,
        unfinished_attempts=None
        if state is None
        else tuple(
            UnfinishedAttempt(str(attempt.attempt_id), str(attempt.item_id), attempt.state.value)
            for attempt in state.lifecycle.attempts
            if attempt.state != work_models.AttemptState.DONE
        ),
        pending_proposal_ids=None
        if state is None
        else tuple(str(proposal.proposal_id) for proposal in state.proposals.proposals if proposal.disposition is None),
        current_replacement_items=None
        if state is None
        else tuple(
            str(replacement.affected_item_id)
            for replacement in state.replacements.planned_replacements
            if replacement.status == work_models.PlannedReplacementStatus.CURRENT
        ),
        recent_receipt_limit=10,
        recent_receipts=None
        if receipts is None
        else tuple(
            RecentReceipt(
                int(receipt.history_id),
                receipt.project_revision,
                receipt.action_kind,
                str(receipt.subject_id),
                receipt.committed_at.isoformat(),
            )
            for receipt in receipts
        ),
        validation=DiagnosticHealth(
            "valid" if report.valid else "invalid",
            "explicit-project-wide",
            tuple(
                DiagnosticView(value.code, value.severity.value, str(value.path), value.message, value.hint)
                for value in report.diagnostics
            ),
        ),
    )
    write_json(view)
    return 0 if report.valid else 10
