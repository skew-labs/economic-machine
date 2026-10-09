"""Invariant used by persistent-risk tests; no historical operator runner."""
import struct
from wire import *

def check_cache(data):
    tv=struct.unpack_from('<Q',data,168)[0]
    for s in range(SEATS):
        a=SEAT+s*256
        if struct.unpack_from('<Q',data,a+200)[0]!=0x32524d5041434801:continue
        qty=[0,0];notional=[0,0];prices=[[],[]];fees=0
        for k in range(16):
            o=NODE+(s*16+k)*48;x=struct.unpack_from('<Q',data,o)[0];p=struct.unpack_from('<I',data,o+16)[0]
            if not x:continue
            side=k//8;qty[side]+=x;notional[side]+=x*p;prices[side].append(p);fees+=(x*p*tv+1999)//2000
        actual=struct.unpack_from('<IIQQQ',data,a+208)
        assert actual==(*qty,*notional,fees),(s,actual,qty,notional,fees)
        bounds=struct.unpack_from('<IIII',data,a+240)
        for side in (0,1):
            if prices[side]:assert bounds[side*2]<=min(prices[side])<=max(prices[side])<=bounds[side*2+1]
