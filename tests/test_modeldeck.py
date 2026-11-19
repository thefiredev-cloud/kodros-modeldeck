import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from modeldeck.adapters import (
    endpoint,
    fresh_telemetry,
    models_from,
    parse_metrics,
    profile_rows,
    stamp,
    swap_history,
)
from modeldeck.guard import GuardError, SwapGuard
from modeldeck.server import make_server


class AdapterTests(unittest.TestCase):
    def test_model_catalog_dialects(self):
        self.assertEqual(models_from({'data': [{'id': 'qwen', 'meta': {'n_ctx': 8192}}]})[0]['id'], 'qwen')
        self.assertEqual(models_from({'models': [{'name': 'jina', 'details': {'context_length': 8192}}]})[0]['id'], 'jina')
        self.assertEqual(models_from({'ok': True, 'model': 'clef', 'gpu_gib': 1.88})[0]['id'], 'clef')
        self.assertEqual(models_from({'data': 'malformed'}), [])

    def test_metrics_labels_and_missing_not_zero(self):
        m = parse_metrics('vllm:num_requests_running{model_name="x"} 2\nvllm:num_requests_waiting 3\nfoo NaN\n')
        self.assertEqual(m['running'], 2)
        self.assertEqual(m['queue'], 3)
        self.assertIsNone(m['tok_s'])

    def test_stale_telemetry_never_claims_live(self):
        value = fresh_telemetry({'updated_at': '2020-01-01T00:00:00Z', 'status': 'online', 'decode_tok_s': 61}, now=1792619000)
        self.assertEqual(value['status'], 'stale')
        self.assertIsNone(value['decode_tok_s'])
        self.assertEqual(fresh_telemetry({}, now=0)['status'], 'unavailable')

    def test_naive_timestamp_is_utc_not_host_local(self):
        # A telemetry file written without a zone is UTC. Reading it as local time shifted it by
        # the host offset, so fresh data was reported stale and its live numbers were nulled.
        now = datetime(2026, 11, 19, 6, 0, 30, tzinfo=UTC).timestamp()
        value = fresh_telemetry({'updated_at': '2026-11-19T06:00:00', 'decode_tok_s': 61}, now=now)
        self.assertTrue(value['fresh'])
        self.assertEqual(value['decode_tok_s'], 61)
        self.assertNotIn('status', value)
        self.assertEqual(stamp('2026-11-19T06:00:00'), datetime(2026, 11, 19, 6, 0, 0, tzinfo=UTC).timestamp())

    def test_mixed_naive_and_aware_history_keeps_the_measured_duration(self):
        h = swap_history({'history': [{'profile': 'a', 'started_at': '2026-11-19T06:00:00',
                                       'finished_at': '2026-11-19T06:03:32+00:00'}]})
        self.assertEqual(h[0]['duration_s'], 212)

    def test_history_duration_is_measured_only(self):
        h = swap_history({'history': [{'profile': 'a', 'started_at': 100, 'finished_at': 312, 'gates': {'math': True}}, {'profile': 'b'}]})
        self.assertEqual(h[0]['duration_s'], 212)
        self.assertIsNone(h[1]['duration_s'])
        self.assertEqual(h[0]['gates']['math'], True)

    def test_profiles_hidden_and_arbitrary_commands_not_exposed(self):
        rows = profile_rows({'profiles': {'x': {'name': 'X', 'recipe_dir': '/private', 'command': 'secret'}, 'old': {'hidden': True}}})
        self.assertEqual(rows, [{'id': 'x', 'name': 'X', 'model': None, 'context_length': None, 'slots': None, 'engine': None}])

    def test_endpoint_failure_and_recovery(self):
        with patch('modeldeck.adapters.fetch', side_effect=TimeoutError):
            self.assertEqual(endpoint('http://127.0.0.1:1', '/health')['status'], 'offline')
        with patch('modeldeck.adapters.fetch', return_value={'ok': True, 'model': 'recovered'}):
            self.assertEqual(endpoint('http://127.0.0.1:1', '/health')['status'], 'online')


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.state = {'cluster_online': True, 'profiles_fresh': True, 'gated_swap_available': True,
                      'swap_state': 'idle', 'current': 'old', 'profiles': [{'id': 'new'}]}
        self.calls = []
        self.guard = SwapGuard(lambda: self.state, lambda p, job: self.calls.append(p) or {'job': job}, lambda s: True)

    def test_two_step_and_replay_protection(self):
        ticket = self.guard.prepare('new')
        self.assertEqual(self.calls, [])
        with self.assertRaises(GuardError):
            self.guard.commit(ticket['ticket'], 'yes')
        ticket = self.guard.prepare('new')
        self.guard.commit(ticket['ticket'], ticket['confirmation'])
        self.assertEqual(self.calls, ['new'])
        with self.assertRaises(GuardError):
            self.guard.commit(ticket['ticket'], ticket['confirmation'])

    def test_offline_busy_unknown_stale_and_missing_gate_fail_closed(self):
        for field, value in [('cluster_online', False), ('profiles_fresh', False), ('gated_swap_available', False), ('swap_state', 'loading')]:
            with self.subTest(field=field):
                old = self.state[field]
                self.state[field] = value
                with self.assertRaises(GuardError): self.guard.prepare('new')
                self.state[field] = old
        with self.assertRaises(GuardError): self.guard.prepare('unregistered')
        self.assertEqual(self.calls, [])

    def test_expiry_state_change_and_jev_denial(self):
        with patch('modeldeck.guard.time.monotonic', return_value=1): ticket = self.guard.prepare('new')
        with patch('modeldeck.guard.time.monotonic', return_value=62), self.assertRaises(GuardError):
            self.guard.commit(ticket['ticket'], ticket['confirmation'])
        ticket = self.guard.prepare('new')
        self.state['current'] = 'different'
        with self.assertRaises(GuardError): self.guard.commit(ticket['ticket'], ticket['confirmation'])
        self.guard.gate = lambda s: False
        ticket = self.guard.prepare('new')
        with self.assertRaises(GuardError): self.guard.commit(ticket['ticket'], ticket['confirmation'])
        self.assertEqual(self.calls, [])

    def test_competing_tickets_cannot_submit_twice(self):
        first = self.guard.prepare('new')
        second = self.guard.prepare('new')
        self.guard.commit(first['ticket'], first['confirmation'])
        with self.assertRaises(GuardError):
            self.guard.commit(second['ticket'], second['confirmation'])
        with self.assertRaises(GuardError):
            self.guard.prepare('new')
        self.assertEqual(self.calls, ['new'])

    def test_only_existing_gated_helper_is_submitted(self):
        from modeldeck.guard import execute_gated
        with patch('modeldeck.guard.subprocess.run') as runner:
            runner.return_value.returncode = 0
            result = execute_gated('safe-profile')
            command = runner.call_args.args[0]
            self.assertEqual(command[0], 'ssh')
            self.assertIn('ops-cluster-improve-exec/bin/swap-gate swap safe-profile', command[-1])
            self.assertIn('--property=RemainAfterExit=yes', command[-1])
            self.assertNotIn('/v1/cluster/swap', command[-1])
            self.assertEqual(result['status'], 'submitted')
        with self.assertRaises(GuardError):
            execute_gated('new; touch /tmp/not-allowed')


    def test_pending_survives_restart_and_clears_after_proven_job_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pending-swap.json'
            guard = SwapGuard(lambda: self.state, lambda p, job: {'job': job}, lambda s: True, path)
            ticket = guard.prepare('new')
            result = guard.commit(ticket['ticket'], ticket['confirmation'])
            self.assertTrue(path.exists())
            restarted = SwapGuard(lambda: self.state, lambda p, job: {}, lambda s: True, path)
            with self.assertRaises(GuardError):
                restarted.prepare('new')
            # A rollback can leave the same resident. Proven terminal job + idle unlocks it.
            self.state['action_job'] = {'job': result['job'], 'terminal': True}
            restarted.prepare('new')
            self.assertFalse(path.exists())

class ServerTests(unittest.TestCase):
    def setUp(self):
        class Store:
            csrf = 'test-token'
            def snapshot(self): return {'hosts': [], 'models': [], 'history': [], 'cluster': {}}
        self.calls = []
        state = {'cluster_online': True, 'profiles_fresh': True, 'gated_swap_available': True,
                 'swap_state': 'idle', 'current': 'old', 'profiles': [{'id': 'new'}]}
        guard = SwapGuard(lambda: state, lambda p, job: self.calls.append(p) or {'status': 'submitted', 'job': 'mock-only'}, lambda s: True)
        self.server = make_server(Store(), guard, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_root_and_foreign_host(self):
        with urllib.request.urlopen(self.base) as r: self.assertEqual(r.status, 200)
        req = urllib.request.Request(self.base, headers={'Host': 'evil.example'})
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()

    def test_swap_cross_origin_and_missing_csrf_rejected(self):
        for headers in [{'Origin': 'https://evil.example', 'X-ModelDeck-CSRF': 'test-token'}, {'Origin': self.base}]:
            req = urllib.request.Request(self.base+'/api/swap/prepare', data=b'{"profile":"new"}', headers={'Content-Type':'application/json', **headers})
            with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            e.exception.close()

    def test_mocked_swap_endpoint_requires_both_steps(self):
        def post(path, payload):
            req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-ModelDeck-CSRF': 'test-token'})
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        code, review = post('/api/swap/prepare', {'profile': 'new'})
        self.assertEqual(code, 200)
        self.assertEqual(self.calls, [])
        self.assertIn('3.5 minutes', review['warning'])
        code, result = post('/api/swap/commit', {'ticket': review['ticket'], 'confirmation': review['confirmation']})
        self.assertEqual(code, 202)
        self.assertEqual(result['job'], 'mock-only')
        self.assertEqual(self.calls, ['new'])


if __name__ == '__main__': unittest.main()
