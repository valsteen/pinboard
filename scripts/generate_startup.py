"""Compose the nine checked-in role/runtime startup documents; --check writes nothing.

Sources use Markdown with optional ``<!-- roles: ... -->`` paragraph scopes.
Composition selects those scopes and resolves instruction links within the complete
document. Other relative links retain their source-owned destination.
"""

import argparse
import os
import re
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from pinboard.adapters import startup_documents
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.application.dispatch_models import NativeRuntime

type Role = startup_documents.StartupRole
type Runtime = NativeRuntime

ROLES: tuple[Role, ...] = ("coordinator", "worker", "reviewer")
RUNTIMES: tuple[Runtime, ...] = ("codex", "claude-code", "antigravity")
PHASES = (
    "status-and-selection",
    "setup-and-transitions",
    "consolidation",
    "preparation-and-dispatch",
    "brief-preservation",
    "review-and-disposition",
    "parallel-work",
    "invocation-evidence",
    "state-and-recovery",
    "engineering-health-baseline",
)
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def scoped(text: str, role: Role) -> str:
    selected = True
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("<!-- roles:"):
            scope = line.removeprefix("<!-- roles: ").removesuffix(" -->\n").removesuffix(" -->")
            match scope:
                case "all":
                    selected = True
                case "launched":
                    selected = role in ("worker", "reviewer")
                case "coordinator worker":
                    selected = role in ("coordinator", "worker")
                case "coordinator" | "worker" | "reviewer":
                    selected = role == scope
                case _:
                    raise ValueError(f"Unknown startup role scope: {line.rstrip()}")
        elif selected:
            lines.append(line)
    return "".join(lines).strip()


def anchor(title: str) -> str:
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def read_section(root: Path, selector: str) -> tuple[Path, str]:
    seam = authority_selector(selector)
    selected = select_checkout_brief_source(root, seam, True)
    if isinstance(selected, BriefSourceFailure):
        raise ValueError(selected.message)
    return root / seam.relative_path, selected.content.decode()


def heading_targets(contents: Sequence[tuple[Path, str]]) -> dict[tuple[Path, str], str]:
    headings: dict[tuple[Path, str], str] = {}
    used: set[str] = set()
    for path, text in contents:
        for line in text.splitlines():
            if line.startswith("#"):
                fragment = anchor(line.lstrip("#"))
                selected = fragment
                suffix = 0
                while selected in used:
                    suffix += 1
                    selected = f"{fragment}-{suffix}"
                used.add(selected)
                headings.setdefault((path, fragment), selected)
    return headings


def startup_contents(root: Path, role: Role, runtime: Runtime) -> list[tuple[Path, str]]:
    skill = root / "skills/pinboard"
    parts = skill / "startup/parts"
    sources = [parts / "common.md", parts / f"{role}.md"]
    if role in ("coordinator", "worker"):
        sources.append(parts / "intake.md")
        if runtime == "codex":
            sources.append(root / "skills/pinboard-intake/references/codex-transport.md")
    if role == "coordinator":
        sources.extend(skill / f"references/{phase}.md" for phase in PHASES)
    elif role == "worker":
        sources.append(skill / "references/engineering-health-baseline.md")
    sources.extend((skill / "references/runtime-adapters.md", skill / f"references/runtime-{runtime}.md"))
    sources.extend(
        (
            root / "skills/repository-readiness/references/developer-navigation.md",
            root / "skills/repository-readiness/references/storytelling-readability.md",
        )
    )
    contents = [(path, scoped(path.read_text(encoding="utf-8"), role)) for path in sources]
    contents.append(
        read_section(root, "skills/slop-cleanup/references/structural-consolidation.md#Choose explicit test seams")
    )
    if role == "coordinator":
        contents.append(
            read_section(
                root, "skills/repository-readiness/references/agentic-readiness.md#Check the seven capabilities"
            )
        )
    return contents


def compose(root: Path, role: Role, runtime: Runtime) -> str:
    skill = root / "skills/pinboard"
    parts = skill / "startup/parts"
    contents = startup_contents(root, role, runtime)
    inline_paths = {path for path, _ in contents}
    headings = heading_targets(contents)
    aliases = {
        skill / "SKILL.md": (parts / "common.md", parts / "coordinator.md"),
        root / "skills/pinboard-deliver/SKILL.md": (parts / "worker.md",),
    }
    output = root / startup_documents.reference(role, runtime)
    missing: list[str] = []

    def resolve(path: Path, match: re.Match[str]) -> str:
        label, target = match.groups()
        if target.startswith(("http://", "https://", "mailto:")):
            return match.group()
        selected, marker, fragment = target.partition("#")
        destination = (path.parent / selected).resolve() if selected else path
        if (
            destination == skill / "SKILL.md"
            or destination == root / "skills/pinboard-deliver/SKILL.md"
            or destination.is_relative_to(skill / "references")
            or destination.is_relative_to(parts)
            or destination in inline_paths
        ):
            candidates = aliases.get(destination, (destination,))
            if not fragment:
                if destination == skill / "SKILL.md":
                    fragment = "keep-the-humans-picture-current"
                else:
                    fragment = next((key for source, key in headings if source == destination), "")
            resolved = [headings[source, fragment] for source in candidates if (source, fragment) in headings]
            if not resolved and not selected and path.is_relative_to(parts):
                resolved = [value for (source, key), value in headings.items() if key == fragment]
            if len(resolved) != 1:
                missing.append(f"{path.relative_to(root)}: {target}")
                return match.group()
            return f"[{label}](#{resolved[0]})"
        relative = Path(os.path.relpath(destination, output.parent)).as_posix()
        return f"[{label}]({relative}{marker}{fragment})"

    body = "\n\n".join(LINK.sub(partial(resolve, path), text) for path, text in contents).rstrip()
    if missing:
        raise ValueError(f"{role}/{runtime} lacks inline sections:\n" + "\n".join(missing))
    return (
        f"# Pinboard {role} startup — {runtime}\n\n"
        "<!-- Generated by scripts/generate_startup.py; edit canonical source parts. -->\n\n"
        f"Role: **{role}**. Runtime: **{runtime}**. Read this complete document before acting. "
        "All binding startup procedures for this role and runtime are inline. Conditional companion skills "
        "retain their named scope. `<launcher-root>` is the plugin root containing `skills/pinboard/startup/`; "
        "resolve it from this file's actual absolute location. This document grants no identity, authority or permissions.\n\n"
        f"{body}\n"
    )


def generate(root: Path, check: bool) -> tuple[Path, ...]:
    changed: list[Path] = []
    for role in ROLES:
        for runtime in RUNTIMES:
            destination = root / startup_documents.reference(role, runtime)
            expected = compose(root, role, runtime).encode()
            if not destination.is_file() or destination.read_bytes() != expected:
                changed.append(destination)
                if not check:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(expected)
    return tuple(changed)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail for missing or stale checked-in startup documents")
    options = parser.parse_args(arguments)
    root = Path(__file__).resolve().parent.parent
    changed = generate(root, options.check)
    if options.check and changed:
        print("Startup documents are missing or stale; run uv run --locked python -m scripts.generate_startup:")
        for path in changed:
            print(path.relative_to(root))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
