"""Cooperative, process-wide Old Faithful request serialization and cooldown.

Cache hits never enter this gate. A caller must reserve its existing shared IO
allowance inside the context, before dispatch, and let transport errors escape
the context. The lock covers the complete response read and validation.
"""
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
import urllib.error

from .store import atomic


class ArchiveDeferred(ValueError):
    def __init__(self, retry_at_ms, reason='archive_source_cooldown'):
        self.retry_at_ms, self.reason = int(retry_at_ms), reason
        super().__init__('MAP_ARCHIVE_SOURCE_DEFERRED')


def _retry_after(headers, now):
    value = headers.get('Retry-After', '') if headers else ''
    if not isinstance(value, str) or len(value) > 256:
        return 0
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            delay = stamp.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


class ArchiveGate:
    """Hold one advisory lock for one fully read provider request.

    ``wait=False`` never sleeps or reserves IO. ``wait=True`` checks the caller's
    original scope while polling and releases the lock during provider cooldown.
    A legacy ``last_status=206`` delay is local pacing, not provider backoff.
    """
    def __init__(self, folder, guard, *, wait=True, success_status=206):
        if not callable(guard) or type(wait) is not bool or success_status not in (200,206):
            raise ValueError('ARCHIVE_GATE_ARGUMENT')
        self.folder, self.guard, self.wait = Path(folder), guard, wait
        self.success_status = success_status
        self.path = self.folder/'network-state.json'
        self.fd = None
        self.locked = False

    def _state(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > 65536:
            raise ValueError('ARCHIVE_GATE_STATE_FILE')
        raw = self.path.read_bytes()
        if len(raw) > 65536:
            raise ValueError('ARCHIVE_GATE_STATE_BOUND')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('ARCHIVE_GATE_STATE_SHAPE')
        return value

    @staticmethod
    def _not_before(state):
        if state.get('last_status') == 206:
            return 0
        value = state.get('not_before', 0)
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError('ARCHIVE_GATE_COOLDOWN_VALUE')
        return value

    def _unlock(self):
        if self.locked:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.locked = False

    def close(self):
        try:
            self._unlock()
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        self.guard()
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.folder.is_symlink():
            raise ValueError('ARCHIVE_GATE_DIRECTORY_LINK')
        self.fd = os.open(self.folder/'request.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise ValueError('ARCHIVE_GATE_LOCK_FILE')
            while True:
                self.guard()
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.locked = True
                except BlockingIOError:
                    if not self.wait:
                        retry = max(time.time()+1, self._not_before(self._state()))
                        raise ArchiveDeferred(math.ceil(retry*1000), 'archive_request_in_flight')
                    time.sleep(.25)
                    continue
                self.guard()  # A lock wait cannot admit a request after expiry.
                retry = self._not_before(self._state())
                now = time.time()
                if now >= retry:
                    return self
                self._unlock()
                if not self.wait:
                    raise ArchiveDeferred(math.ceil(retry*1000))
                self.guard()
                time.sleep(max(0, min(.25, retry-time.time())))
        except BaseException:
            self.close()
            raise

    def _finish(self, error):
        state = self._state()
        now = time.time()
        before = self._not_before(state)
        code = error.code if isinstance(error, urllib.error.HTTPError) else None
        transient = (code == 429 or type(code) is int and 500 <= code <= 599 or
                     isinstance(error, (TimeoutError, EOFError)) or
                     isinstance(error, urllib.error.URLError) and
                     not isinstance(error, urllib.error.HTTPError))
        if error is not None and not transient:
            return
        if transient:
            field = 'rate_limit_attempts' if code == 429 else 'transient_attempts'
            prior = state.get(field, 0)
            attempts = (prior if type(prior) is int and prior >= 0 else 0) + 1
            backoff = min(1800, 60*2**min(attempts-1,5)) if code == 429 else min(300, 10*2**min(attempts-1,5))
            retry = _retry_after(error.headers, now) if isinstance(error, urllib.error.HTTPError) else 0
            atomic(self.path, dict(state, not_before=max(before,now+max(backoff,retry)),
                **{field:attempts}, last_status=code, error_type=type(error).__name__,
                failed_reservation_retained=True, updated_ms=int(now*1000)))
        elif before > now:
            # Also avoid erasing a newer provider cooldown written by an older
            # cooperating release while this request was in flight.
            return
        else:
            atomic(self.path, dict(not_before=0, rate_limit_attempts=0, transient_attempts=0,
                last_status=self.success_status, updated_ms=int(now*1000)))

    def __exit__(self, kind, error, traceback):
        try:
            self._finish(error)
        finally:
            self.close()
        return False
