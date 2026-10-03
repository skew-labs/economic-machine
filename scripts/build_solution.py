"""Focused remote build of fixed-buffer Max-Cut workers and sanitizer evidence."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Build on the authorized remote host')
    folder=ROOT/'build/solution';folder.mkdir(parents=True,exist_ok=True)
    source=ROOT/'native/src/solution.cpp';test=ROOT/'native/tests/solution.cpp'
    flags=['g++','-std=c++20','-Wall','-Wextra','-Werror']
    sanitizer=folder/'solution-sanitizer'
    subprocess.run(flags+['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer',str(test),'-o',str(sanitizer)],check=True,timeout=60)
    checked=subprocess.check_output([str(sanitizer)],text=True,timeout=30)
    output=folder/'solution.staged.so';subprocess.run(flags+['-O3','-fPIC','-shared',str(source),'-o',str(output)],check=True,timeout=60)
    sha=hashlib.sha256(output.read_bytes()).hexdigest();target=folder/f'libsolution-{sha}.so';output.replace(target)
    out=ROOT/'artifacts/solution';out.mkdir(parents=True,exist_ok=True)
    proof={'abi':1,'library_path':str(target),'library_sha256':sha,'sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [source,test]},
           'sanitizers':'ADDRESS_UNDEFINED','sanitizer_result':checked.strip(),'authority':'NONE','graph_randomness':'SUPPLIED_RESEARCH_GRAPH_NOT_VRF'}
    (out/'native-build.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))


if __name__=='__main__':main()
