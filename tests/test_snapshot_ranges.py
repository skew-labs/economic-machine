import tarfile
import unittest
import io
from economic_machine.arbitrum_research.snapshot_ranges import parts,split_range,tar_header,HTTP

class Response(io.BytesIO):
    def __init__(self,body,start=0,total=100,status=206,etag='"one"'):
        super().__init__(body);self.status=status;self.will_close=False
        self.headers={'Content-Range':f'bytes {start}-{start+len(body)-1}/{total}','ETag':etag,'Content-Encoding':'identity'}
class Connection:
    def __init__(self,responses):self.responses=responses;self.requests=[];self.closed=False
    def request(self,*a,**kw):self.requests.append((a,kw))
    def getresponse(self):return self.responses.pop(0)
    def close(self):self.closed=True


class SnapshotTests(unittest.TestCase):
    def metadata(self):
        return dict(compression='none',size=dict(total_bytes=4096),parts=[
            dict(filename='full-path.tar.part0000',size_bytes=2048,sha256='a'*64),
            dict(filename='full-path.tar.part0001',size_bytes=2048,sha256='b'*64)])
    def test_public_manifest_pins_order_size_and_hashes(self):
        rows=parts(self.metadata(),'https://robinhood-snapshots.offchainlabs.com/chain/snapshot')
        self.assertEqual([r['offset'] for r in rows],[0,2048])
        split=split_range(rows,2000,100)
        self.assertEqual([(a,n) for _,a,n in split],[(2000,48),(0,52)])
    def test_bad_manifest_compression_host_hash_and_total(self):
        for key,value in [('compression','zstd'),('size',dict(total_bytes=1))]:
            m=self.metadata();m[key]=value
            with self.assertRaises(ValueError):parts(m,'https://snapshot.arbitrum.io/arb1/one')
        for host in ['https://evil.test/x','http://snapshot.arbitrum.io/x']:
            with self.assertRaises(ValueError):parts(self.metadata(),host)
        m=self.metadata();m['parts'][0]['filename']='../private'
        with self.assertRaises(ValueError):parts(m,'https://snapshot.arbitrum.io/arb1/one')
    def test_tar_jump_preserves_location_without_reading_payload(self):
        t=tarfile.TarInfo('l2chaindata/ancient/chain/bodies.0000.cdat');t.size=1000000000
        result=tar_header(t.tobuf(),0,2000000000)
        self.assertEqual(result['data_offset'],512);self.assertEqual(result['next_offset'],1000000512)
        self.assertTrue(result['history_candidate']);self.assertFalse(result['chain_records_verified'])
    def test_path_links_devices_sparse_and_extensions_rejected(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),
                          ('link',tarfile.SYMTYPE),('hard',tarfile.LNKTYPE),('device',tarfile.CHRTYPE),
                          ('sparse',tarfile.GNUTYPE_SPARSE),('pax',tarfile.XHDTYPE)]:
            t=tarfile.TarInfo(name);t.type=kind
            with self.assertRaises(ValueError):tar_header(t.tobuf(),0,10000)
    def test_truncation_checksum_overflow_and_alignment(self):
        t=tarfile.TarInfo('file');t.size=100
        raw=bytearray(t.tobuf());raw[1]^=1
        for data,offset,total in [(bytes(raw),0,10000),(t.tobuf()[:511],0,10000),
                                  (t.tobuf(),1,10000),(t.tobuf(),0,600)]:
            with self.assertRaises(ValueError):tar_header(data,offset,total)
        self.assertTrue(tar_header(bytes(512),0,1024)['end_marker'])

    def test_published_redirect_is_bounded_pinned_and_separately_reserved(self):
        first=Response(b'',status=301);first.headers['Location']='https://snapshot.arbitrum.foundation/arb1/a/full-path.tar.part0000'
        second=Response(b'abc',total=100)
        c=[Connection([first]),Connection([second])];reserved=[]
        h=HTTP(lambda *a,**k:c.pop(0))
        row=dict(url='https://snapshot.arbitrum.io/arb1/a/full-path.tar.part0000',bytes=100)
        raw,pin=h.read(row,0,3,before_redirect=lambda:reserved.append(1))
        self.assertEqual(raw,b'abc');self.assertEqual(h.requests,2);self.assertEqual(reserved,[1])
        self.assertEqual(pin['effective_url'],first.headers['Location'])

    def test_redirect_cannot_change_path_host_scheme_or_add_query(self):
        for target in ['http://snapshot.arbitrum.foundation/a','https://evil.test/a',
                       'https://snapshot.arbitrum.foundation/changed','https://snapshot.arbitrum.foundation/a?token=secret']:
            r=Response(b'',status=301);r.headers['Location']=target
            h=HTTP(lambda *a,**k:Connection([r]));row=dict(url='https://snapshot.arbitrum.io/a',bytes=100)
            with self.assertRaises(ValueError):h.read(row,0,3,before_redirect=lambda:None)
            self.assertEqual(h.requests,1)

    def test_reuse_previously_validated_redirect(self):
        response=Response(b'abc',total=100);hosts=[]
        def factory(host,**kw):hosts.append(host);return Connection([response])
        h=HTTP(factory);row=dict(url='https://snapshot.arbitrum.io/arb1/a',bytes=100)
        pin=dict(etag=response.headers.get('ETag'),bytes=100,effective_url='https://snapshot.arbitrum.foundation/arb1/a')
        raw,got=h.read(row,0,3,pin,reuse_redirect=True)
        self.assertEqual(raw,b'abc');self.assertEqual(got,pin);self.assertEqual(h.requests,1);self.assertEqual(hosts,['snapshot.arbitrum.foundation'])

    def test_reused_redirect_pin_is_still_validated(self):
        h=HTTP(lambda *a,**kw:self.fail('must not connect'));row=dict(url='https://snapshot.arbitrum.io/a',bytes=100)
        for target in ['https://evil.test/a','https://snapshot.arbitrum.foundation/else','https://snapshot.arbitrum.foundation/a?x=1']:
            with self.assertRaises(ValueError):h.read(row,0,3,dict(effective_url=target),reuse_redirect=True)


if __name__=='__main__':unittest.main()
