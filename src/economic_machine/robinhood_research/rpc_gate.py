"""One endpoint schedule shared by history and crosschain RPC consumers."""
from contextlib import nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from economic_machine.arbitrum_research.source import SourceBackoff
from economic_machine.pump_research.store import atomic


class Gate:
    def __init__(self,root,endpoint,units,clock=time.time,sleep=time.sleep):
        if type(units) is not int or not 1<=units<=16:raise ValueError('RPC_GATE_UNITS')
        self.folder=Path(root)/hashlib.sha256(endpoint.encode()).hexdigest()
        self.folder.mkdir(parents=True,exist_ok=True)
        self.units=units;self.clock=clock;self.sleep=sleep;self.lock=None
    def __enter__(self):
        self.lock=(self.folder/'request.lock').open('a+b')
        fcntl.flock(self.lock,fcntl.LOCK_EX)
        try:
            path=self.folder/'state.json'
            self.state=json.loads(path.read_bytes()) if path.exists() else {}
            now=self.clock();until=self.state.get('retry_at',0)
            if until>now:raise SourceBackoff(until-now,-1)
            delay=max(0,self.state.get('next_at',0)-now)
            if delay>0:self.sleep(delay)
            self.started=self.clock();return self
        except BaseException:
            self.lock.close();self.lock=None;raise
    def __exit__(self,kind,error,traceback):
        try:
            now=self.clock();s=self.state;interval=s.get('seconds_per_rpc',.02)
            if isinstance(error,SourceBackoff) and error.status!=-1:
                interval=min(.25,max(.02,interval*2))
                s.update(retry_at=max(s.get('retry_at',0),now+error.retry_after_seconds),
                    success_streak=0,last_error_status=error.status)
            elif error is None:
                streak=s.get('success_streak',0)+1
                if streak>=100:interval=max(.02,interval*.8);streak=0
                s.update(success_streak=streak,retry_at=0)
            s.update(seconds_per_rpc=interval,next_at=max(now,self.started+self.units*interval),
                requests=s.get('requests',0)+1,rpc_units=s.get('rpc_units',0)+self.units,updated_at=now)
            atomic(self.folder/'state.json',s)
        finally:
            self.lock.close();self.lock=None


def request_gate(endpoint,units):
    root=os.environ.get('EM_RPC_GATE_ROOT')
    return Gate(root,endpoint,units) if root else nullcontext()
