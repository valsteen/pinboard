from pathlib import Path
import argparse
import dataclasses
import json
import os
import sys
import time
import signal
import threading
from evals.behavioral import credentials, codex_driver, processes
import benchmark_limits as limits

parser = argparse.ArgumentParser(description='Run one benchmark arm with an explicit cumulative active-time budget.')
parser.add_argument('arm', choices=('luna', 'luna-xhigh', 'sol', 'astra'))
parser.add_argument('mode', choices=('probe', 'implement'))
parser.add_argument('--maximum-active-seconds', type=int, required=True, help=f'Positive active-time budget in seconds, at most {limits.MAXIMUM_ACTIVE_SECONDS} seconds; human waiting is excluded.')
selection = parser.parse_args()
if not 0 < selection.maximum_active_seconds <= limits.MAXIMUM_ACTIVE_SECONDS:
    parser.error(f'--maximum-active-seconds must be positive and at most {limits.MAXIMUM_ACTIVE_SECONDS} seconds')
maximum_active_seconds = selection.maximum_active_seconds
root = Path(__file__).resolve().parents[1]
base = root.with_name('model-benchmark') / 'base'
os.environ.pop('VIRTUAL_ENV', None)
arm = selection.arm
implementation = selection.mode == 'implement'
model, effort = {'luna': ('gpt-6-luna', 'high'), 'sol': ('gpt-6.1-sol', 'high'), 'astra': ('gpt-6-astra', 'low'), 'luna-xhigh': ('gpt-6-luna', 'xhigh')}[arm]
project = root / arm
out = root / 'private' / arm
out.mkdir(exist_ok=True)

def watch_budget(home):
    limit = limits.CODEX_RESERVED_CAP_USD[arm]
    prices = limits.CODEX_PRICES_PER_MILLION[arm]
    while True:
        time.sleep(limits.BUDGET_POLL_SECONDS)
        seen = {}
        for path in (home / 'sessions').rglob('*.jsonl'):
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get('type') == 'token_usage_record':
                    p = row['payload']
                    seen.setdefault(p['response_id'], p['usage'])
        cost = 0
        for u in seen.values():
            a, b, c = prices
            if u['input_tokens'] > limits.LONG_CONTEXT_INPUT_TOKENS:
                a, b, c = a*limits.LONG_CONTEXT_INPUT_PRICE_MULTIPLIER, b*limits.LONG_CONTEXT_INPUT_PRICE_MULTIPLIER, c*limits.LONG_CONTEXT_OUTPUT_PRICE_MULTIPLIER
            cost += ((u['input_tokens']-u.get('cached_input_tokens',0))*a+u.get('cached_input_tokens',0)*b+u['output_tokens']*c)/limits.TOKENS_PER_PRICE_UNIT
        (out / 'budget-observation.json').write_text(json.dumps({'cost_estimate':cost,'reserved_cap':limit,'includes_all_sessions':True}))
        if cost >= limit - min(limits.BUDGET_RESERVATION_MAX_USD, limit*limits.BUDGET_RESERVATION_FRACTION):
            print(json.dumps({'arm':arm,'stop':'budget-reservation','cost_estimate':cost,'reserved_cap':limit}),flush=True)
            os.kill(os.getpid(), signal.SIGINT)
            return
(project / '.codex/audits').mkdir(parents=True, exist_ok=True)
with credentials.exclusive_codex_session(root / 'private', processes.Window(None)):
    window = processes.Window(time.monotonic() + limits.CODEX_SETUP_SECONDS)
    with credentials.isolated_home(credentials.default_source(), None, processes.Window(None)) as home:
        codex_driver.write_config(home.path, base, model, effort, 'user', window)
        config = home.path / 'config.toml'
        with config.open('a') as f:
            f.write('\n[agents]\nenabled = true\ndefault_subagent_model = ' + json.dumps(model) + '\ndefault_subagent_reasoning_effort = ' + json.dumps(effort) + '\n')
        denied = [str(root.with_name('model-benchmark') / a) for a in ('opus', 'sonnet', 'astra', 'sol', 'luna', 'luna-xhigh')] + ['/Users/vincentalsteen/projects', str(root / 'private'), str(root.with_name('model-benchmark') / 'private')] + [str(root / a) for a in ('opus', 'sonnet', 'astra', 'sol', 'luna', 'luna-xhigh') if a != arm]
        with config.open('a') as stream:
            stream.write('\n[permissions.' + codex_driver.PERMISSION_PROFILE + '.filesystem]\n')
            for path in denied:
                stream.write(json.dumps(path) + ' = "deny"\n')
            stream.write(json.dumps(str(base)) + ' = "read"\n')
            stream.write(json.dumps(str(project / '.git')) + ' = "write"\n')
            stream.write(json.dumps(str(project / '.codex/audits')) + ' = "write"\n')
            stream.write(json.dumps(str(Path.home() / '.cache/uv')) + ' = "write"\n')
        manifests = list((home.path / 'plugins/cache').glob('pinboard/pinboard/*/mcp-codex.json'))
        assert len(manifests) == 1, manifests
        manifest = manifests[0]
        data = json.loads(manifest.read_text())
        server = data['mcpServers']['pinboard']
        server['command'] = '/usr/bin/sandbox-exec'
        server['args'] = ['-f', str(root / 'private' / (arm + '.sb')), 'sh', str(base / 'scripts/pinboard'), '--mcp']
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        context = codex_driver.loaded_context(home.path, project, window)
        (out / 'context.json').write_text(json.dumps(dataclasses.asdict(context), indent=2, default=str))
        print(json.dumps({'arm': arm, 'home': str(home.path), 'phase': 'configured'}), flush=True)
        threading.Thread(target=watch_budget,args=(home.path,),daemon=True).start()
        prompt = 'This is a read-only startup probe. Do not implement, acquire authority, or mutate any board. Read the complete installed Pinboard delivery skill, then use the connected Pinboard MCP item-status tool to read item expose-review-and-branch-facts-in-status at project_root ' + str(project) + ' and work_root ' + str(project / '.pinboard') + '. Report whether it is active and its attempt identifier. Do not access any other project or board. End your reply with PROBE_COMPLETE if those exact operations succeeded.'
        if implementation:
            prompt = (root / 'private' / ('prompt-' + arm + '.txt')).read_text()
        thread = None
        active_seconds = 0.0
        turn = 0
        while True:
            remaining_seconds = maximum_active_seconds - active_seconds
            if remaining_seconds <= 0:
                break
            turn += 1
            label = 'implementation-' + str(turn) if implementation else 'probe'
            started = time.monotonic()
            timed_prompt = f'The run has at most {maximum_active_seconds} active seconds across all turns, excluding human waiting; {remaining_seconds:g} active seconds remain.\n\n' + prompt
            arguments = ['exec', '--strict-config', '--json', '-C', str(project), timed_prompt] if thread is None else ['exec', 'resume', '--strict-config', '--json', thread, timed_prompt]
            try:
                completed = codex_driver.codex(arguments, home=home.path, cwd=project, timeout_seconds=remaining_seconds, window=processes.Window(started + remaining_seconds))
            except (processes.ProcessIncomplete, processes.CleanupUnconfirmed) as error:
                (out / (label + '.jsonl')).write_text(error.stdout)
                (out / (label + '-rollout.jsonl')).write_text(credentials.without_login(codex_driver.rollout_text(home.path), home.copied))
                raise
            (out / (label + '.jsonl')).write_text(completed.stdout)
            (out / (label + '.stderr')).write_text(completed.stderr)
            reading = codex_driver.read_events(completed.stdout)
            elapsed = time.monotonic()-started
            active_seconds += elapsed
            thread = reading.thread_id or thread
            summary = {'arm':arm,'phase':label,'seconds':elapsed,'active_seconds':active_seconds,'maximum_active_seconds':maximum_active_seconds,'exit':completed.returncode,'reading':dataclasses.asdict(reading)}
            (out / (label + '-summary.json')).write_text(json.dumps(summary,default=str,indent=2))
            print(json.dumps(summary,default=str),flush=True)
            rollout = credentials.without_login(codex_driver.rollout_text(home.path), home.copied)
            (out / (label + '-rollout.jsonl')).write_text(rollout)
            if reading.messages and 'RUN_COMPLETE' in reading.messages[-1]:
                break
            if not implementation or active_seconds >= maximum_active_seconds:
                break
            print('WAITING_FOR_HUMAN_OR_PUBLICATION: send a JSON string to resume, or JSON null to close this session.',flush=True)
            line = sys.stdin.readline()
            if not line:
                break
            prompt = json.loads(line)
            if prompt is None:
                break

    print(json.dumps({"arm": arm, "credential_settlement": home.settlement.value if home.settlement is not None else None}), flush=True)
    if home.settlement is credentials.CredentialSettlement.REFUSED_SOURCE_CHANGED:
        raise RuntimeError("Credential source changed during isolated run; stop later paid runs pending diagnosis")
