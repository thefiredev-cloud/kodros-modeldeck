import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modeldeck.remote_probe import job_result, log_result, probe


class RemoteTests(unittest.TestCase):
    def test_retained_success_failed_and_missing_job_states(self):
        success = 'LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n'
        failure = 'LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=2\n'
        absent = 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
        self.assertTrue(job_result(success, 'job')['terminal'])
        self.assertTrue(job_result(failure, 'job')['terminal'])
        self.assertFalse(job_result(absent, 'job')['terminal'])
        self.assertFalse(job_result('LoadState=loaded\nActiveState=active\nSubState=running', 'job')['terminal'])

    def test_swap_log_measures_outage_not_idle_wait_and_labels_gate_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'swap-20261021T230000Z.log'
            log.write_text('23:00:00 waiting for quiet\n23:58:00 quiet confirmed after 2 tries; swapping to candidate\ngates ok\n00:01:30 SWAP OK image=candidate\n')
            result = log_result(log)
            self.assertEqual(result['duration_s'], 210)
            self.assertEqual(result['gates']['basic_helper'], 'passed')
            self.assertEqual(result['gates']['full_acceptance'], 'not established by this log')
            log.write_text('23:00:00 waiting for quiet\n')
            self.assertIsNone(log_result(log)['duration_s'])


    def test_completion_before_quiet_confirmed_is_not_this_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'swap-20261022T235800Z.log'
            log.write_text('00:01:30 SWAP OK image=old\n23:58:00 quiet confirmed after 2 tries; swapping to candidate\n')
            result = log_result(log)
            self.assertEqual(result['profile'], 'candidate')
            self.assertIsNone(result['duration_s'])
            self.assertEqual(result['status'], 'incomplete')
            log.write_text('00:01:30 SWAP OK image=old\n00:02:00 quiet confirmed after 1 try; swapping to candidate\n')
            result = log_result(log)
            self.assertIsNone(result['duration_s'])
            self.assertEqual(result['status'], 'incomplete')
            log.write_text('00:01:30 SWAP OK image=old\n23:58:00 quiet confirmed after 2 tries; swapping to candidate\n00:05:00 SWAP OK image=candidate\n')
            result = log_result(log)
            self.assertEqual(result['duration_s'], 420)
            self.assertEqual(result['status'], 'SWAP OK')

    def test_build_host_uses_its_endpoint_without_returning_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch('pathlib.Path.home', return_value=Path(directory)):
            path = Path(directory)/'work/local-inference/api-key'
            path.parent.mkdir(parents=True)
            credential = 'fixture' + '-only'
            path.write_text(credential)
            with patch('modeldeck.remote_probe.command', side_effect=['25769803776', 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\n']), patch('modeldeck.remote_probe.get', return_value={'status':'online','data': {'data':[{'id':'mac-qwen3.5-9b'}]}}) as get:
                result = probe('build-host')
                self.assertTrue(all(c.args[0].startswith('http://127.0.0.1:8011/') for c in get.call_args_list))
                self.assertEqual(get.call_args_list[0].args[1]['Authorization'], 'Bearer '+credential)
                self.assertNotIn(credential, json.dumps(result))
                self.assertEqual(result['memory_total_gib'], 24)
