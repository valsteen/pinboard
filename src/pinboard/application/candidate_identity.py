"""Pure identity for the complete tracked working-tree state."""

import hashlib


def working_tree_identity(preimage_revision: str, diff: bytes) -> str:
    digest = hashlib.sha256(preimage_revision.encode() + b"\0" + diff).hexdigest()
    return f"working-tree-state-sha256:{digest}"
