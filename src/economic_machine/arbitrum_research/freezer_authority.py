"""An append-only owner amendment; never rewrite the original source ledger."""
import hashlib
import json
from pathlib import Path
from .freezer_source import DEADLINE


def source_authority(reference, source_cache, identity_body):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('FREEZER_AUTHORITY_REFERENCE')
    path = Path(reference['path'])
    source_cache = Path(source_cache).resolve()
    if (path.is_symlink() or path.parent.resolve() != source_cache or
        not path.name.startswith('owner-source-amendment-') or path.stat().st_size > 8192):
        raise ValueError('FREEZER_AUTHORITY_LOCATION')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference['sha256']:
        raise ValueError('FREEZER_AUTHORITY_CHANGED')
    value = json.loads(raw)
    expected = dict(version=1, scope='arbitrum_freezer_source_attempt_bytes',
        source_cache=str(source_cache), source_identity_sha256=hashlib.sha256(identity_body.encode()).hexdigest(),
        original_limit_bytes=1024**3, limit_bytes=None, deadline_ms=DEADLINE,
        monthly_limit_krw=1000000, ledger_reset=False)
    if any(k not in value or value[k] != v for k, v in expected.items()):
        raise ValueError('FREEZER_AUTHORITY_SCOPE')
    if type(value.get('authorized_at_ms')) is not int or not 0 < value['authorized_at_ms'] < DEADLINE:
        raise ValueError('FREEZER_AUTHORITY_TIME')
    return value
