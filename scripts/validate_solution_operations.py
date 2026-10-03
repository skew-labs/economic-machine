"""Combine distinct focused validations; do not mislabel them as a final full-suite rerun."""
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote evidence only')
    sys.path.insert(0,str(ROOT/'tests'));sys.path.insert(0,str(ROOT/'scripts'))
    suite=unittest.TestLoader().loadTestsFromNames(['test_solution','test_solution_operations','test_release_proof',
        'test_mining.MiningWorkspace.test_owner_isolation_and_api_boundary',
        'test_mining.MiningWorkspace.test_hosted_agent_key_cannot_freeze_solve_or_read_owner_mining_jobs'])
    def names(group):
        for item in group:
            if isinstance(item,unittest.TestSuite):yield from names(item)
            else:yield item.id()
    expected=set(names(suite));observed={};logs=[]
    folder=ROOT/'artifacts/solution-operations'
    for name in ['tests-initial.log','tests-followup.log','tests-lifecycle-initial.log','tests-lifecycle.log','tests-journal.log','tests-native-sandbox.log','tests-backend.log']:
        text=(folder/name).read_text();logs.append({'path':'artifacts/solution-operations/'+name,'sha256':hashlib.sha256(text.encode()).hexdigest()})
        for identifier,status in re.findall(r'^test_\w+ \(([^)]+)\) \.\.\. (ok|FAIL|ERROR)$',text,re.MULTILINE):
            observed[identifier]=status
    missing=sorted(identifier for identifier in expected if observed.get(identifier)!='ok')
    if missing:raise RuntimeError('Unvalidated current cases: '+','.join(missing))
    build=json.loads((folder/'native-build.json').read_text())
    if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=sha for p,sha in build['sources'].items()):
        raise RuntimeError('Native source changed after checks')
    compiler=json.loads((ROOT/'artifacts/solution/contract-build.json').read_text())
    if compiler['warnings'] or any(hashlib.sha256((ROOT/'contracts'/p).read_bytes()).hexdigest()!=sha for p,sha in compiler['sources'].items()):
        raise RuntimeError('Compiled contract source mismatch')
    sources=['src/machine_engine/solution_chain.py','src/machine_engine/solution_operations.py','src/machine_engine/solution.py',
        'src/machine_engine/solution_client.py','scripts/solution_operator.py','tests/test_solution.py','tests/test_solution_operations.py']
    report={'scope':'DISTINCT_FOCUSED_RUNS_WITH_RETAINED_FAILURES_NOT_FINAL_FULL_SUITE',
        'accepted':True,'distinct_current_cases_passed':len(expected),'current_cases':sorted(expected),'logs':logs,
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
        'native_source_and_compiled_contract_match':True,'thread_sanitizer':build['thread_sanitizer'],
        'live_issuance':False,'public_mining_deployment':None,'public_vrf_verified':False}
    (folder/'validation.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k not in {'current_cases','logs','source_sha256'}}))


if __name__=='__main__':main()
