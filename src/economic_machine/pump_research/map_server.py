"""Private loopback research desk. Scoped public-address queue; no trades."""
import argparse
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .address_index import search
from .research_workspace import Workspace

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--assets',required=True);p.add_argument('--port',type=int,default=8844);a=p.parse_args()
    base=Path(a.base);assets=Path(a.assets)
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def local_origin(self):
            host=self.headers.get('Host','')
            try:hostname=urlsplit('http://'+host).hostname
            except ValueError:return False
            if hostname not in ('127.0.0.1','localhost','[::1]','::1'):return False
            origin=self.headers.get('Origin')
            return (not origin or origin=='http://'+host) and self.headers.get('Sec-Fetch-Site','same-origin') not in ('cross-site','same-site')
        def json_response(self,value,status=200):
            raw=json.dumps(value,separators=(',',':')).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            if not self.local_origin() or not secrets.compare_digest(self.headers.get('X-Research-Token',''),token):
                self.json_response({'error':'Private research session required.'},403);return
            if urlsplit(self.path).path!='/api/investigations':self.send_error(404);return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.send_error(415);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=2048:raise ValueError('Request too large.')
                value=json.loads(self.rfile.read(size))
                if not isinstance(value,dict) or set(value)!={'address'}:raise ValueError('Enter one Solana address.')
                workspace=Workspace(base)
                try:result=workspace.submit(value['address'])
                finally:workspace.close()
                self.json_response({'investigation':result},202)
            except (ValueError,TypeError):self.json_response({'error':'Enter a valid Solana address. The request was not queued.'},400)
            except Exception:self.json_response({'error':'The research queue is temporarily unavailable.'},503)
        def do_GET(self):
            if not self.local_origin():self.send_error(403);return
            route=urlsplit(self.path).path
            if route=='/api/session':self.json_response({'csrf_token':token,'internal_only':True});return
            if route=='/api/workspace' or route.startswith('/api/investigations/'):
                workspace=None
                try:
                    workspace=Workspace(base)
                    value=workspace.snapshot() if route=='/api/workspace' else workspace.report(route.rsplit('/',1)[1])
                    self.json_response(value if value else {'error':'Investigation not found.'},200 if value else 404)
                except Exception:self.json_response({'error':'Research records are temporarily unavailable.'},503)
                finally:
                    if workspace:workspace.close()
                return
            if route.startswith('/data/'):route=route[5:]
            if route=='/api/address':
                try:
                    args=parse_qs(urlsplit(self.path).query,strict_parsing=True,max_num_fields=4)
                    if set(args)-{'address','before_slot','before_id'} or any(len(v)!=1 for v in args.values()):raise ValueError('arguments')
                    value=search(base/'evidence/wallet-map-v1/address-index.sqlite3',args['address'][0],
                        int(args.get('before_slot',['1000000001'])[0]),int(args.get('before_id',[str(2**63-1)])[0]))
                    raw=json.dumps(value,separators=(',',':')).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
                    self.send_header('Content-Length',str(len(raw)));self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
                except (ValueError,KeyError):self.send_error(400)
                except Exception:self.send_error(503)
                return
            paths={'/':(assets/'index.html','text/html; charset=utf-8'),'/map.js':(assets/'map.js','text/javascript; charset=utf-8'),
                '/map.css':(assets/'map.css','text/css; charset=utf-8'),
                '/layout.worker.js':(assets/'layout.worker.js','text/javascript; charset=utf-8'),
                '/assets/ticking-logo.svg':(assets/'assets/ticking-logo.svg','image/svg+xml'),
                '/assets/dm-sans-latin.woff2':(assets/'assets/dm-sans-latin.woff2','font/woff2'),
                '/map.json':(base/'evidence/wallet-map-v1/map.json','application/json'),
                '/status.json':(base/'evidence/wallet-map-v1/status.json','application/json'),
                '/quality.json':(base/'evidence/wallet-map-v1/quality.json','application/json'),
                '/custody.json':(base/'evidence/wallet-map-v1/custody.json','application/json'),
                '/crosschain.json':(base/'evidence/wallet-map-v1/crosschain.json','application/json'),
                '/core-status.json':(base/'evidence/core-trace-v1/status.json','application/json')}
            if route not in paths:self.send_error(404);return
            path,mime=paths[route]
            if not path.is_file():self.send_error(503);return
            if path.stat().st_size>32*1024**2:self.send_error(413);return
            raw=path.read_bytes();self.send_response(200)
            self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store' if route.endswith('.json') else 'no-cache')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; worker-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(raw)
    ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()

if __name__=='__main__':main()
