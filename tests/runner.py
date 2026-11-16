import argparse
import ast
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

type TestResult = tuple[Path, subprocess.CompletedProcess[str]]

TEST_ROOT = Path(__file__).parent
DEFAULT_JOBS = min(max(os.process_cpu_count() or 1, 1), 8)
MAX_METHODS_PER_TARGET = 25

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


def _file_targets(test_root: Path, path: Path) -> tuple[int, tuple[tuple[str, ...], ...]]:
    size = path.stat().st_size
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return size * size, ((),)
    methods = tuple(
        f"{test_root.name}.{path.stem}.{node.name}.{member.name}"
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test_")
    )
    method_count = max(len(methods), 1)
    if len(methods) <= MAX_METHODS_PER_TARGET or not (test_root / "__init__.py").is_file():
        return size * (size // method_count), ((),)
    chunk_count = (len(methods) + MAX_METHODS_PER_TARGET - 1) // MAX_METHODS_PER_TARGET
    chunk_size = (len(methods) + chunk_count - 1) // chunk_count
    targets = tuple(methods[index : index + chunk_size] for index in range(0, len(methods), chunk_size))
    return (size // len(targets)) * (size // method_count), targets


def _run_target(path: Path, targets: tuple[str, ...], coverage: bool) -> TestResult:
    command = [sys.executable]
    if coverage:
        command.extend(("-m", "coverage", "run", "--parallel-mode"))
    if targets:
        command.extend(("-m", "unittest", "-v", *targets))
    else:
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
    return path, subprocess.run(command, capture_output=True, check=False, cwd=path.parent.parent, text=True)


def run(test_root: Path, *, jobs: int, coverage: bool, modules: frozenset[str] | None) -> int:
    paths = sorted(test_root.glob("test_*.py"))
    if modules is not None:
        if missing := sorted(modules - {path.stem for path in paths}):
            print(f"Selected test modules are missing from {test_root}: {', '.join(missing)}")
            return 1
        paths = [path for path in paths if path.stem in modules]
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
    plans = {path: _file_targets(test_root, path) for path in paths}
    scheduled_paths = sorted(
        paths,
        key=lambda path: (plans[path][0], len(plans[path][1])),
        reverse=True,
    )
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures_by_path: dict[Path, list[Future[TestResult]]] = {path: [] for path in paths}
        for path in scheduled_paths:
            for targets in plans[path][1]:
                futures_by_path[path].append(executor.submit(_run_target, path, targets, coverage))
        results_by_path = {path: [future.result()[1] for future in futures_by_path[path]] for path in paths}

    failed = False
    for path in paths:
        file_results = results_by_path[path]
        file_failed = any(result.returncode != 0 for result in file_results)
        failed |= file_failed
        print(f"[{'FAIL' if file_failed else 'PASS'}] {path.name}")
        if file_failed:
            for result in file_results:
                if result.returncode:
                    for output in (result.stdout, result.stderr):
                        if output:
                            print(output, end="" if output.endswith("\n") else "\n")
    return int(failed)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=_positive_jobs, default=DEFAULT_JOBS)
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
