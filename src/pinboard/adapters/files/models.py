from dataclasses import dataclass

from pinboard.domain.identifiers import AttemptId, HistoryId, ItemId


@dataclass(frozen=True, slots=True)
class ViewWarning:
    message: str
    repair: str


@dataclass(frozen=True, slots=True)
class ViewRefreshResult:
    database_revision: int
    warning: ViewWarning | None = None


@dataclass(frozen=True, slots=True)
class AffectedViews:
    current_focus: bool = False
    items: tuple[ItemId, ...] = ()
    attempts: tuple[AttemptId, ...] = ()
    history_receipts: tuple[HistoryId, ...] = ()
