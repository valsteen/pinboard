import hashlib
import io
import json
import tempfile
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files import file_io
from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import work_brief_models, work_briefs
from pinboard.domain import work_models
from pinboard.mcp import (
    common,
    contract_schemas,
    execution,
    job_operations,
    mutation_operations,
    pr_review_operations,
    read_operations,
    server,
)
from pinboard.mcp.contracts import JsonValue
from tests import test_correction_source_review, test_dispatch, test_pr_reviews
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import NativeTools, call_advertised_tool, native_tools
from tests.support import JsonObject
from tests.work_brief_support import needs_correction_review, ready_review


class McpFileInputsTest(CheckpointPackageSupport):
    def test_brief_file_capture_preserves_item_overrides_and_single_read(self) -> None:
        for project_mode, item_mode in (("on", "off"), ("off", "on")):
            with self.subTest(project_mode=project_mode, item_mode=item_mode):
                fixture = self.checkpoint_fixture()
                selected = fixture.work / "selected-brief.json"
                raw = msgspec.json.encode(fixture.brief)
                selected.write_bytes(raw)
                (fixture.work / "contributor-traces.config").write_text(
                    '[pinboard "unsafe_persist_exact_pinboard_traces"]\n'
                    f'mode = {project_mode}\n[item "{fixture.brief.item_id}"]\nmode = {item_mode}\n'
                )
                executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
                self.addCleanup(executor.shutdown)
                native = NativeTools(
                    server.create_server(
                        executor,
                        execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256),
                        execution.AutomaticCapture(common.select_capture_item),
                    )
                )
                roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
                traces = fixture.work / "invocation-traces"
                for tool, fields in (
                    (server.BRIEF_PUBLISH_TOOL, {"brief": msgspec.to_builtins(fixture.brief)}),
                    (
                        server.BRIEF_PUBLISH_FILE_TOOL,
                        {"brief_path": str(selected), "brief_sha256": hashlib.sha256(raw).hexdigest()},
                    ),
                ):
                    before = set(traces.rglob("*.json"))
                    with patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read:
                        result = native.call_advertised_tool(tool, roots | fields)
                    self.assertEqual("unchanged", result["status"], result)
                    self.assertEqual(item_mode == "on", bool(set(traces.rglob("*.json")) - before))
                    if tool == server.BRIEF_PUBLISH_FILE_TOOL:
                        self.assertEqual(1, sum(call.args[0] == selected for call in read.call_args_list))

    def test_large_pretty_brief_file_publishes_same_canonical_bytes_and_reloads(self) -> None:
        fixture = self.checkpoint_fixture()
        brief = msgspec.structs.replace(
            fixture.brief,
            artifact_revision=fixture.brief.artifact_revision + 1,
            product_decision_and_provenance="Exact complete selected input. " * 6000,
        )
        selected = fixture.work / "selected-brief.json"
        raw = json.dumps(msgspec.to_builtins(brief), indent=4).encode()
        selected.write_bytes(raw)
        roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        arguments: JsonObject = roots | {"brief_path": str(selected), "brief_sha256": hashlib.sha256(raw).hexdigest()}
        with patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read:
            published = call_advertised_tool("pinboard_brief_publish_file", arguments)
        self.assertEqual(1, sum(call.args[0] == selected for call in read.call_args_list))
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        canonical = work_briefs.canonical_work_brief_bytes(brief)
        self.assertGreater(len(canonical), 125 * 1024)
        self.assertGreater(len(raw), 125 * 1024)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), reference["sha256"])
        self.assertNotEqual(hashlib.sha256(raw).hexdigest(), reference["sha256"])
        selector = reference["selector"]
        assert isinstance(selector, str)
        self.assertEqual(canonical, (fixture.work / selector).read_bytes())
        self.assertEqual(raw, selected.read_bytes())
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        accepted = fresh.read_artifact_reference(
            work_models.ArtifactKind.BRIEF, brief.attempt_id, brief.artifact_revision
        )
        assert accepted is not None
        self.assertEqual(reference["artifact_ref_id"], accepted.artifact_ref_id)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), accepted.content_sha256)
        self.assertEqual(len(canonical), accepted.size_bytes)
        inline = call_advertised_tool(server.BRIEF_PUBLISH_TOOL, roots | {"brief": msgspec.to_builtins(brief)})
        self.assertEqual("unchanged", inline["status"])
        self.assertEqual(reference, inline["reference"])
        self.assertLess(
            len(msgspec.json.encode(arguments)),
            len(msgspec.json.encode(roots | {"brief": msgspec.to_builtins(brief)})) // 20,
        )

    def test_brief_selected_input_rejects_before_publication(self) -> None:
        fixture = self.checkpoint_fixture()
        selected = fixture.work / "rejected-brief.json"
        arguments: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "brief_path": str(selected),
            "brief_sha256": "f" * 64,
        }
        before = fixture.store.validated_snapshot()
        for raw, digest, code in (
            (None, "f" * 64, "INPUT_FILE_UNREADABLE"),
            (b"{}", "f" * 64, "INPUT_FILE_DIGEST_MISMATCH"),
            (b"{", hashlib.sha256(b"{").hexdigest(), "WORK_BRIEF_INVALID"),
            (b'{"schema":"wrong"}', hashlib.sha256(b'{"schema":"wrong"}').hexdigest(), "WORK_BRIEF_INVALID"),
        ):
            with self.subTest(code=code):
                if raw is not None:
                    selected.write_bytes(raw)
                rejected = call_advertised_tool("pinboard_brief_publish_file", arguments | {"brief_sha256": digest})
                self.assertEqual(code, rejected["code"], rejected)
                self.assertEqual("unchanged", rejected["effect"])
                self.assertEqual("correct-input", rejected["retry"])
                self.assertIn(str(selected), str(rejected["message"]))
                if code in {"INPUT_FILE_UNREADABLE", "INPUT_FILE_DIGEST_MISMATCH"}:
                    self.assertIn(digest, str(rejected["message"]))
                    self.assertIn(
                        "unavailable" if raw is None else hashlib.sha256(raw).hexdigest(), str(rejected["message"])
                    )
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        with patch.object(file_io, "read_verified_input", side_effect=AssertionError("Invalid controls must not read")):
            changes: tuple[JsonObject, ...] = ({"brief_sha256": "bad"}, {"brief": {}}, {"brief_path": ""})
            for change in changes:
                rejected = mutation_operations._read_brief_file(arguments | change)
                assert isinstance(rejected, execution.OperationResult)
                self.assertEqual("WORK_BRIEF_INVALID", rejected.content["code"])
                contract_schemas.validate_result("pinboard_brief_publish_file", rejected.content)

    def test_negative_review_file_preserves_identity_collision_and_independence(self) -> None:
        fixture = self.checkpoint_fixture()
        review = msgspec.json.decode(
            needs_correction_review(fixture.brief), type=work_brief_models.WorkBriefReviewNeedsCorrection
        )
        selected = fixture.work / "negative-review.json"
        raw = json.dumps(msgspec.to_builtins(review), indent=4).encode()
        selected.write_bytes(raw)
        brief_reference = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.BRIEF, fixture.brief.attempt_id, fixture.brief.artifact_revision
        )
        assert brief_reference is not None
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "operation": "publish-file",
            "brief_artifact_ref_id": brief_reference.artifact_ref_id,
            "review_path": str(selected),
            "review_sha256": hashlib.sha256(raw).hexdigest(),
        }
        published = call_advertised_tool(server.BRIEF_REVIEW_TOOL, {"request": request})
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        selector = reference["selector"]
        assert isinstance(selector, str)
        self.assertEqual(
            work_briefs.canonical_work_brief_review_needs_correction_bytes(review),
            (fixture.work / selector).read_bytes(),
        )
        inline = request.copy()
        inline.update(operation="publish", review=msgspec.to_builtins(review))
        del inline["review_path"], inline["review_sha256"]
        self.assertEqual(reference, call_advertised_tool(server.BRIEF_REVIEW_TOOL, {"request": inline})["reference"])
        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        for changed, code in (
            (
                msgspec.structs.replace(review, reviewer_task_id=fixture.brief.owner_task_id),
                "WORK_BRIEF_REVIEW_NOT_INDEPENDENT",
            ),
            (msgspec.structs.replace(review, accepted_brief_sha256="f" * 64), "WORK_BRIEF_REVIEW_STALE"),
        ):
            raw = msgspec.json.encode(changed)
            selected.write_bytes(raw)
            rejected = call_advertised_tool(
                server.BRIEF_REVIEW_TOOL, {"request": request | {"review_sha256": hashlib.sha256(raw).hexdigest()}}
            )
            self.assertEqual(code, rejected["code"], rejected)
            self.assertEqual([], rejected["changed_surfaces"])
            self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

        different = msgspec.structs.replace(review, reviewer_task_id="different-independent-reviewer")
        raw = msgspec.json.encode(different)
        selected.write_bytes(raw)
        with self.assertRaises(ArtifactError):
            read_operations._brief_review(
                {"request": request | {"review_sha256": hashlib.sha256(raw).hexdigest()}}, execution.CancellationToken()
            )
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_negative_review_file_rejects_input_without_publication(self) -> None:
        fixture = self.checkpoint_fixture()
        accepted = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.BRIEF, fixture.brief.attempt_id, fixture.brief.artifact_revision
        )
        assert accepted is not None
        selected = fixture.work / "invalid-negative-review.json"
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "operation": "publish-file",
            "brief_artifact_ref_id": accepted.artifact_ref_id,
            "review_path": str(selected),
            "review_sha256": "f" * 64,
        }
        before = fixture.store.validated_snapshot()
        negative = self.json_object(msgspec.json.decode(needs_correction_review(fixture.brief)))
        cases = (
            (None, "f" * 64, "INPUT_FILE_UNREADABLE"),
            (b"{}", "f" * 64, "INPUT_FILE_DIGEST_MISMATCH"),
            (b"{", None, "BRIEF_REVIEW_REQUEST_INVALID"),
            (msgspec.json.encode(negative | {"unknown": True}), None, "BRIEF_REVIEW_REQUEST_INVALID"),
            (ready_review(fixture.brief), None, "BRIEF_REVIEW_REQUEST_INVALID"),
        )
        for raw, supplied_digest, code in cases:
            with self.subTest(code=code):
                if raw is not None:
                    selected.write_bytes(raw)
                digest = supplied_digest if supplied_digest is not None else hashlib.sha256(raw or b"").hexdigest()
                rejected = call_advertised_tool(
                    server.BRIEF_REVIEW_TOOL, {"request": request | {"review_sha256": digest}}
                )
                self.assertEqual(code, rejected["code"], rejected)
                self.assertEqual("unchanged", rejected["effect"])
                self.assertEqual("correct-input", rejected["retry"])
                self.assertIn(str(selected), str(rejected["message"]))
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        with patch.object(file_io, "read_verified_input", side_effect=AssertionError("Invalid controls must not read")):
            for change in ({"review_sha256": "bad"}, {"review": negative}, {"review_path": ""}):
                rejected = read_operations._brief_review({"request": request | change}, execution.CancellationToken())
                self.assertEqual("BRIEF_REVIEW_REQUEST_INVALID", rejected.content["code"])
                contract_schemas.validate_result(server.BRIEF_REVIEW_TOOL, rejected.content)

    def test_large_ready_review_file_preserves_canonical_bytes_and_inline_identity(self) -> None:
        owner = test_dispatch.DispatchTest()
        project = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        project, roots, _store, brief, action, environment = owner.initialized(project=project)
        canonical = ready_review(brief, result="Exact independent review evidence. " * 2000)
        review = msgspec.json.decode(canonical, type=work_brief_models.WorkBriefReview)
        inline = owner.dispatch_choice(action(), environment, canonical, "file-ready-review", None)
        selected = roots.work_root / "ready-input.json"
        raw = json.dumps(msgspec.to_builtins(review), indent=4).encode()
        selected.write_bytes(raw)
        choice = inline.copy()
        del choice["brief_review"]
        choice.update(
            kind="reviewed-file", brief_review_path=str(selected), brief_review_sha256=hashlib.sha256(raw).hexdigest()
        )
        arguments: JsonObject = {"project_root": str(project), "work_root": str(roots.work_root), "dispatch": choice}
        published = call_advertised_tool(server.DISPATCH_TOOL, arguments)
        self.assertEqual("ready", published["status"], published)
        fresh = SQLiteWorkStore(roots.database_path)
        reference = fresh.read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE,
            f"{brief.attempt_id}-brief-review-{work_briefs.ready_review_key_sha256(brief)}",
            1,
        )
        assert reference is not None
        self.assertEqual(canonical, (roots.work_root / reference.selector).read_bytes())
        reused = call_advertised_tool(server.DISPATCH_TOOL, arguments | {"dispatch": inline})
        self.assertEqual(
            {
                key: value
                for key, value in self.json_object(published["prompt_reference"]).items()
                if key not in {"artifact_created", "ledger_changed"}
            },
            {
                key: value
                for key, value in self.json_object(reused["prompt_reference"]).items()
                if key not in {"artifact_created", "ledger_changed"}
            },
        )
        self.assertEqual([], reused["changed_surfaces"])
        self.assertLess(
            len(msgspec.json.encode(arguments)), len(msgspec.json.encode(arguments | {"dispatch": inline})) // 20
        )
        before = fresh.validated_snapshot()
        for changed, code in (
            (
                msgspec.structs.replace(review, reviewer_task_id=brief.owner_task_id),
                "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT",
            ),
            (msgspec.structs.replace(review, accepted_brief_sha256="f" * 64), "DISPATCH_BRIEF_REVIEW_STALE"),
        ):
            raw = msgspec.json.encode(changed)
            selected.write_bytes(raw)
            rejected = call_advertised_tool(
                server.DISPATCH_TOOL,
                arguments | {"dispatch": choice | {"brief_review_sha256": hashlib.sha256(raw).hexdigest()}},
            )
            self.assertEqual(code, rejected["code"], rejected)
            self.assertEqual(before, fresh.validated_snapshot())
        raw = msgspec.json.encode(review)
        selected.write_bytes(raw)
        stale_receipt = self.json_object(choice["receipt"]) | {"subject_revision": "0"}
        rejected = call_advertised_tool(
            server.DISPATCH_TOOL,
            arguments
            | {"dispatch": choice | {"brief_review_sha256": hashlib.sha256(raw).hexdigest(), "receipt": stale_receipt}},
        )
        self.assertEqual("STALE_ACTION", rejected["code"], rejected)
        self.assertEqual(before, fresh.validated_snapshot())

    def test_review_file_rejections_remain_inside_negotiated_contract(self) -> None:
        owner = test_dispatch.DispatchTest()
        project = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        project, roots, store, brief, action, environment = owner.initialized(project=project)
        inline = owner.dispatch_choice(action(), environment, ready_review(brief), "file-rejections", None)
        choice = inline.copy()
        del choice["brief_review"]
        selected = roots.work_root / "invalid-review.json"
        choice.update(kind="reviewed-file", brief_review_path=str(selected), brief_review_sha256="f" * 64)
        arguments: JsonObject = {"project_root": str(project), "work_root": str(roots.work_root), "dispatch": choice}
        before = store.validated_snapshot()
        review = self.json_object(inline["brief_review"])
        cases: tuple[tuple[JsonObject | None, str], ...] = (
            (None, "INPUT_FILE_UNREADABLE"),
            ({}, "INPUT_FILE_DIGEST_MISMATCH"),
            (review | {"unknown": True}, "DISPATCH_INVALID"),
            (review | {"verdict": "needs-correction"}, "DISPATCH_INVALID"),
            (self.json_object(msgspec.json.decode(needs_correction_review(brief))), "DISPATCH_INVALID"),
        )
        for value, code in cases:
            with self.subTest(code=code):
                digest = "f" * 64
                if value is not None:
                    raw = msgspec.json.encode(value)
                    selected.write_bytes(raw)
                    if code != "INPUT_FILE_DIGEST_MISMATCH":
                        digest = hashlib.sha256(raw).hexdigest()
                rejected = call_advertised_tool(
                    server.DISPATCH_TOOL, arguments | {"dispatch": choice | {"brief_review_sha256": digest}}
                )
                self.assertEqual(code, rejected["code"], rejected)
                self.assertEqual("correct-input", rejected["retry"])
                self.assertIn(str(selected), str(rejected["message"]))
                self.assertEqual(before, store.validated_snapshot())
        denied = PermissionError("Selected file access denied")
        with patch.object(Path, "read_bytes", side_effect=denied):
            result = file_io.read_verified_input(selected, "f" * 64)
        assert isinstance(result, file_io.SelectedInputFailure)
        self.assertEqual("INPUT_FILE_UNREADABLE", result.code)

    def test_all_correction_file_leaves_preserve_existing_recovery_journeys(self) -> None:
        observed: set[str] = set()
        candidate_boundaries: set[str] = set()
        original = job_operations._dispatch_job

        def from_file(
            project_root: str, work_root: str, dispatch: Mapping[str, JsonValue], token: execution.CancellationToken
        ) -> execution.OperationResult:
            choice = dict(dispatch)
            kind = choice["kind"]
            if kind in (
                "correction",
                "reuse-correction",
                "local-correction",
                "candidate-correction",
                "replacement-readiness",
            ):
                assert isinstance(kind, str)
                selected = Path(work_root) / f"selected-{kind}.json"
                raw = json.dumps(choice.pop("brief_review"), indent=4).encode()
                selected.write_bytes(raw)
                choice.update(
                    kind=kind + "-file",
                    brief_review_path=str(selected),
                    brief_review_sha256=hashlib.sha256(raw).hexdigest(),
                )
                with patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read:
                    outcome = original(project_root, work_root, choice, token)
                self.assertEqual(1, sum(call.args[0] == selected for call in read.call_args_list))
                contract_schemas.validate_result(server.DISPATCH_TOOL, outcome.content)
                if outcome.content["status"] in ("ready", "replacement-ready"):
                    observed.add(kind)
                    if kind == "candidate-correction":
                        candidate_boundaries.add(str(self.json_object(dispatch["brief_review"])["boundary"]))
                return outcome
            return original(project_root, work_root, choice, token)

        for journey in ("pause-resume", "reuse", "local", "replacement", "candidate", "candidate-rejections"):
            owner = test_correction_source_review.CorrectionSourceReviewTest()
            try:
                with patch.object(job_operations, "_dispatch_job", side_effect=from_file):
                    match journey:
                        case "pause-resume":
                            owner.test_native_correction_survives_same_brief_pause_resume_with_fresh_dispatch_receipt()
                        case "reuse":
                            owner.test_reuse_correction_requires_fresh_independent_candidate_assessment()
                        case "local":
                            owner.test_local_return_dispatches_and_corrected_commit_reaches_disposition()
                        case "replacement":
                            owner.replacement_brief_recovery(True, "source")
                        case "candidate":
                            owner.test_advertised_candidate_correction_accepts_tracked_and_untracked_drift()
                        case "candidate-rejections":
                            owner.test_current_candidate_binding_and_independent_source_gates_reject_before_effects()
            finally:
                owner.doCleanups()
        self.assertEqual(
            {"correction", "reuse-correction", "local-correction", "candidate-correction", "replacement-readiness"},
            observed,
        )
        self.assertEqual({"local", "cross-boundary"}, candidate_boundaries)

    def test_file_publishers_report_committed_artifact_sync_failure(self) -> None:
        fixture = self.checkpoint_fixture()
        brief = msgspec.structs.replace(fixture.brief, artifact_revision=fixture.brief.artifact_revision + 1)
        review = msgspec.json.decode(
            needs_correction_review(fixture.brief), type=work_brief_models.WorkBriefReviewNeedsCorrection
        )
        accepted = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.BRIEF, fixture.brief.attempt_id, fixture.brief.artifact_revision
        )
        assert accepted is not None
        roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        selected = fixture.work / "post-publication-input.json"
        synchronize = file_io._sync_directory
        before = fixture.store.validated_snapshot()
        cases: tuple[tuple[str, JsonObject, str, bytes], ...] = (
            (
                server.BRIEF_PUBLISH_FILE_TOOL,
                roots | {"brief_path": str(selected)},
                brief.attempt_id,
                work_briefs.canonical_work_brief_bytes(brief),
            ),
            (
                server.BRIEF_REVIEW_TOOL,
                {
                    "request": roots
                    | {
                        "operation": "publish-file",
                        "brief_artifact_ref_id": accepted.artifact_ref_id,
                        "review_path": str(selected),
                    }
                },
                work_briefs.needs_correction_review_key(fixture.brief),
                work_briefs.canonical_work_brief_review_needs_correction_bytes(review),
            ),
        )

        def fail_artifact_sync(selected_key: str, path: Path) -> None:
            if path.name == selected_key:
                raise FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "Selected artifact sync failed")
            synchronize(path)

        for tool, arguments, key, raw in cases:
            with self.subTest(tool=tool):
                selected.write_bytes(raw)
                if tool == server.BRIEF_PUBLISH_FILE_TOOL:
                    arguments["brief_sha256"] = hashlib.sha256(raw).hexdigest()
                else:
                    self.json_object(arguments["request"])["review_sha256"] = hashlib.sha256(raw).hexdigest()

                with patch.object(file_io, "_sync_directory", side_effect=partial(fail_artifact_sync, key)):
                    failed = call_advertised_tool(tool, arguments)
                self.assertEqual("failed-after-publication", failed["status"], failed)
                self.assertEqual("committed", failed["effect"])
                self.assertEqual("do-not-retry", failed["retry"])
                self.assertEqual(["immutable-artifact"], failed["changed_surfaces"])
                selector = failed["published_selector"]
                assert isinstance(selector, str)
                self.assertEqual(raw, (fixture.work / selector).read_bytes())
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_pr_record_files_preserve_two_head_review_and_rejection_contracts(self) -> None:  # noqa: PLR0915 - one persisted PR journey
        native = self.enterContext(native_tools())
        observed: set[str] = set()
        exercised_failures: set[str] = set()

        def from_file(tool: str, arguments: JsonObject) -> JsonObject:
            request = self.json_object(arguments["request"]).copy()
            operation = request["operation"]
            if operation not in ("start", "review-brief", "round"):
                return native.call_advertised_tool(tool, arguments)
            assert isinstance(operation, str)
            field = "brief" if operation == "start" else "brief_review" if operation == "review-brief" else "round"
            record = self.json_object(request.pop(field))
            selected = Path(str(request["work_root"])) / f"selected-{operation}.json"
            raw = json.dumps(record, indent=4).encode()
            record = self.json_object(msgspec.json.decode(raw))
            selected.write_bytes(raw)
            request.update(operation=operation + "-file")
            request[field + "_path"] = str(selected)
            request[field + "_sha256"] = hashlib.sha256(raw).hexdigest()
            store = SQLiteWorkStore(Path(str(request["work_root"])) / "state.sqlite3")
            before = store.validated_snapshot()
            if operation not in exercised_failures:
                for invalid in (b"{", msgspec.json.encode(record | {"unknown": True})):
                    selected.write_bytes(invalid)
                    rejected = native.call_advertised_tool(
                        tool, {"request": request | {field + "_sha256": hashlib.sha256(invalid).hexdigest()}}
                    )
                    self.assertEqual("PR_REVIEW_INVALID", rejected["code"], rejected)
                    self.assertEqual("unchanged", rejected["effect"])
                    self.assertIn(str(selected), str(rejected["message"]))
                    self.assertEqual(before, store.validated_snapshot())
                selected.write_bytes(raw)
                token = execution.CancellationToken()
                read = file_io.read_verified_input

                def cancel_after_read(path: Path, sha256: str) -> bytes | file_io.SelectedInputFailure:
                    content = read(path, sha256)
                    token.cancel()
                    return content

                cancellation_request = request | {
                    "actor_task_id": record["prepared_by_task_id" if operation == "start" else "reviewer_task_id"]
                }
                with (
                    patch.object(file_io, "read_verified_input", side_effect=cancel_after_read),
                    self.assertRaises(execution.OperationCancelled),
                ):
                    pr_review_operations.execute({"request": cancellation_request}, token)
                self.assertEqual(before, store.validated_snapshot())
                exercised_failures.add(operation)
            response = native.call_advertised_tool(tool, {"request": request})
            if response["status"] == "committed":
                observed.add(operation)
                if operation == "round":
                    rounds = response["rounds"]
                    assert isinstance(rounds, list)
                    self.assertEqual(record, self.json_object(rounds[-1])["round"])
                else:
                    self.assertEqual(record, response["brief" if operation == "start" else "brief_review"])
                committed = store.validated_snapshot()
                stale = native.call_advertised_tool(tool, {"request": request})
                self.assertEqual("rejected", stale["status"], stale)
                self.assertEqual("unchanged", stale["effect"])
                self.assertEqual(committed, store.validated_snapshot())
            else:
                self.assertEqual(before, store.validated_snapshot())
            return response

        owner = test_pr_reviews.HumanOwnedPrReviewTest()
        try:
            with patch.object(test_pr_reviews, "call_native_tool", side_effect=from_file):
                owner.test_two_heads_and_human_stop_survive_fresh_store()
        finally:
            owner.doCleanups()
        self.assertEqual({"start", "review-brief", "round"}, observed)
        self.assertEqual(observed, exercised_failures)

    def test_brief_file_preflight_refuses_before_reading_selected_bytes(self) -> None:
        fixture = self.checkpoint_fixture()
        settings = fixture.work / "contributor-traces.config"
        settings.write_text('[pinboard "unsafe_persist_exact_pinboard_traces"]\nmode = invalid\n')
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        self.addCleanup(executor.shutdown)
        transport = server.create_server(
            executor,
            execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256),
            execution.AutomaticCapture(common.select_capture_item),
        )
        before = fixture.store.validated_snapshot()
        with patch.object(file_io, "read_verified_input", side_effect=AssertionError("Target read ran")):
            refused = NativeTools(transport).call_advertised_tool(
                server.BRIEF_PUBLISH_FILE_TOOL,
                {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "brief_path": str(fixture.work / "not-read.json"),
                    "brief_sha256": "f" * 64,
                },
            )
        self.assertEqual("TRACE_PREFLIGHT_FAILED", refused["code"], refused)
        self.assertFalse(refused["target_ran"])
        self.assertEqual("unchanged", refused["effect"])
        self.assertEqual(str(settings), refused["resource"])
        self.assertEqual(before, fixture.store.validated_snapshot())
