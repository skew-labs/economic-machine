/* SKEW market panel. A missing market is an empty chart, never a sample price. */
(() => {
  'use strict';
  const script = document.currentScript;
  const base = new URL('.', script.src);
  const node = (tag, cls, text) => {const n=document.createElement(tag);n.className=cls||'';if(text!==undefined)n.textContent=text;return n;};
  const svgNode = (tag, attrs) => {const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));return n;};
  const usd = value => value===null||value===undefined?'—':Number(value).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:Number(value)<1?8:2});
  let pending=null, cached=null, expires=0;
  async function load(force=false) {
    if(!force&&cached&&Date.now()<expires)return cached;
    if(pending)return pending;
    pending=fetch(new URL('market/skew',base),{credentials:'omit',signal:AbortSignal.timeout(15000)}).then(async r=>{if(!r.ok)throw Error('unavailable');const d=await r.json();if(d.symbol!=='SKEW'||d.chain_id!==42161)throw Error('identity');cached=d;expires=Date.now()+60000;return d;}).finally(()=>{pending=null;});return pending;
  }
  function mount(root, {compact=false}={}) {
    if(root.dataset.marketMounted)return;
    root.dataset.marketMounted='true';root.classList.add('skew-market');if(compact)root.classList.add('market-compact');
    const header=node('div','market-header'),identity=node('div','market-identity'),image=node('img');
    image.src=new URL('assets/skew-token.svg?v=visual-1',base);image.alt='SKEW token';image.width=52;image.height=52;
    const name=node('div');name.append(node('h3','','SKEW'),node('span','market-network','Arbitrum One'));
    identity.append(image,name);const status=node('span','market-status','Checking market');header.append(identity,status);
    const quote=node('div','market-quote'),price=node('strong','market-price','—'),caption=node('span','','USD price');quote.append(price,caption);
    const ranges=node('div','market-ranges');ranges.setAttribute('role','group');ranges.setAttribute('aria-label','Chart range');
    let days=1, data=null;
    for(const [label,n]of [['24H',1],['7D',7]]){const b=node('button','',label);b.type='button';b.setAttribute('aria-pressed',String(n===days));b.onclick=()=>{days=n;for(const x of ranges.children)x.setAttribute('aria-pressed',String(x===b));draw();};ranges.append(b);}
    const plot=node('div','market-plot'),hint=node('p','market-chart-note','Loading price history…');
    const stats=node('dl','market-stats'),volume=node('dd','','—'),cap=node('dd','','—');
    for(const[label,value]of [['Volume · 24h',volume],['Market cap',cap]]){const cell=node('div');cell.append(node('dt','',label),value);stats.append(cell);}
    const issuance=node('section','market-issuance'),issuanceHeader=node('div','market-issuance-heading'),supply=node('strong','','—'),supplyNote=node('span','','Checking on-chain supply…'),progress=node('progress','market-supply-progress');
    progress.max=160000;progress.setAttribute('aria-label','SKEW issued out of maximum supply');
    issuanceHeader.append(node('span','','Total issued'),supply);issuance.append(issuanceHeader,progress,supplyNote);
    const foot=node('div','market-foot'),source=node('span','','Market data is read-only.'),refresh=node('button','market-refresh','Refresh');refresh.type='button';foot.append(source,refresh);
    root.append(header,quote,ranges,plot,hint,stats,issuance,foot);
    function draw() {
      plot.replaceChildren();
      const svg=svgNode('svg',{viewBox:'0 0 720 240',role:'img','aria-label':'SKEW USD price history'});
      for(const y of [24,80,136,192])svg.append(svgNode('line',{x1:8,y1:y,x2:650,y2:y,class:'market-grid'}));
      plot.append(svg);
      const end=data?.observed_at||Date.now()/1000;
      const points=(data?.candles||[]).filter(p=>p.timestamp>=end-days*86400&&Number.isFinite(Number(p.price_usd))&&Number(p.price_usd)>0);
      if(points.length<2){
        svg.setAttribute('aria-label','No SKEW price history available');
        const empty=node('div','market-chart-empty');
        empty.append(node('strong','',data?.status==='AWAITING_TOKEN_ADDRESS'||data?.status==='AWAITING_MARKET'?'Trading has not started':'No price history for this range'),node('span','','The line appears when verified pool trades are available.'));plot.append(empty);
        hint.textContent=data?.status==='AWAITING_TOKEN_ADDRESS'?'Token contract not yet published. Price, volume and market cap are unavailable.':data?.status==='AWAITING_MARKET'?'Waiting for a configured SKEW trading pool.':'Historical data is unavailable. No sample prices are shown.';return;
      }
      const prices=points.map(p=>Number(p.price_usd)),low=Math.min(...prices),high=Math.max(...prices),pad=(high-low)*.1||high*.01;
      const min=low-pad,max=high+pad,start=end-days*86400;
      const x=p=>8+(p.timestamp-start)/(end-start)*630,y=p=>192-(Number(p.price_usd)-min)/(max-min)*168;
      let d='';points.forEach((p,i)=>{d+=(i===0||p.timestamp-points[i-1].timestamp>5400?'M':'L')+x(p).toFixed(2)+' '+y(p).toFixed(2)+' ';});
      svg.append(svgNode('path',{d,class:'market-line'}));
      for(const [v,ypos] of [[high,30],[low,190]]){const t=svgNode('text',{x:660,y:ypos,class:'market-axis'});t.textContent=usd(v);svg.append(t);}
      const cursor=svgNode('circle',{r:5,class:'market-cursor',cx:x(points[points.length-1]),cy:y(points[points.length-1])});svg.append(cursor);
      const describe=i=>{const p=points[i];cursor.setAttribute('cx',x(p));cursor.setAttribute('cy',y(p));hint.textContent=new Date(p.timestamp*1000).toLocaleString()+' · '+usd(p.price_usd)+' · hourly close';};
      svg.setAttribute('tabindex','0');svg.setAttribute('aria-label','SKEW price history. Use left and right arrow keys to inspect hourly closes.');let chosen=points.length-1;
      svg.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();chosen=Math.max(0,Math.min(points.length-1,chosen+(e.key==='ArrowLeft'?-1:1)));describe(chosen);}};
      svg.onpointermove=e=>{const bounds=svg.getBoundingClientRect(),position=(e.clientX-bounds.left)/bounds.width*720;chosen=points.reduce((best,p,i)=>Math.abs(x(p)-position)<Math.abs(x(points[best])-position)?i:best,0);describe(chosen);};
      hint.textContent='Hourly closes · GeckoTerminal · gaps indicate missing trading intervals.';
    }
    async function update(force=false){
      refresh.disabled=true;
      try{data=await load(force);if(!root.isConnected)return;
        const labels={AVAILABLE:'Pool reference price',AWAITING_TOKEN_ADDRESS:'Pre-launch',AWAITING_MARKET:'No market yet',SOURCE_UNAVAILABLE:'Source unavailable'};
        status.textContent=labels[data.status]||'Unavailable';status.dataset.status=data.status;
        price.textContent=usd(data.price_usd);volume.textContent=usd(data.volume_24h_usd);cap.textContent=usd(data.market_cap_usd);
        if(data.supply?.status==='AVAILABLE'){
          const issued=Number(data.supply.total_supply);supply.textContent=issued.toLocaleString('en-US',{maximumFractionDigits:6})+' / 160,000 SKEW';progress.value=issued;progress.hidden=false;
          supplyNote.textContent=Number(data.supply.issued_percent).toLocaleString('en-US',{maximumFractionDigits:6})+'% issued · block '+data.supply.block_number.toLocaleString('en-US')+' · checked '+new Date(data.supply.observed_at*1000).toLocaleTimeString()+'. Total supply, not circulating supply.';
        }else{supply.textContent='Unavailable';progress.hidden=true;progress.removeAttribute('value');supplyNote.textContent='Live supply could not be reconciled. The maximum contract supply is 160,000 SKEW.';}
        source.replaceChildren();
        if(data.source&&data.pool_address){const a=node('a','',data.source);a.href='https://dexscreener.com/arbitrum/'+data.pool_address;a.target='_blank';a.rel='noopener noreferrer';source.append(a,document.createTextNode(' · checked '+new Date(data.observed_at*1000).toLocaleTimeString()));}
        else source.textContent=data.deployment?'Mainnet deployed · no market valuation':'SKEW token design · no market valuation';
        if(data.token_address){const a=node('a','market-contract','Contract ↗');a.href='https://arbiscan.io/token/'+data.token_address;a.target='_blank';a.rel='noopener noreferrer';source.append(document.createTextNode(' · '),a);}
        if(data.deployment?.transaction_hash){const a=node('a','market-contract','Deployment ↗');a.href='https://arbiscan.io/tx/'+data.deployment.transaction_hash;a.target='_blank';a.rel='noopener noreferrer';source.append(document.createTextNode(' · '),a);}
        draw();
      }catch{status.textContent='Market unavailable';price.textContent=volume.textContent=cap.textContent='—';supply.textContent='Unavailable';progress.hidden=true;supplyNote.textContent='Supply observation unavailable. Try refresh.';data=null;source.textContent='Could not reach the market source. Try refresh.';draw();}finally{refresh.disabled=false;}
    }
    refresh.onclick=()=>update(true);draw();update();
    const onVisibility=()=>{if(!root.isConnected){document.removeEventListener('visibilitychange',onVisibility);return;}if(!document.hidden)update();};document.addEventListener('visibilitychange',onVisibility);
  }
  window.SkewMarket={mount,load};
  document.querySelectorAll('[data-skew-market]').forEach(root=>mount(root));
})();
