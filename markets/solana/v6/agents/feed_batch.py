"""Shared per-frame facts, derived from the exact same account generation."""
import ctypes as C
from pathlib import Path
from wire import SEATS
class Input(C.Structure):
    _fields_=[(n,C.c_int64 if n in ('position','imbalance') else C.c_uint64) for n in ('now','observed','slot','oracle_slot','mark','sequence','position','bid','ask','imbalance','movement','depth')]
INPUT_BYTES=C.sizeof(Input)
class Batch:
    def __init__(self):
        self.lib=C.CDLL(str(Path(__file__).resolve().parents[1]/'build/libmachine_program.so'))
        self.lib.mp_batch.argtypes=[C.c_char_p]+[C.c_uint64]*5+[C.POINTER(Input),C.c_uint64];self.lib.mp_batch.restype=C.c_int
    def prepare(self,data,slot,received,now,wall):
        values=(Input*SEATS)();code=self.lib.mp_batch(data,len(data),slot,received,now,wall,values,SEATS)
        if code:raise RuntimeError('market batch rejected: '+str(code))
        return bytes(values)
