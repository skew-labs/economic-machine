"""Hash already-decoded native output without a second large NVMe copy."""
import hashlib
import json


def decoded_digest(value):
    encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256()
    size = 0
    # Match json.dumps' C encoder path, without joining or writing another
    # canonical copy. The producer retains its existing memory/worker bounds.
    for text in encoder.iterencode(value, _one_shot=True):
        raw = text.encode('utf-8')
        size += len(raw)
        if size >= 1024**3:
            raise ValueError('DECODED_MEMORY_BOUND')
        digest.update(raw)
    digest.update(b'\n'); size += 1
    return size, digest.hexdigest()


def enabled(chain, prepared, setting, existing_policy=None):
    if setting not in ('0', '1'):
        raise ValueError('DECODED_MEMORY_SETTING')
    if existing_policy is not None:
        value = existing_policy.get('decoded_materialization')
        if value not in (None, 'memory_v1'):
            raise ValueError('DECODED_MEMORY_POLICY')
        value = value == 'memory_v1'
    else:
        value = setting == '1' and chain == 42161 and prepared
    if value and (chain != 42161 or prepared is not True):
        raise ValueError('DECODED_MEMORY_SCOPE')
    return value


def missing_is_memory_derived(report, name):
    return (name == 'candidate-decoded.json'
            and report.get('derived_reconstruction', {}).get('decoded_materialization') == 'memory_v1')
