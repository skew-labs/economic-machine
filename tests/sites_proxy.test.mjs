import {test} from 'node:test';import assert from 'node:assert/strict';
import {commerceProxy} from '../infra/sites-commerce-proxy.ts';
test('wallet binding survives custom domain without unrelated cookies',async()=>{
 const old=globalThis.fetch;let seen;
 globalThis.fetch=async(url,opts)=>{seen={url:String(url),opts};const h=new Headers();h.append('set-cookie','machine_login=challenge; Path=/commerce/; Secure; HttpOnly');h.append('set-cookie','unrelated=secret; Path=/');return new Response('{}',{headers:h});};
 try{const r=await commerceProxy(new Request('https://skew.deals/commerce/api/auth/challenge',{method:'POST',headers:{origin:'https://skew.deals','content-type':'application/json',cookie:'oai_token=private; machine_login=bound; machine_buyer=owner','OAI-Sites-Authorization':'secret'},body:'{}'}));assert.equal(seen.url,'https://machine.148-113-153-116.nip.io/commerce/api/auth/challenge');assert.equal(seen.opts.headers.get('origin'),'https://skew.deals');assert.equal(seen.opts.headers.get('cookie'),'machine_login=bound; machine_buyer=owner');assert.equal(seen.opts.headers.get('OAI-Sites-Authorization'),null);assert.deepEqual(r.headers.getSetCookie(),['machine_login=challenge; Path=/commerce/; Secure; HttpOnly']);assert.equal(r.headers.get('cache-control'),'private, no-store');}finally{globalThis.fetch=old;}
});
test('fixed upstream and bounded request paths',async()=>{assert.equal(await commerceProxy(new Request('https://skew.deals/other')),null);assert.equal((await commerceProxy(new Request('https://evil.example/commerce/api/auth'))).status,403);assert.equal((await commerceProxy(new Request('https://skew.deals/commerce/api/auth',{method:'DELETE'}))).status,405);assert.equal((await commerceProxy(new Request('https://skew.deals/commerce/api/auth',{method:'POST',body:'x'.repeat(262145)}))).status,413);});
test('one canonical landing and legacy links without forwarding credentials',async()=>{
 const old=globalThis.fetch;let seen;
 globalThis.fetch=async(url,opts)=>{seen={url:String(url),opts};return new Response('<head><base href="/commerce/"></head><a href="#tools">Tools</a>',{headers:{'content-type':'text/html'}});};
 try{
  for(const path of ['/home','/home/','/home/index.html','/index.html','/commerce','/commerce/']){
   const r=await commerceProxy(new Request('https://skew.deals'+path));assert.equal(r.status,308);assert.equal(r.headers.get('location'),'https://skew.deals/');
  }
  const r=await commerceProxy(new Request('https://skew.deals/',{headers:{authorization:'private',cookie:'machine_buyer=owner',origin:'https://skew.deals'}}));
  assert.equal(r.status,200);assert.equal(seen.url,'https://machine.148-113-153-116.nip.io/commerce/');
  assert.equal(seen.opts.headers.get('authorization'),null);assert.equal(seen.opts.headers.get('cookie'),null);
  assert.ok(!(await r.text()).includes('<base'));
  assert.equal((await commerceProxy(new Request('https://skew.deals/home/atlas.html'))).headers.get('location'),'https://skew.deals/commerce/atlas.html');
  assert.equal((await commerceProxy(new Request('https://skew.deals/',{method:'POST',body:'x'}))).status,405);
 }finally{globalThis.fetch=old;}
});
