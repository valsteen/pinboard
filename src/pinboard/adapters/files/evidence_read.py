"""Read one explicitly selected UTF-8 file; never discover files or write state."""

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SelectedText:
    text: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class TextUnavailable:
    resource: str
    message: str


def read(path: Path) -> SelectedText | TextUnavailable:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return TextUnavailable(str(path), f"Cannot read selected UTF-8 text: {error}")
    return SelectedText(text, hashlib.sha256(raw).hexdigest(), len(raw))
