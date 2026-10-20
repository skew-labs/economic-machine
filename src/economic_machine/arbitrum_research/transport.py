"""Bounded HTTP gzip decoding; wire and decoded bytes have separate limits."""
import zlib
from .source import SourceIntegrityError,SourceLimit

def read_body(response,limit):
    if type(limit) is not int or limit<1:raise ValueError('HTTP_BODY_BOUND')
    encoding=response.headers.get('Content-Encoding','identity').strip().lower()
    if encoding in ('','identity'):
        raw=response.read(limit+1)
        return raw[:limit],len(raw)>limit,dict(content_encoding='identity',wire_bytes=len(raw),
            decoded_bytes=len(raw),compression_integrity='not_applicable')
    if encoding!='gzip':raise SourceIntegrityError('SOURCE_UNEXPECTED_CONTENT_ENCODING')
    decoder=zlib.decompressobj(31);raw=bytearray();wire=0
    while True:
        if wire>=limit+1:raise SourceLimit('SOURCE_COMPRESSED_WIRE_BOUND')
        chunk=response.read(min(65536,limit+1-wire));wire+=len(chunk)
        if not chunk:
            if not decoder.eof:raise SourceIntegrityError('SOURCE_GZIP_INCOMPLETE')
            break
        if decoder.eof:raise SourceIntegrityError('SOURCE_GZIP_TRAILING_DATA')
        try:raw.extend(decoder.decompress(chunk,limit+1-len(raw)))
        except zlib.error as error:raise SourceIntegrityError('SOURCE_GZIP_INTEGRITY') from error
        if decoder.unused_data:raise SourceIntegrityError('SOURCE_GZIP_TRAILING_DATA')
        if len(raw)>limit:
            return bytes(raw[:limit]),True,dict(content_encoding='gzip',wire_bytes=wire,
                decoded_bytes=len(raw),compression_integrity='bounded_prefix_only')
    return bytes(raw),False,dict(content_encoding='gzip',wire_bytes=wire,
        decoded_bytes=len(raw),compression_integrity='gzip_crc_complete')
