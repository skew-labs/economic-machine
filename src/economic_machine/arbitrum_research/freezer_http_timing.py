"""Observation-only HTTP stage timings; no request or provider policy changes."""
import time


def instrument(http):
    stats=dict(request_headers_seconds=0.0,body_read_seconds=0.0,headers=0,reads=0,
               returned_body_bytes=0,errors=0,samples=[],sample_limit=128,
               scope='client request through response headers, then bounded body read; not wire TTFB')

    class Response:
        def __init__(self,response,headers_seconds):
            self.response=response;self.headers_seconds=headers_seconds
        def __getattr__(self,name):return getattr(self.response,name)
        def read(self,*args,**kwargs):
            began=time.monotonic();n=0;error=None
            try:
                data=self.response.read(*args,**kwargs);n=len(data);return data
            except BaseException as exc:
                error=type(exc).__name__;stats['errors']+=1;raise
            finally:
                elapsed=time.monotonic()-began;stats['body_read_seconds']+=elapsed
                stats['reads']+=1;stats['returned_body_bytes']+=n
                if len(stats['samples'])<128:
                    stats['samples'].append(dict(headers_seconds=self.headers_seconds,body_seconds=elapsed,
                                                 returned_bytes=n,status=self.response.status,error_type=error))

    class Connection:
        def __init__(self,connection):self.connection=connection;self.began=None
        def __getattr__(self,name):return getattr(self.connection,name)
        def request(self,*args,**kwargs):
            self.began=time.monotonic()
            try:return self.connection.request(*args,**kwargs)
            except BaseException:
                stats['request_headers_seconds']+=time.monotonic()-self.began;stats['errors']+=1;self.began=None;raise
        def getresponse(self,*args,**kwargs):
            began=self.began if self.began is not None else time.monotonic()
            try:response=self.connection.getresponse(*args,**kwargs)
            except BaseException:
                stats['errors']+=1;raise
            finally:
                elapsed=time.monotonic()-began;stats['request_headers_seconds']+=elapsed;self.began=None
            stats['headers']+=1;return Response(response,elapsed)

    original=http.factory
    http.factory=lambda *args,**kwargs:Connection(original(*args,**kwargs))
    http.connections={key:Connection(value) for key,value in http.connections.items()}
    return stats
