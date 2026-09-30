"""Isolated Codex homes holding a private copy of the human's Codex login, with conditional write-back.

Each Codex run gets a fresh mode-0700 home outside the repository and every output directory, containing only a
mode-0600 copy of the source ``auth.json`` and the harness-written ``config.toml``. The source is read only to copy
and to compare it; its contents are never logged, printed, hashed into a record or copied anywhere else, and any of
its values that Codex writes into a kept session rollout are replaced before the rollout is stored. If Codex
rewrote the copy during the run (a token refresh), the refreshed bytes are written back atomically, at mode 0600,
only while the source is still byte-identical to what was copied; otherwise nothing is written and further Codex
runs must stop. The home is removed at the end of every run, including on failure.
"""

import fcntl
import os
import re
import shutil
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from evals.behavioral.processes import Window

AUTH_FILE = "auth.json"


class CredentialSettlement(Enum):
    UNCHANGED = "unchanged"
    WRITTEN_BACK = "written-back"
    REFUSED_SOURCE_CHANGED = "refused-source-changed"


@dataclass
class IsolatedHome:
    path: Path
    source: Path
    copied: bytes
    settlement: CredentialSettlement | None


def default_source() -> Path:
    return Path.home() / ".codex" / AUTH_FILE


@contextmanager
def exclusive_codex_session(lock_directory: Path, window: Window) -> Generator[None]:
    """Serialize Codex sessions across harness processes so concurrent refreshes cannot rotate the login."""
    with (lock_directory / "pinboard-behavioral-eval-codex.lock").open("a") as lock:
        while True:
            window.timeout(300)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(window.timeout(0.05))
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def isolated_home(source: Path, parent: Path | None, window: Window) -> Generator[IsolatedHome]:
    """Yield a private home holding a copy of ``source``; settle the credential, then remove the home."""
    window.timeout(300)
    copied = source.read_bytes()
    path = Path(tempfile.mkdtemp(prefix="pinboard-eval-codex-home-", dir=parent))
    home = IsolatedHome(path=path, source=source, copied=copied, settlement=None)
    try:
        path.chmod(0o700)
        descriptor = os.open(path / AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(copied)
        yield home
    finally:
        try:
            home.settlement = settle(home)
        finally:
            shutil.rmtree(path, ignore_errors=True)


LOGIN_VALUE = re.compile(rb'"([^"\\]{16,})"')
LOGIN_PLACEHOLDER = "<codex-login-value>"


def without_login(text: str, copied: bytes) -> str:
    """Remove every long string value of the copied login from evidence text Codex wrote, such as its account id."""
    values = {match.group(1).decode() for match in LOGIN_VALUE.finditer(copied)}
    for _, value in sorted(((len(value), value) for value in values), reverse=True):
        text = text.replace(value, LOGIN_PLACEHOLDER)
    return text


def settle(home: IsolatedHome) -> CredentialSettlement:
    copy = home.path / AUTH_FILE
    if not copy.is_file():
        return CredentialSettlement.UNCHANGED
    refreshed = copy.read_bytes()
    if refreshed == home.copied:
        return CredentialSettlement.UNCHANGED
    if home.source.read_bytes() != home.copied:
        return CredentialSettlement.REFUSED_SOURCE_CHANGED
    descriptor, temporary = tempfile.mkstemp(prefix=".auth.json.", dir=home.source.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(refreshed)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(home.source)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return CredentialSettlement.WRITTEN_BACK
