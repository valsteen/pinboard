import ast
import tempfile
import tomllib
import unittest
from importlib.util import resolve_name
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "pinboard"
FORBIDDEN_DEPENDENCIES = {
    "domain": ("pinboard.adapters", "pinboard.application", "pinboard.cli", "pinboard.mcp"),
    "application": ("pinboard.adapters", "pinboard.cli", "pinboard.mcp"),
    "adapters": ("pinboard.cli", "pinboard.mcp"),
}


def _package(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root)
    return ".".join(("pinboard", *relative.parent.parts))


def _imports(path: Path, source_root: Path = SOURCE_ROOT) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                modules.extend(name.name for name in names)
            case ast.ImportFrom(module=module, level=level):
                if level == 0:
                    imported = module
                else:
                    relative_name = "." * level + (module or "")
                    try:
                        imported = resolve_name(relative_name, _package(path, source_root))
                    except ImportError as error:
                        raise ValueError(f"{path}: invalid relative import {relative_name}") from error
                if imported is not None:
                    modules.append(imported)
                    modules.extend(f"{imported}.{name.name}" for name in node.names if name.name != "*")
            case _:
                continue
    return tuple(modules)


def _violations(source_root: Path) -> list[str]:
    violations: list[str] = []
    for layer, forbidden_prefixes in FORBIDDEN_DEPENDENCIES.items():
        for path in sorted(source_root.joinpath(layer).rglob("*.py")):
            violations.extend(
                f"{path.relative_to(source_root)} imports {imported}"
                for imported in _imports(path, source_root)
                if imported.startswith(forbidden_prefixes)
            )
    return violations


def _cli_cycles(source_root: Path = SOURCE_ROOT) -> tuple[tuple[str, ...], ...]:
    cli_root = source_root / "cli"
    modules = {f"pinboard.cli.{path.stem}": path for path in cli_root.glob("*.py") if path.name != "__init__.py"}
    edges = {
        module: tuple(sorted(value for value in _imports(path, source_root) if value in modules))
        for module, path in modules.items()
    }
    cycles: set[tuple[str, ...]] = set()

    def visit(module: str, path: tuple[str, ...]) -> None:
        if module in path:
            cycle = (*path[path.index(module) :], module)
            rotations = tuple((*cycle[index:-1], *cycle[:index]) for index in range(len(cycle) - 1))
            cycles.add(min(rotations))
            return
        for dependency in edges[module]:
            visit(dependency, (*path, module))

    for module in sorted(modules):
        visit(module, ())
    return tuple(sorted(cycles))


def _sqlite_store_constructors(source_root: Path = SOURCE_ROOT) -> tuple[Path, ...]:
    constructors: list[Path] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constructors.extend(
            path.relative_to(source_root)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SQLiteWorkStore"
        )
    return tuple(constructors)


def _sqlite_store_importers(source_root: Path = SOURCE_ROOT) -> tuple[Path, ...]:
    concrete_module = "pinboard.adapters.sqlite.store"
    return tuple(
        path.relative_to(source_root)
        for path in sorted(source_root.rglob("*.py"))
        if concrete_module in _imports(path, source_root)
    )


def _database_location_literals(source_root: Path = SOURCE_ROOT) -> tuple[Path, ...]:
    owners: list[Path] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owners.extend(
            path.relative_to(source_root)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "state.sqlite3"
        )
    return tuple(owners)


class ArchitectureDependencyTest(unittest.TestCase):
    def test_package_exposes_cli_and_local_stdio_mcp_entrypoints(self) -> None:
        metadata = tomllib.loads((SOURCE_ROOT.parents[1] / "pyproject.toml").read_text())
        self.assertEqual(
            {
                "pinboard": "pinboard.cli.entrypoint:main",
                "pinboard-mcp": "pinboard.mcp.server:main",
            },
            metadata["project"]["scripts"],
        )
        self.assertIn("mcp==2.2.0", metadata["project"]["dependencies"])
        self.assertNotIn("mcp==2.2.0", metadata["dependency-groups"]["dev"])

    def test_outward_relative_import_cannot_bypass_dependency_direction(self) -> None:
        source_root = Path(tempfile.mkdtemp()) / "src" / "pinboard"
        module = source_root / "application" / "probe.py"
        module.parent.mkdir(parents=True)
        module.write_text("from ..adapters import sqlite\n", encoding="utf-8")

        self.assertIn("application/probe.py imports pinboard.adapters", _violations(source_root))

    def test_relative_import_beyond_package_root_is_rejected(self) -> None:
        source_root = Path(tempfile.mkdtemp()) / "src" / "pinboard"
        module = source_root / "application" / "probe.py"
        module.parent.mkdir(parents=True)
        module.write_text("from ...adapters import sqlite\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid relative import"):
            _imports(module, source_root)

    def test_production_layers_preserve_inward_dependency_direction(self) -> None:
        self.assertEqual([], _violations(SOURCE_ROOT))

    def test_cli_composition_is_acyclic_and_the_entrypoint_only_routes(self) -> None:
        self.assertFalse((SOURCE_ROOT / "interfaces").exists())
        self.assertEqual((), _cli_cycles())
        allowed_non_cli = (
            "pinboard.adapters.files.errors",
            "pinboard.adapters.sqlite.errors",
            "pinboard.domain.errors",
        )
        cli_imports = _imports(SOURCE_ROOT / "cli" / "entrypoint.py")
        outward = {
            value
            for value in cli_imports
            if value.startswith(("pinboard.adapters", "pinboard.application", "pinboard.domain"))
        }
        self.assertEqual(
            set(),
            {
                value
                for value in outward
                if not any(value == allowed or value.startswith(f"{allowed}.") for allowed in allowed_non_cli)
            },
        )

    def test_cli_and_mcp_are_independent_sibling_boundaries(self) -> None:
        cli_imports = tuple(
            imported
            for path in (SOURCE_ROOT / "cli").glob("*.py")
            for imported in _imports(path)
            if imported.startswith("pinboard.mcp")
        )
        mcp_imports = tuple(
            imported
            for path in (SOURCE_ROOT / "mcp").glob("*.py")
            for imported in _imports(path)
            if imported.startswith("pinboard.cli")
        )
        self.assertEqual((), cli_imports)
        self.assertEqual((), mcp_imports)

    def test_sqlite_location_and_store_composition_have_one_explicit_owner(self) -> None:
        self.assertEqual((Path("adapters/files/file_io.py"),), _database_location_literals())
        self.assertEqual((Path("cli/work_state_commands.py"), Path("mcp/server.py")), _sqlite_store_importers())
        self.assertEqual((Path("cli/work_state_commands.py"), Path("mcp/server.py")), _sqlite_store_constructors())


if __name__ == "__main__":
    unittest.main()
