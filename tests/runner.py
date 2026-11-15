import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

type TestResult = tuple[Path, subprocess.CompletedProcess[str]]

TEST_ROOT = Path(__file__).parent

# Modules whose tests depend on operating-system behavior: file locks, SQLite contention, link and
# path resolution, signals, child processes, shell scripts, Git, or installed plugin entry points.
# CI runs only these on macOS and every module on Linux. Add a module when it exercises such a contract.
PLATFORM_CONTRACT_MODULES = frozenset(
    {
        "test_artifacts",
        "test_board_views",
        "test_contributor_traces",
        "test_invocation_capture",
        "test_launcher",
        "test_native_checkpoint_effects",
        "test_plugin_packaging",
        "test_project_export",
        "test_root",
        "test_sqlite_concurrency",
        "test_sqlite_store",
        "test_storage_migration",
        "test_worktree_preparation",
    }
)


def _positive_jobs(value: str) -> int:
    jobs = int(value)
    if jobs < 1:
        raise argparse.ArgumentTypeError("jobs must be positive")
    return jobs


def _run_file(path: Path, coverage: bool) -> TestResult:
    command = [sys.executable]
    if coverage:
        command.extend(("-m", "coverage", "run", "--parallel-mode"))
    command.extend(
        (
            "-m",
            "unittest",
            "discover",
            "-v",
            "-s",
            str(path.parent),
            "-t",
            str(path.parent.parent),
            "-p",
            path.name,
        )
    )
    return path, subprocess.run(command, capture_output=True, check=False, text=True)


def run(test_root: Path, *, jobs: int, coverage: bool, modules: frozenset[str] | None) -> int:
    paths = sorted(test_root.glob("test_*.py"))
    if modules is not None:
        if missing := sorted(modules - {path.stem for path in paths}):
            print(f"Selected test modules are missing from {test_root}: {', '.join(missing)}")
            return 1
        paths = [path for path in paths if path.stem in modules]
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [executor.submit(_run_file, path, coverage) for path in paths]
        results = [future.result() for future in futures]

    failed = False
    for path, result in results:
        failed |= result.returncode != 0
        print(f"[{'FAIL' if result.returncode else 'PASS'}] {path.name}")
        if result.returncode:
            for output in (result.stdout, result.stderr):
                if output:
                    print(output, end="" if output.endswith("\n") else "\n")
    return int(failed)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=_positive_jobs, default=min(max(os.process_cpu_count() or 0, 4), 8))
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument(
        "--platform-contract", action="store_true", help="run only the modules in PLATFORM_CONTRACT_MODULES"
    )
    options = parser.parse_args(arguments)
    if options.platform_contract and options.coverage:
        parser.error("--coverage measures the full suite; omit it with --platform-contract")

    coverage_statuses: list[int] = []
    if options.coverage:
        coverage_statuses.append(subprocess.run([sys.executable, "-m", "coverage", "erase"], check=False).returncode)
    modules = PLATFORM_CONTRACT_MODULES if options.platform_contract else None
    test_status = run(TEST_ROOT, jobs=options.jobs, coverage=options.coverage, modules=modules)
    if options.coverage:
        coverage_statuses.extend(
            subprocess.run([sys.executable, "-m", "coverage", command], check=False).returncode
            for command in ("combine", "report")
        )
    return int(test_status != 0 or any(coverage_statuses))


if __name__ == "__main__":
    raise SystemExit(main())
