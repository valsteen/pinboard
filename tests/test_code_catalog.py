"""Installed code lookup and producer-derived membership."""

import ast
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import msgspec

import pinboard
from pinboard.cli import code_catalog
from pinboard.cli.entrypoint import main
from pinboard.domain.errors import DescribedCode
from pinboard.mcp.contracts import BriefSourcesRejected
from tests.support import JsonObject, JsonValue


def _installed_sources() -> tuple[Path, ...]:
    return tuple(Path(pinboard.__file__).parent.rglob("*.py"))


def _declared_and_emitted_codes() -> set[str]:  # noqa: C901 - independent source completeness scan
    codes: set[str] = set()
    for path in _installed_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name) and node.name.id.endswith("Code"):
                codes.update(
                    value.value
                    for value in ast.walk(node.value)
                    if isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.isupper()
                    and "_" in value.value
                )
            if isinstance(node, ast.ClassDef) and node.name.endswith(("Code", "Reason")):
                for member in node.body:
                    if isinstance(member, ast.Assign):
                        value = member.value.elts[0] if isinstance(member.value, ast.Tuple) else member.value
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            codes.add(value.value)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "code":
                codes.update(
                    value.value
                    for value in ast.walk(node.annotation)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str) and "_" in value.value
                )
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "code":
                        codes.update(
                            literal.value
                            for literal in ast.walk(value)
                            if isinstance(literal, ast.Constant)
                            and isinstance(literal.value, str)
                            and literal.value.isupper()
                            and "_" in literal.value
                        )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id
                in {
                    "_read_failure",
                    "_integration_rejection",
                    "_rejected",
                    "_error_diagnostic",
                    "Diagnostic",
                }
            ):
                codes.update(
                    value.value
                    for argument in node.args
                    for value in ast.walk(argument)
                    if isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.isupper()
                    and "_" in value.value
                )
    return codes


class CodeCatalogTest(unittest.TestCase):
    def entries(self, value: JsonValue) -> list[JsonObject]:
        if not isinstance(value, list):
            self.fail("Catalog entries must be a list")
        entries: list[JsonObject] = []
        for entry in value:
            if not isinstance(entry, dict):
                self.fail("Catalog entry must be an object")
            entries.append(entry)
        return entries

    def run_json(self, *arguments: str) -> tuple[int, JsonObject]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(arguments)
        return status, json.loads(output.getvalue())

    def test_static_discovery_list_exact_and_unknown(self) -> None:
        with patch("pinboard.cli.entrypoint.work_state_commands.resolve_roots", side_effect=AssertionError("rooted")):
            status, tools = self.run_json("tool-contract", "--json")
            self.assertEqual(0, status)
            operations = self.entries(tools["operations"])
            self.assertIn(
                "code-catalog",
                {operation["operation_id"] for operation in operations if isinstance(operation["operation_id"], str)},
            )
            status, catalog = self.run_json("code-catalog", "--json")
            self.assertEqual(0, status)
            entries = self.entries(catalog["codes"])
            self.assertEqual(len(entries), len({entry["code"] for entry in entries}))
            for code in (
                "EXECUTOR_BUSY",
                "BRANCH_OWNER_NOT_FOUND",
                "closure-unknown",
                "inspect",
                "inspect:live-order",
                "startup",
            ):
                with self.subTest(code=code):
                    status, exact = self.run_json("code-catalog", "--json", "--code", code)
                    self.assertEqual(0, status)
                    self.assertEqual(code, exact["code"])
                    self.assertTrue(exact["meaning"])
                    self.assertTrue(exact["recovery"])
            status, busy = self.run_json("code-catalog", "--json", "--code", "EXECUTOR_BUSY")
            self.assertEqual(0, status)
            busy_meaning = busy["meaning"]
            self.assertIsInstance(busy_meaning, str)
            assert isinstance(busy_meaning, str)
            self.assertIn("request did not run", busy_meaning)
            status, unavailable = self.run_json("code-catalog", "--json", "--code", "DISPATCH_ACTION_UNAVAILABLE")
            self.assertEqual(0, status)
            unavailable_meaning = unavailable["meaning"]
            self.assertIsInstance(unavailable_meaning, str)
            assert isinstance(unavailable_meaning, str)
            self.assertIn("not legal for the current attempt", unavailable_meaning)
            status, inspect = self.run_json("code-catalog", "--json", "--code", "inspect")
            self.assertEqual(0, status)
            self.assertEqual("receipt-event", inspect["kind"])
            inspect_meaning = inspect["meaning"]
            self.assertIsInstance(inspect_meaning, str)
            assert isinstance(inspect_meaning, str)
            self.assertIn("proposal intake", inspect_meaning.lower())
            status, publication = self.run_json("code-catalog", "--json", "--code", "ARTIFACT_PUBLICATION_FAILED")
            self.assertEqual(0, status)
            publication_recovery = publication["recovery"]
            self.assertIsInstance(publication_recovery, str)
            assert isinstance(publication_recovery, str)
            self.assertIn("returned resource", publication_recovery)
            self.assertIn("do not replay", publication_recovery)
            status, unknown = self.run_json("code-catalog", "--json", "--code", "NO_SUCH_CODE")
            self.assertEqual(11, status)
            self.assertEqual("TRANSITION_INPUT_INVALID", unknown["code"])
            self.assertEqual("rejected", unknown["status"])
            self.assertEqual("correct-input", unknown["retry"])

    def test_declared_and_emitted_codes_have_exact_lookup(self) -> None:
        status, catalog = self.run_json("code-catalog", "--json")
        self.assertEqual(0, status)
        entries = self.entries(catalog["codes"])
        available = {entry["code"] for entry in entries if isinstance(entry["code"], str)}
        self.assertFalse(_declared_and_emitted_codes() - available)
        self.assertIn("inspect", {entry["code"] for entry in entries if entry["kind"] == "receipt-event"})
        self.assertNotIn("report-blocker", available)
        self.assertFalse([entry["code"] for entry in entries if str(entry["meaning"]).startswith("Pinboard returned ")])

    def test_new_defined_code_does_not_break_existing_lookups(self) -> None:
        class ExtraCode(DescribedCode):
            EXTRA = ("EXTRA", "A newly defined failure.")

        with patch.object(code_catalog, "_FAILURE_ENUMS", (*code_catalog._FAILURE_ENUMS, ExtraCode)):
            entries = {entry.code: entry for entry in code_catalog.installed_code_catalog().codes}
        self.assertEqual("A newly defined failure.", entries["EXTRA"].meaning)
        self.assertEqual("EXECUTOR_BUSY", entries["EXECUTOR_BUSY"].code)

    def test_mcp_code_description_preserves_result_schema(self) -> None:
        schema = msgspec.json.schema(BriefSourcesRejected)
        code_schema = schema["$defs"]["BriefSourcesRejected"]["properties"]["code"]
        self.assertIn("enum", code_schema)


if __name__ == "__main__":
    unittest.main()
