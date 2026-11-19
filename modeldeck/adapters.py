"""Read-only, bounded adapters. Missing measurements stay null, not invented zeros."""
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_BYTES = 2 * 1024 * 1024
CLUSTER_GATEWAY_URL = os.environ.get('MODELDECK_CLUSTER_GATEWAY', 'http://127.0.0.1:8642')
BUILD_HOST_URL = os.environ.get('MODELDECK_BUILD_HOST_URL', 'http://127.0.0.1:8011')


def fetch(url, headers=None, timeout=3, text=False):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with OPENER.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('source response exceeds limit')
            return raw.decode('utf-8') if text else json.loads(raw)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def models_from(data):
    if not isinstance(data, dict):
        return []
    rows = data.get('data', data.get('models', []))
    if not rows and isinstance(data.get('model'), str):
        rows = [{'id': data['model'], 'gpu_gib': data.get('gpu_gib')}]
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get('id', row.get('name', row.get('model')))
        if not isinstance(mid, str):
            continue
        meta = row.get('meta') or row.get('details') or {}
        if not isinstance(meta, dict):
            meta = {}
        out.append({'id': mid[:200], 'context_length': number(meta.get('n_ctx', meta.get('context_length'))),
                    'format': meta.get('ftype', meta.get('quantization_level')), 'gpu_gib': number(row.get('gpu_gib'))})
    return out


def parse_metrics(text):
    values = {}
    for line in text.splitlines():
        match = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^\n]*\})?\s+([-+\d.eE]+)(?:\s|$)', line)
        if match:
            name, value = match.groups()
            n = number(value)
            if n is not None:
                values[name] = values.get(name, 0) + n
    def first(*names):
        return next((values[n] for n in names if n in values), None)
    return {'running': first('tensorfold:requests_running', 'vllm:num_requests_running', 'requests_running', 'llamacpp:requests_processing'),
            'queue': first('tensorfold:requests_waiting', 'vllm:num_requests_waiting', 'requests_waiting', 'llamacpp:requests_deferred'),
            'tok_s': first('llamacpp:predicted_tokens_seconds', 'decode_tok_s'),
            'generation_tokens': first('tensorfold:generation_tokens_total', 'vllm:generation_tokens_total', 'llamacpp:tokens_predicted_total')}


def utc(value):
    """Parse an ISO timestamp. A value without a zone is UTC, not the host's local time."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def fresh_telemetry(data, now=None):
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        data = {}
    out = dict(data)
    try:
        age = now - utc(data['updated_at']).timestamp()
        fresh = 0 <= age <= 180
    except (KeyError, ValueError, TypeError, AttributeError):
        fresh = False
    out['fresh'] = fresh
    if not fresh:
        out['status'] = 'stale' if data else 'unavailable'
        for key in ['decode_tok_s', 'requests_running', 'queue', 'aggregate_wall_tok_s', 'decode_tps_per_stream']:
            out[key] = None
    return out


def profile_rows(data):
    rows = data.get('profiles', []) if isinstance(data, dict) else []
    if isinstance(rows, dict):
        rows = [dict(v, id=k) for k, v in rows.items() if isinstance(v, dict)]
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get('hidden') or row.get('enabled') is False:
            continue
        pid = row.get('id', row.get('profile'))
        if not isinstance(pid, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', pid):
            continue
        out.append({'id': pid, 'name': row.get('name', row.get('display_name', pid)),
                    'model': row.get('model'), 'context_length': row.get('context_length', row.get('context')),
                    'slots': row.get('slots', row.get('parallel')), 'engine': row.get('engine')})
    return out


def stamp(value):
    n = number(value)
    if n is not None:
        return n
    try:
        return utc(value).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def swap_history(data):
    rows = data.get('history', []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[-40:]:
        if not isinstance(row, dict):
            continue
        start = stamp(row.get('started_at', row.get('start')))
        end = stamp(row.get('finished_at', row.get('completed_at', row.get('end'))))
        duration = number(row.get('duration_s', row.get('elapsed_s')))
        if duration is None and start is not None and end is not None and end >= start:
            duration = end - start
        out.append({'profile': row.get('profile', row.get('target', 'unknown')),
                    'started_at': row.get('started_at', row.get('start')), 'duration_s': duration,
                    'status': row.get('status', 'unknown'), 'gates': row.get('gates', row.get('gate_results')),
                    'source': row.get('source', 'cluster-swap state history')})
    return out


def endpoint(base, path='/v1/models'):
    try:
        body = fetch(base + path)
        if not isinstance(body, dict) or 'error' in body or body.get('ok') is False or body.get('status') in ['offline', 'error', 'unhealthy']:
            return {'status': 'unhealthy', 'models': [], 'error': 'Endpoint reported unhealthy'}
        return {'status': 'online', 'models': models_from(body), 'health': {k: body[k] for k in ['ok', 'backend', 'quant', 'gpu_gib'] if k in body}}
    except urllib.error.HTTPError as e:
        return {'status': 'auth required' if e.code in (401, 403) else 'unavailable', 'models': [], 'error': 'HTTP ' + str(e.code)}
    except (OSError, ValueError, TimeoutError):
        return {'status': 'offline', 'models': [], 'error': 'No response within probe timeout'}


def run(argv, timeout=5, input=None):
    result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise OSError('probe command failed')
    return result.stdout


def remote(host, job=None):
    script = Path(__file__).with_name('remote_probe.py').read_text()
    suffix = ' ' + job if host == 'cluster-a' and isinstance(job, str) and re.fullmatch(r'modeldeck-swap-[0-9a-f]{12}', job) else ''
    try:
        raw = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', '-o', 'ConnectionAttempts=1',
                   host, 'python3 - ' + host + suffix], timeout=10, input=script)
        return json.loads(raw)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'status': 'offline', 'error': 'SSH read-only probe unavailable', 'services': []}


def read_gateway_env():
    values = {}
    try:
        for line in (Path.home() / '.hermes/.env').read_text().splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                if key in ('API_SERVER_KEY', 'API_SERVER_PORT', 'API_SERVER_HOST'):
                    values[key] = value.strip().strip('\"\'')
    except OSError:
        pass
    return values


def gateway_profiles():
    # The default-profile gateway may bind a non-loopback address, so its URL is configurable.
    env = read_gateway_env()
    if not env.get('API_SERVER_KEY'):
        return {'profiles': [], 'available': False, 'error': 'Default gateway key unavailable'}
    try:
        data = fetch(CLUSTER_GATEWAY_URL + '/v1/cluster/profiles',
                     {'Authorization': 'Bearer ' + env['API_SERVER_KEY']}, timeout=9)
        if not isinstance(data, dict) or 'error' in data:
            raise ValueError('invalid profile data')
        return {**data, 'available': True}
    except (OSError, ValueError, TimeoutError):
        return {'profiles': [], 'available': False, 'error': 'Cluster profile controller unavailable'}
