import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from economic_machine.pump_research.archive_gate import ArchiveDeferred, ArchiveGate


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.state = self.folder/'network-state.json'

    def gate(self, **kwargs):
        return ArchiveGate(self.folder, lambda: None, **kwargs)

    def fail_transport(self, error):
        try:
            with self.assertRaises(type(error)):
                with self.gate():
                    raise error
        finally:
            if isinstance(error, urllib.error.HTTPError):
                error.close()

    def test_separate_process_defers_while_request_in_flight(self):
        code = '''import json,sys
from economic_machine.pump_research.archive_gate import ArchiveGate,ArchiveDeferred
try:
    with ArchiveGate(sys.argv[1],lambda:None,wait=False):
        raise AssertionError('request overlap')
except ArchiveDeferred as e:
    print(json.dumps({'reason':e.reason,'retry_at_ms':e.retry_at_ms}))
'''
        with self.gate():
            result = subprocess.run([sys.executable, '-c', code, str(self.folder)],
                capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout)['reason'], 'archive_request_in_flight')
            self.assertFalse(self.state.exists())
        self.assertEqual(json.loads(self.state.read_text())['not_before'], 0)

    def test_blocking_process_waits_until_request_finishes(self):
        code = '''import sys
from economic_machine.pump_research.archive_gate import ArchiveGate
def guard():
    print('guard',flush=True)
with ArchiveGate(sys.argv[1],guard,wait=True):
    print('acquired',flush=True)
'''
        child = None
        try:
            with self.gate():
                child = subprocess.Popen([sys.executable, '-c', code, str(self.folder)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(child.stdout.readline().strip(), 'guard')
                self.assertIsNone(child.poll())
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn('acquired', out)
        finally:
            if child is not None and child.poll() is None:
                child.kill(); child.communicate(timeout=10)

    def test_429_shared_cooldown_cannot_be_overwritten_by_success(self):
        with patch('economic_machine.pump_research.archive_gate.time.time', return_value=1000):
            with self.gate():
                # Emulate an older cooperating writer during this request.
                self.state.write_text(json.dumps(dict(not_before=1300, last_status=429,
                    rate_limit_attempts=2, failed_reservation_retained=True)))
            previous = self.state.read_bytes()
            with patch('economic_machine.pump_research.archive_gate.time.sleep') as sleep:
                with self.assertRaises(ArchiveDeferred) as caught:
                    with self.gate(wait=False):
                        self.fail('must not dispatch')
            self.assertEqual(caught.exception.retry_at_ms, 1300000)
            sleep.assert_not_called()
            self.assertEqual(self.state.read_bytes(), previous)

    def test_429_preserves_numeric_retry_after_and_failed_reservation(self):
        error = urllib.error.HTTPError('https://example.invalid/', 429, 'limit',
            {'Retry-After': '120'}, None)
        with patch('economic_machine.pump_research.archive_gate.time.time', return_value=1000):
            self.fail_transport(error)
        state = json.loads(self.state.read_text())
        self.assertEqual(state['not_before'], 1120)
        self.assertEqual(state['rate_limit_attempts'], 1)
        self.assertTrue(state['failed_reservation_retained'])

    def test_http_date_retry_after(self):
        value = format_datetime(datetime.fromtimestamp(1300, timezone.utc), usegmt=True)
        with patch('economic_machine.pump_research.archive_gate.time.time', return_value=1000):
            self.fail_transport(urllib.error.HTTPError('https://example.invalid/', 503, 'busy',
                {'Retry-After': value}, None))
        self.assertEqual(json.loads(self.state.read_text())['not_before'], 1300)

    def test_legacy_success_pacing_is_ignored(self):
        self.state.write_text(json.dumps(dict(last_status=206, not_before=time.time()+3600)))
        with patch('economic_machine.pump_research.archive_gate.time.sleep') as sleep:
            with self.gate(wait=False):
                pass
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())['not_before'], 0)

    def test_old_unknown_status_cooldown_still_respected(self):
        self.state.write_text(json.dumps(dict(not_before=time.time()+60)))
        with self.assertRaises(ArchiveDeferred):
            with self.gate(wait=False):
                self.fail('must not dispatch')

    def test_all_5xx_and_network_errors_backoff(self):
        for error in [urllib.error.HTTPError('https://example.invalid/', code, 'busy', {}, None)
                      for code in (500, 502, 503, 504, 529)]+[
                      urllib.error.URLError('disconnected'), TimeoutError('timeout'), EOFError('truncated')]:
            with self.subTest(error=error):
                self.state.unlink(missing_ok=True)
                with patch('economic_machine.pump_research.archive_gate.time.time', return_value=1000):
                    self.fail_transport(error)
                state = json.loads(self.state.read_text())
                self.assertEqual(state['not_before'], 1010)
                self.assertEqual(state['transient_attempts'], 1)

    def test_400_404_do_not_create_cooldown(self):
        for error in [urllib.error.HTTPError('https://example.invalid/', code, 'missing', {}, None)
                      for code in (400, 404)]:
            with self.subTest(error=error):
                self.fail_transport(error)
                self.assertFalse(self.state.exists())
        with self.gate(wait=False):
            pass  # Every failed request released the lock.

    def test_repeated_expired_429_increases_backoff(self):
        self.state.write_text(json.dumps(dict(not_before=999, last_status=429, rate_limit_attempts=3)))
        with patch('economic_machine.pump_research.archive_gate.time.time', return_value=1000):
            self.fail_transport(urllib.error.HTTPError('https://example.invalid/', 429, 'limit', {}, None))
        self.assertEqual(json.loads(self.state.read_text())['not_before'], 1480)

    def test_expired_backoff_success_resets_attempts(self):
        self.state.write_text(json.dumps(dict(not_before=1, last_status=429, rate_limit_attempts=3)))
        with self.gate(success_status=200):
            pass
        state = json.loads(self.state.read_text())
        self.assertEqual(state['rate_limit_attempts'], 0)
        self.assertEqual(state['last_status'], 200)

    def test_scope_checked_after_lock_and_lock_released_on_failure(self):
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 3:
                raise ValueError('original deadline')
        gate = ArchiveGate(self.folder, guard, wait=False)
        with self.assertRaisesRegex(ValueError, 'original deadline'):
            with gate:
                self.fail('expired request must not dispatch')
        self.assertIsNone(gate.fd)
        with self.gate(wait=False):
            pass

    def test_nonfinite_retry_after_uses_bounded_backoff(self):
        for invalid in ('NaN', 'inf', 'junk'):
            self.state.unlink(missing_ok=True)
            with patch('economic_machine.pump_research.archive_gate.time.time', return_value=1000):
                self.fail_transport(urllib.error.HTTPError('https://example.invalid/', 429, 'limit',
                    {'Retry-After': invalid}, None))
            self.assertEqual(json.loads(self.state.read_text())['not_before'], 1060)

    def test_invalid_state_and_lock_symlink_fail_closed(self):
        self.state.write_text('not JSON')
        with self.assertRaises(ValueError):
            with self.gate(wait=False):
                self.fail('must not dispatch')
        self.state.unlink()
        (self.folder/'request.lock').unlink()
        (self.folder/'request.lock').symlink_to(self.folder/'other')
        with self.assertRaises(OSError):
            with self.gate(wait=False):
                self.fail('must not dispatch')


if __name__ == '__main__':
    unittest.main()
