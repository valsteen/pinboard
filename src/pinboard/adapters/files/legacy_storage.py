"""Bound compatibility move from .codex/pinboard to .pinboard.

The caller guarantees quiescence. The root rename is the authority switch;
plans, progress and full-tree predecessors stay in a stable ignored sibling.
"""

import os
import re
import shutil
import stat
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Literal

import msgspec

from pinboard.adapters.files.errors import FileIOError, ImmutableFilePublishedError, RootError, RootErrorCode
from pinboard.adapters.files.file_io import (
    DurableRoots,
    _sync_directory,
    atomic_replace,
    create_immutable,
    ensure_child_directory,
)
from pinboard.adapters.files.root import ensure_git_exclude


class StorageLocation(Enum):
    FRESH = "fresh"
    LEGACY = "legacy"
    CURRENT = "current"
    ALIASED = "aliased"
    CONFLICT = "conflict"


def observe_storage_location(repository: Path) -> StorageLocation:
    legacy = repository / ".codex" / "pinboard"
    current = repository / ".pinboard"
    if current.exists(follow_symlinks=False) and (current.is_symlink() or not current.is_dir()):
        return StorageLocation.CONFLICT
    if legacy.is_symlink():
        if (
            not legacy.parent.is_symlink()
            and current.is_dir()
            and legacy.readlink() == Path("../.pinboard")
            and legacy.resolve() == current
        ):
            return StorageLocation.ALIASED
        return StorageLocation.CONFLICT
    if legacy.exists(follow_symlinks=False):
        if not legacy.is_dir() or legacy.parent.is_symlink() or current.exists(follow_symlinks=False):
            return StorageLocation.CONFLICT
        return StorageLocation.LEGACY
    return StorageLocation.CURRENT if current.is_dir() else StorageLocation.FRESH


class ForwardRootPlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="forward", tag_field="kind"):
    schema: Literal["pinboard-root-plan/v1"]
    repository: str
    starting_location: Literal["legacy", "current"]
    tree_sha256: str
    authority_sha256: str
    git_other_sha256: str


class UnchangedRootPlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="unchanged", tag_field="kind"):
    schema: Literal["pinboard-root-plan/v1"]
    repository: str
    tree_sha256: str
    authority_sha256: str
    git_other_sha256: str


class ReverseRootPlan(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="reverse", tag_field="kind"):
    schema: Literal["pinboard-root-plan/v1"]
    repository: str
    forward_plan_id: str
    starting_location: Literal["legacy", "current"]
    tree_sha256: str
    authority_sha256: str
    git_other_sha256: str


type RootPlan = ForwardRootPlan | UnchangedRootPlan | ReverseRootPlan

type RootStep = Literal[
    "plan-published",
    "exclusions-installed",
    "backup-verified",
    "root-moved",
    "alias-installed",
    "reverse-plan-published",
    "alias-removed",
    "root-restored",
    "reverse-complete",
]


class RootProgress(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-root-progress/v1"]
    plan_id: str
    step: RootStep
    next_step: str
    tree_sha256: str


@dataclass(frozen=True, slots=True)
class PlannedRoot:
    plan_id: str
    plan: RootPlan


@dataclass(frozen=True, slots=True)
class AppliedRoot:
    plan_id: str
    direction: Literal["forward", "reverse", "unchanged"]
    changed_surfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootProcedureFailure:
    message: str
    changed_surfaces: tuple[str, ...]
    retry: Literal["correct-input", "retry-same-input"]


type RootApplication = AppliedRoot | RootProcedureFailure

_SIDECAR_EXCLUDE = b"/.codex/pinboard-migration/"
_ROOT_EXCLUDE = b"/.pinboard/"
_ALIAS_EXCLUDE = b"/.codex/pinboard"


def _sidecar(repository: Path) -> Path:
    return repository / ".codex" / "pinboard-migration"


def _canonical(plan: RootPlan) -> bytes:
    return msgspec.json.encode(plan, order="sorted")


def _identity(plan: RootPlan) -> str:
    digest = sha256(_canonical(plan)).hexdigest()
    if isinstance(plan, ReverseRootPlan):
        return f"reverse-{plan.forward_plan_id}-{digest}"
    return digest


def _valid_id(plan_id: str) -> bool:
    return re.fullmatch(r"(?:[0-9a-f]{64}|reverse-[0-9a-f]{64}-[0-9a-f]{64})", plan_id) is not None


def _read_plan(repository: Path, plan_id: str) -> RootPlan | None:
    if not _valid_id(plan_id):
        return None
    try:
        content = (_sidecar(repository) / f"{plan_id}.json").read_bytes()
    except FileNotFoundError:
        return None
    plan: RootPlan = msgspec.json.decode(content, type=RootPlan)
    if content != _canonical(plan) or _identity(plan) != plan_id:
        raise ValueError("Stored root plan differs from its identity.")
    return plan


def _read_progress(repository: Path, plan_id: str) -> RootProgress | None:
    try:
        value = msgspec.json.decode((_sidecar(repository) / f"{plan_id}.progress.json").read_bytes(), type=RootProgress)
    except FileNotFoundError:
        return None
    if value.plan_id != plan_id:
        raise ValueError("Stored root progress belongs to another plan.")
    return value


def _tree_digest(root: Path) -> str:
    digest = sha256()
    for path in (root, *sorted(root.rglob("*"))):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind = b"link"
            content = os.fsencode(path.readlink())
        elif stat.S_ISDIR(metadata.st_mode):
            kind = b"directory"
            content = b""
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"file"
            content = sha256(path.read_bytes()).digest()
        else:
            raise ValueError(f"Unsupported work-root entry: {path}")
        digest.update(len(relative.encode()).to_bytes(8, "big"))
        digest.update(relative.encode())
        digest.update(kind)
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(2, "big"))
        digest.update(content)
    return digest.hexdigest()


def _authority_digest(root: Path) -> str:
    digest = sha256()
    roots = DurableRoots(root.parent, (root.name,))
    digest.update(sha256(roots.database_path.read_bytes()).digest())
    digest.update(_tree_digest(roots.artifacts_root).encode())
    return digest.hexdigest()


def _git_other_digest(repository: Path) -> str:
    exclude = repository / ".git" / "info" / "exclude"
    try:
        lines = exclude.read_bytes().splitlines()
    except FileNotFoundError:
        lines: list[bytes] = []
    return sha256(b"\n".join(line for line in lines if line != _SIDECAR_EXCLUDE)).hexdigest()


def _source_tree(repository: Path, location: StorageLocation) -> Path:
    return repository / ".codex" / "pinboard" if location == StorageLocation.LEGACY else repository / ".pinboard"


def preview_root(repository: Path, reverse_forward_id: str | None = None) -> PlannedRoot | RootProcedureFailure:
    location = observe_storage_location(repository)
    try:
        if reverse_forward_id is not None:
            forward = _read_plan(repository, reverse_forward_id)
            if not isinstance(forward, ForwardRootPlan) or forward.repository != str(repository):
                return RootProcedureFailure(
                    "The selected effectful forward root plan is unavailable.", (), "correct-input"
                )
            if location != StorageLocation.ALIASED:
                return RootProcedureFailure(
                    "Root reversal requires the unchanged completed alias state.", (), "correct-input"
                )
            if _authority_digest(repository / ".pinboard") != forward.authority_sha256:
                return RootProcedureFailure(
                    "The migrated work root changed after relocation; retain it and diagnose later writes before reversal.",
                    (),
                    "correct-input",
                )
            progress = _read_progress(repository, reverse_forward_id)
            if progress is None or progress.step != "alias-installed":
                return RootProcedureFailure(
                    "Finish the original forward --apply before previewing reversal.", (), "correct-input"
                )
            if forward.starting_location == "legacy":
                backup = _sidecar(repository) / f"{reverse_forward_id}.backup"
                if _tree_digest(backup) != forward.tree_sha256:
                    return RootProcedureFailure("The retained legacy-tree backup is invalid.", (), "correct-input")
            plan: RootPlan = ReverseRootPlan(
                "pinboard-root-plan/v1",
                str(repository),
                reverse_forward_id,
                forward.starting_location,
                forward.tree_sha256,
                forward.authority_sha256,
                _git_other_digest(repository),
            )
            return PlannedRoot(_identity(plan), plan)
        if location not in (StorageLocation.LEGACY, StorageLocation.CURRENT, StorageLocation.ALIASED):
            return RootProcedureFailure(
                "Migration requires one verified legacy or current work root.", (), "correct-input"
            )
        source_tree = _source_tree(repository, location)
        tree = _tree_digest(source_tree)
        authority = _authority_digest(source_tree)
        other = _git_other_digest(repository)
        if location == StorageLocation.ALIASED:
            plan = UnchangedRootPlan("pinboard-root-plan/v1", str(repository), tree, authority, other)
        else:
            plan = ForwardRootPlan("pinboard-root-plan/v1", str(repository), location.value, tree, authority, other)
        return PlannedRoot(_identity(plan), plan)
    except (OSError, RootError, ValueError, msgspec.ValidationError) as error:
        return RootProcedureFailure(f"Root migration observation failed: {error}", (), "correct-input")


def _progress(repository: Path, plan_id: str, step: RootStep, next_step: str, tree: str) -> bool:
    forward_steps: tuple[RootStep, ...] = (
        "plan-published",
        "exclusions-installed",
        "backup-verified",
        "root-moved",
        "alias-installed",
    )
    reverse_steps: tuple[RootStep, ...] = (
        "reverse-plan-published",
        "alias-removed",
        "root-restored",
        "reverse-complete",
    )
    steps = reverse_steps if step in reverse_steps else forward_steps
    previous = _read_progress(repository, plan_id)
    if previous is not None:
        if previous.step not in steps or previous.tree_sha256 != tree:
            raise ValueError("Stored root progress conflicts with the selected plan.")
        if steps.index(previous.step) >= steps.index(step):
            return False
    progress = RootProgress("pinboard-root-progress/v1", plan_id, step, next_step, tree)
    atomic_replace(_sidecar(repository) / f"{plan_id}.progress.json", msgspec.json.encode(progress, order="sorted"))
    return True


def _backup(repository: Path, plan_id: str, expected: str) -> None:
    def depth(path: Path) -> int:
        return len(path.parts)

    sidecar = _sidecar(repository)
    backup = sidecar / f"{plan_id}.backup"
    if backup.is_dir():
        if _tree_digest(backup) != expected:
            raise ValueError("Retained legacy-tree backup differs from the plan.")
        return
    staging = sidecar / f".{plan_id}.backup-stage"
    if staging.exists(follow_symlinks=False):
        shutil.rmtree(staging)
    shutil.copytree(repository / ".codex" / "pinboard", staging, symlinks=True, copy_function=shutil.copy2)
    if _tree_digest(staging) != expected:
        raise ValueError("Copied legacy-tree backup differs from the source.")
    for path in staging.rglob("*"):
        if path.is_file(follow_symlinks=False):
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for path in sorted((staging, *staging.rglob("*")), key=depth, reverse=True):
        if path.is_dir(follow_symlinks=False):
            _sync_directory(path)
    staging.rename(backup)
    _sync_directory(sidecar)
    if _tree_digest(backup) != expected:
        raise ValueError("Published legacy-tree backup could not be verified.")


def apply_root(repository: Path, plan_id: str) -> RootApplication:  # noqa: C901, PLR0912, PLR0915 - one resumable effect owner
    changes: list[str] = []

    def advance(step: RootStep, next_step: str, tree: str) -> None:
        try:
            published = _progress(repository, plan_id, step, next_step, tree)
        except FileIOError:
            try:
                observed = _read_progress(repository, plan_id)
            except OSError, ValueError, msgspec.ValidationError:
                observed = None
            if (
                observed == RootProgress("pinboard-root-progress/v1", plan_id, step, next_step, tree)
                and "migration-evidence" not in changes
            ):
                changes.append("migration-evidence")
            raise
        if published and "migration-evidence" not in changes:
            changes.append("migration-evidence")

    try:
        selected = _read_plan(repository, plan_id)
        if selected is None:
            if plan_id.startswith("reverse-") and _valid_id(plan_id):
                preview = preview_root(repository, plan_id.split("-")[1])
            else:
                preview = preview_root(repository)
            if isinstance(preview, RootProcedureFailure):
                return preview
            if preview.plan_id != plan_id:
                return RootProcedureFailure(
                    "The root plan changed since preview; request a new plan.", (), "correct-input"
                )
            selected = preview.plan
        if selected.repository != str(repository):
            return RootProcedureFailure("The root plan belongs to another repository.", (), "correct-input")
        location = observe_storage_location(repository)
        if isinstance(selected, UnchangedRootPlan):
            preview = preview_root(repository)
            if not isinstance(preview, PlannedRoot) or preview.plan_id != plan_id:
                return RootProcedureFailure("The aliased work root changed since preview.", (), "correct-input")
            return AppliedRoot(plan_id, "unchanged", ())
        if isinstance(selected, ForwardRootPlan):
            if location not in (StorageLocation.LEGACY, StorageLocation.CURRENT, StorageLocation.ALIASED):
                return RootProcedureFailure("The planned work-root authority is unavailable.", (), "correct-input")
            if selected.starting_location == "current" and location == StorageLocation.LEGACY:
                return RootProcedureFailure("A legacy root appeared after the current-root plan.", (), "correct-input")
            source_tree = _source_tree(repository, location)
            matches = (
                _tree_digest(source_tree) == selected.tree_sha256
                if location == StorageLocation.LEGACY
                else _authority_digest(source_tree) == selected.authority_sha256
            )
            if not matches:
                return RootProcedureFailure(
                    "The work-root tree differs from the bound plan; retain both roots and diagnose the changed data.",
                    (),
                    "correct-input",
                )
            if _read_plan(repository, plan_id) is None:
                preview = preview_root(repository)
                if not isinstance(preview, PlannedRoot) or preview.plan_id != plan_id:
                    return RootProcedureFailure("The root preconditions changed before apply.", (), "correct-input")
            if ensure_git_exclude(repository, _SIDECAR_EXCLUDE, require_repository=True) is not None:
                changes.append("repository-git-exclude")
            sidecar = _sidecar(repository)
            if not sidecar.parent.exists():
                sidecar.parent.mkdir()
            ensure_child_directory(sidecar.parent, sidecar.name)
            if create_immutable(sidecar / f"{plan_id}.json", _canonical(selected)):
                changes.append("migration-evidence")
            advance("plan-published", "install root exclusions", selected.tree_sha256)
            for entry in (_ROOT_EXCLUDE, _ALIAS_EXCLUDE):
                if (
                    ensure_git_exclude(repository, entry, require_repository=True) is not None
                    and "repository-git-exclude" not in changes
                ):
                    changes.append("repository-git-exclude")
            advance("exclusions-installed", "verify predecessor backup", selected.tree_sha256)
            if selected.starting_location == "legacy":
                if location == StorageLocation.LEGACY:
                    _backup(repository, plan_id, selected.tree_sha256)
                    if "migration-evidence" not in changes:
                        changes.append("migration-evidence")
                elif _tree_digest(sidecar / f"{plan_id}.backup") != selected.tree_sha256:
                    return RootProcedureFailure(
                        "The retained legacy backup is missing or changed.", tuple(changes), "correct-input"
                    )
                advance("backup-verified", "move root", selected.tree_sha256)
                if location == StorageLocation.LEGACY:
                    (repository / ".codex" / "pinboard").rename(repository / ".pinboard")
                    changes.append("work-root")
                    _sync_directory(repository / ".codex")
                    _sync_directory(repository)
                    location = StorageLocation.CURRENT
                advance("root-moved", "install alias", selected.tree_sha256)
            if location == StorageLocation.CURRENT:
                legacy = repository / ".codex" / "pinboard"
                legacy.parent.mkdir(exist_ok=True)
                legacy.symlink_to("../.pinboard", target_is_directory=True)
                changes.append("compatibility-alias")
                _sync_directory(legacy.parent)
            advance("alias-installed", "complete", selected.tree_sha256)
            return AppliedRoot(plan_id, "forward", tuple(changes))
        forward = _read_plan(repository, selected.forward_plan_id)
        if not isinstance(forward, ForwardRootPlan):
            return RootProcedureFailure("The forward root plan is unavailable.", (), "correct-input")
        reverse_path = _sidecar(repository) / f"{plan_id}.json"
        published = reverse_path.is_file()
        if not published:
            preview = preview_root(repository, selected.forward_plan_id)
            if not isinstance(preview, PlannedRoot) or preview.plan_id != plan_id:
                return RootProcedureFailure(
                    "The reverse root preconditions changed since preview.", (), "correct-input"
                )
        elif selected.git_other_sha256 != _git_other_digest(repository):
            return RootProcedureFailure(
                "Repository-local Git metadata changed since reverse planning.", (), "correct-input"
            )
        if location == StorageLocation.LEGACY:
            if (
                selected.starting_location != "legacy"
                or _authority_digest(repository / ".codex" / "pinboard") != selected.authority_sha256
            ):
                return RootProcedureFailure(
                    "The restored legacy root differs from its predecessor.", (), "correct-input"
                )
            advance("reverse-complete", "complete", selected.tree_sha256)
            return AppliedRoot(plan_id, "reverse", tuple(changes))
        if location not in (StorageLocation.CURRENT, StorageLocation.ALIASED):
            return RootProcedureFailure("The migrated root is unavailable for reversal.", (), "correct-input")
        if _authority_digest(repository / ".pinboard") != selected.authority_sha256:
            return RootProcedureFailure(
                "The migrated work root changed after relocation; retain it and diagnose later writes.",
                (),
                "correct-input",
            )
        if create_immutable(reverse_path, _canonical(selected)):
            changes.append("migration-evidence")
        advance("reverse-plan-published", "remove alias", selected.tree_sha256)
        if location == StorageLocation.ALIASED:
            (repository / ".codex" / "pinboard").unlink()
            changes.append("compatibility-alias")
            _sync_directory(repository / ".codex")
            location = StorageLocation.CURRENT
        advance("alias-removed", "restore predecessor root", selected.tree_sha256)
        if selected.starting_location == "legacy":
            if _tree_digest(_sidecar(repository) / f"{selected.forward_plan_id}.backup") != selected.tree_sha256:
                return RootProcedureFailure("The retained legacy backup is invalid.", tuple(changes), "correct-input")
            (repository / ".pinboard").rename(repository / ".codex" / "pinboard")
            changes.append("work-root")
            _sync_directory(repository)
            _sync_directory(repository / ".codex")
            advance("root-restored", "complete", selected.tree_sha256)
        advance("reverse-complete", "complete", selected.tree_sha256)
        return AppliedRoot(plan_id, "reverse", tuple(changes))
    except (OSError, FileIOError, RootError, ValueError, msgspec.ValidationError) as error:
        if isinstance(error, RootError) and error.code == RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE and not changes:
            return RootProcedureFailure(
                f"Repository-local Git exclusion could not be verified at {repository / '.git' / 'info' / 'exclude'}: "
                f"{error}. Restore Git access and request a fresh work-root plan before applying.",
                (),
                "correct-input",
            )
        if isinstance(error, ImmutableFilePublishedError) and "migration-evidence" not in changes:
            changes.append("migration-evidence")
        return RootProcedureFailure(
            f"Work-root migration stopped: {error}. Inspect the named roots and resume only --apply {plan_id}.",
            tuple(changes),
            "retry-same-input",
        )
