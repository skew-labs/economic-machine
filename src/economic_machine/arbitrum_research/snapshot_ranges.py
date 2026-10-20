"""Read a multipart *uncompressed* snapshot without downloading its state DB.

Tar headers supply file locations only. Neither a valid header checksum nor a
source ETag verifies the publisher's full-part SHA or chain consensus. This
catalogue deliberately never admits blocks into the chain index.
"""
import hashlib
import http.client
import json
from pathlib import PurePosixPath
import re
import tarfile
import urllib.error
import urllib.parse

ALLOWED_HOSTS={'snapshot.arbitrum.io','snapshot.arbitrum.foundation','robinhood-snapshots.offchainlabs.com'}


def parts(metadata,base_url):
    u=urllib.parse.urlsplit(base_url)
    if u.scheme!='https' or u.netloc not in ALLOWED_HOSTS or u.query or u.fragment:
        raise ValueError('SNAPSHOT_PUBLIC_SOURCE')
    if metadata.get('compression')!='none':raise ValueError('SNAPSHOT_NOT_SEEKABLE_TAR')
    rows=metadata.get('parts');out=[];offset=0;seen=set()
    if not isinstance(rows,list) or not 1<=len(rows)<=128:raise ValueError('SNAPSHOT_PARTS')
    for row in rows:
        name=row.get('filename');n=row.get('size_bytes');digest=row.get('sha256')
        if (not isinstance(name,str) or not re.fullmatch(r'[a-z-]+\.tar\.part[0-9]{4}',name)
            or name in seen or type(n) is not int or not 0<n<=4*1024**4
            or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)):
            raise ValueError('SNAPSHOT_PART_IDENTITY')
        seen.add(name);out.append(dict(url=base_url.rstrip('/')+'/'+name,offset=offset,bytes=n,sha256=digest));offset+=n
    if metadata.get('size',{}).get('total_bytes')!=offset:raise ValueError('SNAPSHOT_TOTAL_SIZE')
    return out


def split_range(rows,start,size):
    if type(start) is not int or start<0 or type(size) is not int or not 0<size<=1024**2:
        raise ValueError('SNAPSHOT_RANGE_BOUND')
    if start+size>sum(r['bytes'] for r in rows):raise ValueError('SNAPSHOT_RANGE_OUTSIDE')
    result=[]
    for row in rows:
        lo=max(start,row['offset']);hi=min(start+size,row['offset']+row['bytes'])
        if lo<hi:result.append((row,lo-row['offset'],hi-lo))
    if sum(r[2] for r in result)!=size:raise ValueError('SNAPSHOT_PART_GAP')
    return result


def tar_header(raw,offset,total):
    if len(raw)!=512 or offset%512:raise ValueError('SNAPSHOT_TAR_ALIGNMENT')
    if raw==bytes(512):return dict(end_marker=True,next_offset=offset+512)
    try:info=tarfile.TarInfo.frombuf(raw,'utf-8','strict')
    except (tarfile.HeaderError,UnicodeError,ValueError) as e:raise ValueError('SNAPSHOT_TAR_HEADER') from e
    name=info.name
    if (not name or name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name
        or '\x00' in name or len(name)>4096):raise ValueError('SNAPSHOT_UNSAFE_PATH')
    if info.type not in (tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.DIRTYPE,tarfile.XHDTYPE,tarfile.GNUTYPE_LONGNAME):
        raise ValueError('SNAPSHOT_UNSAFE_MEMBER_TYPE')
    if info.type in (tarfile.XHDTYPE,tarfile.GNUTYPE_LONGNAME):
        # Stop at a recoverable offset. No guessing how extensions affect paths/sizes.
        raise ValueError('SNAPSHOT_EXTENDED_HEADER_REQUIRES_PARSER')
    if info.size<0 or info.size>total-offset-512:raise ValueError('SNAPSHOT_MEMBER_BOUND')
    end=offset+512+((info.size+511)//512)*512
    if end>total:raise ValueError('SNAPSHOT_MEMBER_END')
    path=PurePosixPath(name)
    return dict(name=name,header_offset=offset,data_offset=offset+512,bytes=info.size,
        directory=info.isdir(),next_offset=end,header_sha256=hashlib.sha256(raw).hexdigest(),
        history_candidate=('ancient' in path.parts or 'freezer' in path.parts),
        header_checksum_verified=True,whole_snapshot_sha256_verified=False,
        chain_records_verified=False,end_marker=False)


class HTTP:
    def __init__(self,factory=http.client.HTTPSConnection):self.connections={};self.factory=factory;self.requests=0
    def close(self):
        for c in self.connections.values():c.close()
        self.connections={}
    def read(self,row,start,size,pin=None,*,before_redirect=None,reuse_redirect=False):
        u=urllib.parse.urlsplit(row['url'])
        if u.netloc not in ALLOWED_HOSTS or u.scheme!='https':raise ValueError('SNAPSHOT_SOURCE')
        headers={'User-Agent':'EconomicMachineResearch/0.1','Accept-Encoding':'identity','Range':f'bytes={start}-{start+size-1}'}
        try:
            effective=row['url']
            if reuse_redirect and pin and pin.get('effective_url'):
                target=urllib.parse.urlsplit(pin['effective_url'])
                if target.scheme!='https' or target.netloc not in ALLOWED_HOSTS or target.path!=u.path or target.query or target.fragment:
                    raise ValueError('SNAPSHOT_PIN_REDIRECT_REJECTED')
                effective=pin['effective_url'];u=target
            for hop in range(2):
                if pin and pin.get('etag') and pin.get('effective_url',row['url'])==effective:headers['If-Match']=pin['etag']
                c=self.connections.get(u.netloc)
                if c is None:c=self.factory(u.hostname,timeout=20);self.connections[u.netloc]=c
                self.requests+=1;c.request('GET',u.path,headers=headers);r=c.getresponse()
                if r.status not in (301,308):break
                location=urllib.parse.urljoin(effective,r.headers.get('Location',''));target=urllib.parse.urlsplit(location)
                if (hop or target.scheme!='https' or target.netloc not in ALLOWED_HOSTS or target.path!=u.path
                    or target.query or target.fragment or before_redirect is None):
                    raise ValueError('SNAPSHOT_REDIRECT_REJECTED')
                c.close();self.connections.pop(u.netloc,None)
                before_redirect();u=target;effective=location
            if r.status!=206:raise urllib.error.HTTPError(row['url'],r.status,'snapshot range rejected',r.headers,None)
            if r.headers.get('Content-Range')!=f"bytes {start}-{start+size-1}/{row['bytes']}" or r.headers.get('Content-Encoding','identity')!='identity':
                raise ValueError('SNAPSHOT_RANGE_NOT_HONORED')
            identity=dict(etag=r.headers.get('ETag'),bytes=row['bytes'])
            if effective!=row['url']:identity['effective_url']=effective
            if pin and identity!=pin:raise ValueError('SNAPSHOT_SOURCE_CHANGED')
            data=r.read(size+1)
            if len(data)!=size:raise ValueError('SNAPSHOT_RANGE_LENGTH')
            if r.will_close:c.close();self.connections.pop(u.netloc,None)
            return data,identity
        except (OSError,http.client.HTTPException) as e:
            self.close()
            if isinstance(e,urllib.error.HTTPError):raise
            raise urllib.error.URLError(type(e).__name__) from e
        except BaseException:self.close();raise
