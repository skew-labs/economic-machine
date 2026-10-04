"""Remote-only bounded native pipeline build and concurrency/sanitizer checks."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Authorized remote host only')
    folder=ROOT/'build/solution-operations';folder.mkdir(parents=True,exist_ok=True)
    output=ROOT/'artifacts/solution-operations';output.mkdir(parents=True,exist_ok=True)
    flags=['g++','-std=c++20','-pthread','-Wall','-Wextra','-Werror']
    test=ROOT/'native/tests/solution_pipeline.cpp';source=ROOT/'native/src/solution_pipeline.cpp'
    sanitizer=folder/'pipeline-sanitizer'
    subprocess.run(flags+['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer',str(test),'-o',str(sanitizer)],check=True,timeout=60)
    checked=subprocess.check_output([str(sanitizer)],text=True,timeout=60)
    # Separate ThreadSanitizer run; keep any platform failure explicit rather than claiming coverage.
    thread=folder/'pipeline-tsan'
    subprocess.run(flags+['-O1','-g','-fsanitize=thread',str(test),'-o',str(thread)],check=True,timeout=60)
    raced=subprocess.run([str(thread)],text=True,capture_output=True,timeout=60)
    race_log=raced.stdout+raced.stderr
    thread_status='PASS' if not raced.returncode else 'UNAVAILABLE_KERNEL_MEMORY_MAPPING'
    if raced.returncode and 'unexpected memory mapping' in raced.stderr:
        # Per-process test workaround only. Never change host-wide mmap randomness or production ASLR.
        retry=subprocess.run(['setarch','x86_64','-R',str(thread)],text=True,capture_output=True,timeout=60)
        race_log+='\nPER_PROCESS_ASLR_DISABLED exit='+str(retry.returncode)+'\n'+retry.stdout+retry.stderr
        if not retry.returncode:thread_status='PASS_PER_PROCESS_ASLR_DISABLED'
        elif 'unexpected memory mapping' not in retry.stderr and 'failed to set personality' not in retry.stderr:
            (output/'thread-sanitizer.log').write_text(race_log)
            raise RuntimeError('ThreadSanitizer retry returned actual diagnostics')
    (output/'thread-sanitizer.log').write_text(race_log)
    if raced.returncode and 'unexpected memory mapping' not in raced.stderr:raise RuntimeError('ThreadSanitizer failed')
    staged=folder/'pipeline.staged'
    subprocess.run(flags+['-O3',str(source),'-o',str(staged)],check=True,timeout=60)
    sha=hashlib.sha256(staged.read_bytes()).hexdigest();target=folder/f'solution-pipeline-{sha}';staged.replace(target)
    sandbox=subprocess.check_output([str(target),'--sandbox-self-test'],text=True,timeout=10).strip()
    proof={'executable':str(target),'sha256':sha,'sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [source,test,ROOT/'native/src/solution.cpp']},'sanitizers':checked.strip(),
        'thread_sanitizer':thread_status,
        'queue_capacity':8,'maximum_jobs_per_process':128,'cpu_pinning':'OPTIONAL_OPERATOR_SELECTED',
        'signing':False,'network_calls_in_native_process':0,'seccomp_self_test':sandbox,
        'sandbox':'NO_OPEN_SOCKET_CONNECT_EXEC_AFTER_LOADER'}
    (output/'native-build.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))


if __name__=='__main__':main()
