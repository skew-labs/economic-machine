"""Read-only, bounded PG custom-archive research reader; never executes SQL.

Wire format checked against PostgreSQL REL_17_STABLE pg_backup_archiver.c and
pg_backup_custom.c. Only v1.15/1.16, custom format, zlib and COPY text supported.
Partial HTTPS ranges establish reproducible dataset bytes, not chain finality.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from types import SimpleNamespace
import urllib.request
import zlib

from .io_budget import reserve


class Reader:
    def __init__(self, raw):
        self.raw, self.p = raw, 0

    def take(self, n):
        if not 0 <= n <= 16*1024**2 or self.p+n > len(self.raw):
            raise ValueError('DUMP_TRUNCATED_OR_BOUND')
        out = self.raw[self.p:self.p+n]
        self.p += n
        return out

    def integer(self):
        sign = self.take(1)[0]
        if sign not in (0, 1):
            raise ValueError('DUMP_INTEGER_SIGN')
        return (-1 if sign else 1)*int.from_bytes(self.take(4), 'little')

    def string(self):
        n = self.integer()
        if n == -1:
            return None
        if not 0 <= n <= 1024**2:
            raise ValueError('DUMP_STRING_BOUND')
        return self.take(n).decode('utf8')


def toc(raw):
    r = Reader(raw)
    if r.take(5) != b'PGDMP':
        raise ValueError('DUMP_MAGIC')
    version = tuple(r.take(3))
    if version not in ((1, 15, 0), (1, 16, 0)):
        raise ValueError('DUMP_UNSUPPORTED_VERSION')
    if r.take(4) != bytes((4, 8, 1, 1)):
        raise ValueError('DUMP_UNSUPPORTED_ENCODING_COMPRESSION')
    date = [r.integer() for _ in range(7)]
    header = dict(version=version, date_fields=date, database=r.string(),
                  server=r.string(), dumper=r.string())
    n = r.integer()
    if not 0 < n <= 20000:
        raise ValueError('DUMP_TOC_BOUND')
    entries = []
    for _ in range(n):
        row = dict(id=r.integer(), has_data=r.integer(), tableoid=r.string(),
                   oid=r.string(), tag=r.string(), description=r.string(),
                   section=r.integer(), definition=r.string(), drop=r.string(),
                   copy=r.string(), namespace=r.string(), tablespace=r.string(),
                   tableam=r.string())
        if version >= (1, 16, 0):
            row['relkind'] = r.integer()
        row.update(owner=r.string(), with_oids=r.string())
        deps = []
        while True:
            v = r.string()
            if v is None:
                break
            if len(deps) >= 20000:
                raise ValueError('DUMP_DEPENDENCY_BOUND')
            deps.append(v)
        row['dependencies'] = deps
        row['offset_state'] = r.take(1)[0]
        row['offset'] = int.from_bytes(r.take(8), 'little')
        if row['offset_state'] not in (1, 2, 3) or row['id'] <= 0:
            raise ValueError('DUMP_OFFSET_ID')
        entries.append(row)
    if len({e['id'] for e in entries}) != n:
        raise ValueError('DUMP_DUPLICATE_ID')
    return dict(header=header, toc_bytes=r.p, entries=entries)


def columns(entry):
    # Strict subset. Treat the SQL as data; no database connections or execution.
    m = re.fullmatch(r'COPY public\.([a-z_][a-z_0-9]*) \(([a-z_0-9, \"\"]+)\) FROM stdin;\n?', entry['copy'] or '')
    if not m or m[1] != entry['tag']:
        raise ValueError('DUMP_COPY_FORMAT')
    names = m[2].split(', ')
    if any(not re.fullmatch(r'(?:[a-z_][a-z_0-9]*|"[a-z_][a-z_0-9]*")', n) for n in names):
        raise ValueError('DUMP_COPY_COLUMN')
    names = [n.strip('"') for n in names]
    if len(names) != len(set(names)):
        raise ValueError('DUMP_COPY_DUPLICATE_COLUMN')
    return names


def copy_lines(raw, entry, output_limit=2*1024**3):
    """Yield complete COPY lines with zlib checksum, id and end-marker checks.

    Consumers must not publish an accepted report until exhaustion succeeds.
    Raw input and temporary analysis are retained if validation fails.
    """
    r = Reader(raw)
    if r.take(1) != b'\x01' or r.integer() != entry['id']:
        raise ValueError('DUMP_BLOCK_ID')
    z = zlib.decompressobj()
    total, pending = 0, b''
    while True:
        n = r.integer()
        if n == 0:
            break
        if not 0 < n <= 4*1024**2 or z.eof:
            raise ValueError('DUMP_BLOCK_BOUND')
        chunk = r.take(n)
        while chunk:
            out = z.decompress(chunk, 1024**2)
            chunk = z.unconsumed_tail
            total += len(out)
            if total > output_limit or z.unused_data:
                raise ValueError('DUMP_INFLATE_BOUND_OR_TRAILING')
            pending += out
            lines = pending.split(b'\n')
            pending = lines.pop()
            for line in lines:
                if len(line) > 4*1024**2:
                    raise ValueError('DUMP_LINE_BOUND')
                yield line
            if len(pending) > 4*1024**2:
                raise ValueError('DUMP_LINE_BOUND')
    if not z.eof or pending:
        raise ValueError('DUMP_INCOMPLETE_STREAM')
    # Caller may fetch a few bytes beyond a table boundary, so r.p need not
    # equal len(raw). The checksum and zero-length block authenticate its end.


def copy_value(raw):
    if raw == b'\\N':
        return None
    raw = raw.decode('utf8')
    def replace(m):
        t = m[1]
        if t[0] in '01234567':
            return chr(int(t, 8))
        if t[0] == 'x':
            return chr(int(t[1:], 16))
        return {'b':'\b', 'f':'\f', 'n':'\n', 'r':'\r', 't':'\t', 'v':'\v', '\\':'\\'}[t]
    return re.sub(r'\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|[bfnrtv\\])', replace, raw)


def copy_rows(raw, entry, output_limit=2*1024**3):
    names = columns(entry)
    end = False
    for line in copy_lines(raw, entry, output_limit):
        if end:
            if line:
                raise ValueError('DUMP_DATA_AFTER_COPY_END')
            continue
        if line == b'\\.':
            end = True
            continue
        cells = line.split(b'\t')
        if len(cells) != len(names):
            raise ValueError('DUMP_COPY_CELL_COUNT')
        yield dict(zip(names, (copy_value(c) for c in cells)))
    if not end:
        raise ValueError('DUMP_COPY_END_MISSING')


class Ranges:
    """Pinned public parts, durable shared IO reservations, no retries by stealth."""
    def __init__(self, base, out, parts):
        self.base, self.out, self.parts = Path(base), Path(out), parts
        self.out.mkdir(parents=True, exist_ok=True)
        self.store = SimpleNamespace(root=self.base/'data', state=lambda *args: 0)

    def fetch(self, offset, length):
        if not 0 <= offset or not 0 < length <= 320*1024**2:
            raise ValueError('DUMP_RANGE_BOUND')
        result = bytearray()
        for part in self.parts:
            if offset >= part['size']:
                offset -= part['size']
                continue
            while offset < part['size'] and length:
                size = min(length, part['size']-offset, 32*1024**2)
                result.extend(self._part(part, offset, size))
                offset += size
                length -= size
            offset = 0
            if not length:
                return bytes(result)
        raise ValueError('DUMP_RANGE_OUTSIDE_PARTS')

    def _part(self, part, start, size):
        ident = hashlib.sha256((part['url']+':'+str(start)+':'+str(size)).encode()).hexdigest()
        path, meta = self.out/(ident+'.range'), self.out/(ident+'.json')
        if path.exists() and meta.exists():
            raw = path.read_bytes()
            info = json.loads(meta.read_text())
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != info['sha256']:
                raise ValueError('DUMP_CACHE_HASH')
            return raw
        scope = json.loads((self.base/'data/research-model-scope.json').read_text())
        if time.time()*1000 >= scope['deadline_ms']:
            raise ValueError('RESEARCH_SCOPE_ENDED')
        # Include the complete research tree, not just the ingest DB quota.
        used = sum(p.stat().st_size for p in self.base.rglob('*') if p.is_file())
        if used+size > 5*1024**3 or shutil.disk_usage(self.base).free-size < 48*1024**3:
            raise ValueError('RESEARCH_STORAGE_LIMIT')
        if not part['url'].startswith(('https://raw.githubusercontent.com/', 'https://ndownloader.figshare.com/')):
            raise ValueError('DUMP_SOURCE_HOST')
        reserve(self.store, size, 'history_alpha_range')
        req = urllib.request.Request(part['url'], headers={
            'Range': f'bytes={start}-{start+size-1}', 'Accept-Encoding': 'identity',
            'User-Agent': 'Skew-Pump-Research/1.0'})
        with urllib.request.urlopen(req, timeout=40) as response:
            expected = f'bytes {start}-{start+size-1}/{part["size"]}'
            if response.status != 206 or response.headers.get('Content-Range') != expected:
                raise ValueError('DUMP_RANGE_NOT_HONORED')
            raw = response.read(size+1)
            if len(raw) != size:
                raise ValueError('DUMP_RANGE_LENGTH')
            info = dict(url=part['url'], start=start, bytes=size,
                        sha256=hashlib.sha256(raw).hexdigest(),
                        etag=response.headers.get('ETag'), at=time.time(),
                        whole_part_checksum_verified=False, chain_verified=False)
        # Unique immutable range files; incomplete writes are never cache hits.
        path.write_bytes(raw)
        meta.write_text(json.dumps(info, sort_keys=True)+'\n')
        return raw
