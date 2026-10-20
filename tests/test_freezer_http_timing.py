from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from economic_machine.arbitrum_research.freezer_http_timing import instrument


class TimingTests(unittest.TestCase):
    def connection(self):
        response=Mock(status=206,headers={'Content-Range':'unchanged'},will_close=False)
        response.read.return_value=b'abc';connection=Mock();connection.getresponse.return_value=response
        http=SimpleNamespace(factory=Mock(return_value=connection),connections={})
        stats=instrument(http);wrapped=http.factory('host',timeout=20)
        return connection,response,wrapped,stats
    def test_preserves_request_response_and_body_bound(self):
        c,r,w,s=self.connection()
        with patch('economic_machine.arbitrum_research.freezer_http_timing.time.monotonic',side_effect=[1,3,4,9]):
            w.request('GET','/path',headers={'Range':'bytes=0-2'});out=w.getresponse();self.assertEqual(out.read(4),b'abc')
        c.request.assert_called_once_with('GET','/path',headers={'Range':'bytes=0-2'});r.read.assert_called_once_with(4)
        self.assertEqual(s['request_headers_seconds'],2);self.assertEqual(s['body_read_seconds'],5)
        self.assertEqual(out.headers,r.headers);self.assertFalse(out.will_close);self.assertEqual(s['returned_body_bytes'],3)
    def test_body_exception_propagates_and_is_counted(self):
        c,r,w,s=self.connection();r.read.side_effect=TimeoutError('do not record credential-like detail')
        w.request('GET','/');out=w.getresponse()
        with self.assertRaises(TimeoutError):out.read(4)
        self.assertEqual(s['errors'],1);self.assertEqual(s['samples'][0]['error_type'],'TimeoutError')
        self.assertNotIn('credential',str(s))
    def test_headers_exception_propagates_without_retry(self):
        c,r,w,s=self.connection();c.getresponse.side_effect=OSError('secret')
        w.request('GET','/')
        with self.assertRaises(OSError):w.getresponse()
        self.assertEqual(c.getresponse.call_count,1);self.assertEqual(s['errors'],1);self.assertEqual(s['headers'],0)
    def test_request_exception_and_close_forwarded(self):
        c,r,w,s=self.connection();c.request.side_effect=OSError('secret')
        with self.assertRaises(OSError):w.request('GET','/')
        w.close();c.close.assert_called_once();self.assertEqual(s['errors'],1)
    def test_samples_bounded_but_totals_continue(self):
        c,r,w,s=self.connection()
        for _ in range(130):w.request('GET','/');w.getresponse().read(4)
        self.assertEqual(len(s['samples']),128);self.assertEqual(s['reads'],130);self.assertEqual(s['returned_body_bytes'],390)
