import msgspec


class CloseView(msgspec.Struct, frozen=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


class ItemRevisionView(msgspec.Struct, frozen=True):
    item_id: str
    definition_revision: int
    definition_digest: str
    project_revision: str
