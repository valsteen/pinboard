"""Reject match arms and isinstance guards in Pinboard production code that only raise over a closed family.

An arm or guard that raises claims that the members it selects cannot arrive. Production code expresses that claim
by narrowing the type its producer declares, returns a typed result when the producer can return the case, or keeps
the raise as a declared exception at a named decoding or effect boundary. The check cannot see a subject's type, so
it is syntactic and reports two shapes: every match arm, over any subject, whose last statement raises and whose
body cannot return, break or continue; and every `if` statement whose test calls `isinstance`, negated or not, and
whose body cannot complete except by raising `AssertionError`. A guard raising any other exception type, and an
`assert` statement, is left to review. The arms and guards kept at a decoding or effect boundary are listed in
`EXEMPTIONS` with their reason, and an exemption that matches nothing the check reports fails the check so the list
cannot go stale. This module is outside the installed application and runs in process from the `verify-repo` group
of `scripts/verify.py`.
"""

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import override

PRODUCTION_ROOT = Path("src/pinboard")


@dataclass(frozen=True, slots=True)
class RaisingArm:
    module: str
    function: str
    pattern: str
    line: int


@dataclass(frozen=True, slots=True)
class RaisingGuard:
    module: str
    function: str
    test: str
    line: int


@dataclass(frozen=True, slots=True)
class Exemption:
    module: str
    function: str
    pattern: str
    reason: str


_SYMLINK_REASON = "Filesystem effect boundary: a real link outcome stops verification under ARCHITECTURE.md's trusted-local link rule."

EXEMPTIONS = (
    *(
        Exemption(module, function, pattern, _SYMLINK_REASON)
        for module, function in (
            ("adapters/files/file_io.py", "_verify_directory_links"),
            ("adapters/files/root.py", "_verify_checkout_directory_links"),
            ("adapters/files/setting_resolution.py", "verify_directory_links"),
        )
        for pattern in (
            "DanglingSymlink(link=link, target=target)",
            "SymlinkLoop(link=link, target=target)",
            "NonDirectorySymlink(link=link, target=target)",
        )
    ),
    Exemption(
        "adapters/files/root.py",
        "observe_reviewed_diff_at_target",
        "_",
        "Git effect boundary: any exit status other than applied or absent means Git could not check the diff.",
    ),
    Exemption(
        "adapters/sqlite/amendments.py",
        "_stored",
        "_",
        "Stored-row decoding: a partial review base in a stored amendment row is invalid stored state.",
    ),
    Exemption(
        "adapters/sqlite/lifecycle.py",
        "_read_review_event",
        "_",
        "Stored-row decoding: the review walk's query selects only its listed receipt kinds.",
    ),
    Exemption(
        "adapters/sqlite/lifecycle.py",
        "read_parallel_preview_lifecycle",
        "_",
        "Stored-row decoding: an open attempt row must hold a nonterminal attempt state.",
    ),
    Exemption(
        "adapters/sqlite/lifecycle.py",
        "read_attempt_context",
        "_",
        "Stored-row decoding: a nonterminal attempt's stored item state must be live and equal to the attempt state.",
    ),
    Exemption(
        "adapters/sqlite/migration/schema_unblockers_v12.py",
        "classify_holds",
        "_",
        "Predecessor-schema decoding: a v12 held row must record a paused, blocked or deferred item state.",
    ),
    Exemption(
        "cli/migration/schema_migration.py",
        "_authority",
        "_",
        "Stored-schema decoding: a stored schema version outside the supported predecessors names no authority.",
    ),
    Exemption(
        "mcp/contracts.py",
        "decode_transition_request",
        "_",
        "MCP request decoding: the receipt's action kind is caller-supplied text that may name no transition leaf.",
    ),
    Exemption(
        "mcp/execution.py",
        "SemanticCapture.__init__",
        "CaptureUnavailable()",
        "Filesystem effect boundary: a capture directory that cannot be written stops capture startup.",
    ),
    Exemption(
        "mcp/execution.py",
        "SemanticCapture.__init__",
        "CapturePublishedWithWarning(path=path)",
        "Filesystem effect boundary: a capture directory that cannot be synchronized stops capture startup.",
    ),
)


def _always_raises(statements: Sequence[ast.stmt]) -> bool:
    if not statements:
        return False
    last = statements[-1]
    match last:
        case ast.Raise():
            return True
        case ast.With() | ast.AsyncWith():
            return _always_raises(last.body)
        case ast.If():
            return _always_raises(last.body) and _always_raises(last.orelse)
        case _:
            return False


def _raises_assertion(exception: ast.expr | None) -> bool:
    match exception:
        case ast.Name(id="AssertionError") | ast.Call(func=ast.Name(id="AssertionError")):
            return True
        case _:
            return False


def _always_raises_assertion(statements: Sequence[ast.stmt]) -> bool:
    if not statements:
        return False
    last = statements[-1]
    match last:
        case ast.Raise(exc=exception):
            return _raises_assertion(exception)
        case ast.With() | ast.AsyncWith():
            return _always_raises_assertion(last.body)
        case ast.If():
            return _always_raises_assertion(last.body) and _always_raises_assertion(last.orelse)
        case _:
            return False


def _calls_isinstance(test: ast.expr) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
        for node in ast.walk(test)
    )


def _is_nested_scope(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda)


def _local_nodes(statements: Sequence[ast.stmt]) -> Iterator[ast.AST]:
    """Yield the nodes of a block outside nested function and class definitions."""
    pending: list[ast.AST] = [statement for statement in statements if not _is_nested_scope(statement)]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(child for child in ast.iter_child_nodes(node) if not _is_nested_scope(child))


def _cannot_leave(statements: Sequence[ast.stmt]) -> bool:
    return not any(isinstance(node, ast.Return | ast.Break | ast.Continue) for node in _local_nodes(statements))


def _only_raises(case: ast.match_case) -> bool:
    return _always_raises(case.body) and _cannot_leave(case.body)


class _ArmCollector(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.scope: list[str] = []
        self.arms: list[RaisingArm] = []
        self.guards: list[RaisingGuard] = []

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    @override
    def visit_Match(self, node: ast.Match) -> None:
        for case in node.cases:
            if _only_raises(case):
                pattern = ast.unparse(case.pattern)
                if case.guard is not None:
                    pattern = f"{pattern} if {ast.unparse(case.guard)}"
                self.arms.append(RaisingArm(self.module, ".".join(self.scope), pattern, case.pattern.lineno))
        self.generic_visit(node)

    @override
    def visit_If(self, node: ast.If) -> None:
        if _calls_isinstance(node.test) and _always_raises_assertion(node.body) and _cannot_leave(node.body):
            self.guards.append(RaisingGuard(self.module, ".".join(self.scope), ast.unparse(node.test), node.lineno))
        self.generic_visit(node)


def _collect(module: str, source: str) -> _ArmCollector:
    collector = _ArmCollector(module)
    collector.visit(ast.parse(source, filename=module))
    return collector


def raising_arms(module: str, source: str) -> tuple[RaisingArm, ...]:
    """Return every match arm in one module whose body only raises."""
    return tuple(_collect(module, source).arms)


def raising_guards(module: str, source: str) -> tuple[RaisingGuard, ...]:
    """Return every isinstance `if` guard in one module whose body can only raise AssertionError."""
    return tuple(_collect(module, source).guards)


def violations(
    arms: Sequence[RaisingArm], guards: Sequence[RaisingGuard], exemptions: Sequence[Exemption]
) -> tuple[str, ...]:
    """Describe each unexempted raising arm or guard and each exemption that matches nothing reported."""
    exempted = {(value.module, value.function, value.pattern) for value in exemptions}
    reported = {
        *((value.module, value.function, value.pattern) for value in arms),
        *((value.module, value.function, value.test) for value in guards),
    }
    repair = (
        "Narrow what its producer declares, return a typed result, or stop and report; exempt it in "
        "scripts/check_raising_arms.py only when it is a declared exception at a decoding or effect boundary."
    )
    unexempted_arms = tuple(
        f"src/pinboard/{arm.module}:{arm.line}: {arm.function}: match arm `case {arm.pattern}` only raises. {repair}"
        for arm in arms
        if (arm.module, arm.function, arm.pattern) not in exempted
    )
    unexempted_guards = tuple(
        f"src/pinboard/{guard.module}:{guard.line}: {guard.function}: isinstance guard `if {guard.test}` only "
        f"raises AssertionError. {repair}"
        for guard in guards
        if (guard.module, guard.function, guard.test) not in exempted
    )
    stale = tuple(
        f"scripts/check_raising_arms.py: exemption for src/pinboard/{value.module}: {value.function}: "
        f"`{value.pattern}` matches no raising arm or isinstance guard; remove it."
        for value in exemptions
        if (value.module, value.function, value.pattern) not in reported
    )
    return (*unexempted_arms, *unexempted_guards, *stale)


def check_production_tree(root: Path) -> tuple[str, ...]:
    """Check every module under the repository's production package against the declared exemptions."""
    production = root / PRODUCTION_ROOT
    collected = tuple(
        _collect(path.relative_to(production).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(production.rglob("*.py"))
    )
    arms = tuple(arm for collector in collected for arm in collector.arms)
    guards = tuple(guard for collector in collected for guard in collector.guards)
    return violations(arms, guards, EXEMPTIONS)
