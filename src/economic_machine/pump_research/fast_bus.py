"""Local, bounded, nonblocking native signal handoff; no order messages exist."""
from collections import Counter
import os
from pathlib import Path
import socket
import struct
from .fast_native import RESULT_FIELDS,REASONS
from .store import decode

# 232-byte little-endian packet. Result starts at byte 32 and is ABI v1's
# 136-byte struct. Flag 1 = provisional research, never execution authority.
PACKET=struct.Struct('<4sIIIQQ5Qq11Q32s32s')
def packet(result,kind,now_ms,valid_until_ms):
    if kind not in (1,2) or result['execution_authority'] or result['history_complete']:
        raise ValueError('SIGNAL_BUS_SCOPE')
    if not now_ms<valid_until_ms<=now_ms+1000:raise ValueError('SIGNAL_BUS_TTL')
    values=[REASONS.index(result[k]) if k=='reason' else result[k] for k in RESULT_FIELDS]
    mint=decode(result['mint']);digest=bytes.fromhex(result['source_reference']['raw_sha256'])
    if len(mint)!=32 or len(digest)!=32:raise ValueError('SIGNAL_BUS_IDENTITY')
    return PACKET.pack(b'EMPS',1,kind,1,now_ms,valid_until_ms,*values,mint,digest)

def unpack(raw,now_ms):
    if len(raw)!=PACKET.size:raise ValueError('SIGNAL_BUS_SIZE')
    magic,version,kind,flags,created,expires,*rest=PACKET.unpack(raw)
    if magic!=b'EMPS' or version!=1 or kind not in (1,2) or flags!=1:raise ValueError('SIGNAL_BUS_HEADER')
    if not created<=now_ms<expires<=created+1000:raise ValueError('SIGNAL_BUS_EXPIRED')
    r=dict(zip(RESULT_FIELDS,rest[:17]))
    if r['execution_authority'] or r['history_complete'] or r['reason']>=len(REASONS):raise ValueError('SIGNAL_BUS_AUTHORITY')
    r.update(kind=kind,created_ms=created,expires_ms=expires,mint_bytes=rest[17],raw_sha256=rest[18].hex())
    return r

class Bus:
    def __init__(self,path):
        self.path=Path(path);self.clients=[];self.stats=Counter()
        self.server=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET|socket.SOCK_NONBLOCK)
        # Existing paths are never silently unlinked; systemd owns the runtime
        # directory lifecycle and a second producer must fail.
        self.server.bind(str(self.path));os.chmod(self.path,0o600);self.server.listen(4)
    def accept(self):
        while True:
            try:c,_=self.server.accept()
            except BlockingIOError:return
            _,uid,_=struct.unpack('3i',c.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.geteuid() or len(self.clients)>=4:c.close();self.stats['rejected_subscribers']+=1;continue
            c.setblocking(False);c.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,16384)
            self.clients.append(c);self.stats['subscribers_accepted']+=1
    def publish(self,result,kind,now_ms,valid_until_ms):
        data=packet(result,kind,now_ms,valid_until_ms);self.stats['messages']+=1
        if not self.clients:self.stats['messages_without_subscriber']+=1
        for client in self.clients[:]:
            try:
                if client.send(data,socket.MSG_DONTWAIT|socket.MSG_NOSIGNAL)!=len(data):raise OSError('short seqpacket')
                self.stats['delivered_packets']+=1
            except OSError:
                self.clients.remove(client);client.close();self.stats['backpressure_or_disconnected']+=1
    def close(self):
        for c in self.clients:c.close()
        self.server.close();self.path.unlink(missing_ok=True)
