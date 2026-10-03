"""Owner-configured SSH storage: ciphertext only, pinned host, retrieval proof."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from economic_machine.values import MachineError, require_keys

from .backup import restore


def private_reference(raw, *, key=False):
    if not isinstance(raw, str):
        raise MachineError('PRIVATE_BACKUP_REFERENCE_REQUIRED')
    path = Path(raw)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid()
            or path.stat().st_nlink != 1 or path.stat().st_mode & (0o077 if key else 0o022)):
        raise MachineError('OWNER_CONTROLLED_BACKUP_REFERENCE_REQUIRED')
    return path


def destination(path):
    reference = private_reference(str(path), key=True)
    if reference.stat().st_size > 4096:
        raise MachineError('BOUNDED_BACKUP_DESTINATION_REQUIRED')
    raw = json.loads(reference.read_bytes())
    require_keys(raw, {'host', 'user', 'directory', 'identity_file', 'known_hosts_file'}, 'backup destination')
    for field, expression in [('host', r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}'), ('user', r'[a-z_][a-z0-9_-]{0,31}'),
                              ('directory', r'/var/lib/skew-backups/[A-Za-z0-9/_-]{1,160}')]:
        if not isinstance(raw[field], str) or re.fullmatch(expression, raw[field]) is None or '..' in raw[field]:
            raise MachineError('PINNED_BACKUP_DESTINATION_REQUIRED')
    private_reference(raw['identity_file'], key=True)
    private_reference(raw['known_hosts_file'])
    return raw


def export_and_restore(encrypted, key, profile_path, *, runner=subprocess.run):
    """Never transfer the encryption key or replace the owner's destination.

    The directory must already exist with mode 0700. A failure, lost transfer
    or failed restore is not off-host acceptance. No unattended host admission.
    """
    encrypted = Path(encrypted)
    if encrypted.is_symlink() or not encrypted.is_file() or encrypted.stat().st_size > 256 * 1024 * 1024:
        raise MachineError('BOUNDED_ENCRYPTED_BACKUP_REQUIRED')
    raw = destination(profile_path)
    ciphertext = encrypted.read_bytes()
    fingerprint = hashlib.sha256(ciphertext).hexdigest()
    filename = 'machine-' + fingerprint + '.encrypted'
    remote_path = raw['directory'].rstrip('/') + '/' + filename
    host = raw['user'] + '@' + raw['host']
    options = ['-i', raw['identity_file'], '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes',
               '-o', 'UserKnownHostsFile=' + raw['known_hosts_file'], '-o', 'ConnectTimeout=10']
    def execute(args):
        try:
            response = runner(args, check=True, capture_output=True, timeout=60)
            if len(response.stdout) > 4096 or len(response.stderr) > 4096:
                raise MachineError('BOUNDED_BACKUP_SSH_RESPONSE_REQUIRED')
            return response.stdout
        except (OSError, subprocess.SubprocessError):
            raise MachineError('BACKUP_TRANSFER_OR_REMOTE_CHECK_FAILED') from None
    path_arg = shlex.quote(raw['directory'])
    local_id = execute(['cat', '/etc/machine-id']).strip()
    remote_id = execute(['ssh', *options, host, 'cat /etc/machine-id']).strip()
    if (re.fullmatch(rb'[a-f0-9]{32}', local_id) is None
            or re.fullmatch(rb'[a-f0-9]{32}', remote_id) is None or local_id == remote_id):
        raise MachineError('DISTINCT_BACKUP_HOST_REQUIRED')
    execute(['ssh', *options, host, f'test -d {path_arg} && test ! -L {path_arg} && test "$(stat -c %a {path_arg})" = 700 && test "$(stat -c %u {path_arg})" = "$(id -u)"'])
    execute(['scp', *options, '--', str(encrypted), host + ':' + remote_path])
    observed = execute(['ssh', *options, host, 'sha256sum -- ' + shlex.quote(remote_path)]).split()
    if len(observed) != 2 or observed[0].decode('ascii', errors='replace') != fingerprint:
        raise MachineError('OFF_HOST_BACKUP_HASH_MISMATCH')
    with tempfile.TemporaryDirectory(dir=encrypted.parent) as temporary:
        returned = Path(temporary) / 'retrieved.encrypted'
        execute(['scp', *options, '--', host + ':' + remote_path, str(returned)])
        if returned.is_symlink() or hashlib.sha256(returned.read_bytes()).hexdigest() != fingerprint:
            raise MachineError('RETRIEVED_BACKUP_HASH_MISMATCH')
        proof = restore(returned.read_bytes(), key, Path(temporary) / 'restore')
    return {'off_host_verified': True, 'remote_ciphertext_sha256': fingerprint,
        'distinct_machine_ids_observed': True,
        'retrieval_sha256_matches': True, 'restore_from_retrieved_ciphertext': proof['restore_verified'],
        'off_host_key_transferred': False, 'disaster_recovery_key_custody': 'SEPARATE_EXTERNAL_KEY_CUSTODY_REQUIRED'}
