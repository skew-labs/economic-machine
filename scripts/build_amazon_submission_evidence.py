"""Remote evidence index. Distinguishes reproducible implementation from live admission."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote evidence required')
    directory=ROOT/'evidence'
    public=json.loads((directory/'pr16-public.json').read_text())
    source=json.loads((directory/'pr16-source-readback.json').read_text())
    quote=json.loads((directory/'mainnet-launch-review-final.json').read_text())
    assert public['public_workflow']=='DELIVERED' and not source['mismatches']
    names=['pr16-public.json','pr16-source-readback.json','pr16-history-regression.json',
           'task-comparison.json','amazon-workflow.mp4','amazon-video-metadata.json','mainnet-launch-review-final.json']
    report={'schema':'skew-submission-evidence-1','source_base_commit':source['source_commit_base'],
        'review_prs':[12,13,14,15,16],'implemented':['SKEW artifact reward contract','mainnet unsigned launch review','task payment recovery','MCP 2025-11-25','Bedrock and Strands bounded provider','owner-reviewed resumable work'],
        'public_workflow':'DELIVERED','source_files_matched':source['matched_files'],
        'model_provider':public['provider'],'actual_bedrock_response':False,
        'mainnet':{'status':quote['status'],'deployed':False,'broadcasts':0,'maximum_gas_wei':quote['maximum_gas_wei'],'review_expires_at':quote['expires_at']},
        'paypal_sandbox_capture_verified':False,'independent_contract_audit':False,
        'amazon_submission':'NOT_SUBMITTED','submission_receipt':None,'youtube_or_vimeo_url':None,
        'blockers':['AWS organization permission for bedrock:CallWithBearerToken','Owner wallet approval for mainnet deployment','PayPal sandbox merchant/buyer provisioning for paid demo','Public video upload and contest form submission'],
        'sha256':{n:hashlib.sha256((directory/n).read_bytes()).hexdigest() for n in names}}
    (directory/'submission-evidence.json').write_text(json.dumps(report,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='sha256'}))

if __name__=='__main__':main()
