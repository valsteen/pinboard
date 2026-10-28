"""Produce historical Git patches before recording genuine accepted snapshot evidence."""

import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def historical_candidate_diffs(abbreviation: str) -> Generator[None]:
    def read(cwd: Path, *revisions: str, unavailable_message: str) -> bytes:
        result = subprocess.run(
            ["git", "diff", "--binary", f"--abbrev={abbreviation}", *revisions, "--"],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, unavailable_message
        return result.stdout

    with patch("pinboard.adapters.files.root._read_candidate_diff", side_effect=read):
        yield
