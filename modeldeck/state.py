import concurrent.futures
import copy
import json
import secrets
import sqlite3
import subprocess
import threading
import time
import tomllib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import (
    BUILD_HOST_URL,
    endpoint,
    fetch,
    fresh_telemetry,
    gateway_profiles,
    models_from,
    parse_metrics,
    profile_rows,
    remote,
    run,
    swap_history,
)

LOCAL = [
    ('cluster', 'Cluster A + B', 8080, '/v1/models', 'Cluster front door'),
    ('clef', 'workstation', 8098, '/health', 'Decision model'),
    ('jev', 'workstation', 8097, '/health', 'Decision router, not a separate resident model'),
    ('phone-fast', 'workstation', 8845, '/v1/models', 'Fast text model'),
    ('homelab-stt', 'workstation', 8798, '/health', 'Speech recognition'),
    ('sonnet-proxy', 'workstation', 8081, '/v1/models', 'Cluster facade, not separate weights'),
    ('reason-proxy', 'workstation', 8082, '/v1/models', 'Cluster facade, not separate weights'),
]


def local_service(spec):
    sid, host, port, path, kind = spec
    base = 'http://127.0.0.1:' + str(port)
    result = endpoint(base, path)
    metrics = {'running': None, 'queue': None, 'tok_s': None}
    if sid in ('cluster', 'phone-fast'):
        try:
            metrics = parse_metrics(fetch(base + '/metrics', text=True))
        except (OSError, ValueError, TimeoutError):
            pass
    if sid == 'cluster':
        try:
            health = fetch(base + '/health')
            if health.get('vllm_up') is False or health.get('resident_up') is False:
                result['status'] = 'offline'
        except (OSError, ValueError, TimeoutError):
            pass
    return {'id': sid, 'host': host, 'port': port, 'endpoint': base, 'kind': kind, **result, **metrics}


class Store:
    def __init__(self, data_dir=None):
        self.directory = Path(data_dir or Path.home() / '.local/share/modeldeck')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.db = self.directory / 'history.sqlite'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS samples (at REAL PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.chmod(0o600)
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {'hosts': [], 'models': [], 'history': [], 'cluster': {}, 'loading': True}
        self.last_models = {}
        self.last_profiles = []
        self.last_swaps = []
        self.stop = threading.Event()

    def snapshot(self):
        with self.lock:
            data = copy.deepcopy(self.data)
        data['csrf'] = self.csrf
        if data.get('sampled_epoch') and time.time() - data['sampled_epoch'] > 90:
            data['stale'] = True
        return data

    def swap_state(self):
        with self.lock:
            c = self.data.get('cluster', {})
            fresh = time.time() - self.data.get('sampled_epoch', 0) <= 60
            return {'cluster_online': fresh and c.get('online') is True,
                    'profiles_fresh': fresh and c.get('profiles_fresh') is True,
                    'gated_swap_available': fresh and c.get('gated_swap_available') is True,
                    'swap_state': c.get('swap_state'), 'current': c.get('current'), 'profiles': c.get('profiles', []),
                    'action_job': c.get('action_job') if fresh else None}

    def collect(self):
        jobs = {}
        try:
            pending = json.loads((self.directory / 'pending-swap.json').read_text())
        except (OSError, ValueError):
            pending = {}
        if not isinstance(pending, dict):
            pending = {'job': 'unknown'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for spec in LOCAL:
                jobs[spec[0]] = pool.submit(local_service, spec)
            for host in ['cluster-a', 'cluster-b', 'build-host', 'client']:
                jobs[host] = pool.submit(remote, host, pending.get('job') if host == 'cluster-a' else None)
            jobs['profiles'] = pool.submit(gateway_profiles)
            results = {key: future.result() for key, future in jobs.items()}
        now = time.time()
        at = datetime.now(UTC).isoformat()
        try:
            telemetry = fresh_telemetry(json.loads((Path.home() / 'ops/state/cluster-telemetry/latest.json').read_text()))
        except (OSError, ValueError):
            telemetry = fresh_telemetry({})
        services = [results[spec[0]] for spec in LOCAL]
        hosts = []
        for host in ['cluster-a', 'cluster-b', 'build-host', 'client']:
            r = results[host]
            hosts.append({'id': host, 'status': r['status'], 'memory_kind': 'Unified memory' if host != 'client' else 'System RAM',
                          'memory_total_gib': r.get('memory_total_gib'), 'memory_available_gib': r.get('memory_available_gib'),
                          'error': r.get('error')})
            for idx, service in enumerate(r.get('services', [])):
                metrics = parse_metrics(r.get('/metrics', {}).get('data', '')) if r.get('/metrics', {}).get('status') == 'online' else {}
                row = {'id': host + '-' + str(idx), 'host': host, 'port': service['port'], 'kind': service['name'],
                       'endpoint': BUILD_HOST_URL if host == 'build-host' else 'http://127.0.0.1:' + str(service['port']) + ' (on ' + host + ')',
                       'status': service['status'], 'models': models_from(service.get('data', {})), **metrics}
                if host == 'cluster-b':
                    loaded = service.get('loaded', {})
                    row['loaded_models'] = [m['id'] for m in models_from(loaded.get('data', {}))] if loaded.get('status') == 'online' else None
                    row['kind'] = 'Ollama installed catalogue; resident status from /api/ps'
                services.append(row)
            if not r.get('services') and host in ('build-host', 'client', 'cluster-b'):
                services.append({'id': host + '-0', 'host': host, 'port': {'build-host': 8011, 'client': 8742, 'cluster-b': 11434}[host],
                                 'kind': 'Endpoint unavailable', 'status': 'offline', 'models': [], 'endpoint': 'Read-only SSH inventory'})
        gpu = {}
        try:
            raw = run(['nvidia-smi', '--query-gpu=name,memory.used,memory.total', '--format=csv,noheader,nounits'])
            name, used, total = raw.splitlines()[0].split(',')
            gpu = {'gpu_name': name.strip(), 'gpu_used_gib': float(used)/1024, 'gpu_total_gib': float(total)/1024}
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable'):
                mem[key] = int(value.split()[0])/2**20
        hosts.insert(0, {'id': 'workstation', 'status': 'online', 'memory_kind': 'System RAM', 'memory_total_gib': mem.get('MemTotal'),
                         'memory_available_gib': mem.get('MemAvailable'), **gpu})
        try:
            vt = tomllib.loads((Path.home() / '.config/voxtype/config.toml').read_text())
            engine = vt.get('engine', 'unknown')
            section = vt.get(engine, {}) if isinstance(engine, str) else {}
            model = section.get('remote_model') if section.get('mode') == 'remote' else section.get('model')
            model = model or 'not exposed by config'
            active = run(['systemctl', '--user', 'is-active', 'voxtype.service']).strip() == 'active'
            services.append({'id': 'voxtype', 'host': 'workstation', 'port': None, 'endpoint': 'Local systemd daemon',
                             'kind': 'Dictation client to homelab-stt, not separate weights' if section.get('mode') == 'remote' else 'Dictation daemon; activity does not prove transcription quality', 'status': 'online' if active else 'offline',
                             'models': [{'id': str(model), 'format': str(engine)}]})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            services.append({'id': 'voxtype', 'host': 'workstation', 'port': None, 'endpoint': 'Local systemd daemon',
                             'kind': 'Dictation state unavailable', 'status': 'unknown', 'models': []})
        for service in services:
            sid = service['id']
            if sid in ('sonnet-proxy', 'reason-proxy') and results['cluster']['status'] != 'online' and service['status'] == 'online':
                service['status'] = 'upstream offline'
            if service.get('models'):
                self.last_models[sid] = (service['models'], at)
            elif sid in self.last_models:
                service['models'], service['last_seen'] = self.last_models[sid]
                service['catalog_stale'] = True
            service['observed_at'] = at
        profiles = results['profiles']
        cluster_a = results['cluster-a']
        if not profiles.get('available') and cluster_a.get('profiles'):
            profiles = cluster_a['profiles']
            if isinstance(profiles, list):
                profiles = {'profiles': profiles}
            profiles = {**profiles, 'available': True}
        rows = profile_rows(profiles)
        if rows:
            self.last_profiles = rows
        state = profiles.get('state', profiles.get('swap', cluster_a.get('swap', {})))
        if not isinstance(state, dict):
            state = cluster_a.get('swap', {})
        swaps = swap_history(state) + cluster_a.get('swap_logs', [])
        if swaps:
            self.last_swaps = swaps
        cluster_service = results['cluster']
        online = cluster_service['status'] == 'online' and bool(cluster_service.get('models'))
        current = state.get('current_profile', state.get('profile', state.get('current', telemetry.get('profile'))))
        cluster = {'online': online, 'telemetry': telemetry, 'profiles': rows or self.last_profiles,
                   'profiles_fresh': profiles.get('available') is True, 'profile_error': profiles.get('error'),
                   'gated_swap_available': cluster_a.get('gated_swap_available', False), 'current': current,
                   'swap_state': state.get('status', state.get('phase', telemetry.get('swap_state'))),
                   'swaps': swaps or self.last_swaps, 'swaps_stale': not bool(swaps), 'expected_outage_s': 210,
                   'action_job': cluster_a.get('action_job'), 'pending_job': pending.get('job'),
                   'route': 'Cluster A existing swap-gate swap <profile>; raw gateway passthrough is never used'}
        if online and telemetry.get('fresh') and telemetry.get('status') not in ('offline', 'stale', 'unavailable'):
            cluster_service.update(tok_s=telemetry.get('decode_tok_s'), running=telemetry.get('requests_running'), queue=telemetry.get('queue'))
        for host in hosts:
            host['service_ids'] = [s['id'] for s in services if s['host'] == host['id'] or (s['id'] == 'cluster' and host['id'] in ('cluster-a', 'cluster-b'))]
            host['observed_at'] = at
        summary = {'at': now, 'cluster_tok_s': cluster_service.get('tok_s') if online else None,
                   'running': cluster_service.get('running') if online else None, 'queue': cluster_service.get('queue') if online else None,
                   'gpu_used_gib': gpu.get('gpu_used_gib'), 'online_services': sum(s['status'] == 'online' for s in services)}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO samples VALUES (?, ?)', (now, json.dumps(summary)))
            conn.execute('DELETE FROM samples WHERE at < ?', (now - 7*86400,))
            history = [json.loads(r[0]) for r in conn.execute('SELECT payload FROM samples ORDER BY at DESC LIMIT 720')][::-1]
        with self.lock:
            self.data = {'hosts': hosts, 'models': services, 'history': history, 'cluster': cluster,
                         'sampled_at': at, 'sampled_epoch': now, 'loading': False}

    def loop(self):
        while not self.stop.is_set():
            try:
                self.collect()
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
                # Preserve last sample but make collector failure visible; no fabricated measurements.
                with self.lock:
                    self.data['collector_error'] = 'Collector failed. Last sample is retained; retrying in 30 seconds.'
            self.stop.wait(30)
