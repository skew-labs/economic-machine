"""Internal CLI and optional loopback-only HTTP wallet search. No signing."""
import argparse,json,time
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs
from economic_machine.arbitrum_research.freezer_search import register,wallet

def main():
 p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--register');p.add_argument('--wallet');p.add_argument('--chain',type=int,default=42161);p.add_argument('--port',type=int);a=p.parse_args()
 if sum(x is not None for x in (a.register,a.wallet,a.port))!=1:raise ValueError('SEARCH_MODE')
 if a.register:print(json.dumps(register(a.base,a.register)));return
 if a.wallet:print(json.dumps(wallet(a.base,a.chain,a.wallet)));return
 if not 1024<=a.port<=65535:raise ValueError('SEARCH_PORT')
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_GET(self):
   began=time.monotonic();u=urlsplit(self.path)
   try:
    if u.path!='/api/wallet' or len(self.path)>256:raise ValueError('SEARCH_ROUTE')
    args=parse_qs(u.query,strict_parsing=True)
    if set(args)!={'address','chain'} or any(len(v)!=1 for v in args.values()):raise ValueError('SEARCH_QUERY')
    result=wallet(a.base,int(args['chain'][0]),args['address'][0]);result['query_ms']=(time.monotonic()-began)*1000;code=200
   except (ValueError,KeyError):result={'error':'Invalid query or unavailable verified data'};code=400
   except Exception:result={'error':'Search temporarily unavailable'};code=503
   raw=json.dumps(result).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
 HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()

if __name__=='__main__':main()
