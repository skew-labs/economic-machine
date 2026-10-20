"""Exercise frozen experiment scopes without renewing production authority."""
import time
import unittest
from unittest.mock import patch


_wall = time.time
_wall_ns = time.time_ns


class HistoricalScopeTestCase(unittest.TestCase):
    """Shift only each fixture's wall clock; elapsed and monotonic time still run.

    Individual deadline tests can override these patches to exercise expiry.
    Production code and its absolute deadlines are never changed.
    """

    def run(self, result=None):
        offset_ns = 1792032000000000000 - _wall_ns()
        with patch('time.time', side_effect=lambda: _wall() + offset_ns / 1e9), \
             patch('time.time_ns', side_effect=lambda: _wall_ns() + offset_ns):
            return super().run(result)
