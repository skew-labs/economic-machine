"""Bounded equal-work comparison. No fabricated token or success-rate advantage."""
import json,statistics,tempfile,time
from pathlib import Path
from machine_engine.workspace import Workspace
from machine_engine.task_results import LocalWork
from machine_engine.task_checkout import clean_csv
from economic_machine.values import digest
ROOT=Path(__file__).resolve().parents[1]

def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote measurements required')
    source={'csv':'name,email\n Alice , alice@example.test \n Bob , bob@example.test \n'}
    direct=[];managed=[];equal=0
    with tempfile.TemporaryDirectory() as temp:
        work=Workspace(Path(temp)/'work.db');runner=LocalWork(work)
        for index in range(20):
            start=time.perf_counter_ns();a=clean_csv(source,['name','email'],'csv');direct.append((time.perf_counter_ns()-start)/1e6)
            start=time.perf_counter_ns()
            t=work.tasks.create({'request_id':f'compare-{index}','kind':'data_cleanup','title':'Contact import','instructions':'Trim cells and keep rows.','budget':{'currency':'USD','maximum':'0'},'deadline_at':None,'constraints':{'required_fields':['name','email'],'output_format':'csv'},'preference_id':None,'connection_ids':[]})
            p=runner.plans(t['id'],{'request_id':f'plans-{index}','expected_revision':1,'input':source})
            runner.run(p['id'],{'plan_hash':p['plans'][0]['hash']});b=runner.result(p['id'])
            managed.append((time.perf_counter_ns()-start)/1e6)
            equal+=a['content']==b['content']
    result={'schema':'equal-work-measurement-1','environment':'authorized Canadian host, isolated DB',
        'cases':20,'matching_results':equal,'input_hash':digest(source),'model_calls':{'direct':0,'managed':0},
        'external_payment_count':0,'direct_ms_median':statistics.median(direct),'managed_ms_median':statistics.median(managed),
        'managed_ms_max':max(managed),'direct_successes':20,'managed_successes':equal,
        'scope':'Direct Python transformation vs typed task + two proposals + approval + durable delivery. No human elapsed time or paid provider included.',
        'conclusion':'No demonstrated token savings or success-rate improvement. Managed path adds durable owner review, replay and resumption; its runtime overhead is measured.'}
    (ROOT/'evidence/task-comparison.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':main()
