"""Finite public snapshot transport comparison, centrally admitted per request.

The worker receives only a public immutable-range ticket. It never receives
model, Blob, signing, or spending credentials. The coordinator retains the
original source writer lock, source ledger, budget, and provider gate.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import time
import urllib.error
import urllib.parse
import uuid

DEADLINE = 1792351694298
SIZE = 8 * 1024**2
HOSTS = {'snapshot.arbitrum.io', 'snapshot.arbitrum.foundation'}


def save(path, body):
    raw = (json.dumps(body, sort_keys=True, separators=(',', ':')) + '\n').encode()
    with Path(path).open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def validate_ticket(t, now_ms=None):
    now_ms = time.time_ns() // 1000000 if now_ms is None else now_ms
    if (not isinstance(t, dict) or t.get('schema') != 'economic-machine.public-range-probe.v1'
            or not re.fullmatch('[0-9a-f]{32}', str(t.get('id', '')))
            or t.get('site') not in ('amsterdam', 'tokyo')
            or type(t.get('bytes')) is not int or t['bytes'] != SIZE
            or type(t.get('relative')) is not int or t['relative'] < 0
            or type(t.get('expires_ms')) is not int
            or not now_ms < t['expires_ms'] <= min(now_ms + 180000, DEADLINE)
            or not re.fullmatch('[0-9a-f]{64}', str(t.get('expected_sha256', '')))):
        raise ValueError('PROBE_TICKET_SCOPE')
    row, pin = t['part'], t['pin']
    if type(row.get('bytes')) is not int or t['relative'] + SIZE > row['bytes']:
        raise ValueError('PROBE_PART_BOUND')
    url = urllib.parse.urlsplit(row['url'])
    target = urllib.parse.urlsplit(pin.get('effective_url', row['url']))
    if (url.scheme != 'https' or target.scheme != 'https'
            or url.netloc not in HOSTS or target.netloc not in HOSTS
            or url.path != target.path or url.query or target.query
            or url.fragment or target.fragment or pin.get('bytes') != row['bytes']
            or not isinstance(pin.get('etag'), str) or not 1 <= len(pin['etag']) <= 256
            or '\n' in pin['etag'] or '\r' in pin['etag']):
        raise ValueError('PROBE_SOURCE_PIN')
    return target


def response_error(receipt):
    if receipt.get('state') == 'HTTP_ERROR':
        code = receipt.get('status')
        if type(code) is not int or not 100 <= code <= 599:
            return ValueError('PROBE_BAD_REMOTE_STATUS')
        retry = receipt.get('retry_after', '')
        headers = {'Retry-After': retry} if isinstance(retry, str) and len(retry) <= 256 else {}
        return urllib.error.HTTPError('public-snapshot-probe', code, 'range rejected', headers, None)
    if receipt.get('state') == 'TRANSPORT_ERROR':
        return urllib.error.URLError('REMOTE_TRANSPORT_FAILED')
    return ValueError('PROBE_REMOTE_VALIDATION_FAILED')


def validate_receipt(ticket, receipt):
    if receipt.get('id') != ticket['id'] or receipt.get('site') != ticket['site']:
        raise ValueError('PROBE_REMOTE_IDENTITY')
    if receipt.get('state') != 'VERIFIED':
        raise response_error(receipt)
    if (receipt.get('sha256') != ticket['expected_sha256'] or receipt.get('bytes') != SIZE
            or receipt.get('source') != ticket['pin'] or receipt.get('requests') != 1
            or receipt.get('ended_ms', DEADLINE) >= ticket['expires_ms']):
        raise ValueError('PROBE_REMOTE_CONTENT_MISMATCH')
    return receipt


def admitted(guard, gate, reserve, request, commit):
    """The lease covers remote dispatch, the full body, and central validation."""
    guard()
    with gate:
        guard()
        reservation = reserve()
        receipt = request(reservation)
        guard()
        commit(reservation, receipt)
        return receipt


def fetch(ticket, out, factory=http.client.HTTPSConnection):
    target = validate_ticket(ticket)
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    # Existing Tokyo worker admission is 8GiB host free and a 2GiB retained cache.
    # Amsterdam admission is additionally checked by the central Cloud guard.
    if shutil.disk_usage(out).free - 2 * SIZE < (8 if ticket['site'] == 'tokyo' else 128) * 1024**3:
        raise ValueError('PROBE_HOST_FREE_SPACE')
    if ticket['site'] == 'tokyo':
        existing = Path('/srv/economic-machine/history-worker/data')
        retained = sum(p.stat().st_size for p in existing.glob('*/*') if p.is_file())
        retained += sum(p.stat().st_size for p in out.glob('*') if p.is_file())
        if retained + 2 * SIZE > 2 * 1024**3:
            raise ValueError('PROBE_EXISTING_WORKER_CACHE_CAP')
    path = out / (ticket['id'] + '.raw')
    if path.exists() or path.with_suffix('.json').exists():
        raise ValueError('PROBE_NO_REPLAY')
    began = time.monotonic(); started = time.time_ns() // 1000000
    c = factory(target.hostname, timeout=20)
    result = dict(id=ticket['id'], site=ticket['site'], started_ms=started,
                  requests=1, bytes=0, model_calls=0, credentials_received=False)
    try:
        start = ticket['relative']; end = start + SIZE - 1
        c.request('GET', target.path, headers={'Range': f'bytes={start}-{end}',
            'If-Match': ticket['pin']['etag'], 'Accept-Encoding': 'identity',
            'User-Agent': 'EconomicMachineResearch/0.1'})
        response = c.getresponse(); headers_at = time.monotonic()
        result.update(headers_seconds=headers_at-began, status=response.status)
        if response.status != 206:
            result.update(state='HTTP_ERROR', retry_after=response.headers.get('Retry-After', '')[:256])
        else:
            if (response.headers.get('Content-Range') != f"bytes {start}-{end}/{ticket['part']['bytes']}"
                    or response.headers.get('Content-Encoding', 'identity') != 'identity'):
                raise ValueError('PROBE_RANGE_MISMATCH')
            source = dict(etag=response.headers.get('ETag'), bytes=ticket['part']['bytes'])
            if ticket['pin'].get('effective_url'): source['effective_url'] = ticket['pin']['effective_url']
            if source != ticket['pin']: raise ValueError('PROBE_ETAG_MISMATCH')
            # Central accounting reserves the requested bytes plus overflow detection.
            data = response.read(SIZE + 1)
            result.update(body_seconds=time.monotonic()-headers_at, bytes=len(data))
            with path.open('xb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != SIZE or digest != ticket['expected_sha256']:
                raise ValueError('PROBE_BODY_MISMATCH')
            if time.time_ns() // 1000000 >= ticket['expires_ms']:
                raise ValueError('PROBE_DEADLINE_AFTER_READ')
            result.update(state='VERIFIED', sha256=digest, source=source, retained_path=str(path))
    except (OSError, http.client.HTTPException) as error:
        result.update(state='TRANSPORT_ERROR', error_type=type(error).__name__)
    except Exception as error:
        result.update(state='VALIDATION_ERROR', error_type=type(error).__name__, error=str(error)[:100])
    finally:
        c.close()
    result.update(ended_ms=time.time_ns()//1000000, seconds=time.monotonic()-began)
    save(path.with_suffix('.json'), result)
    return result


def main():
    p = argparse.ArgumentParser(); p.add_argument('--ticket', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); args = p.parse_args()
    if args.ticket.stat().st_size > 8192: raise ValueError('PROBE_TICKET_BOUND')
    ticket = json.loads(args.ticket.read_bytes())
    print(json.dumps(fetch(ticket, args.out), sort_keys=True))


if __name__ == '__main__': main()
