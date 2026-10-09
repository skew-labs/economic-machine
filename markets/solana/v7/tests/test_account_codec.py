import base64,sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'agents'),str(Path(__file__).resolve().parents[1]/'client')]
import zstandard as zstd
from account_codec import decode_account
from wire import SIZE
def encoded(raw,kind='base64+zstd'):return [base64.b64encode(raw).decode(),kind]
class AccountCodec(unittest.TestCase):
    def test_both_frame_size_modes_and_uncompressed_are_exact(self):
        data=(bytes(range(256))*((SIZE+255)//256))[:SIZE]
        self.assertEqual(decode_account(encoded(data,'base64')),data)
        for known in (True,False):self.assertEqual(decode_account(encoded(zstd.ZstdCompressor(write_content_size=known).compress(data))),data)
    def test_size_bomb_truncated_trailing_concat_and_bad_encoding_rejected(self):
        good=zstd.ZstdCompressor().compress(bytes(SIZE))
        bad=[encoded(good[:-1]),encoded(good+b'x'),encoded(good+good),encoded(good,'jsonParsed'),['!!!','base64'],encoded(bytes(SIZE-1),'base64')]
        for known in (True,False):bad.append(encoded(zstd.ZstdCompressor(write_content_size=known).compress(bytes(SIZE+1))))
        for payload in bad:
            with self.assertRaises(RuntimeError):decode_account(payload)
if __name__=='__main__':unittest.main(verbosity=2)
