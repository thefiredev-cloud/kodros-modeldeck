import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modeldeck.state import Store


class CollectorTests(unittest.TestCase):
    def test_offline_sample_and_recovery_preserve_unknowns(self):
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            store = Store(Path(directory)/'data')
            online = False
            def local(spec):
                sid, host, port, _path, kind = spec
                return {'id': sid, 'host': host, 'port': port, 'kind': kind, 'endpoint': 'http://127.0.0.1:'+str(port),
                        'status': 'online' if online else 'offline', 'models': [{'id': 'discovered-live'}] if online else [],
                        'running': None, 'queue': None, 'tok_s': None}
            with patch('modeldeck.state.local_service', side_effect=local), patch('modeldeck.state.remote', return_value={'status':'offline','services':[]}), patch('modeldeck.state.gateway_profiles', return_value={'available':False,'profiles':[]}), patch('modeldeck.state.run', return_value='GPU, 1000, 16000'):
                store.collect()
                first = store.snapshot()
                self.assertFalse(first['cluster']['online'])
                self.assertIsNone(first['history'][-1]['cluster_tok_s'])
                self.assertEqual(len(first['hosts']), 5)
                self.assertFalse(store.swap_state()['cluster_online'])
                online = True
                store.collect()
                recovered = store.snapshot()
                self.assertTrue(recovered['cluster']['online'])
                self.assertEqual(recovered['models'][0]['models'][0]['id'], 'discovered-live')
                online = False
                store.collect()
                lost = store.snapshot()
                self.assertFalse(lost['cluster']['online'])
                self.assertTrue(lost['models'][0]['catalog_stale'])
                self.assertEqual(lost['models'][0]['status'], 'offline')
                self.assertIsNone(lost['history'][-1]['cluster_tok_s'])
                self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(store.db.stat().st_mode & 0o777, 0o600)
    def test_loop_does_not_hide_programming_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'data')
            with patch.object(store, 'collect', side_effect=TypeError('invalid collector state')), \
                    patch.object(store.stop, 'wait', side_effect=lambda _timeout: store.stop.set()), \
                    self.assertRaisesRegex(TypeError, 'invalid collector state'):
                store.loop()
