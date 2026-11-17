"""Sent over stdin to named hosts. No writes, generation, credentials returned or shell input."""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=2, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def get(url, headers=None, text=False):
    try:
        request = urllib.request.Request(url, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            raw = response.read(1048576)
            return {'status': 'online', 'data': raw.decode() if text else json.loads(raw)}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError, UnicodeDecodeError) as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        return {'status': 'auth required' if getattr(e, 'code', 0) in (401, 403) else 'offline'}


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def job_result(raw, job):
    values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    # Successful jobs remain active/exited because the launcher sets RemainAfterExit.
    terminal = values.get('LoadState') == 'loaded' and (values.get('SubState') in ('exited', 'failed') or values.get('ActiveState') == 'failed')
    return {'job': job, 'terminal': terminal, 'state': values.get('SubState', 'unknown'),
            'result': values.get('Result'), 'exit_code': values.get('ExecMainStatus')}


def log_result(log):
    with log.open('rb') as stream:
        stream.seek(max(0, log.stat().st_size - 262144))
        text = stream.read(262144).decode(errors='replace')
    start = re.search(r'(?m)^(\d\d:\d\d:\d\d) quiet confirmed .*?swapping to (\S+)', text)
    end = None
    if start:
        end = re.search(
            r'(?m)^(\d\d:\d\d:\d\d) (SWAP OK|rollback ok|rollback start failed|rollback gates failed)',
            text[start.end():])
    duration = None
    if start and end:
        seconds = lambda t: sum(int(v) * m for v, m in zip(t.split(':'), [3600, 60, 1]))
        duration = (seconds(end[1]) - seconds(start[1])) % 86400
    return {'profile': start[2] if start else 'unknown', 'started_at': log.stem.removeprefix('swap-'),
            'duration_s': duration, 'status': end[2] if end else 'incomplete',
            'gates': {'basic_helper': 'passed' if 'gates ok' in text else 'not recorded',
                      'exactness': 'failed' if 'EXACTNESS MISMATCH' in text else 'not independently recorded',
                      'full_acceptance': 'not established by this log'}, 'source': log.name}


def probe(host, job=None):
    result: dict[str, Any] = {'status': 'online', 'services': []}
    if host == 'build-host':
        total = command(['sysctl', '-n', 'hw.memsize']).strip()
        result['memory_total_gib'] = int(total) / 2**30 if total.isdigit() else None
        vm = command(['vm_stat'])
        page = re.search(r'page size of (\d+) bytes', vm)
        fields = dict(re.findall(r'([^\n:]+):\s+(\d+)\.', vm))
        result['memory_available_gib'] = (sum(int(fields.get(k, 0)) for k in ['Pages free', 'Pages inactive', 'Pages speculative']) * int(page.group(1)) / 2**30) if page else None
        try:
            key = (Path.home() / 'work/local-inference/api-key').read_text().strip()
        except OSError:
            key = ''
        auth = {'Authorization': 'Bearer ' + key} if key else {}
        for path in ['/v1/models', '/health', '/metrics']:
            result[path] = get(os.environ.get('MODELDECK_BUILD_HOST_URL', 'http://127.0.0.1:8011') + path, auth, text=path == '/metrics')
        result['services'].append({'name': 'mac-qwen3.5-9b', 'port': 8011, **result['/v1/models']})
    elif host in ('cluster-a', 'cluster-b', 'client'):
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                if k in ('MemTotal', 'MemAvailable'):
                    mem[k] = int(v.strip().split()[0]) / 2**20
        result.update(memory_total_gib=mem.get('MemTotal'), memory_available_gib=mem.get('MemAvailable'))
        if host == 'client':
            data = get('http://127.0.0.1:8742/v1/models')
            result['services'].append({'name': 'qwen3.5-2b', 'port': 8742, **data})
            result['/metrics'] = get('http://127.0.0.1:8742/metrics', text=True)
        if host == 'cluster-b':
            data = get('http://127.0.0.1:11434/api/tags')
            loaded = get('http://127.0.0.1:11434/api/ps')
            result['services'].append({'name': 'Ollama embeddings', 'port': 11434, **data, 'loaded': loaded})
        if host == 'cluster-a':
            result['profiles'] = load(Path.home() / 'ops/state/cluster-profiles.json')
            result['swap'] = load(Path.home() / 'ops/state/cluster-swap-state.json')
            helper = Path.home() / 'ops-cluster-improve-exec/bin/swap-gate'
            result['gated_swap_available'] = helper.is_file() and os.access(helper, os.X_OK)
            if isinstance(job, str) and re.fullmatch(r'modeldeck-swap-[0-9a-f]{12}', job):
                raw = command(['systemctl', '--user', 'show', job, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result', '-p', 'ExecMainStatus'])
                result['action_job'] = job_result(raw, job)
            result['swap_logs'] = []
            for log in sorted((Path.home() / 'ds-swap').glob('swap-*.log'))[-20:]:
                try:
                    result['swap_logs'].append(log_result(log))
                except OSError:
                    continue
    return result


if __name__ == '__main__':
    print(json.dumps(probe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
