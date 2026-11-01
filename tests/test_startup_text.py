"""Selected startup pages preserve exact bytes, identities and read-only native effects."""

import asyncio
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

import msgspec
from mcp import Client
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import MCPServer
from mcp_types import ClientCapabilities, Implementation, InitializeRequestParams, TextContent

from pinboard.adapters.files import git_config, host_settings
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.application import artifact_identity
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import common, contract_schemas, contracts, execution, host_profiles, server, startup_text
from tests.artifact_support import write_revision
from tests.checkpoint_support import CheckpointPackageSupport
from tests.support import SQLITE_NOW

DUAL = host_profiles.Representation.STRUCTURED_AND_TEXT
MEASURED_CLIENTS = (
    ("claude-code", "2.1.293", host_profiles.HostProfile.CLAUDE_CODE),
    ("codex-mcp-client", "0.164.0", host_profiles.HostProfile.CODEX),
    ("antigravity-client", "v1.0.0", host_profiles.HostProfile.ANTIGRAVITY),
)


def client_params(name: str, version: str) -> InitializeRequestParams:
    return InitializeRequestParams(
        protocol_version="2025-11-25",
        capabilities=ClientCapabilities(),
        client_info=Implementation(name=name, version=version),
    )


class StartupTextTest(CheckpointPackageSupport):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "selected.txt"
        self.request: dict[str, contracts.JsonValue] = {
            "operation": "file-start",
            "project_root": str(self.root),
            "work_root": str(self.root / "absent"),
            "path": str(self.path),
            "max_batch_bytes": 4000,
        }

    def call(
        self,
        request: dict[str, contracts.JsonValue],
        profile: host_profiles.HostProfile = host_profiles.HostProfile.UNMEASURED,
    ) -> dict[str, contracts.JsonValue]:
        result = startup_text.read({"request": request}, profile, execution.CancellationToken())
        return contract_schemas.validate_result("pinboard_startup_text", result.content)

    def test_complete_unicode_escaping_long_lines_and_empty_inputs(self) -> None:
        for text in ('{"long":"' + 'café 音\\"\n' * 4000 + '"}', "short\n", ""):
            with self.subTest(size=len(text)):
                self.path.write_bytes(text.encode())
                request = self.request
                parts: list[str] = []
                start = 0
                while True:
                    page = self.call(request)
                    self.assertEqual("ok", page["status"], page)
                    self.assertLessEqual(host_profiles.envelope_size(page, DUAL), 4000)
                    self.assertEqual(start, page["start_character"])
                    source = page["source"]
                    assert isinstance(source, dict)
                    self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), source["sha256"])
                    self.assertEqual(len(text.encode()), source["size_bytes"])
                    chunk = page["text"]
                    assert isinstance(chunk, str)
                    parts.append(chunk)
                    start += len(chunk)
                    self.assertEqual(start, page["end_character"])
                    continuation = page["continuation"]
                    if continuation is None:
                        break
                    assert isinstance(continuation, dict)
                    request = continuation
                self.assertEqual(text.encode(), "".join(parts).encode())
                self.assertFalse((self.root / "absent").exists())

    def test_changed_source_stale_continuation_and_invalid_input(self) -> None:
        self.path.write_text("abcdef" * 4000)
        first = self.call(self.request)
        continuation = first["continuation"]
        assert isinstance(continuation, dict)
        self.path.write_text("other" * 4000)
        self.assertEqual("rejected", self.call(continuation)["status"])
        self.path.write_text("abcdef" * 4000)
        for change in ({"start_character": 999999}, {"start_character": 0}, {"extra": True}):
            with self.subTest(change=change):
                self.assertEqual("rejected", self.call(continuation | change)["status"])
        for change in (
            {"path": "relative"},
            {"max_batch_bytes": 0},
            {"max_batch_bytes": "host"},
            {"max_batch_bytes": True},
            {"operation": "unknown"},
        ):
            with self.subTest(change=change):
                self.assertEqual("rejected", self.call(self.request | change)["status"])
        self.path.write_bytes(b"\xff")
        self.assertEqual("rejected", self.call(self.request)["status"])
        self.path.unlink()
        self.assertEqual("rejected", self.call(self.request)["status"])

    def test_budget_accounts_for_metadata_and_sdk_duplication(self) -> None:
        self.path.write_text("x" * 10000)
        rejected = self.call(self.request | {"max_batch_bytes": 1})
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("max_batch_bytes", rejected["resource"])
        page = self.call(self.request)
        self.assertLess(len(msgspec.json.encode(page)), host_profiles.envelope_size(page, DUAL))
        self.assertGreater(len(msgspec.json.encode([page, page, page])), 4000)
        invalid = dict(page)
        invalid["end_character"] = 0
        with self.assertRaises(msgspec.ValidationError):
            contract_schemas.validate_result("pinboard_startup_text", invalid)

    def test_exact_accepted_artifact_tuple_and_bytes_are_required(self) -> None:
        fixture = self.checkpoint_fixture()
        store = fixture.store
        reference = store.read_latest_artifact_reference(work_models.ArtifactKind.BRIEF, fixture.brief.attempt_id)
        assert reference is not None
        source: dict[str, contracts.JsonValue] = {
            "kind": "artifact",
            "artifact_ref_id": int(reference.artifact_ref_id),
            "selector": reference.selector,
            "sha256": reference.content_sha256,
            "size_bytes": reference.size_bytes,
        }
        request: dict[str, contracts.JsonValue] = {
            "operation": "artifact-start",
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "source": source,
            "max_batch_bytes": 8000,
        }
        self.assertEqual("ok", self.call(request)["status"])
        for change in (
            {"artifact_ref_id": 999999},
            {"selector": "different"},
            {"sha256": "0" * 64},
            {"size_bytes": 1},
        ):
            with self.subTest(change=change):
                self.assertEqual("rejected", self.call(request | {"source": source | change})["status"])
        path = fixture.work / reference.selector
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        self.assertEqual("rejected", self.call(request)["status"])
        self.assertEqual(original + b" ", path.read_bytes())

    def test_enabled_capture_never_writes_or_resolves_startup_settings(self) -> None:
        self.path.write_text("exact selected text")
        directory = self.root / "traces"
        directory.mkdir()
        for capture in (execution.SemanticCapture(directory), execution.AutomaticCapture(common.select_capture_item)):
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            transport = server.create_server(
                executor, execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256), capture
            )

            async def scenario(selected_transport: MCPServer) -> None:
                async with Client(selected_transport) as client:
                    result = await client.call_tool("pinboard_startup_text", {"request": self.request})
                    self.assertFalse(result.is_error)
                    self.assertEqual("ok", result.structured_content["status"])

            try:
                asyncio.run(scenario(transport))
            finally:
                executor.shutdown()
        self.assertEqual([], list(directory.iterdir()))
        self.assertFalse((self.root / "absent").exists())

    def test_codex_worker_and_reviewer_artifacts_reduce_actual_sdk_transport(self) -> None:  # noqa: PLR0915 - one paired SDK paging and artifact-rejection journey
        line = (
            '{"requirement":"Preserve complete accepted scope, independent review, explicit human authority '
            'and exact UTF-8 bytes.","text":"Line one\\nLine two"}\n'
        )
        payload = (line * 2000).encode()[:189728]
        self.assertEqual(
            "377a779a8662c012c03c49444540ba92372d36ba77229cfb9254044440b7b055", hashlib.sha256(payload).hexdigest()
        )
        fixture = self.checkpoint_fixture()
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        transport = server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=2, line_limit=256))
        self.addCleanup(executor.shutdown)

        async def consume(source: dict[str, contracts.JsonValue], dual: bool) -> tuple[int, int]:
            request: dict[str, contracts.JsonValue] = {
                "operation": "artifact-start",
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "source": source,
                "max_batch_bytes": 20000,
            }
            parts: list[str] = []
            byte_count = 0
            position = 0
            async with Client(
                transport, client_info=Implementation(name="codex-mcp-client", version="0.164.0"), mode="legacy"
            ) as client:
                while True:
                    result = await client.call_tool("pinboard_startup_text", {"request": request})
                    sent = result.model_dump_json(by_alias=True, exclude_none=True).encode()
                    self.assertLessEqual(len(sent), 20000)
                    byte_count += len(sent)
                    page = result.structured_content
                    self.assertEqual("ok", page["status"])
                    self.assertEqual(source, page["source"])
                    self.assertEqual(position, page["start_character"])
                    parts.append(page["text"])
                    position += len(parts[-1])
                    self.assertEqual(position, page["end_character"])
                    self.assertEqual("delivery-only", page["authority"])
                    self.assertFalse(page["state_changed"])
                    if dual:
                        companion = result.content[0]
                        assert isinstance(companion, TextContent)
                        self.assertEqual(page, msgspec.json.decode(companion.text))
                    else:
                        self.assertEqual([], result.content)
                    if page["continuation"] is None:
                        break
                    request = page["continuation"]
                self.assertEqual(payload, "".join(parts).encode())
                for changed in (
                    {"artifact_ref_id": 999999},
                    {"selector": "unavailable"},
                    {"sha256": "0" * 64},
                    {"size_bytes": 1},
                ):
                    rejected = await client.call_tool(
                        "pinboard_startup_text",
                        {
                            "request": {
                                "operation": "artifact-start",
                                "project_root": str(fixture.project),
                                "work_root": str(fixture.work),
                                "source": source | changed,
                                "max_batch_bytes": 20000,
                            }
                        },
                    )
                    self.assertEqual("rejected", rejected.structured_content["status"])
                    self.assertEqual("unchanged", rejected.structured_content["effect"])
                    self.assertEqual("correct-input", rejected.structured_content["retry"])
            return len(parts), byte_count

        with patch.dict(os.environ, {"HOME": str(self.root / "home")}):
            for role in ("worker", "reviewer"):
                published = write_revision(
                    resolve_durable_roots(fixture.project),
                    NewArtifact(work_models.ArtifactKind.EVIDENCE, f"{role}-prompt", 1, ".txt", payload),
                )
                accepted = fixture.store.accept_artifact_reference(
                    fixture.work,
                    published,
                    SQLITE_NOW,
                    ownership=artifact_identity.AttemptMembership(
                        AttemptId(fixture.brief.attempt_id), artifact_identity.AttemptRole.SOURCE
                    ),
                )
                assert not isinstance(accepted, DecisionFailure)
                reference = accepted.reference
                source: dict[str, contracts.JsonValue] = {
                    "kind": "artifact",
                    "artifact_ref_id": int(reference.artifact_ref_id),
                    "selector": reference.selector,
                    "sha256": reference.content_sha256,
                    "size_bytes": reference.size_bytes,
                }
                with self.subTest(role=role):
                    # Freeze the accepted base's only changed decision, keeping the same SDK and artifact.
                    with patch.object(host_profiles, "representation", return_value=DUAL):
                        baseline = asyncio.run(consume(source, True))
                    candidate = asyncio.run(consume(source, False))
                    self.assertLess(candidate[0], baseline[0])
                    self.assertLess(candidate[1], baseline[1])
                    print(f"{role} SDK baseline calls/bytes={baseline}; candidate={candidate}")

    def test_high_level_sdk_server_metadata_fits_the_complete_page_budget(self) -> None:
        self.path.write_text("x" * 17000)
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        transport = server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=2, line_limit=256))

        async def scenario() -> None:
            async with Client(transport) as client:
                result = await client.call_tool("pinboard_startup_text", {"request": self.request})
                self.assertLessEqual(len(result.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000)
                self.assertEqual("ok", result.structured_content["status"])

        try:
            asyncio.run(scenario())
        finally:
            executor.shutdown()

    def test_native_sdk_pages_fit_budget_without_capture_settings(self) -> None:
        text = '"\\café 音\n' * 1000
        self.path.write_text(text)

        async def scenario() -> None:
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "pinboard.mcp"], env=os.environ.copy()
            )
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()
                tool = next(tool for tool in (await session.list_tools()).tools if tool.name == "pinboard_startup_text")
                assert tool.annotations is not None
                self.assertTrue(tool.annotations.read_only_hint)
                request = self.request
                parts: list[str] = []
                while True:
                    result = await session.call_tool("pinboard_startup_text", {"request": request})
                    self.assertFalse(result.is_error)
                    self.assertLessEqual(len(result.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000)
                    page = result.structured_content
                    assert isinstance(page, dict)
                    self.assertEqual("ok", page["status"], page)
                    companion = result.content[0]
                    assert isinstance(companion, TextContent)
                    self.assertEqual(page, msgspec.json.decode(companion.text))
                    parts.append(page["text"])
                    if page["continuation"] is None:
                        break
                    request = page["continuation"]
                self.assertEqual(text.encode(), "".join(parts).encode())
                rejected = await session.call_tool(
                    "pinboard_startup_text", {"request": self.request | {"path": str(self.root / "missing")}}
                )
                self.assertLessEqual(len(rejected.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000)
                self.assertEqual("rejected", rejected.structured_content["status"])

        asyncio.run(scenario())
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(text, self.path.read_text())


class HostProfileTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = patch.dict(os.environ, {"HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.settings = self.root / "home" / ".config" / "pinboard" / "hosts.config"
        self.path = self.root / "selected.txt"
        self.path.write_text("Pinboard guidance line with ordinary prose.\n" * 3000)
        self.request: dict[str, contracts.JsonValue] = {
            "operation": "file-start",
            "project_root": str(self.root),
            "work_root": str(self.root / "absent"),
            "path": str(self.path),
            "max_batch_bytes": "host-profile",
        }

    def read(
        self, request: dict[str, contracts.JsonValue], profile: host_profiles.HostProfile
    ) -> dict[str, contracts.JsonValue]:
        result = startup_text.read({"request": request}, profile, execution.CancellationToken())
        return contract_schemas.validate_result("pinboard_startup_text", result.content)

    def test_recognized_hosts_select_their_profile_at_every_version_and_others_stay_unmeasured(self) -> None:
        for name, version, profile in MEASURED_CLIENTS:
            for observed in (version, "3.0.0", "0.1.0-beta"):
                with self.subTest(name=name, version=observed):
                    self.assertEqual(profile, host_profiles.select(client_params(name, observed)))
        for name, version in (("Claude Code", "2.1.293"), ("mcp", "0.1.0"), ("claude-code", "")):
            with self.subTest(name=name, version=version):
                self.assertEqual(
                    host_profiles.HostProfile.UNMEASURED, host_profiles.select(client_params(name, version))
                )
        self.assertEqual(host_profiles.HostProfile.UNMEASURED, host_profiles.select(None))
        self.assertEqual(8000, host_profiles.measured_page_bytes(host_profiles.HostProfile.UNMEASURED))

    def test_pages_fit_the_effective_budget_and_continuations_preserve_it(self) -> None:
        for profile in host_profiles.HostProfile:
            ceiling = host_profiles.measured_page_bytes(profile)
            representation = host_profiles.representation(profile)
            for requested, effective in (("host-profile", ceiling), (ceiling + 5000, ceiling), (3000, 3000)):
                with self.subTest(profile=profile, requested=requested):
                    request = self.request | {"max_batch_bytes": requested}
                    parts: list[str] = []
                    while True:
                        page = self.read(request, profile)
                        self.assertEqual("ok", page["status"], page)
                        self.assertEqual(effective, page["max_batch_bytes"])
                        self.assertLessEqual(host_profiles.envelope_size(page, representation), effective)
                        chunk = page["text"]
                        assert isinstance(chunk, str)
                        parts.append(chunk)
                        continuation = page["continuation"]
                        if continuation is None:
                            break
                        assert isinstance(continuation, dict)
                        self.assertEqual(effective, continuation["max_batch_bytes"])
                        request = continuation
                    self.assertEqual(self.path.read_text(), "".join(parts))
                    if effective == ceiling and len(parts) > 1:
                        self.assertGreater(
                            host_profiles.envelope_size(page | {"text": parts[0]}, representation), effective // 2
                        )

    def test_a_continuation_from_a_larger_profile_is_bounded_by_the_current_one(self) -> None:
        first = self.read(self.request, host_profiles.HostProfile.CLAUDE_CODE)
        continuation = first["continuation"]
        assert isinstance(continuation, dict)
        page = self.read(continuation, host_profiles.HostProfile.UNMEASURED)
        self.assertEqual(8000, page["max_batch_bytes"])
        self.assertLessEqual(host_profiles.envelope_size(page, DUAL), 8000)

    def test_codex_sdk_preserves_edge_inputs_and_rejection_diagnostics(self) -> None:
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        transport = server.create_server(executor, execution.Diagnostics(io.StringIO(), event_limit=2, line_limit=256))
        self.addCleanup(executor.shutdown)

        async def scenario() -> None:
            async with Client(
                transport, client_info=Implementation(name="codex-mcp-client", version="0.164.0"), mode="legacy"
            ) as client:
                for text in ('"\\café 音\n' * 4000, 'é音\\"' * 20000, "short", ""):
                    self.path.write_bytes(text.encode())
                    request = self.request | {"max_batch_bytes": 4000}
                    parts: list[str] = []
                    while True:
                        result = await client.call_tool("pinboard_startup_text", {"request": request})
                        self.assertEqual([], result.content)
                        self.assertLessEqual(
                            len(result.model_dump_json(by_alias=True, exclude_none=True).encode()), 4000
                        )
                        page = result.structured_content
                        self.assertEqual("ok", page["status"])
                        self.assertEqual(sum(map(len, parts)), page["start_character"])
                        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), page["source"]["sha256"])
                        parts.append(page["text"])
                        if page["continuation"] is None:
                            break
                        request = page["continuation"]
                    self.assertEqual(text.encode(), "".join(parts).encode())

                self.path.write_text("long" * 10000)
                first = await client.call_tool("pinboard_startup_text", {"request": self.request})
                continuation = first.structured_content["continuation"]
                self.path.write_text("changed")
                requests = (
                    continuation,
                    continuation | {"start_character": 999999},
                    continuation | {"extra": True},
                    self.request | {"max_batch_bytes": 1},
                    self.request | {"path": str(self.root / "missing")},
                )
                for request in requests:
                    with patch.object(host_profiles, "representation", return_value=DUAL):
                        baseline = await client.call_tool("pinboard_startup_text", {"request": request})
                    candidate = await client.call_tool("pinboard_startup_text", {"request": request})
                    self.assertEqual([], candidate.content)
                    self.assertEqual(baseline.structured_content, candidate.structured_content)
                    self.assertEqual("rejected", candidate.structured_content["status"])
                    self.assertEqual("unchanged", candidate.structured_content["effect"])
                    self.assertTrue(candidate.structured_content["recovery"])
                    # After the changed-source case, test malformed ranges against the original bytes.
                    self.path.write_text("long" * 10000)
                arguments = {
                    "request": {
                        "operation": "starter",
                        "boundary": "local",
                        "project_root": str(self.root),
                        "work_root": str(self.root / "absent"),
                    }
                }
                ordinary = await client.call_tool("pinboard_brief_contract", arguments)
                companion = ordinary.content[0]
                assert isinstance(companion, TextContent)
                self.assertEqual(ordinary.structured_content, msgspec.json.decode(companion.text))
                self.settings.write_text('[host "codex"]\nstartup-page-bytes = broken\n')
                with patch.object(host_profiles, "representation", return_value=DUAL):
                    baseline = await client.call_tool("pinboard_startup_text", {"request": self.request})
                candidate = await client.call_tool("pinboard_startup_text", {"request": self.request})
                self.assertEqual([], candidate.content)
                self.assertEqual(baseline.structured_content, candidate.structured_content)
                self.assertEqual("HOST_PAGE_SETTING_UNAVAILABLE", candidate.structured_content["code"])
                self.assertEqual("unchanged", candidate.structured_content["effect"])

        asyncio.run(scenario())

    def test_initialize_client_info_selects_budget_and_representation(self) -> None:
        clients = (
            *MEASURED_CLIENTS,
            ("unlisted-host", "1.0.0", host_profiles.HostProfile.UNMEASURED),
            ("codex-mcp-client", "", host_profiles.HostProfile.UNMEASURED),
        )
        for name, version, profile in clients:
            executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
            transport = server.create_server(
                executor, execution.Diagnostics(io.StringIO(), event_limit=2, line_limit=256)
            )

            async def scenario(selected: MCPServer, info: Implementation, expected: host_profiles.HostProfile) -> None:
                async with Client(selected, client_info=info, mode="legacy") as client:
                    result = await client.call_tool("pinboard_startup_text", {"request": self.request})
                    page = result.structured_content
                    assert isinstance(page, dict)
                    self.assertEqual("ok", page["status"], page)
                    self.assertEqual(host_profiles.measured_page_bytes(expected), page["max_batch_bytes"])
                    sent = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
                    self.assertLessEqual(sent, host_profiles.measured_page_bytes(expected))
                    match expected:
                        case host_profiles.HostProfile.CLAUDE_CODE | host_profiles.HostProfile.CODEX:
                            self.assertEqual([], result.content)
                        case host_profiles.HostProfile.ANTIGRAVITY | host_profiles.HostProfile.UNMEASURED:
                            companion = result.content[0]
                            assert isinstance(companion, TextContent)
                            self.assertEqual(page, msgspec.json.decode(companion.text))

            with self.subTest(name=name):
                try:
                    asyncio.run(scenario(transport, Implementation(name=name, version=version), profile))
                finally:
                    executor.shutdown()
                host = host_profiles.setting_host(profile)
                if host is not None:
                    listed = git_config.list_entries(self.settings)
                    assert isinstance(listed, git_config.Entries)
                    self.assertIn(
                        git_config.Entry(
                            host_settings.setting_key(host), str(host_profiles.measured_page_bytes(profile))
                        ),
                        listed.entries,
                    )
        listed = git_config.list_entries(self.settings)
        assert isinstance(listed, git_config.Entries)
        self.assertEqual(3, len(listed.entries))
