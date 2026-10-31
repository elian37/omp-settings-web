import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server


class TestModelProbe(unittest.TestCase):
    selector = 'google-antigravity/gemini-3.8-flash'

    def setUp(self):
        self.catalog = patch.object(server, 'list_models', return_value=[{
            'selector': self.selector, 'provider': 'google-antigravity',
            'name': 'Gemini 3.8 Flash', 'contextWindow': 1048576, 'reasoning': True,
        }])
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def report(self, result, model=None, returncode=0):
        return subprocess.CompletedProcess([], returncode, json.dumps({
            'runs': 1, 'maxTokens': 256, 'profile': 'chat',
            'models': [{'selector': self.selector + ':off', 'model': model or self.selector,
                        'thinking': 'off', 'results': [result]}],
            'failures': 0 if result.get('ok') else 1,
        }), '')

    def test_failed_request_is_not_accepted_as_connectivity(self):
        failed = self.report({'ok': False, 'error': 'HTTP 429: quota exhausted'}, returncode=1)
        with patch.object(server.subprocess, 'run', return_value=failed):
            result = server.probe_model(self.selector)
        self.assertFalse(result['ok'])
        self.assertIn('429', result['error'])

    def test_success_reports_actual_model_and_timing(self):
        success = self.report({'ok': True, 'durationMs': 3836.64, 'ttftMs': 3792.54,
                               'inputTokens': 13, 'outputTokens': 1})
        with patch.object(server.subprocess, 'run', return_value=success):
            result = server.probe_model(self.selector)
        self.assertTrue(result['ok'])
        self.assertEqual(result['model'], self.selector)
        self.assertAlmostEqual(result['durationMs'], 3836.64)
        self.assertGreaterEqual(result['elapsedMs'], 0)

    def test_other_model_response_does_not_pass(self):
        success = self.report({'ok': True, 'durationMs': 20, 'ttftMs': 10,
                               'inputTokens': 10, 'outputTokens': 1}, model='other/model')
        with patch.object(server.subprocess, 'run', return_value=success):
            result = server.probe_model(self.selector)
        self.assertFalse(result['ok'])

    def test_unknown_alias_and_injected_selectors_never_launch(self):
        with patch.object(server.subprocess, 'run') as invoke:
            for value in ('@smol', 'unknown/model', '--help', self.selector + '\n--runs=99', None):
                with self.subTest(value=value):
                    result = server.probe_model(value)
                    self.assertFalse(result['ok'])
            invoke.assert_not_called()

    def test_timeout_releases_probe_slot(self):
        with patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('omp', 60)):
            result = server.probe_model(self.selector)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'timeout')
        with patch.object(server.subprocess, 'run', return_value=self.report({'ok': False, 'error': 'offline'})):
            retry = server.probe_model(self.selector)
        self.assertNotEqual(retry['code'], 'busy')

    def test_concurrent_probe_is_refused_without_a_second_request(self):
        entered = threading.Event()
        release = threading.Event()
        def pending(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test request did not get released')
            return self.report({'ok': False, 'error': 'offline'})
        with patch.object(server.subprocess, 'run', side_effect=pending) as invoke:
            worker = threading.Thread(target=lambda: server.probe_model(self.selector))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                result = server.probe_model(self.selector)
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'busy')
                self.assertEqual(invoke.call_count, 1)
            finally:
                release.set()
                worker.join(5)

    def test_malformed_report_is_a_visible_failure(self):
        for output in ('not json', '{}', '{"models": []}'):
            with self.subTest(output=output), patch.object(server.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')):
                result = server.probe_model(self.selector)
                self.assertFalse(result['ok'])
                self.assertTrue(result.get('error'))

    def test_upstream_credentials_are_redacted(self):
        failed = self.report({'ok': False, 'error': 'HTTP 401 Authorization: Bearer fake-secret-1234567890'}, returncode=1)
        with patch.object(server.subprocess, 'run', return_value=failed):
            result = server.probe_model(self.selector)
        self.assertFalse(result['ok'])
        self.assertNotIn('fake-secret-1234567890', result['error'])


if __name__ == '__main__':
    unittest.main()
