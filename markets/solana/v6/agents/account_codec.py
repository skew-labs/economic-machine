"""Bounded account decompression; never trust an advertised frame allocation."""
import base64,binascii
import zstandard as zstd
from wire import SIZE

def decode_account(data,expected=SIZE):
    if not isinstance(data,list) or len(data)!=2 or not isinstance(data[0],str):raise RuntimeError('invalid account encoding')
    if not 0<expected<=549440 or len(data[0])>4*((expected+1024+2)//3):raise RuntimeError('oversized account frame')
    try:raw=base64.b64decode(data[0],validate=True)
    except (ValueError,binascii.Error):raise RuntimeError('invalid account base64') from None
    if data[1]=='base64':
        if len(raw)!=expected:raise RuntimeError('invalid account size')
        return raw
    if data[1]!='base64+zstd':raise RuntimeError('unsupported account encoding')
    try:
        frame=zstd.get_frame_parameters(raw)
        if frame.content_size not in (zstd.CONTENTSIZE_UNKNOWN,expected) or frame.window_size>2*1024*1024 or frame.dict_id:
            raise RuntimeError('invalid compressed account bounds')
        # Check the frame's byte window explicitly above; the C backend's window
        # argument behaves differently for known/unknown-size single-shot frames.
        decoded=zstd.ZstdDecompressor(max_window_size=2*1024*1024).decompress(raw,max_output_size=expected,allow_extra_data=False)
    except zstd.ZstdError:raise RuntimeError('invalid compressed account') from None
    if len(decoded)!=expected:raise RuntimeError('invalid account size')
    return decoded
