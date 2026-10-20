"""Append-only owner approval for one public historical address-index epoch."""
import hashlib
import json

GIB=1024**3

def extension(base, parent_hash, deadline):
    folder=base/'evidence/address-epoch-v1';path=folder/'grant.json'
    if not path.exists():return None
    raw=path.read_bytes();v=json.loads(raw)
    if (v.get('schema')!='economic-machine.address-epoch-grant.v1' or
        v.get('parent_grant_sha256')!=parent_hash or v.get('deadline_ms')!=deadline or
        v.get('additional_io_bytes')!=32*GIB or v.get('total_storage_bytes')!=96*GIB or
        v.get('maximum_cpu_percent')!=100 or v.get('epochs')!=[685] or
        v.get('paid_api') is not False or v.get('trading') is not False or
        v.get('new_server') is not False or
        (folder/'grant.sha256').read_text().strip()!=hashlib.sha256(raw).hexdigest()):
        raise ValueError('CORE_EXTENSION_IDENTITY')
    return v
