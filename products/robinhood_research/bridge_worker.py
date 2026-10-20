"""Independent chain backoffs keep one unavailable source from stalling others."""
import argparse
import fcntl
import json
from pathlib import Path
import signal
import time
import urllib.error
from economic_machine.arbitrum_research.storage import Storage
from economic_machine.pump_research.store import atomic,canonical
from economic_machine.robinhood_research.authority import authority
from economic_machine.robinhood_research.bridge import Bridge
from economic_machine.robinhood_research.source import SourceBackoff,SourceHTTPError

p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--shared-base',required=True);a=p.parse_args()
base=Path(a.base);authority(base,a.shared_base)
lock=(base/'evidence/crosschain/worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
stop=False

def end(*_):
 global stop
 stop=True
for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,end)
storage=Storage(base,a.shared_base,chain_id=4663,namespace='crosschain');bridge=Bridge(base,storage)
try:
 # Preserve the prior inline worker's cooldown across this split.
 parent=base/'evidence/runtime.sqlite3'
 import sqlite3
 with sqlite3.connect(f'file:{parent}?mode=ro',uri=True) as db:prior=json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
 inherited=prior.get('bridge_not_before',0)
 while not stop:
  row=bridge.db.execute("SELECT value FROM state WHERE key='round'").fetchone();number=int(row[0]) if row else 0
  chain=(1,42161,4663)[number%3];key='backoff:'+str(chain)
  cooldown=bridge.db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
  until=float(cooldown[0]) if cooldown else inherited
  now=time.time()
  try:
   if storage.cloud.spend.totals()['remaining_krw']<=0:raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
   storage.cloud.guard_local(64*1024**2)
   if now>=until:
    pending=bridge.db.execute("SELECT count(*) FROM jobs WHERE chain=? AND state='pending'",(chain,)).fetchone()[0]
    # Drain retained receipt work before admitting more sparse query results.
    if pending<24:bridge.scan(chain,history=(number//3)%2==1)
    bridge.process(12,only_chain=chain)
    atomic(base/'evidence/crosschain/network-status.json',dict(at_ms=time.time_ns()//1000000,state='RUNNING',chain_id=chain))
  except (SourceBackoff,SourceHTTPError,urllib.error.URLError,TimeoutError) as e:
   until=time.time()+max(60,getattr(e,'retry_after_seconds',300))
   with bridge.db:bridge.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,str(until)))
   atomic(base/'evidence/crosschain/network-status.json',dict(at_ms=time.time_ns()//1000000,state='SOURCE_BACKOFF',chain_id=chain,
    error_type=type(e).__name__,http_status=getattr(e,'status',None),retry_at=until))
  finally:
   with bridge.db:bridge.db.execute("INSERT OR REPLACE INTO state VALUES('round',?)",(str(number+1),))
   bridge.publish()
  backlog=bridge.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0]
  deadline=time.monotonic()+(2 if backlog else 20)
  while not stop and time.monotonic()<deadline:time.sleep(1)
finally:bridge.close();storage.close();lock.close()
