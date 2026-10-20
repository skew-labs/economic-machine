"""Read public metadata and tiny frame boundaries, never a whole archive."""
import argparse
import hashlib
import http.client
import json
import struct
import urllib.error
import urllib.parse
from pathlib import Path

from economic_machine.robinhood_research.snapshot_scout import Scout
from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.pump_research.archive_gate import ArchiveGate
from economic_machine.pump_research.store import atomic


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    p.add_argument('--catalogue',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();cloud=Cloud(a.base);s=Scout(a.catalogue,a.out,cloud)
    records=[]
    def get(url,*,start=None,size=65536,total=None):
        origin=urllib.parse.urlsplit(url);filename=Path(origin.path).name
        for hop in range(2):
            u=urllib.parse.urlsplit(url)
            if u.scheme!='https' or u.hostname not in {'snapshot.titandeployer.com','dl.titandeployer.com'} or u.query or u.fragment:
                raise ValueError('PUBLIC_ARCHIVE_SOURCE')
            with ArchiveGate(s.catalogue/'source-gate',s.guard,wait=True):
                attempt=s.reserve(start or 0,size+1)
                record=dict(attempt=attempt,url=url,start=start,reserved_bytes=size+1)
                records.append(record);atomic(s.out/'attempts.json',dict(records=records))
                c=http.client.HTTPSConnection(u.hostname,timeout=20)
                headers={'Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'}
                if start is not None:headers['Range']=f'bytes={start}-{start+size-1}'
                try:
                    c.request('GET',u.path,headers=headers);r=c.getresponse();record['http_status']=r.status
                    if r.status in (301,302,307,308):
                        target=urllib.parse.urljoin(url,r.headers.get('Location',''))
                        if hop or Path(urllib.parse.urlsplit(target).path).name!=filename:
                            raise ValueError('PUBLIC_ARCHIVE_REDIRECT_IDENTITY')
                        record['redirect']=target;url=target;continue
                    expected=200 if start is None else 206
                    if r.status!=expected:
                        raise urllib.error.HTTPError(url,r.status,'public archive probe',r.headers,None)
                    if r.headers.get('Content-Encoding','identity')!='identity':raise ValueError('PUBLIC_ARCHIVE_ENCODING')
                    if start is not None and r.headers.get('Content-Range')!=f'bytes {start}-{start+size-1}/{total}':
                        raise ValueError('PUBLIC_ARCHIVE_RANGE')
                    raw=r.read(size+1)
                    if len(raw)>size or start is not None and len(raw)!=size:raise ValueError('PUBLIC_ARCHIVE_BOUND')
                    record.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),etag=r.headers.get('ETag'))
                    with s.db:s.db.execute("UPDATE attempts SET state='received' WHERE id=?",(attempt,))
                finally:
                    c.close();atomic(s.out/'attempts.json',dict(records=records))
            path=s.out/f'{len(records):03d}.raw';path.write_bytes(raw)
            record['blob']=cloud.upload(path);atomic(s.out/'attempts.json',dict(records=records))
            return raw,record
        raise ValueError('PUBLIC_ARCHIVE_REDIRECT_BOUND')
    try:
        raw,_=get('https://snapshot.titandeployer.com/snapshots/latest.json')
        meta=json.loads(raw);name=meta['name'];total=meta['sizeBytes']
        import re
        if not re.fullmatch(r'robinhood-snapshot-[0-9]{8}-[0-9]{4}\.tar\.zst',name) or type(total)is not int or total<=65536:
            raise ValueError('PUBLIC_ARCHIVE_METADATA')
        # The public snapshot page's download/sidecar redirects identify this
        # data host. The website route can return HTML with 200 for a file URL.
        url='https://dl.titandeployer.com/'+name
        first,first_r=get(url,start=0,size=32,total=total)
        tail,tail_r=get(url,start=total-4096,size=4096,total=total)
        if first_r['etag']!=tail_r['etag']:raise ValueError('PUBLIC_ARCHIVE_CHANGED')
        seek_magic=tail[-4:]==bytes.fromhex('b1ea928f')
        report=dict(state='FRAME_BOUNDARIES_INSPECTED',metadata=meta,
            zstd_magic=first[:4].hex(),zstd_frame_magic_matches=first[:4]==bytes.fromhex('28b52ffd'),
            seekable_footer_magic_present=seek_magic,tail_last_9_bytes=tail[-9:].hex(),
            whole_archive_hash_verified=False,all_frame_boundaries_known=False,
            chain_records_indexed=0,records=records)
        if seek_magic:
            frames,descriptor=struct.unpack('<IB',tail[-9:-4]);report['seekable_frame_count']=frames;report['seekable_descriptor']=descriptor
            report['seek_table_validation_pending']=True
        atomic(s.out/'report.json',report);print(json.dumps(report))
    except Exception as e:
        atomic(s.out/'error.json',dict(type=type(e).__name__,error=str(e)[:200],records=records));raise
    finally:s.close();cloud.close()


if __name__=='__main__':main()
