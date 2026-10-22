"""Run development checks with independent results and per-run temporary logs.

Commands may build packages, run tests, contact existing package registries and
write their usual reports. This module is outside the installed application.
"""

import argparse
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO, assert_never


class Group(Enum):
    REPO = "verify-repo"
    TYPES = "verify-types"
    TESTS = "verify-tests"
    PACKAGE = "verify-package"


@dataclass(frozen=True)
class GroupResult:
    group: Group
    log: Path
    returncode: int
    diagnostic: str


def run_command(command: Sequence[str], root: Path, log: TextIO, stream: bool) -> int:
    heading = f"$ {shlex.join(command)}\n"
    log.write(heading)
    log.flush()
    if stream:
        print(heading, end="", flush=True)
    with subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    ) as process:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                if stream:
                    print(line, end="", flush=True)
            return process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise


def run_commands(commands: Sequence[Sequence[str]], root: Path, log: TextIO, stream: bool) -> int:
    for command in commands:
        if status := run_command(command, root, log, stream):
            return status
    return 0


def verify_group(group: Group, root: Path, log: TextIO, stream: bool, jobs: int | None) -> int:
    match group:
        case Group.REPO:
            return run_commands(
                (
                    ("uv", "run", "--locked", "python", "-m", "docs.how_it_works.render", "--check"),
                    ("uv", "run", "--locked", "ruff", "format", "--check", "."),
                    ("uv", "run", "--locked", "ruff", "check", "."),
                    ("uv", "run", "--locked", "python", "scripts/validate-metadata.py"),
                    (
                        "npx",
                        "--yes",
                        "@anthropic-ai/claude-code",
                        "plugin",
                        "validate",
                        ".claude-plugin/plugin.json",
                        "--strict",
                    ),
                    ("npx", "--yes", "@anthropic-ai/claude-code", "plugin", "validate", ".", "--strict"),
                    ("npm", "run", "duplication:ci"),
                ),
                root,
                log,
                stream,
            )
        case Group.TYPES:
            return run_command(("scripts/check-types",), root, log, stream)
        case Group.TESTS:
            command = ["uv", "run", "--locked", "python", "-m", "tests.runner", "--coverage"]
            if jobs is not None:
                command.extend(("--jobs", str(jobs)))
            return run_command(command, root, log, stream)
        case Group.PACKAGE:
            with tempfile.TemporaryDirectory(prefix="pinboard-wheel-smoke-") as directory:
                build_directory = Path(directory) / "dist"
                smoke_repository = Path(directory) / "repository"
                smoke_repository.mkdir()
                if status := run_command(
                    ("uv", "build", "--no-sources", "--out-dir", str(build_directory)), root, log, stream
                ):
                    return status
                wheel_options = ("--with", *(str(path) for path in sorted(build_directory.glob("*.whl"))))
                isolated = ("uv", "run", "--isolated", "--no-project", *wheel_options)
                if status := run_commands(
                    (
                        (*isolated, "pinboard", "--version"),
                        (*isolated, "pinboard", "--help"),
                        (*isolated, "python", "-m", "pinboard", "--version"),
                        ("git", "-C", str(smoke_repository), "init"),
                        (*isolated, "pinboard", "--project-root", str(smoke_repository), "init"),
                    ),
                    root,
                    log,
                    stream,
                ):
                    return status
                if not (smoke_repository / ".pinboard/state.sqlite3").is_file():
                    log.write(f"Wheel smoke failed: {smoke_repository}/.pinboard/state.sqlite3 is missing\n")
                    return 1
                return run_command(
                    ("git", "-C", str(smoke_repository), "check-ignore", "-q", ".pinboard/state.sqlite3"),
                    root,
                    log,
                    stream,
                )
        case _ as unreachable:
            assert_never(unreachable)


def run_group(group: Group, root: Path, log_path: Path, stream: bool, jobs: int | None) -> GroupResult:
    try:
        with log_path.open("w", encoding="utf-8") as log:
            status = verify_group(group, root, log, stream, jobs)
        return GroupResult(group, log_path, status, "")
    except OSError as error:
        return GroupResult(group, log_path, 1, f"Verification could not complete: {error}")


def run(groups: Sequence[Group], root: Path, sequential: bool, jobs: int | None) -> int:
    try:
        log_root = Path(tempfile.mkdtemp(prefix="pinboard-verify-"))
    except OSError as error:
        print(f"Verification could not create temporary logs: {error}", file=sys.stderr)
        return 1
    for group in groups:
        print(f"[RUN] {group.value} — log: {log_root / (group.value + '.log')}", flush=True)
    if sequential:
        results = [run_group(group, root, log_root / f"{group.value}.log", True, jobs) for group in groups]
    else:
        with ThreadPoolExecutor(max_workers=len(groups)) as executor:
            futures = [
                executor.submit(run_group, group, root, log_root / f"{group.value}.log", False, jobs)
                for group in groups
            ]
            results = [future.result() for future in futures]
    failed = False
    print("\nVerification results:", flush=True)
    for result in results:
        failed |= result.returncode != 0
        print(
            f"[{'FAIL' if result.returncode else 'PASS'}] {result.group.value} "
            f"(exit {result.returncode}) — log: {result.log}",
            flush=True,
        )
        if result.diagnostic:
            print(result.diagnostic, flush=True)
        if result.returncode and not sequential:
            try:
                with result.log.open(encoding="utf-8") as log:
                    for line in log:
                        print(line, end="", flush=True)
            except OSError as error:
                print(f"Cannot read verification log {result.log}: {error}", flush=True)
    return int(failed)


def positive_jobs(value: str) -> int:
    jobs = int(value)
    if jobs < 1:
        raise argparse.ArgumentTypeError("jobs must be positive")
    return jobs


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "group", nargs="?", choices=["verify-all", *(group.value for group in Group)], default="verify-all"
    )
    parser.add_argument("--sequential", action="store_true", help="stream group output in order")
    parser.add_argument("--jobs", type=positive_jobs, help="pass the worker count to tests.runner")
    options = parser.parse_args(arguments)
    groups = tuple(Group) if options.group == "verify-all" else (Group(options.group),)
    return run(groups, Path(__file__).resolve().parent.parent, options.sequential or len(groups) == 1, options.jobs)


if __name__ == "__main__":
    raise SystemExit(main())
