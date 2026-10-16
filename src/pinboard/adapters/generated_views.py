"""Compose aggregate view reads from one supplied store and verified board evidence."""

from dataclasses import dataclass
from datetime import datetime

from pinboard.adapters import review_operations
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application import ports, queries, query_models, roadmap


@dataclass(frozen=True, slots=True)
class ComposedBoardReader:
    """Read board evidence and the selected roadmap while the file owner holds publication order."""

    durable: DurableRoots
    store: ports.BoardStore

    def read_roadmap(self) -> roadmap.Projection:
        return self.store.read_roadmap()

    def read_board(self, now: datetime) -> query_models.BoardPortfolio:
        live = self.store.read_board_records(now)
        history = review_operations.read_board_history(self.durable, self.store, live.records)
        return queries.project_live_portfolio(live.portfolio, history, now)
