import ctypes
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import time
import unittest

from economic_machine.pump_research.gsfa_index import (
    Budget, ChunkStream, GsfaIndex, MIB, ZstdStream, inspect_download,
    inspect_tar, xxh64,
)
from economic_machine.pump_research.store import encode


def budget(maximum=4 * MIB, guard=lambda extra: None):
    return Budget(time.time_ns() // 1000000 + 60000, maximum, guard)


def compress(raw):
    lib = ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    buf = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(raw)))
    size = lib.ZSTD_compress(buf, len(buf), raw, len(raw), 1)
    if size > len(buf):
        raise RuntimeError('test zstd compression failed')
    return buf.raw[:size]


def vint(n):
    parts = []
    while n >= 128:
        parts.append((n & 127) | 128)
        n >>= 7
    return bytes(parts + [n])


def pointer(offset, size):
    return offset.to_bytes(6, 'little') + size.to_bytes(3, 'little')


def meta(values):
    return bytes([len(values)]) + b''.join(bytes([len(k)]) + k + bytes([len(v)]) + v for k, v in values.items())


def fixture(directory, count=3, epoch=685):
    directory.mkdir()
    root_cid = b'\x01\x71\x12\x20' + bytes(32)
    identity = {b'epoch': struct.pack('<Q', epoch), b'rootCid': root_cid, b'network': b'mainnet'}
    (directory / 'manifest').write_bytes(b'gsfamnfs' + struct.pack('<Q', 5) + meta(identity))
    locations = [(1000 + i * 10, 100 + i, epoch * 432000 + i, 3) for i in range(count)]
    body = compress(b''.join(vint(o) + vint(n) + vint(s) + bytes([f]) for o, n, s, f in locations)) + pointer(0, 0)
    log = vint(len(body)) + body
    (directory / 'linked-log').write_bytes(log)
    ih = struct.pack('<QI', 9, 1) + b'\x01' + meta(identity | {b'kind': b'pubkey-to-offset-and-size'})
    header = b'compiszd' + struct.pack('<I', len(ih)) + ih
    key = bytes(range(32))
    hashed = xxh64(bytes(32) + key) & 0xffffff
    bucket = struct.pack('<II', 0, 1) + b'\x03\x00' + (len(header) + 16).to_bytes(6, 'little')
    index = header + bucket + hashed.to_bytes(3, 'little') + pointer(0, len(log))
    (directory / 'pubkey-to-offset-and-size.index').write_bytes(index.ljust(1734, b'\x00'))
    return encode(key), root_cid, log, len(header)


def tar_bytes(files=None, special=None, fmt=tarfile.USTAR_FORMAT):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w', format=fmt) as archive:
        if files is None:
            files = {'epoch-685-gsfa/manifest': b'm',
                     'epoch-685-gsfa/linked-log': b'l',
                     'epoch-685-gsfa/pubkey-to-offset-and-size.index': b'i'}
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if special is not None:
            archive.addfile(special)
    return result.getvalue()


class GsfaTests(unittest.TestCase):
    def test_xxhash_reference_vectors(self):
        self.assertEqual(xxh64(b''), 0xef46db3751d8e999)
        self.assertEqual(xxh64(b'a'), 0xd24ec4f1a98c6e5b)
        self.assertEqual(xxh64(b'hello'), 0x26c7827d889f6da3)

    def test_xxhash_long_input_native_parity(self):
        try:
            lib = ctypes.CDLL('libxxhash.so.0')
        except OSError:
            self.skipTest('optional native xxhash reference is not installed')
        lib.XXH64.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulonglong]
        lib.XXH64.restype = ctypes.c_ulonglong
        for n in (31, 32, 33, 63, 64, 65, 255):
            raw = bytes(range(n))
            self.assertEqual(xxh64(raw), lib.XXH64(raw, n, 0))

    def test_deadline_is_absolute(self):
        with self.assertRaisesRegex(ValueError, 'ORIGINAL_DEADLINE'):
            Budget(1, MIB, lambda extra: None)

    def test_storage_callback_propagates(self):
        def guard(extra):
            raise ValueError('existing aggregate storage limit')
        with self.assertRaisesRegex(ValueError, 'aggregate storage'):
            budget(guard=guard)

    def test_tar_inspect_does_not_extract(self):
        result = inspect_tar(io.BytesIO(tar_bytes()), budget())
        self.assertEqual(result['member_bytes'], 3)
        self.assertFalse(result['extracted'])
        self.assertFalse(result['index_query_verified'])

    def test_tar_extraction_fresh_directory_and_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            dst = Path(folder) / 'new'
            report = inspect_tar(io.BytesIO(tar_bytes()), budget(), dst)
            self.assertTrue(report['extracted'])
            self.assertEqual((dst / 'linked-log').read_bytes(), b'l')
            with self.assertRaises(FileExistsError):
                inspect_tar(io.BytesIO(tar_bytes()), budget(), dst)

    def test_tar_path_escape_rejected(self):
        for name in ('../manifest', '/manifest', 'root/../manifest', 'a\\manifest'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'TAR_(PATH|NAME)'):
                inspect_tar(io.BytesIO(tar_bytes({name: b'x'})), budget())

    def test_tar_symlink_hardlink_device_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            info = tarfile.TarInfo('bad')
            info.type = kind
            info.linkname = '/elsewhere'
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'LINK_OR_SPECIAL'):
                inspect_tar(io.BytesIO(tar_bytes(special=info)), budget())

    def test_tar_duplicate_member_rejected(self):
        info = tarfile.TarInfo('epoch-685-gsfa/manifest')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            inspect_tar(io.BytesIO(tar_bytes(special=info)), budget())

    def test_tar_multiple_roots_rejected(self):
        raw = tar_bytes({'one/manifest': b'm', 'two/linked-log': b'l'})
        with self.assertRaisesRegex(ValueError, 'MULTIPLE_ROOTS'):
            inspect_tar(io.BytesIO(raw), budget())

    def test_tar_missing_required_members_rejected(self):
        with self.assertRaisesRegex(ValueError, 'REQUIRED_MEMBERS'):
            inspect_tar(io.BytesIO(tar_bytes({'manifest': b'm'})), budget())

    def test_tar_bad_checksum_and_trailer_rejected(self):
        raw = bytearray(tar_bytes())
        raw[0] ^= 1
        with self.assertRaisesRegex(ValueError, 'CHECKSUM'):
            inspect_tar(io.BytesIO(raw), budget())
        with self.assertRaisesRegex(ValueError, 'TRAILING_DATA'):
            inspect_tar(io.BytesIO(tar_bytes() + b'evil'), budget())

    def test_tar_expansion_bound(self):
        with self.assertRaisesRegex(ValueError, 'EXPANSION_LIMIT'):
            inspect_tar(io.BytesIO(tar_bytes()), budget(1024))

    def test_tar_pax_path_escape(self):
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode='w', format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo('manifest')
            info.pax_headers = {'path': '../escape'}
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, 'TAR_PATH'):
            inspect_tar(io.BytesIO(result.getvalue()), budget())

    def test_tar_pax_sparse_rejected(self):
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode='w', format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo('manifest')
            info.pax_headers = {'GNU.sparse.size': '1000000000'}
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, 'PAX_UNSUPPORTED'):
            inspect_tar(io.BytesIO(result.getvalue()), budget())

    def test_streaming_chunks_verify_without_joined_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'chunks').mkdir()
            raw = compress(tar_bytes())
            split = len(raw) // 2
            rows = []
            for i, data in enumerate((raw[:split], raw[split:])):
                name = f'{i}.range'
                (root / 'chunks' / name).write_bytes(data)
                rows.append(dict(start=sum(r['bytes'] for r in rows), bytes=len(data),
                                 path=name, sha256=hashlib.sha256(data).hexdigest()))
            manifest = dict(source={'size': len(raw)}, chunks=rows, sha256=hashlib.sha256(raw).hexdigest())
            path = root / 'download-manifest.json'
            path.write_text(json.dumps(manifest))
            report = inspect_download(path, budget())
            self.assertTrue(report['downloaded_chunk_hashes_verified'])
            self.assertEqual(len(list(root.iterdir())), 2)
            (root / 'chunks' / rows[0]['path']).write_bytes(bytes(rows[0]['bytes']))
            with self.assertRaisesRegex(ValueError, 'CHUNK_HASH'):
                inspect_download(path, budget())

    def test_zstd_bomb_and_truncation(self):
        for raw, maximum, error in ((compress(bytes(100000)), 1000, 'EXPANSION_LIMIT'),
                                     (compress(bytes(100))[:-2], MIB, 'TRUNCATED')):
            stream = ZstdStream(io.BytesIO(raw), budget(maximum))
            try:
                with self.assertRaisesRegex(ValueError, error):
                    while stream.read(1000):
                        pass
            finally:
                stream.close()

    def test_zstd_flushes_large_output_across_small_reads(self):
        original = bytes(range(256)) * 3500
        stream = ZstdStream(io.BytesIO(compress(original)), budget())
        try:
            output = bytearray()
            while part := stream.read(113):
                output.extend(part)
            self.assertEqual(output, original)
        finally:
            stream.close()

    def test_lookup_pagination_and_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            wallet, cid, _, _ = fixture(directory)
            with GsfaIndex(directory, 685, budget(), cid) as index:
                first = index.page(wallet, limit=2)
                second = index.page(wallet, limit=2, cursor=first['next_cursor'])
                self.assertEqual(len(first['candidates']), 2)
                self.assertEqual(len(second['candidates']), 1)
                self.assertIsNone(second['next_cursor'])
                self.assertFalse(second['complete_address_history'])
                self.assertTrue(second['fingerprint_may_collide'])
                self.assertFalse(second['address_membership_verified'])

    def test_missing_fingerprint_is_only_index_negative(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            fixture(directory)
            with GsfaIndex(directory, 685, budget()) as index:
                result = index.page(encode(bytes(32)))
                self.assertEqual(result['candidates'], [])
                self.assertFalse(result['complete_address_history'])

    def test_other_epoch_metadata_and_candidate_slots_remain_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            wallet, cid, _, _ = fixture(directory, epoch=1008)
            with GsfaIndex(directory, 1008, budget(), cid) as index:
                result = index.page(wallet, limit=3)
                self.assertEqual(result['epoch'], 1008)
                self.assertTrue(all(row['slot']//432000 == 1008 for row in result['candidates']))
                self.assertFalse(result['address_membership_verified'])
            with self.assertRaisesRegex(ValueError, 'METADATA_IDENTITY'):
                GsfaIndex(directory, 685, budget())

    def test_cursor_wallet_or_index_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            wallet, _, _, _ = fixture(directory)
            with GsfaIndex(directory, 685, budget()) as index:
                cursor = index.page(wallet, 1)['next_cursor']
                cursor['wallet'] = encode(bytes(32))
                with self.assertRaisesRegex(ValueError, 'CURSOR_IDENTITY'):
                    index.page(wallet, cursor=cursor)

    def test_epoch_and_cid_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            fixture(directory)
            with self.assertRaisesRegex(ValueError, 'METADATA_IDENTITY'):
                GsfaIndex(directory, 684, budget())
            with self.assertRaisesRegex(ValueError, 'ROOT_CID_MISMATCH'):
                GsfaIndex(directory, 685, budget(), b'wrong')

    def test_unsupported_manifest_version_and_buggy_index(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            fixture(directory)
            path = directory / 'manifest'
            original = path.read_bytes()
            path.write_bytes(original[:8] + struct.pack('<Q', 4) + original[16:])
            with self.assertRaisesRegex(ValueError, 'MANIFEST_VERSION'):
                GsfaIndex(directory, 685, budget())
            path.write_bytes(original)
            p = directory / 'pubkey-to-offset-and-size.index'
            p.write_bytes(p.read_bytes()[:1733])
            with self.assertRaisesRegex(ValueError, 'BUGGY_OR_SHORT'):
                GsfaIndex(directory, 685, budget())

    def test_linked_log_cycle_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            wallet, _, log, _ = fixture(directory)
            (directory / 'linked-log').write_bytes(log[:-9] + pointer(0, len(log)))
            with GsfaIndex(directory, 685, budget()) as index:
                with self.assertRaisesRegex(ValueError, 'CYCLE_OR_OVERLAP'):
                    index.page(wallet)

    def test_symlink_index_member_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            fixture(directory)
            source = directory / 'linked-log'
            moved = directory / 'saved'
            source.rename(moved)
            source.symlink_to(moved)
            with self.assertRaises(OSError):
                GsfaIndex(directory, 685, budget())

    def test_bucket_pointer_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'index'
            wallet, _, _, header = fixture(directory)
            p = directory / 'pubkey-to-offset-and-size.index'
            data = bytearray(p.read_bytes())
            data[header + 10:header + 16] = (10 ** 9).to_bytes(6, 'little')
            p.write_bytes(data)
            with GsfaIndex(directory, 685, budget()) as index:
                with self.assertRaisesRegex(ValueError, 'BUCKET_BOUND'):
                    index.page(wallet)


if __name__ == '__main__':
    unittest.main()
