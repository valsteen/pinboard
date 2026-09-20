import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override

from .support import JsonObject, JsonValue

INVENTORY = Path(__file__).parents[1] / "skills" / "slop-cleanup" / "scripts" / "inventory.py"


class SlopCleanupInventoryTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        (self.repository / "src").mkdir()
        (self.repository / "tests").mkdir()
        (self.repository / "assets").mkdir()
        self.write(
            "src/models.py",
            """from enum import Enum
from typing import Literal, assert_never

import msgspec


class PythonMode(Enum):
    LIVE = "live"
    LEGACY = "legacy"


class SingletonMode(Enum):
    ONLY = "only"


class Opened:
    pass


class Closed:
    pass


type Result = Opened | Closed

type ArtifactRole = Literal["requirements", "plan", "design", "evidence"]


class LocalBoundary(msgspec.Struct, tag="local", tag_field="kind"):
    pass


class RemoteBoundary(msgspec.Struct, tag="remote", tag_field="kind"):
    pass


type Boundary = LocalBoundary | RemoteBoundary


def test_support_only() -> None:
    pass


TEST_ONLY_NAMES: tuple[str, ...] = ("value",)


class FirstFactory:
    def build(self) -> str:
        return "first"

    def __str__(self) -> str:
        return "first"


class SecondFactory:
    def build(self) -> str:
        return "second"


class Command:
    def __init__(self, action: object, value: object) -> None:
        self.action = action
        self.value = value


class Action:
    def command(self, value: object) -> Command:
        return Command(self, value)


def direct(value: Result) -> str:
    match value:
        case Opened():
            return "opened"
        case Closed():
            return "closed"


def borrowed(value: Result) -> str:
    match value:
        case Opened():
            return "opened"
        case Closed():
            return "closed"


def equivalent(value: Result) -> str:
    match value:
        case Opened():
            return "same"
        case Closed():
            return "same"


def exhaustive_passthrough(value: Result) -> str:
    match value:
        case Opened() | Closed():
            return str(value)
        case _ as unreachable:
            assert_never(unreachable)


EMBEDDED_SCHEMA = "CREATE VIEW current_items AS SELECT * FROM item_artifacts"
""",
        )
        self.write(
            "src/model.go",
            """package sample

type GoMode int

func runGo() {}
""",
        )
        self.write(
            "src/model.rs",
            """enum RustMode {
    Live,
    Legacy,
}

fn run_rust() {}
""",
        )
        self.write(
            "src/Model.kt",
            """enum class KotlinMode {
    LIVE,
    LEGACY,
}

fun runKotlin() = Unit
""",
        )
        self.write(
            "src/model.ts",
            """enum TypeScriptMode {
  Live,
  Legacy,
}

export function runTypeScript(): void {}
""",
        )
        self.write(
            "src/schema.sql",
            """CREATE TABLE item_artifacts (
    role TEXT NOT NULL CHECK (role IN ('requirements', 'plan', 'design', 'evidence'))
);
CREATE INDEX item_artifacts_role ON item_artifacts(role);
""",
        )
        self.write(
            "tests/test_models.py",
            """from models import FirstFactory, SecondFactory, TEST_ONLY_NAMES, test_support_only


def test_helper() -> None:
    test_support_only()
    assert TEST_ONLY_NAMES
    assert FirstFactory().build()
    assert SecondFactory().build()
""",
        )
        (self.repository / "assets" / "orphan.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        self.git("init")
        self.git("add", ".")
        self.write("src/untracked.rs", "fn untracked_probe() {}\n")

    def write(self, relative_path: str, content: str) -> None:
        (self.repository / relative_path).write_text(content, encoding="utf-8")

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def inventory(self, mode: str) -> JsonObject:
        result = subprocess.run(
            [
                sys.executable,
                str(INVENTORY),
                "--repository",
                str(self.repository),
                "--production-root",
                "src",
                "--test-root",
                "tests",
                "--mode",
                mode,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
        self.assertIsInstance(value, dict)
        return value

    def inventory_to_file(self, mode: str, output: Path) -> JsonObject:
        result = subprocess.run(
            [
                sys.executable,
                str(INVENTORY),
                "--repository",
                str(self.repository),
                "--production-root",
                "src",
                "--test-root",
                "tests",
                "--mode",
                mode,
                "--output",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        receipt = json.loads(result.stdout)
        self.assertIsInstance(receipt, dict)
        return receipt

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def json_objects(self, value: JsonValue) -> list[JsonObject]:
        if not isinstance(value, list):
            self.fail("JSON value must be a list")
        return [self.json_object(item) for item in value]

    def json_strings(self, value: JsonValue) -> list[str]:
        if not isinstance(value, list):
            self.fail("JSON value must be a string list")
        return [self.json_string(item) for item in value]

    def json_string(self, value: JsonValue) -> str:
        if not isinstance(value, str):
            self.fail("JSON value must be a string")
        return value

    def json_int(self, value: JsonValue) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            self.fail("JSON value must be an integer")
        return value

    def test_generic_mode_finds_portable_inventory_and_schema_residue(self) -> None:
        report = self.inventory("generic")

        self.assertEqual("slop-cleanup-inventory/v2", report["schema"])
        self.assertEqual("generic", report["mode"])
        summary = self.json_object(report["summary"])
        self.assertEqual(
            {"go", "kotlin", "python", "rust", "sql", "typescript"},
            set(self.json_strings(summary["languages"])),
        )
        declarations = self.json_objects(report["declarations"])
        declaration_names = {self.json_string(declaration["name"]) for declaration in declarations}
        self.assertTrue(
            {
                "PythonMode",
                "test_support_only",
                "GoMode",
                "runGo",
                "RustMode",
                "run_rust",
                "KotlinMode",
                "runKotlin",
                "TypeScriptMode",
                "runTypeScript",
                "untracked_probe",
            }
            <= declaration_names
        )
        closed_families = self.json_objects(report["closed_families"])
        family_names = {self.json_string(family["name"]) for family in closed_families}
        self.assertTrue(
            {"PythonMode", "RustMode", "KotlinMode", "TypeScriptMode", "item_artifacts.role"} <= family_names
        )
        schema_objects = {
            (self.json_string(schema_object["kind"]), self.json_string(schema_object["name"]))
            for schema_object in self.json_objects(report["schema_objects"])
        }
        self.assertEqual(
            {("index", "item_artifacts_role"), ("table", "item_artifacts"), ("view", "current_items")},
            schema_objects,
        )
        candidates = self.json_object(report["candidates"])
        self.assertIn(
            "src/models.py::test_support_only",
            {
                self.json_string(candidate["selector"])
                for candidate in self.json_objects(candidates["test_only_definitions"])
            },
        )
        self.assertIn(
            "src/models.py::TEST_ONLY_NAMES",
            {
                self.json_string(candidate["selector"])
                for candidate in self.json_objects(candidates["test_only_definitions"])
            },
        )
        duplicated_vocabularies = [
            set(self.json_strings(candidate["families"]))
            for candidate in self.json_objects(candidates["duplicated_closed_vocabularies"])
        ]
        self.assertIn(
            {
                "src/Model.kt::KotlinMode",
                "src/model.rs::RustMode",
                "src/model.ts::TypeScriptMode",
                "src/models.py::PythonMode",
            },
            duplicated_vocabularies,
        )
        self.assertEqual(
            ["src/models.py::SingletonMode"],
            [
                self.json_string(candidate["selector"])
                for candidate in self.json_objects(candidates["singleton_closed_families"])
            ],
        )
        ambiguous_groups = {
            self.json_string(candidate["name"]): candidate
            for candidate in self.json_objects(candidates["ambiguous_zero_production_definitions"])
        }
        build_group = ambiguous_groups["build"]
        self.assertEqual(2, len(self.json_objects(build_group["definitions"])))
        self.assertGreater(self.json_int(build_group["test_uses"]), 0)
        self.assertNotIn(
            "src/models.py::__str__",
            {
                self.json_string(candidate["selector"])
                for candidate in self.json_objects(candidates["unreferenced_definitions"])
            },
        )
        self.assertIn(
            "src/models.py::__str__",
            {
                self.json_string(candidate["selector"])
                for candidate in self.json_objects(candidates["implicit_protocol_definitions"])
            },
        )
        self.assertEqual(["assets/orphan.png"], self.json_strings(candidates["unreferenced_assets"]))
        coverage = {
            self.json_string(item["category"]): self.json_string(item["status"])
            for item in self.json_objects(report["coverage"])
        }
        self.assertEqual("complete", coverage["repository-files"])
        self.assertEqual("partial", coverage["generic-declarations"])
        self.assertEqual("unsupported", coverage["python-ast"])
        self.assertEqual("unsupported", coverage["semantic-producer-consumer"])
        self.assertTrue(
            all(
                self.json_string(item["disposition_method"])
                for item in self.json_objects(report["coverage"])
                if self.json_string(item["status"]) != "complete"
            )
        )
        semantic_disposition = self.json_object(report["semantic_disposition"])
        self.assertTrue(semantic_disposition["candidate_is_not_defect"])
        self.assertEqual(
            {
                "boundary",
                "consolidation",
                "removal",
                "retained-exception",
                "supported-root",
            },
            set(self.json_strings(semantic_disposition["terminal_dispositions"])),
        )
        self.assertEqual(
            {
                "cartesian-variant-growth",
                "colliding-complete-names",
                "detached-relational-roles",
                "missing-or-misused-nominal-identifiers",
                "repeated-invariant-enforcement",
                "speculative-obligations",
                "synonymous-representations",
                "unsupported-compatibility",
            },
            {
                self.json_string(category["category"])
                for category in self.json_objects(semantic_disposition["categories"])
            },
        )
        self.assertEqual([], candidates["trivial_callable_bodies"])
        self.assertEqual([], candidates["equivalent_match_arms"])
        self.assertEqual([], candidates["duplicated_match_structures"])

    def test_python_ast_mode_adds_exact_python_evidence_without_dropping_generic_evidence(self) -> None:
        generic_report = self.inventory("generic")
        ast_report = self.inventory("python-ast")

        self.assertEqual(generic_report["input_digest"], ast_report["input_digest"])
        self.assertEqual(generic_report["roots"], ast_report["roots"])
        self.assertEqual(generic_report["semantic_disposition"], ast_report["semantic_disposition"])
        self.assertEqual(generic_report["schema_objects"], ast_report["schema_objects"])
        ast_families = {
            self.json_string(family["name"]): set(self.json_strings(family["atoms"]))
            for family in self.json_objects(ast_report["closed_families"])
            if self.json_string(family["language"]) == "python"
        }
        self.assertEqual({"LIVE", "LEGACY"}, ast_families["PythonMode"])
        self.assertEqual({"live", "legacy"}, ast_families["PythonMode.value"])
        self.assertEqual({"Opened", "Closed"}, ast_families["Result"])
        self.assertEqual(
            {"requirements", "plan", "design", "evidence"},
            ast_families["ArtifactRole"],
        )
        self.assertEqual({"local", "remote"}, ast_families["Boundary.kind"])
        candidates = self.json_object(ast_report["candidates"])
        self.assertEqual(
            ["src/models.py::SingletonMode"],
            [
                self.json_string(candidate["selector"])
                for candidate in self.json_objects(candidates["singleton_closed_families"])
            ],
        )
        declaration_only_atoms = {
            (
                self.json_string(candidate["family"]),
                self.json_string(candidate["atom"]),
            )
            for candidate in self.json_objects(
                candidates["closed_atoms_without_apparent_non_declaration_production_use"]
            )
        }
        self.assertIn(("src/models.py::ArtifactRole", "plan"), declaration_only_atoms)
        self.assertIn(("src/models.py::PythonMode.value", "legacy"), declaration_only_atoms)
        self.assertIn(("src/models.py::Boundary.kind", "remote"), declaration_only_atoms)
        trivial_callables = self.json_objects(candidates["trivial_callable_bodies"])
        self.assertIn(
            ("src/models.py::Action.command", "Command(self, value)"),
            {
                (self.json_string(candidate["selector"]), self.json_string(candidate["expression"]))
                for candidate in trivial_callables
            },
        )
        equivalent_matches = self.json_objects(candidates["equivalent_match_arms"])
        self.assertIn(
            {"Opened()", "Closed()"},
            [set(self.json_strings(candidate["patterns"])) for candidate in equivalent_matches],
        )
        exhaustive_passthroughs = self.json_objects(candidates["exhaustive_passthrough_matches"])
        self.assertEqual(
            ["src/models.py::exhaustive_passthrough"],
            [self.json_string(candidate["selector"]) for candidate in exhaustive_passthroughs],
        )
        duplicated_matches = self.json_objects(candidates["duplicated_match_structures"])
        self.assertIn(
            {"src/models.py::direct", "src/models.py::borrowed"},
            [set(self.json_strings(candidate["selectors"])) for candidate in duplicated_matches],
        )
        coverage = {
            self.json_string(item["category"]): self.json_string(item["status"])
            for item in self.json_objects(ast_report["coverage"])
        }
        self.assertEqual("complete", coverage["python-ast"])
        self.assertEqual("complete", coverage["python-structural-smells"])
        self.assertEqual("partial", coverage["generic-declarations"])
        self.assertEqual("unsupported", coverage["semantic-producer-consumer"])

    def test_missing_inventory_root_fails_instead_of_reporting_empty_coverage(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(INVENTORY),
                "--repository",
                str(self.repository),
                "--production-root",
                "missing",
                "--mode",
                "generic",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("inventory root does not exist: missing", result.stderr)

    def test_output_file_keeps_full_report_and_prints_only_a_compact_receipt(self) -> None:
        output = self.repository / "generic-report.json"

        receipt = self.inventory_to_file("generic", output)
        report = self.json_object(json.loads(output.read_text(encoding="utf-8")))

        self.assertEqual("slop-cleanup-inventory-receipt/v2", receipt["schema"])
        self.assertEqual(str(output.resolve()), receipt["output"])
        self.assertEqual(report["mode"], receipt["mode"])
        self.assertEqual(report["input_digest"], receipt["input_digest"])
        self.assertEqual(report["summary"], receipt["summary"])
        expected_candidate_counts: dict[str, int] = {}
        for name, candidates in self.json_object(report["candidates"]).items():
            if not isinstance(candidates, list):
                self.fail("candidate collection must be a list")
            expected_candidate_counts[name] = len(candidates)
        self.assertEqual(expected_candidate_counts, receipt["candidate_counts"])
        self.assertNotIn("declarations", receipt)


if __name__ == "__main__":
    unittest.main()
