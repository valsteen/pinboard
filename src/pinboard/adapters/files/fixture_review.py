"""Read and hash exactly one caller-selected fixture; never write or resolve state."""

import hashlib
from pathlib import Path

from pinboard.application.fixture_review import FixtureReviewFailure


def read_verified_fixture(path: Path, sha256: str) -> bytes | FixtureReviewFailure:
    try:
        raw = path.read_bytes()
    except OSError as error:
        return FixtureReviewFailure(str(path), f"Cannot read selected fixture {str(path)!r}: {error}")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != sha256:
        return FixtureReviewFailure(
            str(path), f"Selected fixture {str(path)!r} SHA-256 mismatch: expected {sha256}, observed {actual}."
        )
    return raw
