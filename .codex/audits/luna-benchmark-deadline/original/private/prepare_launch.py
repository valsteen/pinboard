from pathlib import Path
import hashlib
import json

root = Path(__file__).resolve().parents[1]
old = root.with_name('model-benchmark')
models = {'sonnet': ('claude-sonnet-5-5', 'high'), 'opus': ('claude-opus-5-5', 'high'), 'astra': ('gpt-6-astra', 'low'), 'sol': ('gpt-6.1-sol', 'high'), 'luna': ('gpt-6-luna', 'high'), 'luna-xhigh': ('gpt-6-luna', 'xhigh')}
for arm, (model, effort) in models.items():
    project = root / arm
    denied = ['/Users/vincentalsteen/projects', str(old / 'private'), str(root / 'private')]
    denied += [str(old / name) for name in models]
    denied += [str(root / name) for name in models if name != arm]
    profile = '(version 1)\n(allow default)\n'
    profile += ''.join('(deny file-read* file-write* (subpath ' + json.dumps(p) + '))\n' for p in denied)
    profile += '(deny file-write* (subpath ' + json.dumps(str(old / 'base')) + '))\n'
    (root / 'private' / (arm + '.sb')).write_text(profile)
    brief_path = project / '.pinboard/artifacts/briefs/expose-review-and-branch-facts-in-status-1/1.json'
    if not brief_path.exists():
        continue
    brief = brief_path.read_bytes()
    prompt = f'''Deliver the accepted integration-by-content checkpoint using Pinboard in this isolated historical replay. You own the entire workflow: implementation, verification, commissioning a fresh independent reviewer, evaluating that review, corrections, final disposition, and the finished pull-request report. Every participating agent must use {model} at {effort} effort, including reviewers. The user explicitly authorizes native subagents for this workflow. Do not select another model or effort.

The historical brief and definition have already been restored, and the attempt is active. Read the installed Pinboard skills relevant to your role. You may implement directly as the attempt worker, then act as the owning coordinator for review, or delegate implementation to a fresh same-model worker. The user explicitly authorizes direct CLI implementation of this replay's preserved accepted brief without repeating initial brief preparation or requiring a new initial brief-ready review. This only replaces the initial worker-launch envelope if you implement directly; subsequent native review and correction workflows remain yours to perform. Review must use a fresh same-model context through the available native agent tool, commissioned through Pinboard. Do not self-review in place of that reviewer.

Use only this checkout and its scratch board. Do not access other projects, other benchmark runs, GitHub history, or later solutions. The original repository and all other candidates are outside your scope. Runtime host is VincentacStudio; use your real current runtime identity, never a historical identity. The main checkout of this standalone repository is the assigned isolated environment; no additional checkout is needed. Base setup has completed successfully. The stable installed plugin supplies coordination; verification of your changes must run from your candidate checkout.

The observing session will not review code, supply findings, choose fixes, or decide whether your work is ready. It will relay genuine human questions and answers; previously approved answers may be reused only when your question has the same scope. Do not invent an answer or treat silence as approval. If you need the human, stop and ask in your final reply; your session will resume with the answer. Otherwise own the complete workflow to its conclusion. The overall experiment has a fresh $100 API-equivalent cap, including all agents and reviews. The run has at most 180 active minutes, excluding human waiting. Report incomplete work honestly if a limit prevents finishing.

The outcome is an assessment-only draft PR, labeled model-benchmark and integration-replay-v2-20261003 and do not merge, then closed without merging. Write the finished PR title to .pinboard/benchmark-pr-title.txt and description to .pinboard/benchmark-pr-description.md. Describe the delivered behavior, verification, review and correction outcomes, remaining issues and human decisions truthfully. These are your words and judgments. The observer will transport your unchanged candidate and exact text to GitHub mechanically after you finish; no external write or remote read is needed in this checkout. Local commits are authorized. Never merge into main. Do not claim publication before it occurs. End with RUN_COMPLETE when your workflow and report are finished, or a concrete human question when blocked.

Project root: {project}
Work root: {project / '.pinboard'}
Attempt: expose-review-and-branch-facts-in-status-1
Checkpoint: integration-by-content
Canonical brief: {brief_path}
SHA-256: {hashlib.sha256(brief).hexdigest()}
Size: {len(brief)} bytes

----- BEGIN CANONICAL BRIEF -----
''' + brief.decode() + '\n----- END CANONICAL BRIEF -----\n'
    (root / 'private' / ('prompt-' + arm + '.txt')).write_text(prompt)

