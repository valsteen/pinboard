"""Seed a disposable world's boards to the states the scenarios' world facts describe.

Every board mutation goes through the evaluated revision's own MCP tools (see ``board``); Git effects model the
maintainer's and workers' repository actions. The seeded host id is the fixed synthetic ``SEEDED_HOST_ID``.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from evals.behavioral import processes
from evals.behavioral.board import (
    SEEDED_AT,
    SEEDED_HOST_ID,
    AcceptedScope,
    ActionId,
    Actions,
    ActivatePayload,
    ArchitectureImpact,
    AttemptInspect,
    AuthorityAcquire,
    AuthorityEnvelope,
    AuthorityRelease,
    AuthorizationBasis,
    BoardClient,
    Brief,
    BriefPublish,
    Checkpoint,
    Correspondence,
    CorrespondenceTarget,
    Criterion,
    Definition,
    DefinitionCurrent,
    Disposition,
    Enveloped,
    InitialReview,
    Inspection,
    Lease,
    LeasedActions,
    LeasedTransition,
    Obligation,
    PreparationStart,
    ProjectActions,
    ProjectTransition,
    Proposal,
    ProposalCreate,
    Published,
    ReasonPayload,
    Receipt,
    RecordReady,
    Relation,
    ReviewCommission,
    ReviewedCompletion,
    ReviewJob,
    Status,
    SubmitPayload,
    Verification,
    require,
)
from evals.behavioral.export import SeedFailure


@dataclass(frozen=True)
class Seeder:
    board: BoardClient
    project: Path
    work_root: Path
    owner: str

    def worktree(self, item: str) -> Path:
        return self.project.parent / f"wt-{item}"

    async def propose(self, item: str, label: str, texts: tuple[str, str, str, str, str], follows: str | None) -> None:
        trigger, why, effect, unlock, statement = texts
        request = ProposalCreate(
            project_root=str(self.project),
            work_root=str(self.work_root),
            actor_task_id=self.owner,
            actor_host_id=SEEDED_HOST_ID,
            proposal=Proposal(
                schema="pinboard-proposal/v2",
                proposal_id=item,
                created_at=SEEDED_AT,
                source_task_id=self.owner,
                user_label=label,
                trigger=trigger,
                evidence=["Observed in tally.sh at the initial commit"],
                why_it_matters=why,
                relation=Relation(kind="independent", item=None)
                if follows is None
                else Relation(kind="follow-up", item=follows),
                effect=effect,
                unlock=unlock,
                urgency_evidence="none observed",
                freshness_assumptions=["tally.sh is unchanged on main"],
                checkout_policy="isolated",
                obligations=[Obligation(obligation_id="outcome", statement=statement, deferral_policy="forbidden")],
            ),
        )
        result = await self.board.call("pinboard_proposal_create", request, Status)
        require(result.status, ("committed", "committed-with-warning"), f"propose {item}")

    async def prepare_activate(self, item: str, title: str, criterion: str, scope: str) -> None:
        worktree = self.worktree(item)
        processes.git_checked(
            ["worktree", "add", "-q", str(worktree), "-b", f"pinboard/{item}"],
            cwd=self.project,
            window=self.board.window,
        )
        base = processes.git_checked(["rev-parse", "HEAD"], cwd=worktree, window=self.board.window).strip()
        preparation = await self.board.call(
            "pinboard_preparation_authority",
            Enveloped(
                request=PreparationStart(
                    operation="start",
                    project_root=str(self.project),
                    work_root=str(self.work_root),
                    item_id=item,
                    task_id=self.owner,
                    host_id=SEEDED_HOST_ID,
                    ttl_seconds=86400,
                )
            ),
            Lease,
        )
        require(preparation.status, ("committed",), f"prepare {item}")
        definition = await self.board.call(
            "pinboard_item_definition",
            Enveloped(
                request=DefinitionCurrent(
                    operation="current", project_root=str(self.project), work_root=str(self.work_root), item_id=item
                )
            ),
            Definition,
        )
        published = await self.board.call(
            "pinboard_brief_publish",
            BriefPublish(
                project_root=str(worktree),
                work_root=str(self.work_root),
                brief=local_brief(item, title, criterion, scope, base, self.owner, definition),
            ),
            Published,
        )
        require(published.status, ("committed",), f"publish {item}")
        activate = ActionId(kind="activate", subject=item)
        actions = await self.board.call(
            "pinboard_actions",
            Enveloped(
                request=LeasedActions(
                    role="preparer",
                    project_root=str(worktree),
                    work_root=str(self.work_root),
                    lease_id=preparation.lease_id,
                    generation=preparation.generation,
                    action_id=activate,
                )
            ),
            Actions,
        )
        transition = await self.board.call(
            "pinboard_transition",
            Enveloped(
                request=LeasedTransition(
                    role="preparer",
                    project_root=str(worktree),
                    work_root=str(self.work_root),
                    lease_id=preparation.lease_id,
                    generation=preparation.generation,
                    receipt=Receipt(action_id=activate, subject_revision=first_revision(actions, f"activate {item}")),
                    payload=ActivatePayload(brief_artifact_ref_id=published.reference.artifact_ref_id),
                )
            ),
            Status,
        )
        require(transition.status, ("committed",), f"activate {item}")

    async def worker_acquire(self, item: str, task: str, ttl_seconds: int) -> Lease:
        lease = await self.board.call(
            "pinboard_attempt_authority",
            AuthorityEnvelope(
                request=AuthorityAcquire(
                    operation="acquire",
                    project_root=str(self.worktree(item)),
                    work_root=str(self.work_root),
                    attempt_id=f"{item}-1",
                    task_id=task,
                    host_id=SEEDED_HOST_ID,
                    ttl_seconds=ttl_seconds,
                )
            ),
            Lease,
        )
        require(lease.status, ("committed",), f"acquire {item}")
        return lease

    async def worker_submit(self, item: str, lease: Lease, text: str) -> None:
        worktree = self.worktree(item)
        attempt = f"{item}-1"
        candidate = head(worktree, self.board.window)
        result = self.work_root / "attempts" / attempt / "result.md"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(
            f"# Result\n\nCandidate: commit {candidate} on branch pinboard/{item}.\n\n{text}\n\n"
            "Verification: ./test.sh passed.\n"
        )
        submit = ActionId(kind="submit-review", subject=attempt)
        actions = await self.board.call(
            "pinboard_actions",
            Enveloped(
                request=LeasedActions(
                    role="worker",
                    project_root=str(worktree),
                    work_root=str(self.work_root),
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                    action_id=submit,
                )
            ),
            Actions,
        )
        submitted = await self.board.call(
            "pinboard_transition",
            Enveloped(
                request=LeasedTransition(
                    role="worker",
                    project_root=str(worktree),
                    work_root=str(self.work_root),
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                    receipt=Receipt(action_id=submit, subject_revision=first_revision(actions, f"submit {item}")),
                    payload=SubmitPayload(candidate=candidate),
                )
            ),
            Status,
        )
        require(submitted.status, ("committed",), f"submit {item}")
        released = await self.board.call(
            "pinboard_attempt_authority",
            AuthorityEnvelope(
                request=AuthorityRelease(
                    operation="release",
                    project_root=str(worktree),
                    work_root=str(self.work_root),
                    attempt_id=attempt,
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                )
            ),
            Status,
        )
        require(released.status, ("committed",), f"release {item}")

    async def review_publish(self, item: str, text: str) -> str:
        worktree = self.worktree(item)
        attempt = f"{item}-1"
        candidate = head(worktree, self.board.window)
        job = await self.board.call(
            "pinboard_review_job",
            ReviewJob(
                project_root=str(worktree),
                work_root=str(self.work_root),
                review=InitialReview(
                    kind="initial",
                    attempt_id=attempt,
                    candidate_revision=candidate,
                    runtime="claude-code",
                    background=False,
                ),
            ),
            ReviewCommission,
        )
        require(job.status, ("ready",), f"review job {item}")
        (self.work_root / "attempts" / attempt / "review.md").write_text(
            f"# Review of {attempt}\n\nReviewer: separate Claude Code reviewer (task review-{item})\n"
            f"Reviewed candidate: commit {candidate} on branch pinboard/{item}\n\n{text}\n"
        )
        return job.prompt_reference.sha256

    async def review_ready(self, item: str, prompt_sha256: str) -> None:
        worktree = self.worktree(item)
        attempt = f"{item}-1"
        candidate = head(worktree, self.board.window)
        inspection = await self.board.call(
            "pinboard_attempt_inspect",
            AttemptInspect(
                project_root=str(worktree), work_root=str(self.work_root), attempt_id=attempt, reconciliation=None
            ),
            Inspection,
        )
        recorded = await self.board.call(
            "pinboard_review_job",
            ReviewJob(
                project_root=str(worktree),
                work_root=str(self.work_root),
                review=RecordReady(
                    kind="record-ready",
                    attempt_id=attempt,
                    candidate_revision=candidate,
                    candidate_snapshot_sha256=inspection.candidate_recovery.sha256,
                    accepted_brief_sha256=inspection.accepted_brief.sha256,
                    result_sha256=self.evidence_sha256(attempt, "result.md"),
                    review_sha256=self.evidence_sha256(attempt, "review.md"),
                    reviewer_prompt_sha256=prompt_sha256,
                    reviewer_task_id=f"review-{item}",
                    reviewer_prompt_sha256=prompt_sha256,
                    verdict="ready",
                    acceptance_evidence="Separate reviewer found the candidate satisfies the accepted criterion",
                ),
            ),
            Status,
        )
        require(recorded.status, ("recorded",), f"record-ready {item}")

    async def project_transition(
        self, kind: str, subject: str, payload: ReasonPayload | ReviewedCompletion, root: Path
    ) -> None:
        action = ActionId(kind=kind, subject=subject)
        actions = await self.board.call(
            "pinboard_actions",
            Enveloped(
                request=ProjectActions(
                    role="project", project_root=str(root), work_root=str(self.work_root), action_id=action
                )
            ),
            Actions,
        )
        result = await self.board.call(
            "pinboard_transition",
            Enveloped(
                request=ProjectTransition(
                    role="project",
                    project_root=str(root),
                    work_root=str(self.work_root),
                    receipt=Receipt(action_id=action, subject_revision=first_revision(actions, f"{kind} {subject}")),
                    payload=payload,
                    actor_task_id=self.owner,
                    actor_host_id=SEEDED_HOST_ID,
                )
            ),
            Status,
        )
        require(result.status, ("committed",), f"transition {kind}:{subject}")

    async def complete_reviewed(self, item: str, evidence: str) -> None:
        attempt = f"{item}-1"
        completion = ReviewedCompletion(
            schema="pinboard-reviewed-completion/v2",
            candidate=head(self.worktree(item), self.board.window),
            evidence=evidence,
            reviewer_task_id=f"review-{item}",
            result_sha256=self.evidence_sha256(attempt, "result.md"),
            review_sha256=self.evidence_sha256(attempt, "review.md"),
            packages=[],
        )
        await self.project_transition("complete", attempt, completion, self.worktree(item))

    def commit_change(self, item: str, file: str, message: str, content: str) -> None:
        worktree = self.worktree(item)
        (worktree / file).write_text(content)
        processes.git_checked(["add", file], cwd=worktree, window=self.board.window)
        processes.git_checked(["commit", "-q", "-m", message], cwd=worktree, window=self.board.window)

    def evidence_sha256(self, attempt: str, name: str) -> str:
        return hashlib.sha256((self.work_root / "attempts" / attempt / name).read_bytes()).hexdigest()


def head(checkout: Path, window: processes.Window) -> str:
    return processes.git_checked(["rev-parse", "HEAD"], cwd=checkout, window=window).strip()


def first_revision(actions: Actions, what: str) -> str:
    if not actions.actions:
        raise SeedFailure(f"no legal action for {what}")
    return actions.actions[0].subject_revision


def local_brief(
    item: str, title: str, criterion: str, scope: str, base: str, owner: str, definition: Definition
) -> Brief:
    basis = AuthorizationBasis(item_id=item, kind="accepted-scope", scope_revision=definition.definition_revision)
    return Brief(
        accepted_scope=AcceptedScope(digest=definition.definition_digest, revision=definition.definition_revision),
        artifact_revision=1,
        attempt_id=f"{item}-1",
        base_revision=base,
        bootstrap=[],
        branch=f"pinboard/{item}",
        checkout_selection="isolated",
        checkpoint=Checkpoint(
            acceptance_criteria=[Criterion(number=1, requirement=criterion)],
            architecture_impact=ArchitectureImpact(kind="none", reason="Changes stay inside tally.sh and its docs"),
            boundary="local",
            checkpoint_id="outcome",
            deferrals=[],
            disposition=Disposition(kind="terminal"),
            outcome_description=criterion,
            title=title,
            verification=[Verification(authorization_basis=basis, obligation="Run ./test.sh")],
        ),
        compatibility=[],
        item_id=item,
        non_goals=[],
        obligation_correspondence=[
            Correspondence(obligation_id="outcome", target=CorrespondenceTarget(kind="criterion", number=1))
        ],
        outcome=criterion,
        owner_task_id=owner,
        product_decision_and_provenance="Requested by the maintainer in conversation",
        schema="pinboard-work-brief/v4",
        scope=[scope],
        supported_production_roots=["tally.sh", "docs/usage.md"],
        testing_strategy="Run ./test.sh",
        title=title,
    )
