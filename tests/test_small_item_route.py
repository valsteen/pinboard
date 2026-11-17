"""A recorded small item takes a four-part brief, coordinator-inline delivery and one independent review."""

import asyncio
import hashlib
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import msgspec
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import project_export, work_brief_contract, work_brief_models, work_briefs
from pinboard.domain import history, work_models
from pinboard.domain.identifiers import AttemptId, CheckpointId, WorkItemId
from pinboard.mcp import contracts
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointPackageSupport
from tests.native_support import call_native_tool
from tests.support import SQLITE_DEFINITION, SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state, initialize_store
from tests.test_proposals import proposal
from tests.work_brief_support import work_a_brief

type Json = contracts.JsonValue
type JsonDict = dict[str, contracts.JsonValue]

_ENVELOPED = {
    mcp_server.ACTIONS_TOOL,
    mcp_server.PREPARATION_AUTHORITY_TOOL,
    mcp_server.ATTEMPT_AUTHORITY_TOOL,
    mcp_server.TRANSITION_TOOL,
    mcp_server.ITEM_STATUS_TOOL,
    mcp_server.BRIEF_CONTRACT_TOOL,
    mcp_server.BRIEF_REVIEW_TOOL,
}


def small_proposal() -> JsonDict:
    value = dict(proposal())
    value["proposal_id"] = "small-1"
    value["obligations"] = [
        {
            "obligation_id": "visible-fix",
            "statement": "The product line reads correctly.",
            "deferral_policy": "forbidden",
        },
        {
            "obligation_id": "kept-check",
            "statement": "The repository check still passes.",
            "deferral_policy": "allowed",
        },
    ]
    route = value["route_facts"]
    assert isinstance(route, dict)
    value["route_facts"] = {**route, "size_class": "small"}
    return value


def _fill(starter: Json, facts: JsonDict) -> Json:
    """Replace every unresolved null of the returned small starter with an accepted fact, by field name."""
    if isinstance(starter, dict):
        return {key: facts[key] if child is None else _fill(child, facts) for key, child in starter.items()}
    if isinstance(starter, list):
        return [_fill(child, facts) for child in starter]
    return starter


class SmallItemRouteTest(CheckpointPackageSupport):
    def git(self, project: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(project), "-c", "user.name=Small", "-c", "user.email=small@example.invalid", *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_small_item_journey_from_proposal_to_completion_is_delivered_inline(self) -> None:  # noqa: PLR0915 - one installed small-route journey
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name).resolve()
        subprocess.run(("git", "init", "--quiet", str(project)), check=True)
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        (project / ".git" / "info" / "exclude").write_text(".pinboard/\n", encoding="utf-8")
        (project / "product.txt").write_text("Accepted base\n", encoding="utf-8")
        self.git(project, "add", "product.txt")
        self.git(project, "commit", "-m", "Accepted base")
        base = self.git(project, "rev-parse", "HEAD")
        branch = self.git(project, "branch", "--show-current")
        common: JsonDict = {"project_root": str(project), "work_root": str(roots.work_root)}
        actor: JsonDict = {"actor_task_id": "coordinator", "actor_host_id": "local"}
        attempt_root = roots.work_root / "attempts" / "small-1-1"
        result_bytes = b"# Result\n\nThe product line reads correctly; the brief's check passed.\n"
        review_bytes = b"# Review\n\nReady: the candidate satisfies the small brief.\n"

        async def scenario() -> None:  # noqa: PLR0915 - ordered real client effects
            parameters = StdioServerParameters(command=sys.executable, args=("-m", "pinboard.mcp"))
            async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
                await session.initialize()

                async def raw(tool: str, arguments: JsonDict) -> JsonDict:
                    request = common | arguments
                    result = await session.call_tool(tool, {"request": request} if tool in _ENVELOPED else request)
                    assert not result.is_error and isinstance(result.structured_content, dict), result
                    return result.structured_content

                async def call(tool: str, arguments: JsonDict) -> JsonDict:
                    content = await raw(tool, arguments)
                    self.assertNotEqual("rejected", content.get("status"), content)
                    return content

                async def transition(
                    kind: str, subject: str, role: str, authority: JsonDict, payload: JsonDict
                ) -> JsonDict:
                    query: JsonDict = {"role": role, "action_id": {"kind": kind, "subject": subject}}
                    discovered = await call(mcp_server.ACTIONS_TOOL, query | ({} if role == "project" else authority))
                    selected = self.json_object(self.json_array(discovered["actions"])[0])
                    request: JsonDict = {
                        "role": role,
                        "receipt": {
                            "action_id": selected["action_id"],
                            "subject_revision": selected["subject_revision"],
                        },
                        "payload": payload,
                        **authority,
                    }
                    return await call(mcp_server.TRANSITION_TOOL, request)

                await call(mcp_server.PROPOSAL_CREATE_TOOL, {"proposal": small_proposal(), **actor})
                prepared = await call(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    {
                        "operation": "start",
                        "item_id": "small-1",
                        "task_id": "coordinator",
                        "host_id": "local",
                        "ttl_seconds": 600,
                    },
                )
                preparation: JsonDict = {"lease_id": prepared["lease_id"], "generation": prepared["generation"]}
                starter = await call(mcp_server.BRIEF_CONTRACT_TOOL, {"operation": "starter", "boundary": "small"})
                self.assertEqual("small", starter["boundary"])
                facts: JsonDict = {
                    "artifact_revision": 1,
                    "attempt_id": "small-1-1",
                    "item_id": "small-1",
                    "branch": branch,
                    "base_revision": base,
                    "owner_task_id": "coordinator",
                    "revision": prepared["definition_revision"],
                    "digest": prepared["definition_digest"],
                    "checkout_selection": "main",
                    "title": "Correct the product line",
                    "outcome": "The product line reads correctly.",
                    "statement": "Rewrite product.txt.",
                    "check": "git diff --check",
                    "reason": "One file changes; no ownership moves.",
                }
                brief = self.json_object(_fill(starter["starter"], facts))
                self.json_object(self.json_array(brief["scope"])[0])["obligations"] = ["visible-fix", "kept-check"]

                v4 = await raw(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {"brief": msgspec.to_builtins(replace_small_with_v4(brief))},
                )
                self.assertEqual(("rejected", "WORK_BRIEF_INVALID"), (v4["status"], v4["code"]), v4)
                self.assertIn("pinboard-small-work-brief/v1", str(v4["message"]))
                missing = await raw(
                    mcp_server.BRIEF_PUBLISH_TOOL,
                    {
                        "brief": brief
                        | {"scope": [{"statement": "Rewrite product.txt.", "obligations": ["visible-fix"]}]}
                    },
                )
                self.assertEqual("rejected", missing["status"], missing)
                self.assertIn("kept-check", str(missing["message"]))
                published = await call(mcp_server.BRIEF_PUBLISH_TOOL, {"brief": brief})
                reference = self.json_object(published["reference"])
                accepted_path = roots.work_root / str(reference["selector"])
                accepted = work_briefs.decode_canonical_accepted_brief(accepted_path.read_bytes())
                assert isinstance(accepted, work_brief_models.SmallWorkBrief)
                self.assertEqual(accepted_path.read_bytes(), work_briefs.canonical_work_brief_bytes(accepted))

                await transition(
                    "activate",
                    "small-1",
                    "preparer",
                    preparation,
                    {"brief_artifact_ref_id": reference["artifact_ref_id"]},
                )
                brief_review = await raw(
                    mcp_server.BRIEF_REVIEW_TOOL,
                    {"operation": "status", "brief_artifact_ref_id": reference["artifact_ref_id"]},
                )
                self.assertEqual("rejected", brief_review["status"], brief_review)
                self.assertEqual("WORK_BRIEF_REVIEW_INVALID", brief_review["code"], brief_review)
                status = await call(mcp_server.ITEM_STATUS_TOOL, {"operation": "item", "item_id": "small-1"})
                current = self.json_object(
                    self.json_object(self.json_array(status["attempts"])[0])["current_checkpoint"]
                )
                self.assertEqual(("delivery", "Correct the product line"), (current["checkpoint_id"], current["title"]))

                before = SQLiteWorkStore(roots.database_path).validated_snapshot()
                dispatch_discovery = await call(
                    mcp_server.ACTIONS_TOOL,
                    {"role": "project", "action_id": {"kind": "dispatch", "subject": "small-1-1"}},
                )
                invocation = self.json_object(
                    self.json_object(self.json_array(dispatch_discovery["actions"])[0])["invocation"]
                )
                arguments = self.json_object(invocation["arguments"])
                self.json_object(arguments["dispatch"]).update(
                    {
                        "kind": "ordinary",
                        "checkpoint_id": "delivery",
                        "prompt": None,
                        "environment": {
                            "schema": "pinboard-dispatch/v2",
                            "runtime": "codex",
                            "background": False,
                            "checkout": str(project),
                            "branch": branch,
                            "starting_revision": base,
                            "host_id": "local",
                            "fresh_context": True,
                            "lease_ttl_seconds": 3600,
                            "permissions": ["repository-read", "repository-write"],
                        },
                    }
                )
                refused = await session.call_tool(str(invocation["tool"]), arguments)
                assert isinstance(refused.structured_content, dict)
                self.assertEqual("rejected", refused.structured_content["status"], refused.structured_content)
                self.assertEqual(
                    "DISPATCH_BRIEF_INVALID", refused.structured_content["code"], refused.structured_content
                )
                self.assertEqual(before, SQLiteWorkStore(roots.database_path).validated_snapshot())

                acquired = await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "operation": "acquire",
                        "attempt_id": "small-1-1",
                        "task_id": "coordinator",
                        "host_id": "local",
                        "ttl_seconds": 600,
                    },
                )
                lease: JsonDict = {"lease_id": acquired["lease_id"], "generation": acquired["generation"]}
                (project / "product.txt").write_text("Corrected product line\n", encoding="utf-8")
                self.git(project, "commit", "-am", "Correct the product line")
                candidate = self.git(project, "rev-parse", "HEAD")
                attempt_root.mkdir(parents=True, exist_ok=True)
                (attempt_root / "result.md").write_bytes(result_bytes)
                await transition("submit-review", "small-1-1", "worker", lease, {"candidate": candidate})
                await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL, {"operation": "release", "attempt_id": "small-1-1", **lease}
                )
                (attempt_root / "review.md").write_bytes(b"# Review\n\nNeeds correction: keep the trailing period.\n")
                returned = await transition(
                    "return-for-correction", "small-1-1", "project", actor, {"reason": "Keep the trailing newline."}
                )
                correction_history_id = returned["history_id"]
                context = await raw(
                    mcp_server.CORRECTION_CONTEXT_TOOL,
                    {"attempt_id": "small-1-1", "correction_history_id": correction_history_id},
                )
                self.assertEqual("rejected", context["status"], context)
                self.assertEqual("DISPATCH_BRIEF_INVALID", context["code"], context)
                reacquired = await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL,
                    {
                        "operation": "acquire",
                        "attempt_id": "small-1-1",
                        "task_id": "coordinator",
                        "host_id": "local",
                        "ttl_seconds": 600,
                    },
                )
                lease = {"lease_id": reacquired["lease_id"], "generation": reacquired["generation"]}
                (project / "product.txt").write_text("Corrected product line.\n", encoding="utf-8")
                self.git(project, "commit", "-am", "Apply the inline correction")
                candidate = self.git(project, "rev-parse", "HEAD")
                await transition("submit-review", "small-1-1", "worker", lease, {"candidate": candidate})
                await call(
                    mcp_server.ATTEMPT_AUTHORITY_TOOL, {"operation": "release", "attempt_id": "small-1-1", **lease}
                )

                job = await call(
                    mcp_server.REVIEW_JOB_TOOL,
                    {
                        "review": {
                            "kind": "correction",
                            "correction_history_id": correction_history_id,
                            "attempt_id": "small-1-1",
                            "candidate_revision": candidate,
                            "runtime": "codex",
                            "background": False,
                        }
                    },
                )
                self.assertEqual("ready", job["status"], job)
                prompt_reference = self.json_object(job["prompt_reference"])
                prompt = (roots.work_root / str(prompt_reference["selector"])).read_bytes()
                self.assertIn(accepted_path.read_bytes().strip(), prompt)
                (attempt_root / "review.md").write_bytes(review_bytes)
                snapshot = SQLiteWorkStore(roots.database_path).read_candidate_snapshot_context(AttemptId("small-1-1"))
                assert snapshot is not None
                ready: JsonDict = {
                    "kind": "record-ready",
                    "attempt_id": "small-1-1",
                    "candidate_revision": candidate,
                    "candidate_snapshot_sha256": snapshot.reference.content_sha256,
                    "accepted_brief_sha256": reference["sha256"],
                    "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                    "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
                    "reviewer_prompt_sha256": prompt_reference["sha256"],
                    "reviewer_task_id": "coordinator",
                    "verdict": "ready",
                    "acceptance_evidence": "The protected candidate satisfies the small brief.",
                }
                owner_review = await raw(mcp_server.REVIEW_JOB_TOOL, {"review": ready})
                self.assertEqual("rejected", owner_review["status"], owner_review)

                async def complete(reviewer: str) -> JsonDict:
                    discovery = await call(
                        mcp_server.ACTIONS_TOOL,
                        {"role": "project", "action_id": {"kind": "complete", "subject": "small-1-1"}},
                    )
                    selected = self.json_object(self.json_array(discovery["actions"])[0])
                    contract = self.json_object(selected["input_contract"])
                    self.assertEqual([], contract["checkpoint_packages"])
                    return await raw(
                        mcp_server.TRANSITION_TOOL,
                        {
                            "role": "project",
                            **actor,
                            "receipt": {
                                "action_id": selected["action_id"],
                                "subject_revision": selected["subject_revision"],
                            },
                            "payload": {
                                "schema": "pinboard-reviewed-completion/v3",
                                "human_decision": "Complete the small item.",
                                "candidate": contract["candidate"],
                                "evidence": "The one independent review is ready.",
                                "reviewer_task_id": reviewer,
                                "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                                "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
                                "packages": [],
                            },
                        },
                    )

                unreviewed = await complete("small-reviewer")
                self.assertEqual("rejected", unreviewed["status"], unreviewed)
                recorded = await call(
                    mcp_server.REVIEW_JOB_TOOL, {"review": ready | {"reviewer_task_id": "small-reviewer"}}
                )
                self.assertEqual("recorded", recorded["status"], recorded)
                owner_completion = await complete("coordinator")
                self.assertEqual("rejected", owner_completion["status"], owner_completion)
                completed = await complete("small-reviewer")
                self.assertIn(completed["status"], {"committed", "committed-with-warning"}, completed)

        asyncio.run(scenario())
        terminal = SQLiteWorkStore(roots.database_path).validated_snapshot()
        item = next(row for row in terminal.lifecycle.work_items if row.item_id == WorkItemId("small-1"))
        self.assertEqual("done", item.state.value)
        cli_roots = ("--project-root", str(project), "--work-root", str(roots.work_root))
        self.assertTrue(self.run_json_cli(*cli_roots, "validate")["valid"])
        code, exported, error = self.run_cli(*cli_roots, "export", "--json")
        self.assertEqual(0, code, error)
        projected = msgspec.json.decode(exported, type=project_export.ProjectExport)
        self.assertEqual((), projected.checkpoint_packages)


def replace_small_with_v4(brief: JsonDict) -> JsonDict:
    """A v4 local brief carrying the same identity, which a qualifying small item must refuse."""
    scope = brief["accepted_scope"]
    assert isinstance(scope, dict)
    revision = scope["revision"]
    return {
        "schema": "pinboard-work-brief/v4",
        **{
            key: brief[key]
            for key in (
                "artifact_revision",
                "attempt_id",
                "item_id",
                "branch",
                "base_revision",
                "owner_task_id",
                "accepted_scope",
                "title",
                "outcome",
                "checkout_selection",
            )
        },
        "supported_production_roots": ["product.txt"],
        "product_decision_and_provenance": "Accepted small proposal.",
        "testing_strategy": "Run the check.",
        "scope": ["Rewrite product.txt."],
        "bootstrap": [],
        "compatibility": [],
        "non_goals": [],
        "checkpoint": {
            "boundary": "local",
            "checkpoint_id": "delivery",
            "title": "Correct the product line",
            "architecture_impact": {"kind": "none", "reason": "One file."},
            "outcome_description": "The product line reads correctly.",
            "disposition": {"kind": "terminal"},
            "acceptance_criteria": [{"number": 1, "requirement": "The product line reads correctly."}],
            "verification": [
                {
                    "authorization_basis": {"kind": "accepted-scope", "item_id": "small-1", "scope_revision": revision},
                    "obligation": "git diff --check",
                }
            ],
            "deferrals": [],
        },
        "obligation_correspondence": [
            {"obligation_id": "visible-fix", "target": {"kind": "criterion", "number": 1}},
            {"obligation_id": "kept-check", "target": {"kind": "criterion", "number": 1}},
        ],
    }


def small_work_a_brief(digest: str) -> work_brief_models.SmallWorkBrief:
    """A small brief for fixture item work-a discharging each of its accepted obligations once."""
    return work_brief_models.SmallWorkBrief(
        "pinboard-small-work-brief/v1",
        1,
        "work-a-1",
        "work-a",
        "codex/work-a",
        "base-revision",
        "coordinator",
        work_brief_models.AcceptedScope(1, digest),
        work_models.CheckoutSelection.MAIN,
        "Make the state explicit",
        "The state becomes explicit.",
        tuple(
            work_brief_models.SmallScopeEntry(f"Discharge {value.obligation_id}.", (str(value.obligation_id),))
            for value in SQLITE_DEFINITION.obligations
        ),
        ("No storage change.",),
        "uv run --locked python -m unittest tests.test_small_item_route",
        work_brief_models.NoArchitectureImpact("One local module changes."),
    )


class SmallBriefRecordTest(CheckpointPackageSupport):
    def test_small_brief_decodes_canonically_and_rejects_invalid_shapes(self) -> None:
        brief = small_work_a_brief(SQLITE_DIGEST)
        data = work_briefs.canonical_work_brief_bytes(brief)
        self.assertEqual(brief, work_briefs.decode_canonical_accepted_brief(data))
        narrowed = work_briefs.decode_canonical_work_brief(data)
        assert isinstance(narrowed, work_brief_models.WorkBriefFailure)
        self.assertEqual(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, narrowed.code)
        payload = self.json_object(msgspec.json.decode(data))
        entry = self.json_object(self.json_array(payload["scope"])[0])
        for invalid in (
            payload | {"deferrals": []},
            payload | {"scope": []},
            payload | {"scope": [entry | {"obligations": []}]},
            payload | {"scope": [entry, entry]},
            payload | {"check": ["first check", "second check"]},
        ):
            with self.subTest(invalid=invalid):
                failure = work_briefs.decode_canonical_accepted_brief(
                    msgspec.json.encode(invalid, order="sorted") + b"\n"
                )
                assert isinstance(failure, work_brief_models.WorkBriefFailure)
                self.assertEqual(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, failure.code)

    def test_retained_v4_brief_bytes_are_unchanged_through_the_accepted_union(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            brief = work_a_brief(Path(directory))
        data = work_briefs.canonical_work_brief_bytes(brief)
        decoded = work_briefs.decode_canonical_accepted_brief(data)
        self.assertEqual(brief, decoded)
        assert isinstance(decoded, work_brief_models.WorkBrief)
        self.assertEqual(data, work_briefs.canonical_work_brief_bytes(decoded))
        self.assertEqual(brief, work_briefs.decode_canonical_historical_work_brief(data))

    def test_attempt_view_renders_outcome_scope_exclusions_and_check(self) -> None:
        brief = small_work_a_brief(SQLITE_DIGEST)
        view = work_briefs.render_work_brief_markdown(brief).decode()
        for fact in (brief.outcome, brief.scope[0].statement, brief.exclusions[0], brief.check, "`delivery`"):
            self.assertIn(fact, view)

    def test_small_starter_decodes_once_filled_and_the_full_contract_lists_it(self) -> None:
        starter = work_brief_contract.describe_work_brief_starter("small")
        self.assertEqual("small", starter.boundary)
        facts: JsonDict = {
            "artifact_revision": 1,
            "attempt_id": "work-a-1",
            "item_id": "work-a",
            "branch": "codex/work-a",
            "base_revision": "base-revision",
            "owner_task_id": "coordinator",
            "revision": 1,
            "digest": SQLITE_DIGEST,
            "checkout_selection": "main",
            "title": "Make the state explicit",
            "outcome": "The state becomes explicit.",
            "statement": "Discharge the obligation.",
            "check": "unittest",
            "reason": "One module.",
        }
        filled = self.json_object(_fill(msgspec.json.decode(starter.starter), facts))
        self.json_object(self.json_array(filled["scope"])[0])["obligations"] = ["next-decision"]
        decoded = work_briefs.decode_accepted_brief(msgspec.json.encode(filled))
        self.assertIsInstance(decoded, work_brief_models.SmallWorkBrief)
        contract = work_brief_contract.describe_work_brief_contract()
        self.assertEqual(starter.starter, contract.small_starter)
        schema = self.json_object(msgspec.json.decode(contract.small_payload_schema))
        self.assertIn("SmallWorkBrief", str(schema["$ref"]))


class SmallBriefPublicationTest(CheckpointPackageSupport):
    def published(
        self, definition: work_models.WorkItemDefinition, brief: work_brief_models.AcceptedBrief
    ) -> tuple[JsonDict, bool]:
        """Publish brief for work-a under definition; report the result and whether the ledger stayed unchanged."""
        digest = history.work_item_definition_digest(definition)
        assert isinstance(digest, str)
        state = complete_sqlite_state()
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                definition_revisions=tuple(
                    replace(row, definition=definition, digest=digest, after_digest=digest)
                    if row.item_id == WorkItemId("work-a")
                    else row
                    for row in state.lifecycle.definition_revisions
                ),
                attempts=tuple(
                    replace(row, accepted_scope_digest=digest) if row.item_id == WorkItemId("work-a") else row
                    for row in state.lifecycle.attempts
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(("git", "init", "--quiet", str(project)), check=True)
            roots = resolve_durable_roots(project)
            initialize_database(roots, SQLITE_NOW)
            initialize_store(SQLiteWorkStore(roots.database_path), state)
            before = SQLiteWorkStore(roots.database_path).validated_snapshot()
            selected = replace_struct(brief, accepted_scope=work_brief_models.AcceptedScope(1, digest))
            result = call_native_tool(
                mcp_server.BRIEF_PUBLISH_TOOL,
                {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                    "brief": msgspec.to_builtins(selected),
                },
            )
            unchanged = before == SQLiteWorkStore(roots.database_path).validated_snapshot()
            if result["status"] == "committed":
                reference = self.json_object(result["reference"])
                reloaded = work_briefs.decode_canonical_accepted_brief(
                    (roots.work_root / str(reference["selector"])).read_bytes()
                )
                self.assertEqual(selected, reloaded)
        return result, unchanged

    def route(self, size: work_models.SizeClass, *, checkpoints: bool) -> work_models.WorkItemDefinition:
        assert SQLITE_DEFINITION.route_facts is not None
        return replace(
            SQLITE_DEFINITION,
            checkpoints=(CheckpointId("delivery-part"),) if checkpoints else (),
            route_facts=replace(SQLITE_DEFINITION.route_facts, size_class=size, declares_checkpoints=checkpoints),
        )

    def test_route_gate_requires_the_small_brief_exactly_for_small_single_checkpoint_items(self) -> None:
        small = small_work_a_brief(SQLITE_DIGEST)
        with tempfile.TemporaryDirectory() as directory:
            v4 = replace_struct(work_a_brief(Path(directory)), checkout_selection=work_models.CheckoutSelection.MAIN)
        refused = (
            ("unclassified", replace(SQLITE_DEFINITION, route_facts=None), small),
            ("standard", self.route(work_models.SizeClass.STANDARD, checkpoints=False), small),
            ("campaign", self.route(work_models.SizeClass.CAMPAIGN, checkpoints=False), small),
            ("small with checkpoints", self.route(work_models.SizeClass.SMALL, checkpoints=True), small),
            ("qualifying v4", self.route(work_models.SizeClass.SMALL, checkpoints=False), v4),
        )
        for label, definition, brief in refused:
            with self.subTest(label):
                result, unchanged = self.published(definition, brief)
                self.assertEqual(("rejected", "WORK_BRIEF_INVALID"), (result["status"], result["code"]), result)
                self.assertTrue(unchanged)
                expected = (
                    "pinboard-work-brief/v4"
                    if isinstance(brief, work_brief_models.SmallWorkBrief)
                    else "pinboard-small-work-brief/v1"
                )
                self.assertIn(expected, str(result["message"]))
        accepted, _ = self.published(self.route(work_models.SizeClass.SMALL, checkpoints=False), small)
        self.assertEqual("committed", accepted["status"], accepted)

    def test_publication_requires_every_accepted_obligation_discharged_exactly_once(self) -> None:
        qualifying = self.route(work_models.SizeClass.SMALL, checkpoints=False)
        small = small_work_a_brief(SQLITE_DIGEST)
        for scope in (
            (work_brief_models.SmallScopeEntry("Unknown work.", ("unaccepted-obligation",)),),
            (*small.scope, work_brief_models.SmallScopeEntry("Extra work.", ("unaccepted-obligation",))),
        ):
            with self.subTest(scope=scope):
                result, unchanged = self.published(qualifying, replace_struct(small, scope=scope))
                self.assertEqual("rejected", result["status"], result)
                self.assertTrue(unchanged)
                self.assertIn("unaccepted-obligation", str(result["message"]))
