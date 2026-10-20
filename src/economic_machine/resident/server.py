"""Loopback delegation API. Access remotely through an authenticated SSH tunnel.

The bearer authorizes reading and delegating only within pre-installed grants;
there is no grant-edit, shell, wallet, webhook or paid-model endpoint.
"""
import argparse
import fcntl
import hmac
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import Store, Rejected, encode
from .worker import Worker, Gate, LocalModel


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--state',required=True)
    ap.add_argument('--native',required=True)
    ap.add_argument('--native-sha256',required=True)
    ap.add_argument('--port',type=int,default=18433)
    args=ap.parse_args()
    os.umask(0o077)
    root=Path(args.state)
    lock=(root/'daemon.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    token=(root/'parent.token').read_text().strip()
    if len(token)<32:
        raise ValueError('parent token must have at least 32 characters')
    store=Store(root/'runtime.sqlite')
    store.recover()
    worker=Worker(store,LocalModel(token_file=root/'model.token'),Gate(args.native,args.native_sha256))
    halt=threading.Event()
    fast_wake=threading.Event()
    model_wake=threading.Event()
    def stop(*_):
        halt.set(); fast_wake.set(); model_wake.set()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,stop)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self,*_):
            pass

        def respond(self,code,body):
            raw=encode(body).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
                self.respond(401,{'error':'unauthorized'})
                return False
            return True

        def do_GET(self):
            if not self.allowed(): return
            path=urlparse(self.path)
            try:
                if path.path=='/v1/status':
                    return self.respond(200,store.status())
                if path.path=='/v1/results':
                    q=parse_qs(path.query)
                    after=int(q.get('after',['0'])[0])
                    if after<0: raise Rejected('cursor')
                    return self.respond(200,store.results(q.get('agent',[''])[0],after))
                self.respond(404,{'error':'not found'})
            except (ValueError,KeyError):
                self.respond(400,{'error':'invalid query'})

        def do_POST(self):
            if not self.allowed(): return
            if self.path!='/v1/delegate':
                return self.respond(404,{'error':'not found'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 1<=size<=4096 or self.headers.get('Transfer-Encoding'):
                    raise Rejected('request size')
                raw=self.rfile.read(size)
                task=json.loads(raw)
                job=store.submit(task)
                (model_wake if task['kind']=='triage' else fast_wake).set()
                self.respond(202,{'job':job,'status':'accepted','execution_authority':False})
            except (ValueError,KeyError,TypeError):
                self.respond(400,{'error':'task rejected; check grant, deadline, idempotency and capacity'})

    http=HTTPServer(('127.0.0.1',args.port),Handler)
    http.timeout=0.25
    def serve():
        while not halt.is_set():
            http.handle_request()
    thread=threading.Thread(target=serve,daemon=True)
    thread.start()
    # Native signals and local tools do not queue behind a model response.
    def fast_lane():
        while not halt.is_set():
            fast_wake.clear()
            while not halt.is_set() and worker.step('fast'):
                pass
            fast_wake.wait(1)
    fast=threading.Thread(target=fast_lane,daemon=True)
    fast.start()
    while not halt.is_set():
        model_wake.clear()
        while not halt.is_set() and worker.step('model'):
            pass
        model_wake.wait(1)
    thread.join(timeout=4)
    fast.join(timeout=4)
    http.server_close()


if __name__=='__main__':
    main()
