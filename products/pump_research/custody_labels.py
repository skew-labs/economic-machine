"""Snapshot public label claims within the existing IO/storage grant; never run SQL."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.store import address,atomic

URL='https://raw.githubusercontent.com/duneanalytics/spellbook/main/dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/solana/cex_solana_addresses.sql'
ROW=re.compile(r"\(\s*'solana'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*date\s*'([^']+)'\s*\)")


def parse(raw, now):
    sha=hashlib.sha256(raw).hexdigest();labels=[];rejected=[]
    for wallet,entity,name,contributor,added in ROW.findall(raw.decode()):
        if not address(wallet):
            rejected.append(dict(address=wallet,reason='not_a_32_byte_solana_address'));continue
        labels.append(dict(chain='solana',address=wallet,entity=entity,name=name,
            contributor=contributor,source_added_date=added,observed_ms=now,
            source_url=URL,source_sha256=sha,evidence_type='third_party_label_claim',
            valid_from_ms=None,valid_to_ms=None,ownership_verified=False))
    if not labels:raise ValueError('CUSTODY_LABEL_PARSE_EMPTY')
    return dict(schema='economic-machine.custody-label-claims.v1',at_ms=now,labels=labels,
        rejected=rejected,source_url=URL,source_sha256=sha,historical_validity_verified=False)


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    b=Path(a.base);out=b/'evidence/wallet-map-v1';scope=Scope(b,b/'evidence/core-trace-v1/grant.json')
    try:
        # One bounded, public source fetch; the complete response and hash remain.
        cap=65536;scope.reserve(cap,'custody-label-claims:dune-spellbook-solana')
        with urllib.request.urlopen(urllib.request.Request(URL,headers={'User-Agent':'SkewResearch/1'}),timeout=20) as response:
            if response.geturl()!=URL:raise ValueError('CUSTODY_SOURCE_REDIRECT')
            raw=response.read(cap+1)
        if len(raw)>cap:raise ValueError('CUSTODY_SOURCE_SIZE')
        value=parse(raw,time.time_ns()//1000000);raw_path=out/('custody-label-source-'+value['source_sha256']+'.sql')
        if not raw_path.exists():raw_path.write_bytes(raw)
        atomic(out/f"custody-labels-{value['at_ms']}.json",value);atomic(out/'custody-labels.json',value)
        print(json.dumps(dict(labels=len(value['labels']),entities=len({r['entity'] for r in value['labels']}),rejected=len(value['rejected']),source_bytes=len(raw),source_sha256=value['source_sha256'])))
    finally:scope.close()


if __name__=='__main__':main()
