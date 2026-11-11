from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

import msgspec

from pinboard.domain.errors import DescribedCode


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationDiagnosticCode(DescribedCode):
    CANDIDATE_SNAPSHOT_INVALID = ("CANDIDATE_SNAPSHOT_INVALID", "Stored candidate snapshot history fails validation.")
    CLOSE_DECISION_INVALID = (
        "CLOSE_DECISION_INVALID",
        "The human closure decision failed validation for this operation.",
    )
    TRANSITION_RECEIPT_DAMAGED = (
        "TRANSITION_RECEIPT_DAMAGED",
        "A recorded transition receipt fails integrity or canonical validation.",
    )
    VIEW_REFRESH_REQUIRED = (
        "VIEW_REFRESH_REQUIRED",
        "A generated view is stale or absent while the ledger remains authoritative.",
    )


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    path: Path
    message: str
    hint: str | None = None

    def render(self) -> str:
        result = f"{self.severity.value.upper()} {self.code} {self.path}: {self.message}"
        if self.hint is not None and self.hint != "":
            result += f" Hint: {self.hint}"
        return result


@dataclass(frozen=True, slots=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(diagnostic.severity == Severity.ERROR for diagnostic in self.diagnostics)

    def render(self) -> str:
        if not self.diagnostics:
            return "OK WORK_STATE_VALID"
        return "\n".join(diagnostic.render() for diagnostic in self.diagnostics)


class RootView(msgspec.Struct, frozen=True):
    source_checkout_root: str
    shared_repository_root: str
    work_root: str


class DiagnosticView(msgspec.Struct, frozen=True):
    code: str
    severity: str
    path: str
    message: str
    hint: str | None


class ValidationView(msgspec.Struct, frozen=True):
    valid: bool
    diagnostics: tuple[DiagnosticView, ...]


class InitializationView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-state-initialized/v1"]
    work_root: str
    resumed: bool
    optional_next_skills: tuple[str, ...]
    configuration_recommendation: str | None
