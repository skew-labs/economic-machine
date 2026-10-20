"""Loopback research endpoint. Use an SSH tunnel; no public unauthenticated API."""
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
from .store import canonical
from .reporting import summary
import json

def serve(store, port):
    if not 1024<=port<=65535: raise ValueError('PORT')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_GET(self):
            try:
                if len(self.path)>2048: raise ValueError('REQUEST_LENGTH')
                u=urlsplit(self.path);q=parse_qs(u.query)
                if u.path=='/status': result=summary(store)
                elif u.path=='/strategy':
                    path=store.root/'strategy-status.json'
                    result=json.loads(path.read_bytes()) if path.exists() else {'state':'NOT_STARTED','trading_enabled':False}
                elif u.path=='/search': result=store.search(q.get('q',[''])[0])
                elif u.path=='/trace': result=store.trace(q.get('wallet',[''])[0],int(q.get('depth',['3'])[0]),before=int(q.get('before',[str(2**62)])[0]))
                elif u.path=='/token': result=store.token(q.get('mint',[''])[0])
                else:self.send_error(404);return
                raw=canonical(result).encode();self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
            except (ValueError,KeyError): self.send_error(400)
    HTTPServer(('127.0.0.1',port),Handler).serve_forever()
