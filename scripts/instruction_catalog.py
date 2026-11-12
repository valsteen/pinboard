"""Compile reviewed guidance owners into bounded native-readable documents."""

import ast
import hashlib
import re
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

import msgspec

from pinboard.domain import decision_models

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src/pinboard/instructions"
RUNTIMES = ("codex", "claude-code", "antigravity")
NATIVE_READ_BYTES = 46080
ROLE_BUDGETS = {"coordinator": 16000, "worker": 36000, "reviewer": 18000}
COORDINATOR_ENTRIES = ("pinboard", "pinboard-intake", "pinboard-deliver")


type OwnerKind = Literal[
    "host-core", "tool-refuses", "tool-returns", "action-attached", "launched-role", "delete-or-fold"
]


class Owner(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source: str
    consumers: tuple[str, ...]
    kind: OwnerKind
    clauses: tuple[str, ...]
    includes: tuple[str, ...]
    implementing_tests: tuple[str, ...]


class Composition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    coordinator: tuple[str, ...]
    worker: tuple[str, ...]
    reviewer: tuple[str, ...]
    brief_author_actions: tuple[str, ...]
    integrator_actions: tuple[str, ...]
    selection: str
    pre_claim_route: str
    role_owners: dict[str, tuple[str, ...]]
    activity_owners: dict[str, tuple[str, ...]]


class InstructionSources(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-guidance-classification/v2"]
    owners: dict[str, Owner]
    composition: Composition


class Document(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    path: str
    sha256: str
    size_bytes: int
    owners: tuple[str, ...]


class Catalog(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-instruction-catalog/v1"]
    native_read_bytes: int
    role_budgets: dict[str, int]
    documents: dict[str, Document]


def source() -> InstructionSources:
    return msgspec.json.decode(
        (ROOT / "docs/pinboard-guidance-classification.json").read_bytes(), type=InstructionSources
    )


def _document_budget(key: str) -> int:
    for role, budget in ROLE_BUDGETS.items():
        if key.startswith(f"{role}-"):
            return budget
    if key.startswith(("brief-author-", "integrator-")):
        return ROLE_BUDGETS["coordinator"]
    return NATIVE_READ_BYTES


def compile_documents(table: InstructionSources) -> dict[str, tuple[bytes, tuple[str, ...]]]:
    """One owner supplies every copy; generated copies carry no independent policy."""
    texts = {key: (ROOT / owner.source).read_text() for key, owner in table.owners.items()}
    selections = dict(table.composition.role_owners)
    core = tuple(key for key in table.owners if key.startswith("core."))
    for runtime in RUNTIMES:
        selections[f"coordinator-{runtime}"] = core
    selections.update(table.composition.activity_owners)
    for runtime in RUNTIMES:
        selections[f"brief-author-small-{runtime}"] = tuple(
            key
            for key in table.composition.activity_owners[f"brief-author-{runtime}"]
            if key != "procedure.brief"
            and not key.endswith(".brief-sources")
            and not any("source-plan" in consumer for consumer in table.owners[key].consumers)
        )
    # Action consumers are explicit in the reviewed table. Produce bounded files rather than a phase index.
    for runtime in RUNTIMES:
        for phase in tuple(kind.value for kind in decision_models.ActionKind):
            if decision_models.action_semantics(decision_models.ActionKind(phase)).permitted_roles == (
                decision_models.Role.WORKER,
            ):
                continue
            for small in (False, True):
                for checkpoints in (False, True):
                    keys = tuple(
                        key
                        for key, owner in table.owners.items()
                        if owner.kind not in ("tool-refuses", "tool-returns", "host-core")
                        and any(_applies(c, phase, runtime, small, checkpoints) for c in owner.consumers)
                    )
                    if phase == "prepare":
                        keys = selections[f"brief-author-{'small-' if small else ''}{runtime}"]
                    elif phase == "complete":
                        keys = selections[f"integrator-{runtime}"]
                    selections[
                        f"{phase}-{runtime}-{'small' if small else 'standard'}-{'checkpoints' if checkpoints else 'single'}"
                    ] = keys
    result: dict[str, tuple[bytes, tuple[str, ...]]] = {}
    for key, owners in selections.items():
        owners = tuple(
            dict.fromkeys(included for owner in owners for included in (owner, *table.owners[owner].includes))
        )
        body = ("\n\n".join(texts[owner] for owner in owners) + "\n").encode()
        budget = _document_budget(key)
        if len(body) > budget:
            raise ValueError(f"{key}: {len(body)} bytes exceeds {budget}")
        result[key] = body, owners
    return result


def _applies(consumer: str, phase: str, runtime: str, small: bool, checkpoints: bool) -> bool:
    parts = consumer.split(":")
    if parts[0] not in (phase, "all-actions", "all-attributed-actions", "all-mutating-legal-actions") and not (
        (phase == "inspect" and parts[0] == "native-diagnosis")
        or (phase == "review-candidate" and parts[0] == "review-coordinator")
    ):
        return False
    if parts[0] == "all-mutating-legal-actions":
        try:
            kind = decision_models.ActionKind(phase)
        except ValueError:
            return False
        if decision_models.action_semantics(kind).lifecycle_effect != decision_models.LifecycleEffect.CHANGES_LIFECYCLE:
            return False
    if parts[-1] in RUNTIMES and parts[-1] != runtime:
        return False
    if "declares_checkpoints=true" in parts and not checkpoints:
        return False
    return not ("size_class=standard|campaign" in parts and small)


def expected_files() -> dict[Path, bytes]:
    table = source()
    compiled = compile_documents(table)
    documents: dict[str, Document] = {}
    files: dict[Path, bytes] = {}
    for key, (body, owners) in compiled.items():
        digest = hashlib.sha256(body).hexdigest()
        # Identical applicability selections share one physical document.
        relative = f"{key}.md" if any(key.startswith(f"{role}-") for role in ROLE_BUDGETS) else f"documents/{digest}.md"
        documents[key] = Document(relative, digest, len(body), owners)
        files[OUTPUT / relative] = body
    files[OUTPUT / "catalog.json"] = (
        msgspec.json.format(
            msgspec.json.encode(Catalog("pinboard-instruction-catalog/v1", NATIVE_READ_BYTES, ROLE_BUDGETS, documents)),
            indent=2,
        )
        + b"\n"
    )
    files[ROOT / "skills/pinboard/references/runtime-adapters.md"] = (
        "# Runtime recovery\n\n## Source-worktree setup recovery\n\n"
        + (ROOT / table.owners["shared.setup-recovery"].source).read_text()
        + "\n"
    ).encode()
    return files


def _hook_instruction_bytes(runtime: str) -> int:
    if runtime == "codex":
        return 0
    path = ROOT / "src/pinboard" / ("claude_hook.py" if runtime == "claude-code" else "antigravity_hook.py")
    count = 0
    for function in ast.parse(path.read_text()).body:
        if not isinstance(function, ast.FunctionDef) or function.name != "session_start_main":
            continue
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "context" for target in targets):
                count += sum(
                    len(value.value.encode())
                    for value in ast.walk(node.value)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                )
    return count


def _validate_test_links(table: InstructionSources) -> None:
    selectors: set[str] = set()
    for key, owner in table.owners.items():
        if owner.kind in ("tool-refuses", "tool-returns") and not owner.implementing_tests:
            raise ValueError(f"Mechanized owner has no implementing-test link: {key}")
        selectors.update(owner.implementing_tests)
    files: dict[str, set[str]] = {}
    for selector in selectors:
        relative, separator, target = selector.partition("::")
        if not separator or not relative.startswith("tests/") or not relative.endswith(".py"):
            raise ValueError(f"Invalid implementing-test selector: {selector}")
        if relative not in files:
            files[relative] = {
                f"{node.name}.{method.name}"
                for node in ast.parse((ROOT / relative).read_text()).body
                if isinstance(node, ast.ClassDef)
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        if target not in files[relative]:
            raise ValueError(f"Implementing-test selector does not resolve: {selector}")


def _validate_document_links(path: Path, body: bytes) -> None:
    for selected in re.findall(r"\]\(([^)]+)\)", body.decode()):
        target = urlsplit(selected)
        if target.scheme or not target.path:
            continue
        if not (path.parent / unquote(target.path)).is_file():
            raise ValueError(f"Generated instruction document has an unresolved link: {path}: {selected}")


def validate_generated() -> None:
    table = source()
    _validate_test_links(table)
    for relative in ("server.py", "cancellation_operations.py", "consolidation_operations.py"):
        for node in ast.walk(ast.parse((ROOT / "src/pinboard/mcp" / relative).read_text())):
            if isinstance(node, ast.Call):
                keywords = {value.arg: value.value for value in node.keywords}
                if "name" in keywords and "description" in keywords:
                    key = "tool-contract." + ast.unparse(keywords["name"])
                    if (
                        key in table.owners
                        and ast.literal_eval(keywords["description"]) != (ROOT / table.owners[key].source).read_text()
                    ):
                        raise ValueError(f"Tool description differs from its canonical owner: {key}")
    for path, expected in expected_files().items():
        if not path.is_file() or path.read_bytes() != expected:
            raise ValueError(f"Generated instruction document is stale: {path}; run scripts/generate-instructions.py")
        if path.suffix == ".md":
            _validate_document_links(path, expected)


def _startup_reads(path: Path, runtime: str, seen: set[Path]) -> int:
    """Follow the entry documents' Markdown links and literal native-read paths, counting shared reads once."""
    path = path.resolve()
    if path in seen:
        return 0
    seen.add(path)
    body = path.read_text()
    size = len(body.encode())
    # These instruction wrappers use Markdown, not a structured boundary protocol.
    for selected in re.findall(r"\]\(([^)]+\.md)\)|`([^`]+\.md)`", body):
        relative = (selected[0] or selected[1]).replace("{codex|claude-code|antigravity}", runtime)
        if "://" in relative:
            continue
        target = path.parent / relative
        if any(target.name.endswith(f"-{other}.md") for other in RUNTIMES if other != runtime):
            continue
        size += _startup_reads(target, runtime, seen)
    return size


def validate_startup_composition() -> dict[str, int]:
    """Count actual mandatory source composition, including always-loaded MCP and native hook guidance."""
    server = ast.parse((ROOT / "src/pinboard/mcp/server.py").read_text())
    mcp_bytes = 0
    for node in ast.walk(server):
        if not isinstance(node, ast.Call):
            continue
        values = {value.arg: value.value for value in node.keywords}
        if "instructions" in values:
            mcp_bytes += len(str(ast.literal_eval(values["instructions"])).encode())
        if "meta" in values and ast.literal_eval(values["meta"]) == {"anthropic/alwaysLoad": True}:
            mcp_bytes += len(str(ast.literal_eval(values["description"])).encode())
    sizes: dict[str, int] = {}
    for entry in COORDINATOR_ENTRIES:
        for runtime in RUNTIMES:
            seen: set[Path] = set()
            size = _startup_reads(ROOT / "skills" / entry / "SKILL.md", runtime, seen)
            size += _startup_reads(OUTPUT / f"coordinator-{runtime}.md", runtime, seen)
            key = f"{entry}:{runtime}"
            sizes[key] = size + mcp_bytes + _hook_instruction_bytes(runtime)
            if sizes[key] > ROLE_BUDGETS["coordinator"]:
                raise ValueError(
                    f"Actual {key} coordinator startup is {sizes[key]} bytes; budget is {ROLE_BUDGETS['coordinator']}"
                )
    return sizes


def generate() -> None:
    files = expected_files()
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    for path in OUTPUT.rglob("*.md"):
        if path not in files:
            path.unlink()
