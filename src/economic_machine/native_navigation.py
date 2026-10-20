"""Hash-pinned, fixed ABI binding to the Ticking C++ product adapter."""
import ctypes, hashlib, json, unicodedata
from pathlib import Path
P=Path(__file__).parent
manifest=json.loads((P/'native-manifest.json').read_text())
for name in ('libticking_navigation.so','commands.json'):
 if hashlib.sha256((P/name).read_bytes()).hexdigest()!=manifest['sha256'][name]:
  raise RuntimeError('Economic Machine native identity mismatch')
commands=json.loads((P/'commands.json').read_text())['commands']
class Plan(ctypes.Structure):
 _fields_=[('abi',ctypes.c_uint32),('count',ctypes.c_uint32),('ids',ctypes.c_uint32*4)]
lib=ctypes.CDLL(str(P/'libticking_navigation.so'))
lib.em_ticking_resolve.argtypes=[ctypes.c_char_p,ctypes.c_size_t,ctypes.c_int,ctypes.POINTER(Plan)]
lib.em_ticking_resolve.restype=ctypes.c_int
lib.em_ticking_abi.restype=ctypes.c_uint32
lib.em_ticking_capabilities.restype=ctypes.c_uint32
if lib.em_ticking_abi()!=1 or lib.em_ticking_capabilities()!=len(commands):raise RuntimeError('Native registry ABI mismatch')
def resolve(query,chart=False):
 q=unicodedata.normalize('NFKC',query).encode('utf-8');out=Plan()
 status=lib.em_ticking_resolve(q,len(q),int(bool(chart)),ctypes.byref(out))
 if status<0:raise ValueError('Invalid native request')
 if status==0:return None
 if out.abi!=1 or not 1<=out.count<=4 or any(x>=len(commands) for x in out.ids[:out.count]):raise ValueError('Invalid native plan')
 selected=[commands[i] for i in out.ids[:out.count]]
 if all(c['kind']=='chart' for c in selected):
  actions=[a for c in selected for a in c['actions']]
  if len(actions)>4:return None
  return {'state':'chart_actions','actions':actions}
 if len(selected)!=1:raise ValueError('Mixed native plan')
 return selected[0]['result'] | ({'catalogAsset':selected[0]['asset']} if 'asset' in selected[0] else {})

class Position(ctypes.Structure):
 _fields_=[('abi',ctypes.c_uint32),('asset',ctypes.c_uint32),('interval',ctypes.c_uint32),('side',ctypes.c_uint32),('style',ctypes.c_uint32)]
lib.em_ticking_position.argtypes=[ctypes.c_char_p,ctypes.c_size_t,ctypes.POINTER(Position)]
lib.em_ticking_position.restype=ctypes.c_int
def position(query):
 q=unicodedata.normalize('NFKC',query).encode('utf-8');out=Position()
 status=lib.em_ticking_position(q,len(q),ctypes.byref(out))
 if status<0:raise ValueError('Invalid native request')
 if status==0:return None
 if out.abi!=1 or out.side>2 or out.style>3:raise ValueError('Invalid position workflow')
 def command(i,kind):
  if i==0xffffffff:return None
  if i>=len(commands) or not commands[i]['id'].startswith(kind):raise ValueError('Invalid workflow command')
  return commands[i]
 asset=command(out.asset,'navigate:chart:');interval=command(out.interval,'interval:')
 return {'asset':asset['asset'] if asset else None,'interval':interval['actions'][0]['value'] if interval else None,'side':[None,'long','short'][out.side],'style':[None,'scalping','intraday','swing'][out.style]}
