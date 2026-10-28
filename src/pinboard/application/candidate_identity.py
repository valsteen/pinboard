"""Raw candidate identity and exact patch comparison across Git object abbreviations."""

import hashlib
import io
import re
from itertools import zip_longest

_INDEX_HEADER = re.compile(rb"index ([0-9a-f]{4,64})\.\.([0-9a-f]{4,64})( [0-7]{6})?\n")


def working_tree_identity(preimage_revision: str, diff: bytes) -> str:
    digest = hashlib.sha256(preimage_revision.encode() + b"\0" + diff).hexdigest()
    return f"working-tree-state-sha256:{digest}"


def candidate_diffs_match(accepted: bytes, observed: bytes) -> bool:
    """Compare original patches, allowing only compatible index-header object prefixes.

    This does not produce canonical bytes or an identity: accepted evidence always
    retains its original bytes. File headers, modes, hunks and binary payload stay exact.
    """
    if accepted == observed:
        return True
    file_header = False
    for original, current in zip_longest(io.BytesIO(accepted), io.BytesIO(observed)):
        if original is None or current is None:
            return False
        if original == current:
            if original.startswith(b"diff --git "):
                file_header = True
            elif original.startswith((b"index ", b"--- ", b"GIT binary patch", b"Binary files ")):
                file_header = False
            continue
        if not file_header:
            return False
        original_index = _INDEX_HEADER.fullmatch(original)
        current_index = _INDEX_HEADER.fullmatch(current)
        if original_index is None or current_index is None or original_index[3] != current_index[3]:
            return False
        for original_id, current_id in zip(original_index.group(1, 2), current_index.group(1, 2), strict=True):
            if not (original_id.startswith(current_id) or current_id.startswith(original_id)):
                return False
        file_header = False
    return True
