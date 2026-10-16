"""Render the complete selected roadmap offline; user text is always escaped."""

from html import escape
from typing import assert_never
from urllib.parse import quote, urlsplit

from pinboard.application import roadmap, stored_state
from pinboard.domain import initiatives
from pinboard.domain.identifiers import WorkItemId


def _link(target: str, label: str) -> str:
    try:
        scheme = urlsplit(target).scheme
    except ValueError:
        return escape(label)
    if scheme not in ("", "file", "https", "http"):
        return escape(label)
    return f'<a href="{escape(target, quote=True)}">{escape(label)}</a>'


def _note(note: initiatives.Provenance) -> str:
    reason = note.reason
    return f"{reason.date.isoformat()} · {reason.human}: {reason.reason} (recorded revision {note.project_revision}, {note.committed_at.isoformat()}, {note.task_id}@{note.host_id})"


def _placement(value: roadmap.Placement | None) -> str:
    match value:
        case roadmap.Wave(number=number):
            return f"Remaining wave {number}"
        case roadmap.Blocked(path=path):
            return f"Blocked by cancelled prerequisite: {' → '.join(path)}"
        case roadmap.Unknown(condition=condition):
            return f"Placement unknown: {condition}"
        case None:
            return "Terminal item"
        case _ as unreachable:
            assert_never(unreachable)


def _counts(value: roadmap.Counts) -> str:
    return f"{value.delivered} Delivered · {value.unfinished} unfinished · {value.cancelled} cancelled · {value.superseded} superseded"


def render(projection: roadmap.Projection) -> tuple[bytes, bytes]:  # noqa: C901, PLR0912, PLR0915 - one ordered complete offline projection
    """Return readable Markdown and self-contained HTML for exactly one immutable snapshot."""
    items = {item.item_id: item for item in projection.facts.items}
    appearances: dict[WorkItemId, int] = {}
    for initiative in projection.facts.initiatives:
        for member in initiative.memberships:
            if member.removed is None:
                appearances[member.item_id] = appearances.get(member.item_id, 0) + 1
    title = f"Roadmap · snapshot revision {projection.facts.project_revision}"
    freshness = "Read-only snapshot. Reload this file to see regenerated output. Dependency waves describe remaining prerequisites and grant no execution authority. Delivered records successful item completion, with no deployment or integration claim. Goal success requires a dated human assessment."
    text = [
        f"# {title}",
        "",
        freshness,
        "",
        "Project totals select distinct current initiative members plus unassigned live items; removed-only history and external prerequisite context receive no credit.",
        "",
        _counts(projection.totals),
        "",
    ]
    page = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pinboard roadmap</title><style>body{font:16px/1.6 system-ui,sans-serif;color:#182338;background:#f6f8fc;margin:auto;padding:clamp(1rem,3vw,3rem);max-width:100rem}a{color:#1850a3;overflow-wrap:anywhere}h1,h2,h3{line-height:1.2}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,24rem),1fr));gap:1.5rem}.lane{background:white;border:1px solid #cbd5e1;border-radius:.8rem;padding:1.25rem;min-width:0}article{border-top:1px solid #d9e2ef;padding:.7rem 0}details{margin:1rem 0}summary{cursor:pointer;font-weight:650}p,li{overflow-wrap:anywhere;white-space:pre-wrap}.muted{color:#536176}li{margin:.5rem 0}header{margin-bottom:2rem}</style><header>',
        f'<h1>{escape(title)}</h1><p>{escape(freshness)}</p><p>{escape(text[4])}</p><p><strong>{escape(_counts(projection.totals))}</strong></p><p><a href="board.html">Open the work board</a></p></header><main>',
    ]

    def card(item_id: WorkItemId) -> None:
        item = items.get(item_id)
        if item is None:
            message = f"Item '{item_id}' is unavailable; placement unknown."
            text.append(f"- {message}")
            page.append(f"<p>{escape(message)}</p>")
            return
        position = _placement(projection.placements.get(item_id))
        shared = " · Shared across initiatives" if appearances.get(item_id, 0) > 1 else ""
        target = f"items/{quote(item_id, safe='')}.md"
        text.append(f"- [{item.title}]({target}) · {item.state.value} · {position}{shared}")
        page.append(
            f'<article><strong>{_link(target, item.title)}</strong><p class="muted">{escape(item.state.value + " · " + position + shared)}</p>'
        )
        if item.dependencies:
            text.append(f"  Prerequisites: {', '.join(item.dependencies)}")
            page.append(
                "<p>Prerequisites: "
                + ", ".join(
                    _link(
                        f"items/{quote(dependency, safe='')}.md",
                        f"{dependency} ({items[dependency].state.value if dependency in items else 'unavailable'})",
                    )
                    for dependency in item.dependencies
                )
                + "</p>"
            )
        page.append("</article>")

    def note(value: str) -> None:
        text.append(f"- {value}")
        page.append(f"<li>{escape(value)}</li>")

    for initiative in projection.facts.initiatives:
        revision = initiative.revisions[-1]
        definition = revision.definition
        current = tuple(member.item_id for member in initiative.memberships if member.removed is None)
        assessment = next(
            (
                value
                for value in reversed(initiative.assessments)
                if value.evaluation_context == revision.evaluation_context
            ),
            None,
        )
        judgment = (
            "Not assessed"
            if assessment is None
            else f"{assessment.judgment.value} · {assessment.provenance.reason.date.isoformat()}"
        )
        text.extend(
            (
                f"## {definition.title}",
                "",
                f"Goal: {definition.goal}",
                f"Rationale: {definition.rationale}",
                f"Success signal: {definition.success_signal}",
                f"Goal assessment: {judgment}",
                _counts(roadmap.counts(tuple(items[item_id] for item_id in current if item_id in items))),
                "",
            )
        )
        page.append(
            f'<section class="lane"><h2>{escape(definition.title)}</h2><p><strong>Goal:</strong> {escape(definition.goal)}</p><p>{escape(definition.rationale)}</p><p><strong>Success signal:</strong> {escape(definition.success_signal)}</p><p><strong>Goal assessment:</strong> {escape(judgment)}</p><p>{escape(text[-2])}</p>'
        )
        if definition.project_references:
            text.append("Project references: " + ", ".join(definition.project_references))
            page.append(
                "<p>Project references: "
                + ", ".join(_link(reference, reference) for reference in definition.project_references)
                + "</p>"
            )
        remaining = tuple(
            item.item_id
            for item in projection.facts.items
            if item.item_id in current and roadmap.is_unfinished(item.state)
        )

        def wave_order(item_id: WorkItemId) -> float:
            placement = projection.placements[item_id]
            return placement.number if isinstance(placement, roadmap.Wave) else float("inf")

        for item_id in sorted(remaining, key=wave_order):
            card(item_id)
        for label, state in (
            ("Delivered", stored_state.StoredWorkItemState.DONE),
            ("Cancelled", stored_state.StoredWorkItemState.DROPPED),
            ("Superseded", stored_state.StoredWorkItemState.SUPERSEDED),
        ):
            selected = tuple(item_id for item_id in current if item_id in items and items[item_id].state == state)
            text.extend((f"### {label} ({len(selected)})", ""))
            page.append(f"<details><summary>{label} ({len(selected)})</summary>")
            for item_id in selected:
                card(item_id)
            page.append("</details>")
        for item_id in current:
            if item_id not in items:
                card(item_id)
        text.extend(("### Membership history", ""))
        page.append("<details><summary>Complete membership history</summary><ul>")
        for member in initiative.memberships:
            note(f"{member.item_id}, episode {member.episode}, added: {_note(member.added)}")
            if member.removed is not None:
                note(
                    f"{member.item_id}, episode {member.episode}, removed: {_note(member.removed)}. Removed membership earns no delivery credit."
                )
        page.append("</ul></details><details><summary>Complete decisions and assessment history</summary><ul>")
        text.extend(("### Decisions and assessment history", ""))
        for decision in initiative.decisions:
            note(f"Decision: {decision.decision}. {_note(decision.provenance)}")
            for link in decision.supporting_links:
                page.append(f"<li>{_link(link, link)}</li>")
                text.append(f"- Evidence: {link}")
        for value in initiative.assessments:
            note(
                f"Assessment: {value.judgment.value}. Evaluated goal: {value.evaluated_goal}. Evaluated success signal: {value.evaluated_signal}. Context {value.evaluation_context}. {_note(value.provenance)}"
            )
            for link in value.supporting_links:
                page.append(f"<li>{_link(link, link)}</li>")
                text.append(f"- Evidence: {link}")
        page.append("</ul></details><details><summary>Initiative revision history</summary><ul>")
        text.extend(("### Initiative revision history", ""))
        for value in initiative.revisions:
            note(
                f"Revision {value.revision}, context {value.evaluation_context}: {value.definition.title}. Goal: {value.definition.goal}. Rationale: {value.definition.rationale}. Success signal: {value.definition.success_signal}. References: {', '.join(value.definition.project_references)}. {_note(value.provenance)}"
            )
        page.append("</ul></details></section>")
    for label, selected in (
        ("Unassigned live work", projection.unassigned),
        (
            "External prerequisite context",
            projection.external_prerequisites,
        ),
    ):
        text.extend((f"## {label}", ""))
        page.append(f'<section class="lane"><h2>{label}</h2>')
        for item_id in selected:
            card(item_id)
        page.append("</section>")
    page.append("</main></html>")
    return ("\n".join(text) + "\n").encode(), "\n".join(page).encode()
