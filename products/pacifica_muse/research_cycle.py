#!/usr/bin/env python3
"""Generate and evaluate read-only Muse signal graphs; never install a policy."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from economic_machine.muse_learning import Graph, ResearchBudget, baseline, compare, grammar, proposal, research_request, validate_available
from economic_machine.muse_maker import digest, unique
from economic_machine.muse_policy_worker import atomic, now_ms, provider_call, read_json


def inputs(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        lease=json.loads(db.execute('SELECT body FROM binding WHERE id=1').fetchone()[0])
        report=json.loads(db.execute('SELECT body FROM snapshot_report WHERE id=1').fetchone()[0])
        frames=[dict(json.loads(body),id=ident) for ident,body in db.execute('SELECT id,body FROM frames ORDER BY id')]
        if report['session_id']!=lease['session_id']:raise ValueError('snapshot scope')
        return lease,report,frames
    finally:db.close()


def previous(root):
    parent=baseline();feedback={};last=0
    for path in sorted(root.glob('candidate-*.json'),key=lambda p:int(p.stem.split('-')[1])):
        candidate=read_json(path);ident=candidate['generation'];last=ident
        result_path=root/f'evaluation-{ident}.json'
        if not result_path.exists():raise ValueError('evaluate the frozen candidate before generating another')
        feedback=read_json(result_path)
        if feedback['improved_forecast']:parent=candidate['proposal']['program']
    return parent,feedback,last


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase',choices=['propose','admit-recorded','evaluate'])
    p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--library',type=Path,required=True);p.add_argument('--provider-key-file',type=Path)
    args=p.parse_args();os.umask(0o077)
    root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
    if (root/'session-start.json').exists() or (root/'control').exists():raise ValueError('research root cannot be a trading runtime')
    lock=os.open(root/'research.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    lease,own,frames=inputs(args.snapshot)
    binding={k:lease[k] for k in ('session_id','start_ms','deadline_ms')}
    if (root/'binding.json').exists():
        if read_json(root/'binding.json')!=binding:raise ValueError('research session changed')
    else:atomic(root/'binding.json',binding)
    budget=ResearchBudget(root/'model.sqlite','research:'+lease['session_id'],lease['deadline_ms'],'0.02',2)
    try:
        if args.phase in ('propose','admit-recorded'):
            if now_ms()>=lease['deadline_ms']:raise ValueError('original session deadline reached')
            if len(frames)<16 or not 0<=now_ms()-frames[-1]['source_ms']<=10000:raise ValueError('fresh public research frames required')
            parent,feedback,last=previous(root)
            recorded=args.phase=='admit-recorded'
            validation_frames,validation_own=frames,own
            if recorded:
                row=budget.db.execute("SELECT id,request_hash FROM calls WHERE status='rejected' ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:raise ValueError('no rejected recorded response')
                ident=row[0]
                if (root/f'candidate-{ident}.json').exists():raise ValueError('recorded candidate already frozen')
                req=read_json(root/f'request-{ident}.json')
                if digest(req)!=row[1]:raise ValueError('recorded request changed')
                original=json.loads(req['messages'][1]['content']);validation_frames=original['public_observations']
                validation_own={'own_fill_learning_ready':original['own_fill_evidence']['ready']}
                raw=(root/f'response-{ident}.json').read_text();completed=now_ms()
                if original['parent_program']!=parent:raise ValueError('recorded parent changed')
            else:
                if args.provider_key_file is None:raise ValueError('separate provider credential path required')
                req=research_request(parent,frames,feedback,own);ident=budget.reserve(req,now_ms())
                atomic(root/f'request-{ident}.json',req)
                raw=provider_call(req,args.provider_key_file);completed=now_ms()
                atomic(root/f'response-{ident}.json',json.loads(raw,object_pairs_hook=unique))
            try:
                result=validate_available(proposal(raw,lower_flags=True),validation_frames,validation_own)
                if grammar().structure(parent)==grammar().structure(result['program']):raise ValueError('graph wiring unchanged')
                with Graph(result['program'],args.library) as graph:graph.evaluate(frames[-1]['features'])
            except (ValueError,TypeError,KeyError) as exc:
                if not recorded:
                    budget.finish(ident,raw,False)
                    atomic(root/f'rejected-{ident}.json',{'generation':ident,'error':str(exc)[:160],'execution_authority':False})
                raise
            if not recorded:budget.finish(ident,raw,True)
            candidate={'generation':ident,'previous_evaluated_generation':last,'session_id':lease['session_id'],
                       'completed_ms':completed,'cutoff_id':frames[-1]['id'],'parent':parent,'proposal':result,
                       'program_hash':digest(result['program']),'structure_hash':grammar().structure(result['program']),
                       'input_snapshot_hash':digest(frames),'execution_authority':False,'live_policy_writes':0,
                       'admitted_from_recorded_response':recorded,'response_semantic_hash':digest(json.loads(raw))}
            atomic(root/f'candidate-{ident}.json',candidate)
            output={'state':'FROZEN_RESEARCH_CANDIDATE','generation':ident,'analysis':result['analysis'],
                    'program_hash':candidate['program_hash'],'structurally_new':True,'budget':budget.totals(),'execution_authority':False}
        else:
            paths=sorted(root.glob('candidate-*.json'),key=lambda p:int(p.stem.split('-')[1]))
            if not paths:raise ValueError('no frozen research candidate')
            candidate=read_json(paths[-1]);ident=candidate['generation'];out_path=root/f'evaluation-{ident}.json'
            if out_path.exists():
                output=read_json(out_path)
            else:
                if digest(candidate['proposal']['program'])!=candidate['program_hash']:raise ValueError('candidate changed')
                prefix=[f for f in frames if f['id']<=candidate['cutoff_id']]
                if digest(prefix)!=candidate['input_snapshot_hash']:raise ValueError('historical input snapshot changed')
                future=[f for f in frames if f['id']>candidate['cutoff_id'] and f['source_ms']>candidate['completed_ms']+2000]
                # A fixed 60-second window anchored to proposal completion.
                end=candidate['completed_ms']+62000
                if not frames or frames[-1]['source_ms']<end:raise ValueError('fixed future window still pending')
                future=[f for f in future if f['source_ms']<=end]
                result=compare(candidate['parent'],candidate['proposal']['program'],future,
                               completed_ms=candidate['completed_ms'],cutoff_id=candidate['cutoff_id'],library=args.library)
                output=dict(result,generation=ident,program_hash=candidate['program_hash'],evaluation_end_ms=end,
                            frame_count=len(future),attributed_fills=own['attributed_fills'],
                            own_fill_learning_ready=own['own_fill_learning_ready'],budget=budget.totals())
                atomic(out_path,output)
        atomic(root/'status.json',output);print(json.dumps(output,sort_keys=True))
    finally:budget.db.close();os.close(lock)


if __name__=='__main__':main()
