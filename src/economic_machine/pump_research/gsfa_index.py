"""Bounded offline GSFA v5 inspection and candidate lookup.

Format: rpcpool/yellowstone-faithful/{compactindexsized,gsfa,indexmeta}.
No network, model calls, graph writes, or evidence promotion occurs here.
The 24-bit compact-index fingerprint can match an absent key: every returned
location MUST be checked against the signed CAR transaction and account list.
"""
from contextlib import ExitStack
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import time

from .store import decode

MIB = 1024 ** 2
GIB = 1024 ** 3
MASK = (1 << 64) - 1
FILES = {'pubkey-to-offset-and-size.index', 'linked-log', 'manifest'}
P1, P2, P3, P4, P5 = (11400714785074694791, 14029467366897019727,
                     1609587929392839161, 9650029242287828579, 2870177450012600261)


def _rot(x, n):
    return ((x << n) | (x >> (64 - n))) & MASK


def _round(x, y):
    return _rot((x + y * P2) & MASK, 31) * P1 & MASK


def xxh64(data):
    """Unseeded xxHash64; small keys only, no optional native dependency."""
    if len(data) > 1024:
        raise ValueError('GSFA_HASH_INPUT_BOUND')
    p = 0
    if len(data) >= 32:
        vs = [(P1 + P2) & MASK, P2, 0, (-P1) & MASK]
        while p + 32 <= len(data):
            for i in range(4):
                vs[i] = _round(vs[i], int.from_bytes(data[p:p + 8], 'little'))
                p += 8
        h = sum(_rot(v, n) for v, n in zip(vs, (1, 7, 12, 18))) & MASK
        for v in vs:
            h = ((h ^ _round(0, v)) * P1 + P4) & MASK
    else:
        h = P5
    h = (h + len(data)) & MASK
    while p + 8 <= len(data):
        h = (_rot(h ^ _round(0, int.from_bytes(data[p:p + 8], 'little')), 27) * P1 + P4) & MASK
        p += 8
    if p + 4 <= len(data):
        h = (_rot(h ^ (int.from_bytes(data[p:p + 4], 'little') * P1 & MASK), 23) * P2 + P3) & MASK
        p += 4
    for b in data[p:]:
        h = _rot(h ^ (b * P5 & MASK), 11) * P1 & MASK
    h ^= h >> 33
    h = h * P2 & MASK
    h ^= h >> 29
    h = h * P3 & MASK
    return h ^ (h >> 32)


class Budget:
    """Call with the active scope guard, effective deadline, and remaining room.

    max_expanded_bytes is a ceiling on all decompressed bytes, including tar
    framing. It cannot exceed 96 GiB. scope_guard(extra) enforces aggregate
    research storage and host free-space while extraction writes new bytes.
    """
    def __init__(self, deadline_ms, max_expanded_bytes, scope_guard):
        if (deadline_ms is not None and type(deadline_ms) is not int) or not callable(scope_guard):
            raise ValueError('GSFA_SCOPE_REQUIRED')
        if deadline_ms is None:
            continuation=getattr(getattr(scope_guard,'__self__',None),'continuation',None)
            if continuation is None or continuation.effective_deadline_ms is not None:
                raise ValueError('GSFA_CONTINUATION_SCOPE_REQUIRED')
        if type(max_expanded_bytes) is not int or not 0 < max_expanded_bytes <= 96 * GIB:
            raise ValueError('GSFA_EXPANSION_LIMIT')
        self.deadline_ms = deadline_ms
        self.maximum = max_expanded_bytes
        self.scope_guard = scope_guard
        self.expanded = 0
        self.check()

    def check(self, extra=0):
        if self.deadline_ms is not None and time.time_ns() // 1000000 >= self.deadline_ms:
            raise ValueError('GSFA_ORIGINAL_DEADLINE')
        self.scope_guard(extra)

    def consume(self, n):
        self.check()
        self.expanded += n
        if self.expanded > self.maximum:
            raise ValueError('GSFA_EXPANSION_LIMIT')


def _regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('GSFA_REGULAR_FILE_REQUIRED')
    return os.fdopen(fd, 'rb')


class ChunkStream:
    """Read retained downloader chunks once, checking each and combined hashes."""
    def __init__(self, manifest_path, budget):
        self.budget = budget
        path = Path(manifest_path)
        if path.stat().st_size > 8 * MIB:
            raise ValueError('GSFA_DOWNLOAD_MANIFEST_BOUND')
        with _regular(path) as f:
            self.manifest = json.load(f)
        self.folder = path.parent / 'chunks'
        if self.folder.is_symlink():
            raise ValueError('GSFA_CHUNK_DIRECTORY_LINK')
        self.rows = self.manifest['chunks']
        if not 1 <= len(self.rows) <= 10000:
            raise ValueError('GSFA_CHUNK_COUNT')
        end = 0
        for r in self.rows:
            if (r['start'] != end or type(r['bytes']) is not int or
                    not 0 < r['bytes'] <= 64 * MIB or
                    Path(r['path']).name != r['path'] or r['path'] in ('.', '..')):
                raise ValueError('GSFA_CHUNK_MANIFEST')
            end += r['bytes']
        if end != self.manifest['source']['size']:
            raise ValueError('GSFA_CHUNK_TOTAL')
        self.i = 0
        self.file = None
        self.left = 0
        self.digest = hashlib.sha256()
        self.combined = hashlib.sha256()
        self.verified = False

    def read(self, size):
        if not 0 < size <= MIB:
            raise ValueError('GSFA_READ_BOUND')
        self.budget.check()
        if self.file is None:
            if self.i == len(self.rows):
                if self.combined.hexdigest() != self.manifest['sha256']:
                    raise ValueError('GSFA_COMPRESSED_HASH')
                self.verified = True
                return b''
            r = self.rows[self.i]
            local = self.folder / r['path']
            ref = self.folder.parent / 'cloud-chunks' / (r['path']+'.json')
            if not local.exists() and ref.exists():
                from .research_cloud import CloudFile
                from .research_spend import FOLDER
                base=next((p for p in list(self.folder.parents)[:6] if (p/FOLDER/'grant.json').is_file()),None)
                if base is None:raise ValueError('GSFA_CLOUD_AUTHORITY_MISSING')
                self.file=_CloudSequence(CloudFile(base,ref,r))
            else:
                self.file = _regular(local)
                if os.fstat(self.file.fileno()).st_size != r['bytes']:
                    raise ValueError('GSFA_CHUNK_SIZE')
            self.left = r['bytes']
            self.digest = hashlib.sha256()
        data = self.file.read(min(size, self.left))
        if not data:
            raise ValueError('GSFA_CHUNK_TRUNCATED')
        self.digest.update(data)
        self.combined.update(data)
        self.left -= len(data)
        if not self.left:
            self.file.close()
            self.file = None
            if self.digest.hexdigest() != self.rows[self.i]['sha256']:
                raise ValueError('GSFA_CHUNK_HASH')
            self.i += 1
        return data

    def close(self):
        if self.file is not None:
            self.file.close()


class _CloudSequence:
    def __init__(self,reader):self.reader=reader;self.position=0
    def read(self,size):
        data=self.reader.read(self.position,min(size,self.reader.size-self.position));self.position+=len(data);return data
    def close(self):self.reader.close()


class _In(ctypes.Structure):
    _fields_ = [('src', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


class _Out(ctypes.Structure):
    _fields_ = [('dst', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


def _zstd():
    lib = ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_DCtx_setMaxWindowSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_DCtx_setMaxWindowSize.restype = ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Out), ctypes.POINTER(_In)]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t
    return lib


class ZstdStream:
    """64 KiB streaming buffers; bounded window and aggregate output."""
    def __init__(self, source, budget):
        self.source, self.budget, self.lib = source, budget, _zstd()
        self.ctx = self.lib.ZSTD_createDStream()
        if not self.ctx:
            raise ValueError('GSFA_ZSTD_CONTEXT')
        self.buffer = None
        self.input = _In(None, 0, 0)
        self.remaining = None
        self.eof = False
        self.source_eof = False
        for ret in (self.lib.ZSTD_initDStream(self.ctx),
                    self.lib.ZSTD_DCtx_setMaxWindowSize(self.ctx, 64 * MIB)):
            if self.lib.ZSTD_isError(ret):
                self.close()
                raise ValueError('GSFA_ZSTD_INIT')

    def read(self, size):
        if not 0 < size <= MIB:
            raise ValueError('GSFA_READ_BOUND')
        while not self.eof:
            self.budget.check()
            if self.input.pos == self.input.size and not self.source_eof:
                data = self.source.read(64 * 1024)
                if not data:
                    self.source_eof = True
                    self.input = _In(None, 0, 0)
                else:
                    self.buffer = ctypes.create_string_buffer(data)
                    # self.buffer owns this memory until the input is consumed.
                    # cast(array, void*) builds ctypes reference cycles on each
                    # 64 KiB chunk; raw addresses avoid unbounded GC backlog.
                    self.input = _In(ctypes.addressof(self.buffer), len(data), 0)
            if self.source_eof and self.remaining == 0:
                self.eof = True
                return b''
            buf = ctypes.create_string_buffer(min(size, 64 * 1024))
            out = _Out(ctypes.addressof(buf), len(buf), 0)
            old = self.input.pos
            self.remaining = self.lib.ZSTD_decompressStream(self.ctx, ctypes.byref(out), ctypes.byref(self.input))
            if self.lib.ZSTD_isError(self.remaining):
                raise ValueError('GSFA_ZSTD_FORMAT')
            if out.pos:
                self.budget.consume(out.pos)
                return buf.raw[:out.pos]
            if old == self.input.pos:
                raise ValueError('GSFA_ZSTD_TRUNCATED' if self.source_eof else 'GSFA_ZSTD_NO_PROGRESS')
        return b''

    def close(self):
        if self.ctx:
            self.lib.ZSTD_freeDStream(self.ctx)
            self.ctx = None


def _exact(stream, count):
    parts = []
    while count:
        raw = stream.read(min(count, 64 * 1024))
        if not raw:
            raise ValueError('GSFA_TRUNCATED')
        count -= len(raw)
        parts.append(raw)
    return b''.join(parts)


def _tar_int(raw):
    if raw[0] & 128:
        if raw[0] & 64:
            raise ValueError('GSFA_TAR_NEGATIVE_SIZE')
        return int.from_bytes(bytes([raw[0] & 127]) + raw[1:], 'big')
    text = raw.strip(b'\x00 ')
    if any(c not in b'01234567' for c in text):
        raise ValueError('GSFA_TAR_NUMBER')
    return int(text or b'0', 8)


def _tar_name(raw):
    try:
        name = raw.split(b'\x00', 1)[0].decode('utf-8')
    except UnicodeError as e:
        raise ValueError('GSFA_TAR_NAME') from e
    if len(name) > 4096 or '\\' in name or any(ord(c) < 32 for c in name):
        raise ValueError('GSFA_TAR_NAME')
    if name.startswith('./'):
        name = name[2:]
    name = name.rstrip('/')
    if not name or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('GSFA_TAR_PATH')
    return name


def _pax(raw):
    result = {}
    while raw:
        space = raw.find(b' ')
        if not 1 <= space <= 8 or not raw[:space].isdigit():
            raise ValueError('GSFA_TAR_PAX')
        n = int(raw[:space])
        if not space + 3 <= n <= len(raw) or raw[n - 1:n] != b'\n':
            raise ValueError('GSFA_TAR_PAX')
        record = raw[space + 1:n - 1]
        if b'=' not in record:
            raise ValueError('GSFA_TAR_PAX')
        key, value = record.split(b'=', 1)
        if key in result or key not in (b'path', b'size', b'mtime', b'atime', b'ctime', b'uid', b'gid', b'uname', b'gname'):
            raise ValueError('GSFA_TAR_PAX_UNSUPPORTED')
        result[key] = value
        raw = raw[n:]
    return result


def inspect_tar(stream, budget, destination=None):
    """Inspect an already decompressed tar; optionally extract to a NEW directory.

    Do a first inspection pass to learn expansion size before extracting.
    Failure preserves a partially written directory for explicit reconciliation.
    Only the three expected regular files in one root directory are accepted.
    """
    target = Path(destination) if destination is not None else None
    if target is not None:
        budget.check()
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
    rows, seen, roots = [], set(), set()
    pending = {}
    total, physical = 0, 0
    def read(n):
        nonlocal physical
        data = _exact(stream, n)
        physical += n
        if physical > budget.maximum:
            raise ValueError('GSFA_EXPANSION_LIMIT')
        budget.check()
        return data
    while True:
        header = read(512)
        if header == bytes(512):
            if read(512) != bytes(512) or pending:
                raise ValueError('GSFA_TAR_END')
            tail = 0
            while data := stream.read(64 * 1024):
                budget.check()
                tail += len(data)
                if any(data) or tail > MIB or physical + tail > budget.maximum:
                    raise ValueError('GSFA_TAR_TRAILING_DATA')
            break
        if _tar_int(header[148:156]) != sum(header[:148]) + 8 * 32 + sum(header[156:]):
            raise ValueError('GSFA_TAR_CHECKSUM')
        if header[257:263] not in (b'ustar\x00', b'ustar '):
            raise ValueError('GSFA_TAR_FORMAT')
        kind = header[156:157]
        size = _tar_int(header[124:136])
        if size > budget.maximum:
            raise ValueError('GSFA_EXPANSION_LIMIT')
        if kind in (b'x', b'L'):
            if pending or size > 65536:
                raise ValueError('GSFA_TAR_EXTENSION_BOUND')
            body = read(size)
            if size % 512:
                read(512 - size % 512)
            pending = _pax(body) if kind == b'x' else {b'path': body.rstrip(b'\x00\n')}
            continue
        if kind not in (b'0', b'\x00', b'5') or header[157:257].strip(b'\x00'):
            raise ValueError('GSFA_TAR_LINK_OR_SPECIAL')
        prefix = header[345:500].split(b'\x00', 1)[0] if header[257:263] == b'ustar\x00' else b''
        name = _tar_name(pending.get(b'path', (prefix + b'/' if prefix else b'') + header[:100].split(b'\x00', 1)[0]))
        if b'size' in pending:
            if not pending[b'size'].isdigit() or len(pending[b'size']) > 12:
                raise ValueError('GSFA_TAR_SIZE')
            size = int(pending[b'size'])
        pending = {}
        if name in seen or len(seen) >= 32:
            raise ValueError('GSFA_TAR_DUPLICATE_OR_COUNT')
        seen.add(name)
        path = PurePosixPath(name)
        if kind == b'5':
            if size or len(path.parts) != 1:
                raise ValueError('GSFA_TAR_DIRECTORY')
            continue
        if path.name not in FILES or len(path.parts) not in (1, 2):
            raise ValueError('GSFA_TAR_UNEXPECTED_MEMBER')
        roots.add(str(path.parent))
        if len(roots) != 1 or any(r['name'] == path.name for r in rows):
            raise ValueError('GSFA_TAR_MULTIPLE_ROOTS')
        total += size
        if total > budget.maximum or physical + size > budget.maximum:
            raise ValueError('GSFA_EXPANSION_LIMIT')
        digest = hashlib.sha256()
        with ExitStack() as stack:
            out = None
            if target is not None:
                budget.check(size)
                fd = os.open(target / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                out = stack.enter_context(os.fdopen(fd, 'wb'))
            left = size
            while left:
                data = read(min(left, 64 * 1024))
                digest.update(data)
                if out is not None:
                    budget.check(len(data))
                    out.write(data)
                left -= len(data)
            if out is not None:
                out.flush()
                os.fsync(out.fileno())
        if size % 512:
            read(512 - size % 512)
        rows.append(dict(name=path.name, archive_path=name, bytes=size, sha256=digest.hexdigest()))
    if {r['name'] for r in rows} != FILES:
        raise ValueError('GSFA_TAR_REQUIRED_MEMBERS')
    return dict(files=rows, member_bytes=total, decompressed_tar_bytes=physical + tail,
                extracted=target is not None, format_inspected=True, index_query_verified=False,
                address_membership_verified=False, independent_consensus_verified=False)


def inspect_download(manifest_path, budget, destination=None):
    source = ChunkStream(manifest_path, budget)
    stream = None
    try:
        stream = ZstdStream(source, budget)
        report = inspect_tar(stream, budget, destination)
        if not source.verified:
            raise ValueError('GSFA_DOWNLOAD_INCOMPLETE')
        report['compressed_sha256'] = source.manifest['sha256']
        report['downloaded_chunk_hashes_verified'] = True
        return report
    finally:
        if stream is not None:
            stream.close()
        source.close()


def _meta(data, pos=0):
    if pos >= len(data):
        raise ValueError('GSFA_METADATA_TRUNCATED')
    count, pos, result = data[pos], pos + 1, {}
    for _ in range(count):
        pair = []
        for _ in range(2):
            if pos >= len(data):
                raise ValueError('GSFA_METADATA_TRUNCATED')
            n = data[pos]
            pos += 1
            if pos + n > len(data):
                raise ValueError('GSFA_METADATA_TRUNCATED')
            pair.append(data[pos:pos + n])
            pos += n
        if not pair[0] or pair[0] in result:
            raise ValueError('GSFA_METADATA_DUPLICATE')
        result[pair[0]] = pair[1]
    return result, pos


def _uvarint(data, pos):
    v = 0
    for i in range(10):
        if pos >= len(data):
            raise ValueError('GSFA_VARINT_TRUNCATED')
        b = data[pos]
        pos += 1
        if i == 9 and b > 1:
            raise ValueError('GSFA_VARINT_OVERFLOW')
        v |= (b & 127) << (7 * i)
        if b < 128:
            if i and b == 0:
                raise ValueError('GSFA_VARINT_NONCANONICAL')
            return v, pos
    raise ValueError('GSFA_VARINT_OVERFLOW')


def _pointer(raw):
    if len(raw) != 9:
        raise ValueError('GSFA_POINTER')
    return int.from_bytes(raw[:6], 'little'), int.from_bytes(raw[6:], 'little')


class GsfaIndex:
    """Read-only v5 candidate lookup. Does not claim account or chain membership."""
    def __init__(self, directory, epoch, budget, expected_root_cid=None, linked_reader=None):
        if type(epoch) is not int or not 0 <= epoch <= 100000:
            raise ValueError('GSFA_EPOCH')
        self.epoch, self.budget = epoch, budget
        self.stack = ExitStack()
        self.root = Path(directory)
        self.linked_reader = linked_reader
        try:
            if self.root.is_symlink():
                raise ValueError('GSFA_DIRECTORY_LINK')
            self.files = {name: self.stack.enter_context(_regular(self.root / name)) for name in FILES
                          if name != 'linked-log' or linked_reader is None}
            self.sizes = {name: os.fstat(f.fileno()).st_size for name, f in self.files.items()}
            if linked_reader is not None:
                self.stack.callback(linked_reader.close)
                self.sizes['linked-log'] = linked_reader.size
            manifest = self.read('manifest', 0, min(self.sizes['manifest'], 131072))
            if len(manifest) < 17 or manifest[:8] != b'gsfamnfs' or int.from_bytes(manifest[8:16], 'little') != 5:
                raise ValueError('GSFA_MANIFEST_VERSION')
            mm, manifest_used = _meta(manifest, 16)
            if (self.sizes['manifest'] - manifest_used) % 16:
                raise ValueError('GSFA_MANIFEST_LENGTH')
            name = 'pubkey-to-offset-and-size.index'
            if self.sizes[name] <= 1733:
                raise ValueError('GSFA_BUGGY_OR_SHORT_INDEX')
            prefix = self.read(name, 0, 25)
            if prefix[:8] != b'compiszd' or prefix[24] != 1:
                raise ValueError('GSFA_INDEX_VERSION')
            header_len = 12 + int.from_bytes(prefix[8:12], 'little')
            if not 26 <= header_len <= 131072:
                raise ValueError('GSFA_INDEX_HEADER_BOUND')
            header = self.read(name, 0, header_len)
            self.header_len = header_len
            if int.from_bytes(header[12:20], 'little') != 9:
                raise ValueError('GSFA_INDEX_VALUE_SIZE')
            self.buckets = int.from_bytes(header[20:24], 'little')
            if not self.buckets or header_len + self.buckets * 16 > self.sizes[name]:
                raise ValueError('GSFA_BUCKET_COUNT')
            im, used = _meta(header, 25)
            if used != len(header) or im.get(b'kind') != b'pubkey-to-offset-and-size':
                raise ValueError('GSFA_INDEX_METADATA')
            for m in (mm, im):
                if (m.get(b'epoch') != struct.pack('<Q', epoch) or m.get(b'network') != b'mainnet' or
                        len(m.get(b'rootCid', b'')) != 36 or not m[b'rootCid'].startswith(b'\x01\x71\x12\x20')):
                    raise ValueError('GSFA_METADATA_IDENTITY')
            if mm[b'rootCid'] != im[b'rootCid'] or (expected_root_cid is not None and im[b'rootCid'] != expected_root_cid):
                raise ValueError('GSFA_ROOT_CID_MISMATCH')
            self.identity = hashlib.sha256(header + manifest[:manifest_used] + json.dumps(self.sizes, sort_keys=True).encode()).hexdigest()
            self.root_cid = im[b'rootCid'].hex()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, name, offset, size):
        self.budget.check()
        if type(offset) is not int or type(size) is not int or not 0 <= size <= 16 * MIB or offset < 0 or offset + size > self.sizes[name]:
            raise ValueError('GSFA_FILE_READ_BOUND')
        data = (self.linked_reader.read(offset,size) if name == 'linked-log' and self.linked_reader is not None
                else os.pread(self.files[name].fileno(), size, offset))
        if len(data) != size:
            raise ValueError('GSFA_FILE_CHANGED')
        return data

    def initial(self, key):
        n = self.buckets
        u = xxh64(key)
        reject_below = ((-n) & MASK) % n
        while u < reject_below:
            u ^= u >> 33
            u = u * 0xff51afd7ed558ccd & MASK
            u ^= u >> 33
            u = u * 0xc4ceb9fe1a85ec53 & MASK
            u ^= u >> 33
        name = 'pubkey-to-offset-and-size.index'
        raw = self.read(name, self.header_len + (u % n) * 16, 16)
        domain, count = struct.unpack('<II', raw[:8])
        offset = int.from_bytes(raw[10:16], 'little')
        if raw[8] != 3 or raw[9] or count > 1 << 24 or offset < self.header_len + n * 16 or offset + count * 12 > self.sizes[name]:
            raise ValueError('GSFA_BUCKET_BOUND')
        wanted = xxh64(struct.pack('<I', domain) + bytes(28) + key) & 0xffffff
        i = 0
        while i < count:
            entry = self.read(name, offset + i * 12, 12)
            found = int.from_bytes(entry[:3], 'little')
            if wanted == found:
                return _pointer(entry[3:])
            i = 2 * i + 1 + int(found < wanted)
        return None

    def _log(self, offset, size):
        data = self.read('linked-log', offset, size)
        length, pos = _uvarint(data, 0)
        if length + pos != len(data) or length <= 9:
            raise ValueError('GSFA_LINKED_LOG_LENGTH')
        previous = _pointer(data[-9:])
        if previous != (0, 0) and (not previous[1] or previous[0] + previous[1] > offset):
            raise ValueError('GSFA_LINKED_LOG_CYCLE_OR_OVERLAP')
        compressed = data[pos:-9]
        # A separate output budget bounds each zstd record before allocation.
        record_budget = Budget(self.budget.deadline_ms, 64 * MIB, self.budget.scope_guard)
        stream = ZstdStream(io.BytesIO(compressed), record_budget)
        try:
            pieces = []
            while raw := stream.read(64 * 1024):
                pieces.append(raw)
            payload = b''.join(pieces)
        finally:
            stream.close()
        entries, p = [], 0
        while p < len(payload):
            self.budget.check()
            values = []
            for _ in range(3):
                value, p = _uvarint(payload, p)
                values.append(value)
            if p >= len(payload):
                raise ValueError('GSFA_MISSING_FLAGS')
            flag = payload[p]
            p += 1
            car_offset, car_size, slot = values
            if flag & ~7 or not 0 < car_size <= 16 * MIB or slot // 432000 != self.epoch:
                raise ValueError('GSFA_CANDIDATE_BOUND')
            entries.append(dict(car_offset=car_offset, car_size=car_size, slot=slot,
                                index_has_metadata=bool(flag & 1), index_success=bool(flag & 2), index_is_vote=bool(flag & 4)))
            if len(entries) > 100000:
                raise ValueError('GSFA_LOG_ENTRY_BOUND')
        return entries, previous

    def page(self, wallet, limit=100, cursor=None):
        key = decode(wallet)
        if len(key) != 32 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('GSFA_QUERY_ARGUMENT')
        if cursor is None:
            pointer = self.initial(key)
            skip = 0
        else:
            if cursor.get('index_identity') != self.identity or cursor.get('wallet') != wallet:
                raise ValueError('GSFA_CURSOR_IDENTITY')
            pointer = (cursor.get('offset'), cursor.get('size'))
            skip = cursor.get('item')
            if type(skip) is not int or skip < 0:
                raise ValueError('GSFA_CURSOR_ITEM')
        rows, next_cursor, visited = [], None, 0
        while pointer is not None and pointer != (0, 0):
            visited += 1
            if visited > 256:
                raise ValueError('GSFA_PAGE_SEGMENT_BOUND')
            values, previous = self._log(*pointer)
            if skip > len(values):
                raise ValueError('GSFA_CURSOR_ITEM')
            take = min(limit - len(rows), len(values) - skip)
            rows.extend(values[skip:skip + take])
            skip += take
            if skip == len(values):
                pointer, skip = previous, 0
            if len(rows) == limit:
                if pointer != (0, 0):
                    next_cursor = dict(index_identity=self.identity, wallet=wallet,
                                       offset=pointer[0], size=pointer[1], item=skip)
                break
        return dict(epoch=self.epoch, wallet=wallet, candidates=rows, next_cursor=next_cursor,
                    index_identity=self.identity, index_chain_exhausted=next_cursor is None,
                    complete_address_history=False, fingerprint_may_collide=True,
                    address_membership_verified=False, independent_consensus_verified=False)


def main():
    """Offline inspection only; output on stdout, no source/service mutation."""
    import argparse
    from .history_core import Scope
    p = argparse.ArgumentParser()
    p.add_argument('--base', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--max-expanded-bytes', type=int, required=True)
    a = p.parse_args()
    base = Path(a.base)
    scope = Scope(base, base / 'evidence/core-trace-v1/grant.json')
    try:
        budget = Budget(scope.grant['deadline_ms'], a.max_expanded_bytes, scope.guard)
        print(json.dumps(inspect_download(a.manifest, budget), sort_keys=True))
    finally:
        scope.close()


if __name__ == '__main__':
    main()
