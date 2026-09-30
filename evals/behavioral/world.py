"""Disposable scenario worlds: the ``tally`` project, its bare ``origin``, seeded boards, hooks and state snapshots.

A world lives under a caller-named directory. The project fixture is identical for every runtime except the one
declared difference in ``fixture_difference``: Codex reads ``AGENTS.md`` and not ``CLAUDE.md``, so a Codex world's
``AGENTS.md`` also carries the project guidance a Claude Code world keeps in ``CLAUDE.md``.
"""

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import msgspec

from evals.behavioral import board, processes, seeding
from evals.behavioral.export import LauncherResult, SeedFailure
from evals.behavioral.records import Hook, ObservedState, Runtime, Scenario, SeededItem, WorldExtra, WorldKind
from evals.behavioral.scenarios import FIXTURE
from evals.behavioral.world_content import (
    EXPERIMENT_STATES,
    FULL_WORLD_STATES,
    seed_full_world,
    seed_skip_comments_experiment,
)

MAINTAINER = ("Sam Rivera", "sam@example.test")


@dataclass(frozen=True)
class World:
    root: Path
    project: Path
    origin: Path
    scratch_board: Path | None
    launcher: Path

    @property
    def work_root(self) -> Path:
        return self.project / ".pinboard"

    @property
    def mcp_log(self) -> Path:
        return self.root / "mcp.log"


def fixture_difference(runtime: Runtime) -> str | None:
    match runtime:
        case Runtime.CLAUDE_CODE:
            return None
        case Runtime.CODEX:
            return (
                "AGENTS.md also carries the project guidance that Claude Code worlds keep in CLAUDE.md "
                "(Pinboard route, inspection-only rule, maintainer identity), because Codex reads AGENTS.md "
                "and not CLAUDE.md"
            )
        case _ as unreachable:
            raise AssertionError(unreachable)


def agents_file(runtime: Runtime) -> str:
    repository = (FIXTURE / "repository-guidance.md").read_text()
    match runtime:
        case Runtime.CLAUDE_CODE:
            return repository
        case Runtime.CODEX:
            return repository + "\n" + (FIXTURE / "project-guidance.md").read_text()
        case _ as unreachable:
            raise AssertionError(unreachable)


def build_world(
    root: Path, scenario: Scenario, runtime: Runtime, plugin_root: Path, owner: str
) -> tuple[World, list[SeededItem]]:
    """Build and seed the scenario's world, then verify every declared item reached its declared state."""
    root.mkdir(parents=True, exist_ok=False)
    launcher = plugin_root / "scripts" / "pinboard"
    world = World(
        root=root,
        project=root / "tally",
        origin=root / "origin.git",
        scratch_board=root / "scratch-board" if scenario.world is WorldKind.MINIMAL else None,
        launcher=launcher,
    )
    create_project(world, runtime)
    init_board(world.launcher, world.project, None)
    seeded: list[SeededItem] = []
    match scenario.world:
        case WorldKind.FULL:
            asyncio.run(_seed(world, owner, full=True))
            seeded += verify_seed(world, "project", world.work_root, FULL_WORLD_STATES)
        case WorldKind.MINIMAL:
            init_board(world.launcher, world.project, world.scratch_board)
        case _ as unreachable:
            raise AssertionError(unreachable)
    match scenario.world_extra:
        case None:
            pass
        case WorldExtra.SKIP_COMMENTS_EXPERIMENT:
            asyncio.run(_seed(world, "experiment-session-0928", full=False))
            if world.scratch_board is None:
                raise SeedFailure("the scratch-board experiment needs a minimal world with a scratch board")
            seeded += verify_seed(world, "scratch", world.scratch_board, EXPERIMENT_STATES)
        case _ as unreachable:
            raise AssertionError(unreachable)
    return world, seeded


def verify_seed(
    world: World, board_name: Literal["project", "scratch"], work_root: Path, declared: dict[str, str]
) -> list[SeededItem]:
    observed = asyncio.run(board.item_states(world.launcher, world.mcp_log, world.project, work_root, tuple(declared)))
    mismatches = [f"{item} is {state}, not {declared[item]}" for item, state in observed if state != declared[item]]
    if mismatches:
        raise SeedFailure(f"the seeded {board_name} board differs from the world facts: {'; '.join(mismatches)}")
    return [SeededItem(board=board_name, item_id=item, state=state) for item, state in observed]


async def _seed(world: World, owner: str, *, full: bool) -> None:
    work_root = world.work_root if full else world.scratch_board
    if work_root is None:
        raise SeedFailure("the scratch-board experiment needs a minimal world with a scratch board")
    async with board.connect(world.launcher, world.mcp_log) as client:
        seeder = seeding.Seeder(board=client, project=world.project, work_root=work_root, owner=owner)
        if full:
            await seed_full_world(seeder)
        else:
            await seed_skip_comments_experiment(seeder)


def create_project(world: World, runtime: Runtime) -> None:
    processes.git_checked(["init", "-q", "--bare", "-b", "main", str(world.origin)], cwd=world.root)
    processes.git_checked(["init", "-q", "-b", "main", str(world.project)], cwd=world.root)
    project = world.project
    processes.git_checked(["config", "user.name", MAINTAINER[0]], cwd=project)
    processes.git_checked(["config", "user.email", MAINTAINER[1]], cwd=project)
    files = {
        "README.md": (FIXTURE / "README.md").read_text(),
        "tally.sh": (FIXTURE / "tally.sh").read_text(),
        "docs/usage.md": (FIXTURE / "usage.md").read_text(),
        "test.sh": (FIXTURE / "test.sh").read_text(),
        "AGENTS.md": agents_file(runtime),
        "CLAUDE.md": (FIXTURE / "project-guidance.md").read_text(),
        ".claude/settings.json": (FIXTURE / "claude-settings.json").read_text(),
        ".gitignore": (FIXTURE / "gitignore").read_text(),
    }
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for script in ("tally.sh", "test.sh"):
        (project / script).chmod(0o755)
    processes.git_checked(["add", "-A"], cwd=project)
    processes.git_checked(["commit", "-q", "-m", "Initial tally CLI"], cwd=project)
    processes.git_checked(["remote", "add", "origin", str(world.origin)], cwd=project)
    processes.git_checked(["push", "-q", "-u", "origin", "main"], cwd=project)


def init_board(launcher: Path, project: Path, work_root: Path | None) -> None:
    completed = processes.launcher_init(launcher, project, work_root)
    for stream in (completed.stdout, completed.stderr):
        try:
            recovery = msgspec.json.decode(stream.strip(), type=LauncherResult)
        except msgspec.DecodeError:
            continue
        raise SeedFailure(f"board init returned a launcher recovery result: {recovery.status}")
    if completed.returncode != 0:
        raise SeedFailure(f"board init failed: {completed.stdout.strip()} {completed.stderr.strip()}"[:2000])


def run_hook(hook: Hook, world: World) -> str:
    match hook:
        case Hook.MERGE_EXPERIMENT:
            return merge_experiment(world)
        case Hook.PUSH_SAM_FIX:
            return push_sam_fix(world)
        case _ as unreachable:
            raise AssertionError(unreachable)


def merge_experiment(world: World) -> str:
    """The maintainer commits pending experiment changes, then merges the most-ahead local branch on origin."""
    project = world.project
    log: list[str] = []
    processes.git_checked(["fetch", "-q", "origin"], cwd=project)
    listing = processes.git_checked(["worktree", "list", "--porcelain"], cwd=project)
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        checkout = Path(line.removeprefix("worktree "))
        branch = processes.git_checked(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout).strip()
        if branch == "main" or not processes.git_checked(["status", "--porcelain"], cwd=checkout).strip():
            continue
        processes.git_checked(["add", "-A"], cwd=checkout)
        processes.git_checked(["commit", "-q", "-m", "Commit experiment changes before merging"], cwd=checkout)
        commit = processes.git_checked(["rev-parse", "HEAD"], cwd=checkout).strip()
        log.append(f"merge-experiment: committed uncommitted changes in {checkout} on {branch} as {commit}")
    best, best_count = "", 0
    for branch in processes.git_checked(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=project
    ).split():
        if branch == "main":
            continue
        count = int(processes.git_checked(["rev-list", "--count", f"origin/main..{branch}"], cwd=project))
        if count > best_count:
            best, best_count = branch, count
    if not best:
        log.append("merge-experiment: no unmerged branch found")
        return "\n".join(log) + "\n"
    with tempfile.TemporaryDirectory() as temporary:
        clone = Path(temporary) / "c"
        processes.git_checked(["clone", "-q", str(world.origin), str(clone)], cwd=world.root)
        configure_maintainer(clone)
        processes.git_checked(["fetch", "-q", str(project), f"{best}:refs/remotes/local/{best}"], cwd=clone)
        processes.git_checked(["merge", "-q", "--no-ff", "-m", f"Merge branch '{best}'", f"local/{best}"], cwd=clone)
        processes.git_checked(["push", "-q", "origin", "main"], cwd=clone)
        branch_head = processes.git_checked(["rev-parse", best], cwd=project).strip()
        merged = processes.git_checked(["rev-parse", "HEAD"], cwd=clone).strip()
    log.append(f"merge-experiment: merged {best} ({best_count} commits, head {branch_head}) into origin/main {merged}")
    return "\n".join(log) + "\n"


def push_sam_fix(world: World) -> str:
    """The maintainer pushes a newer commit to the pull-request branch on origin."""
    with tempfile.TemporaryDirectory() as temporary:
        clone = Path(temporary) / "c"
        processes.git_checked(
            ["clone", "-q", "-b", "sam/readme-example", str(world.origin), str(clone)], cwd=world.root
        )
        configure_maintainer(clone)
        readme = clone / "README.md"
        readme.write_text("".join("9\n" if line == "10\n" else line for line in readme.read_text().splitlines(True)))
        processes.git_checked(["commit", "-q", "-am", "Fix blank-line example total"], cwd=clone)
        processes.git_checked(["push", "-q", "origin", "sam/readme-example"], cwd=clone)
        head = processes.git_checked(["rev-parse", "HEAD"], cwd=clone).strip()
    return f"push-sam-fix: sam/readme-example now at {head}\n"


def configure_maintainer(checkout: Path) -> None:
    processes.git_checked(["config", "user.name", MAINTAINER[0]], cwd=checkout)
    processes.git_checked(["config", "user.email", MAINTAINER[1]], cwd=checkout)


def snapshot(world: World, name: str) -> ObservedState:
    sections = [
        (
            "git (main checkout)",
            _git_text(["log", "--oneline", "--graph", "--all", "--decorate", "-n", "30"], world.project),
        ),
        ("origin main", _git_text(["log", "--oneline", "-n", "10", "main"], world.origin)),
        ("origin branches", _git_text(["branch", "-a"], world.origin)),
        ("worktrees", _git_text(["worktree", "list"], world.project)),
        ("board", _board_text(world, world.work_root, scratch=False)),
    ]
    if world.scratch_board is not None:
        sections.append(("scratch board", _board_text(world, world.scratch_board, scratch=True)))
    return ObservedState(name=name, text="".join(f"## {title}\n{body}" for title, body in sections))


def _git_text(arguments: list[str], cwd: Path) -> str:
    completed = processes.git(arguments, cwd=cwd)
    return completed.stdout + completed.stderr


def _board_text(world: World, work_root: Path, *, scratch: bool) -> str:
    try:
        items = asyncio.run(board.overview(world.launcher, world.mcp_log, world.project, work_root)).items
    except SeedFailure as failure:
        return f"(overview unavailable: {failure})\n"
    lines = []
    for item in items:
        fields: dict[str, str | int | list[str] | None] = {
            "item_id": item.item_id,
            "state": item.state,
            "attempt_id": item.attempt_id,
        }
        if not scratch:
            fields |= {"position": item.position, "depends_on": item.depends_on}
        lines.append(json.dumps(fields, separators=(",", ":")) + "\n")
    return "".join(lines)
