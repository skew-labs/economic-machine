// Fixed upstream only. Browser sessions remain scoped to /commerce on skew.deals.
// No Sites/connector credentials or unrelated cookies reach the runtime.
const upstreamOrigin = 'https://machine.148-113-153-116.nip.io';
export async function commerceProxy(request: Request): Promise<Response | null> {
  const url = new URL(request.url);
  const landing = url.pathname === '/';
  const legacyHome = url.pathname === '/home' || url.pathname.startsWith('/home/');
  if (!landing && !legacyHome && url.pathname !== '/index.html' && url.pathname !== '/commerce' && !url.pathname.startsWith('/commerce/')) return null;
  if (!['GET','HEAD','POST','OPTIONS'].includes(request.method)) return new Response('Method not allowed', {status:405});
  if (!['skew.deals','skew-index-terminal.angus4314.chatgpt.site'].includes(url.hostname))
    return new Response('Unknown public origin', {status:403});
  if (url.hostname !== 'skew.deals') return Response.redirect('https://skew.deals'+url.pathname+url.search, 308);
  if ((landing || legacyHome || url.pathname === '/index.html') && !['GET','HEAD'].includes(request.method)) return new Response('Method not allowed', {status:405});
  if (legacyHome || url.pathname === '/index.html') {
    const asset = url.pathname.replace(/^\/home\/?/, '');
    const path = !asset || asset === 'index.html' || url.pathname === '/index.html' ? '/' : '/commerce/' + asset;
    return Response.redirect('https://skew.deals' + path + url.search, 308);
  }
  if (['/commerce','/commerce/','/commerce/index.html'].includes(url.pathname) && ['GET','HEAD'].includes(request.method)) return Response.redirect('https://skew.deals/' + url.search, 308);
  const headers = new Headers();
  for(const name of ['accept','content-type','origin','authorization']) {
    const value=request.headers.get(name); if(value)headers.set(name,value);
  }
  const cookie=(request.headers.get('cookie')||'').split(';').map(v=>v.trim()).filter(v=>/^(machine_buyer|machine_login)=/.test(v)).join('; ');
  if(cookie && !landing)headers.set('cookie',cookie);
  if(landing){headers.delete('authorization');headers.delete('origin');}
  const target = new URL((landing ? '/commerce/' : url.pathname)+url.search,upstreamOrigin);
  let body: ArrayBuffer | undefined;
  if(request.method==='POST') {
    if(Number(request.headers.get('content-length')||0)>262144)return new Response('Request too large',{status:413});
    const reader=request.body?.getReader();let size=0;const chunks:Uint8Array[]=[];
    if(reader) while(true){const chunk=await reader.read();if(chunk.done)break;size+=chunk.value.length;if(size>262144){await reader.cancel();return new Response('Request too large',{status:413});}chunks.push(chunk.value);}
    const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}body=bytes.buffer;
  }
  try {
    const response=await fetch(target,{method:request.method==='HEAD'?'GET':request.method,headers,body,redirect:'manual',signal:AbortSignal.timeout(55000)});
    const out=new Headers(response.headers);
    out.delete('set-cookie');
    for(const value of response.headers.getSetCookie()) if(/^(machine_buyer|machine_login)=/.test(value))out.append('set-cookie',value);
    const location=out.get('location');if(location?.startsWith(upstreamOrigin))out.set('location','https://skew.deals'+location.slice(upstreamOrigin.length));
    out.set('cache-control','private, no-store');out.set('x-skew-route','commerce');
    if(landing && response.ok && request.method === 'GET') {
      const html=(await response.text()).replace('<base href="/commerce/">','');
      out.delete('content-length');out.delete('content-encoding');out.delete('etag');out.set('x-skew-route','landing');
      return new Response(html,{status:response.status,headers:out});
    }
    return new Response(request.method==='HEAD'?null:response.body,{status:response.status,headers:out});
  } catch {return Response.json({error:'Workspace temporarily unavailable. No automatic retry was sent.'},{status:502,headers:{'Cache-Control':'no-store'}});}
}
