"""Pinned future-only decoder selection; legacy pages retain their binary."""
import hashlib
import json
import os
import time
from pathlib import Path

from .freezer_source import DEADLINE

TYPED_SHA = '4a76341ee4c97e254045cbc86403b836d670ada03aa68a2032a9c7aae2d73810'
TYPED_RELATIVE = 'bin/freezer-decode-typed-v1'
NATIVE_ENV = {'EM_FREEZER_DECODE_WORKERS': '2', 'GOMAXPROCS': '4', 'GOMEMLIMIT': '1536MiB'}


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('DECODER_FILE_IDENTITY')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def amendment(base, path, expected_sha, *, admission=False):
    base, path = Path(base).resolve(), Path(path)
    if (not path.is_relative_to(base/'evidence/freezer-continuous-v1')
            or path.is_symlink() or path.stat().st_size > 32768
            or digest(path) != expected_sha):
        raise ValueError('DECODER_AMENDMENT_HASH')
    value = json.loads(path.read_bytes())
    expected = dict(schema='economic-machine.freezer-decoder-amendment.v1',
        original_policy_sha256=digest(base/'evidence/freezer-continuous-v1/policy.json'),
        deadline_ms=DEADLINE, monthly_budget_krw=1000000,
        minimum_host_free_bytes=128*1024**3, batch_headroom_bytes=8*1024**3,
        source_writers=1, consumers=3, max_inflight=4, max_native_input_bytes=60*1024**2,
        binary_relative=TYPED_RELATIVE, binary_sha256=TYPED_SHA,
        environment=NATIVE_ENV, reset_cursor=False, preserve_existing_policies=True)
    if any(value.get(k) != v for k,v in expected.items()):
        raise ValueError('DECODER_AMENDMENT_SCOPE')
    now = time.time_ns()//1000000
    if (type(value.get('first_batch')) is not int or value['first_batch'] < 37099136
            or type(value.get('authorized_at_ms')) is not int or not 0 < value['authorized_at_ms'] <= now):
        raise ValueError('DECODER_AMENDMENT_TIME')
    if admission and now >= DEADLINE:
        raise ValueError('DECODER_DEADLINE')
    if digest(base/TYPED_RELATIVE) != TYPED_SHA:
        raise ValueError('DECODER_BINARY_CHANGED')
    sources = value.get('native_sources', {})
    if not sources or len(sources) > 32:
        raise ValueError('DECODER_SOURCE_SCOPE')
    for relative, expected in sources.items():
        target = base/'source'/relative
        if (not target.is_relative_to(base/'source/native/freezer_decode_typed_v1')
                or '..' in Path(relative).parts or digest(target) != expected):
            raise ValueError('DECODER_SOURCE_CHANGED')
    return value


def configured(base):
    path, sha = os.environ.get('EM_FREEZER_DECODER_AMENDMENT'), os.environ.get('EM_FREEZER_DECODER_AMENDMENT_SHA')
    if not path and not sha:
        return None
    if not path or not sha:
        raise ValueError('DECODER_AMENDMENT_ARGUMENT')
    value = amendment(base, path, sha, admission=True)
    return dict(path=str(Path(path)), sha256=sha, value=value)


def select_batch(base, folder, legacy_binary, first, route):
    """Existing policy decides resume, regardless of new-write routing flag."""
    base, folder, legacy_binary = Path(base).resolve(), Path(folder), Path(legacy_binary)
    policy_path = folder/'policy.json'
    if policy_path.exists():
        if policy_path.is_symlink():
            raise ValueError('DECODER_BATCH_POLICY_IDENTITY')
        pinned = json.loads(policy_path.read_bytes())['binary_sha256']
        if pinned == digest(legacy_binary):
            return legacy_binary
        if pinned != TYPED_SHA:
            raise ValueError('DECODER_BATCH_BINARY_UNKNOWN')
        # A typed batch always retains a separate durable route receipt, written
        # before the batch is launched. It survives disabling future selection.
        retained = json.loads((folder/'decoder-selection.json').read_bytes())
        checked = amendment(base, retained['path'], retained['sha256'], admission=True)
        if first < checked['first_batch']:
            raise ValueError('DECODER_BATCH_SCOPE')
        return base/TYPED_RELATIVE
    if any(folder.glob('window-*/acquired.json')):
        raise ValueError('DECODER_BATCH_POLICY_MISSING')
    selection = folder/'decoder-selection.json'
    if selection.exists():
        # A crash after the durable selection, before pipeline policy creation,
        # must not silently replace that selection with a different decoder.
        if selection.is_symlink():
            raise ValueError('DECODER_SELECTION_IDENTITY')
        retained = json.loads(selection.read_bytes())
        value = amendment(base, retained['path'], retained['sha256'], admission=True)
        if first < value['first_batch']:
            raise ValueError('DECODER_BATCH_SCOPE')
        return base/TYPED_RELATIVE
    if route and first >= route['value']['first_batch']:
        from economic_machine.pump_research.store import atomic
        receipt = dict(path=route['path'], sha256=route['sha256'])
        target = folder/'decoder-selection.json'
        if target.exists() and json.loads(target.read_bytes()) != receipt:
            raise ValueError('DECODER_SELECTION_CHANGED')
        if not target.exists():
            folder.mkdir(parents=True, exist_ok=True)
            atomic(target, receipt)
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        return base/TYPED_RELATIVE
    return legacy_binary


def page_profile(base, folder, binary, chain, first, prepared, existing_policy=None):
    base, folder, binary = Path(base).resolve(), Path(folder), Path(binary).resolve()
    old = (existing_policy or {}).get('decoder_execution')
    if binary != base/TYPED_RELATIVE:
        if old is not None:
            raise ValueError('DECODER_PAGE_DOWNGRADE')
        return None
    if chain != 42161 or not prepared:
        raise ValueError('DECODER_PAGE_SCOPE')
    receipt = json.loads((folder.parent/'decoder-selection.json').read_bytes())
    route = amendment(base, receipt['path'], receipt['sha256'], admission=True)
    if first < route['first_batch']:
        raise ValueError('DECODER_PAGE_SCOPE')
    profile = dict(schema='economic-machine.freezer-decoder-execution.v1',
        binary_relative=TYPED_RELATIVE, binary_sha256=TYPED_SHA, environment=NATIVE_ENV,
        amendment_path=receipt['path'], amendment_sha256=receipt['sha256'])
    if old is not None and old != profile:
        raise ValueError('DECODER_PAGE_CHANGED')
    return profile


def native_environment(profile):
    env = os.environ.copy()
    # Restrict typed parallel controls to the one native subprocess.
    if profile is not None:
        if profile.get('environment') != NATIVE_ENV:
            raise ValueError('DECODER_ENVIRONMENT')
        env.update(NATIVE_ENV)
    return env


def replay_binary(base, report):
    base = Path(base).resolve()
    proof = report['source_proof']['source']
    profile = proof.get('decoder_execution')
    if profile is None:
        binary = base/'bin/freezer-decode'
        if digest(binary) != proof['binary_sha256']:
            raise ValueError('CLOUD_DERIVED_BINARY_CHANGED')
        return binary, None
    expected = dict(schema='economic-machine.freezer-decoder-execution.v1',
        binary_relative=TYPED_RELATIVE, binary_sha256=TYPED_SHA, environment=NATIVE_ENV)
    if (any(profile.get(k) != v for k,v in expected.items())
            or set(profile) != set(expected)|{'amendment_path','amendment_sha256'}
            or proof['binary_sha256'] != TYPED_SHA
            or report.get('derived_reconstruction',{}).get('decoder_execution') != profile):
        raise ValueError('DECODER_REPLAY_PROFILE')
    amendment(base, profile['amendment_path'], profile['amendment_sha256'])
    return base/TYPED_RELATIVE, profile
