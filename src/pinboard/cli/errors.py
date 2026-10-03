from dataclasses import dataclass
from pathlib import Path

from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.sqlite.errors import SQLiteReadOnlyError, StorageError
from pinboard.application import work_brief_models
from pinboard.application.work_brief_models import WorkBriefFailure  # noqa: ICN003
from pinboard.cli import cli_commands
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailureCode,
    DescribedCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)


class CliErrorCode(DescribedCode):
    ARGUMENT_INVALID = ("CLI_ARGUMENT_INVALID", "The CLI request failed validation for this operation.")
    IO_ERROR = ("CLI_IO_ERROR", "The CLI could not write its result to the selected output stream.")


def storage_failure_details(
    error: StorageError,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
    effect: EffectDisposition,
    changed_surfaces: tuple[ChangedSurface, ...],
    prior_observations: tuple[FailureFact, ...],
) -> FailureDetails:
    observations = prior_observations
    if isinstance(error, SQLiteReadOnlyError):
        if roots is None:
            raise AssertionError("A SQLite read-only failure requires resolved project roots.") from error
        permission_work_root = (
            ".pinboard"
            if not roots.explicit_work_root and roots.source_checkout == roots.shared_repository
            else str(roots.work)
        )
        observations = (
            *observations,
            FailureFact("database_path", str(error.database_path)),
            FailureFact("operation", operation),
            FailureFact("sqlite_error_code", error.code.value),
            FailureFact(
                "permission_recovery",
                "For routine Pinboard commands, select a Codex permission profile extending ':workspace' whose "
                f"narrow filesystem write rule grants access to '{permission_work_root}', the effective work root for "
                "this command. A normal checkout uses the relative '.pinboard' rule; a linked worktree uses "
                "only the resolved absolute shared-repository '.pinboard' directory; an explicit '--work-root' "
                "uses that exact directory. Remove legacy 'sandbox_mode' and 'sandbox_workspace_write' settings "
                "because they override permission profiles. For fresh default initialization, approve the exact "
                "'pinboard init' command once so it can also update '.git/info/exclude'; do not grant persistent "
                "'.git' access.",
            ),
        )
    return FailureDetails(
        observed=observations,
        mismatches=(),
        retry=(
            RetryDisposition.DO_NOT_RETRY
            if effect == EffectDisposition.COMMITTED or not error.retryable
            else RetryDisposition.RETRY_SAME_INPUT
        ),
        effect=effect,
        changed_surfaces=changed_surfaces,
        alternatives=(),
    )


@dataclass(frozen=True, slots=True)
class CommandFailure:
    code: DecisionFailureCode
    message: str
    details: FailureDetails | None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type CommandResult[T] = T | CommandFailure


type InitializationCause = StorageError | ArtifactError | FileIOError | work_brief_models.WorkBriefFailure


@dataclass(frozen=True, slots=True)
class InitializationAfterCommittedEffects:
    """Initialization failed after this invocation durably changed named surfaces."""

    git_exclude_path: Path | None
    database_path: Path | None
    cause: InitializationCause


def initialization_failure_details(
    error: InitializationAfterCommittedEffects,
    operation: str,
    roots: cli_commands.ResolvedRoots | None,
) -> FailureDetails:
    """Describe exactly the initialization surfaces committed before failure."""

    changed_surfaces = (
        *((ChangedSurface.REPOSITORY_GIT_EXCLUDE,) if error.git_exclude_path is not None else ()),
        *((ChangedSurface.LEDGER,) if error.database_path is not None else ()),
    )
    observations = (
        *(
            (
                FailureFact("git_exclude_path", str(error.git_exclude_path)),
                FailureFact("git_exclude_entry", "/.pinboard/"),
            )
            if error.git_exclude_path is not None
            else ()
        ),
        *(
            (FailureFact("database_path", str(error.database_path)),)
            if error.database_path is not None and not isinstance(error.cause, SQLiteReadOnlyError)
            else ()
        ),
    )
    if isinstance(error.cause, StorageError):
        return storage_failure_details(
            error.cause,
            operation,
            roots,
            EffectDisposition.COMMITTED,
            changed_surfaces,
            observations,
        )
    return FailureDetails(
        observed=observations,
        mismatches=(),
        retry=RetryDisposition.DO_NOT_RETRY,
        effect=EffectDisposition.COMMITTED,
        changed_surfaces=changed_surfaces,
        alternatives=(),
    )


type CliFailure = CommandFailure | WorkBriefFailure | InitializationAfterCommittedEffects
type CliResult[T] = T | CliFailure
