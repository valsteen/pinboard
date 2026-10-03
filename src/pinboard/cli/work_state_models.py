from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

import msgspec

from pinboard.adapters.files.legacy_storage import RootPlan
from pinboard.adapters.sqlite.schema_procedure import SchemaPlan


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    path: Path
    message: str
    hint: str | None = None

    def render(self) -> str:
        result = f"{self.severity.value.upper()} {self.code} {self.path}: {self.message}"
        if self.hint:
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


class WorkRootMigrationView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-root-migration/v2"]
    status: Literal["planned", "migrated", "reversed", "unchanged"]
    plan_id: str
    plan: RootPlan | None
    work_root: str
    compatibility_alias: str
    state_changed: bool
    effect: Literal["committed", "unchanged"]
    retry: Literal["do-not-retry", "safe-to-repeat"]
    changed_surfaces: tuple[str, ...]


class SchemaMigrationView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-schema-migration/v2"]
    status: Literal["planned", "migrated", "reversed", "unchanged"]
    plan_id: str
    plan: SchemaPlan | None
    database_path: str
    authority: Literal["sqlite-v6", "sqlite-v7"]
    state_changed: bool
    effect: Literal["committed", "unchanged"]
    retry: Literal["do-not-retry", "safe-to-repeat"]
    changed_surfaces: tuple[str, ...]
