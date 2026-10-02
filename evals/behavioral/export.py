"""Export one skills revision as a disposable plugin root and prepare its own private runtime.

The export is ``git archive`` of the chosen commit. Its runtime is prepared only by that export's own
``scripts/pinboard --prepare-runtime``; no development ``.venv`` is linked or copied. A launcher recovery result
or an unready runtime is a typed seed failure (``SeedFailure``), because the revision cannot be evaluated.
"""

import hashlib
import io
import shutil
import tarfile
from pathlib import Path
from typing import Literal

import msgspec

from evals.behavioral import processes
from evals.behavioral.records import ExportRecord, write_new

EXPORT_RECORD = "export.json"
PLUGIN_DIRECTORY = "plugin"


class SeedFailure(Exception):
    """The evaluated revision's launcher or MCP contract could not seed or serve a world; the run cannot proceed."""


class RuntimeLocation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    base: str
    relative: str


class LauncherAction(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    launcher: str
    arguments: list[str]
    display_command: str
    requires: list[str]


class LauncherResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Pinboard's ``pinboard-launcher-result/v1`` record, emitted by the evaluated revision's launcher."""

    schema: Literal["pinboard-launcher-result/v1"]
    status: str
    pinboard_started: bool
    runtime_location: RuntimeLocation
    observations: list[str]
    upstream_exit_code: int | None
    retry_disposition: str
    effect_disposition: str
    changed_surfaces: list[str]
    next_action: LauncherAction | None


def skills_sha256(plugin_root: Path) -> str:
    """Digest of every Markdown file under ``skills/``: sorted per-file ``shasum -a 256`` lines, then their digest."""
    files = sorted((path.relative_to(plugin_root).as_posix(), path) for path in (plugin_root / "skills").rglob("*.md"))
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n" for relative, path in files]
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def export_revision(source: Path, revision: str, destination: Path, window: processes.Window) -> ExportRecord:
    commit = processes.git_checked(
        ["rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=source, window=window
    ).strip()
    destination.mkdir(parents=True, exist_ok=False)
    plugin_root = destination / PLUGIN_DIRECTORY
    plugin_root.mkdir()
    archive = processes.git_archive(commit, cwd=source, window=window)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(plugin_root, filter="data")
    # Evaluation fixtures and their tests are never part of an agent's plugin.
    shutil.rmtree(plugin_root / "evals", ignore_errors=True)
    shutil.rmtree(plugin_root / "tests", ignore_errors=True)
    prepare_runtime(plugin_root, window)
    record = ExportRecord(
        schema="pinboard-behavioral-export/v1",
        commit=commit,
        skills_sha256=skills_sha256(plugin_root),
        plugin_root=str(plugin_root),
    )
    write_new(destination / EXPORT_RECORD, record)
    return record


def prepare_runtime(plugin_root: Path, window: processes.Window) -> None:
    completed = processes.launcher_prepare_runtime(plugin_root / "scripts" / "pinboard", window=window)
    try:
        result = msgspec.json.decode(completed.stdout.strip() or completed.stderr.strip(), type=LauncherResult)
    except msgspec.DecodeError as error:
        raise SeedFailure(f"--prepare-runtime returned no launcher result: {error}") from error
    if completed.returncode != 0 or result.status != "runtime-ready":
        raise SeedFailure(f"--prepare-runtime did not become ready: {result.status}: {'; '.join(result.observations)}")
    if (plugin_root / ".venv").exists():
        raise SeedFailure("the exported plugin root contains a development .venv")


def load_export(destination: Path) -> ExportRecord:
    record = msgspec.json.decode((destination / EXPORT_RECORD).read_bytes(), type=ExportRecord)
    plugin_root = Path(record.plugin_root)
    if not (plugin_root / ".pinboard-runtime" / ".pinboard-ready").is_file():
        raise SeedFailure(f"the exported plugin root at {plugin_root} has no prepared runtime")
    if skills_sha256(plugin_root) != record.skills_sha256:
        raise SeedFailure(f"the exported skills at {plugin_root} no longer match their recorded digest")
    return record
