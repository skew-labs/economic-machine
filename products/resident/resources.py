"""Read-only host/cgroup evidence; contains no credential or environment dump."""
import argparse
import json
import platform
import subprocess
import time
from pathlib import Path

ap=argparse.ArgumentParser()
ap.add_argument('--out',required=True); ap.add_argument('--label',required=True)
args=ap.parse_args()
cg=Path('/sys/fs/cgroup/resident100.slice')
evidence={'label':args.label,'at_ns':time.time_ns(),'machine':platform.machine(),
          'kernel':platform.release(),'boundary':'shared Canada host cgroup, not rented VPS'}
evidence['cgroup']={name:(cg/name).read_text().strip() for name in (
    'memory.current','memory.peak','memory.max','memory.swap.max','memory.events','cpu.max','cpu.stat')}
evidence['processes']={}
for name in ('skew-resident-model.service','skew-resident-agents.service'):
    props=subprocess.check_output(['systemctl','show',name,'-p','MainPID','-p','ActiveState','-p','SubState']).decode()
    fields=dict(line.split('=',1) for line in props.splitlines())
    pid=int(fields['MainPID'])
    if pid:
        fields['memory']={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in
                          Path(f'/proc/{pid}/status').read_text().splitlines() if line.startswith(('VmRSS:','VmHWM:','Threads:'))}
    evidence['processes'][name]=fields
Path(args.out).write_text(json.dumps(evidence,indent=2))
print(json.dumps(evidence))
