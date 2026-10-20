"""Research-only transport; provider cooldown is retained without exposing bodies."""
from email.utils import parsedate_to_datetime
import os
import stat
import time
import urllib.error
import urllib.request

from ..arbitrum_research.muse import canonical
from ..muse_policy_worker import NoRedirect


class ProviderError(ValueError):
    def __init__(self, code, retry_after_ms=0):
        self.code = code
        self.retry_after_ms = retry_after_ms
        super().__init__(code)


def retry_after(value, now=None):
    if value is None:
        return 0
    now = time.time() if now is None else now
    try:
        if str(value).strip().isdigit():
            return max(0, int(str(value).strip()) * 1000)
        return max(0, int((parsedate_to_datetime(value).timestamp() - now) * 1000))
    except (TypeError, ValueError, OverflowError):
        return 0


def provider_call(request, key_file):
    fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 512:
            raise ProviderError('CREDENTIAL_FILE_INVALID')
        key = source.read().strip()
    if not key or '\n' in key or '\r' in key:
        raise ProviderError('CREDENTIAL_SHAPE_INVALID')
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions', data=canonical(request).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as response:
            raw = response.read(32769)
        if len(raw) > 32768:
            raise ProviderError('PROVIDER_RESPONSE_BOUND')
        return raw.decode()
    except urllib.error.HTTPError as exc:
        raise ProviderError('PROVIDER_HTTP_' + str(exc.code), retry_after(exc.headers.get('Retry-After'))) from None
    except (urllib.error.URLError, TimeoutError, UnicodeError):
        raise ProviderError('PROVIDER_OUTCOME_UNKNOWN') from None
