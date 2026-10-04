(function () {
  'use strict';
  const C = window.MachineConsole, {el,button} = C;
  const root = document.getElementById('ops-overview');
  let ready=false, busy=false, pending='', historyOwner=null, turns=[], quoteVersion=0, boundShown=false;
  let conversationRevision=0;
  let trackingTimer=null, trackingKey=null, trackingRow=null;
  const prefix=C.API_PREFIX || '', storedKey=()=> 'skew-console-swap-'+(C.state.identity?.address?.toLowerCase()||'');
  const api=(path,body)=>C.api('/api/engine'+path,body);
  const friendly = error => ({ASSISTANT_BEDROCK_ORGANIZATION_DENY:'Amazon Bedrock is blocked by the AWS organization policy. The account administrator must allow access. No fallback model is used; workspace actions still work.',FRESH_CHAIN_STATE_REQUIRED:'The chain data could not be verified as current. Request a fresh quote; no order was sent.',INSUFFICIENT_USDC:'This amount exceeds the USDC available in your wallet.',ASSISTANT_BEDROCK_NOT_CONFIGURED:'Amazon Bedrock needs to be connected by the workspace operator. You can use the actions below in the meantime.',ASSISTANT_BEDROCK_CREDENTIALS_REQUIRED:'AWS sign-in is required for the assistant. Your workspace actions are still available.',ASSISTANT_BEDROCK_UNAVAILABLE_OR_INVALID:'Amazon Bedrock could not finish this request. No transaction was sent.',ASSISTANT_PROVIDER_NOT_CONFIGURED:'Your assistant is not connected yet. Your workspace tools are still available.',
    ASSISTANT_PROVIDER_UNAVAILABLE_OR_INVALID:'The assistant could not finish this request. No transaction was sent. Try a shorter request.',
    ASSISTANT_DAILY_LIMIT_REACHED:'Today’s conversation limit is reached. You can still use the workspace tools.',
    ASSISTANT_REQUEST_IN_PROGRESS:'Your previous message is still being processed.',
    DO_NOT_SEND_PRIVATE_KEYS_OR_API_SECRETS:'Keep private keys and API secrets out of chat. Add a connection through Connections.'}[error.message] || WalletBridge.connectionError(error));
  function add(role,text,content) {
    conversationRevision++;
    const row=el('article','chat-message '+role), label=el('span','chat-speaker',role==='user'?'You':'Skew');
    row.append(label,el('p','chat-text',text)); if(content)row.append(content);
    document.getElementById('conversation').append(row);
    document.getElementById('chat-welcome').hidden=true;
    row.scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
    return row;
  }
  const go=(label,view)=>button(label,'button secondary',()=>C.setView(view));
  function action(label,fn) {
    const b=button(label,'button primary',async()=>{
      if(busy)return;busy=true;b.disabled=true;
      try{if(await fn()===true)b.dataset.completed='true';}catch(e){add('assistant',friendly(e));}
      finally{busy=false;b.disabled=b.dataset.completed==='true';}
    });return b;
  }
  function card(title,note) {const n=el('section','chat-action');n.append(el('h3','',title));if(note)n.append(el('p','muted',note));return n;}
  function details(node,pairs) {const dl=el('dl','chat-facts');for(const[k,v]of pairs){dl.append(el('dt','',k),el('dd','',String(v)));}node.append(dl);}
  async function fetchSwap(path,body) {
    const r=await fetch(prefix+'/swap-api/'+path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await r.json();if(!r.ok)throw new Error(data.error||data.detail||'Swap service unavailable.');return data;
  }
  function savedSwap(){try{return JSON.parse(localStorage.getItem(storedKey())||'null');}catch{throw new Error('Saved swap needs recovery. Do not send a replacement.');}}
  function saveSwap(value){localStorage.setItem(storedKey(),JSON.stringify(value));}
  function startTracking() {
    const held=savedSwap(), owner=C.state.identity?.address?.toLowerCase();
    if(!held?.id || !owner || held.status==='USER_REJECTED')return;
    const key=owner+':'+held.id;
    if(key===trackingKey&&trackingRow?.isConnected)return;
    clearTimeout(trackingTimer);trackingKey=key;
    const box=card('Tracking your swap','Your approved order is being checked automatically.');
    trackingRow=add('assistant','I will confirm completion after the chain and your received balance are verified.',box);
    async function poll() {
      if(trackingKey!==key||C.state.identity?.address?.toLowerCase()!==owner)return;
      try {
        const result=await swapStep('watch',{id:held.id},held.fuel_id);
        if(trackingKey!==key||C.state.identity?.address?.toLowerCase()!==owner)return;
        const done=result.status==='FILLED_FINALIZED'&&result.chain_verified===true;
        const expired=result.status==='EXPIRED_UNFILLED'&&result.safe_to_retry===true;
        saveSwap({...held,status:done?'FILLED_FINALIZED':expired?'EXPIRED_UNFILLED':result.status});
        const labels={PENDING:'Waiting for settlement',SUBMITTED:'Order accepted',UNKNOWN_RECONCILE_ONLY:'Checking the original order',AWAITING_FINALITY:'Trade executed · waiting for chain finality',SETTLEMENT_UNCONFIRMED:'Confirming the transaction',BALANCE_RECONCILIATION_REQUIRED:'Checking received ETH'};
        box.replaceChildren(el('h3','',done?'Swap complete':expired?'Order expired without a fill':labels[result.status]||'Tracking your swap'));
        if(done) {
          trackingRow.querySelector('.chat-text').textContent='Done. Your swap is finalized and the ETH receipt has been verified on two independent RPCs.';
          const received=result.proofs?.[0]?.buy_wei;
          if(typeof received==='string'&&/^\d+$/.test(received))box.append(el('p','',SkewSwapWallet.formatUnits(received,18)+' ETH received.'));
        } else if(expired) {
          trackingRow.querySelector('.chat-text').textContent='The order expired without a fill. No replacement order was sent.';
        } else box.append(el('p','muted',result.tracking?.source_error?'A verification source is temporarily unavailable. Tracking will retry; no new order is sent.':'I am checking automatically. You can close this page; the server keeps tracking this order.'));
        for(const tx of result.tx_hashes||[])if(/^0x[0-9a-f]{64}$/i.test(tx)) {
          const a=el('a','text-link','View transaction');a.href='https://arbiscan.io/tx/'+tx;a.target='_blank';a.rel='noopener noreferrer';box.append(a);
        }
        if(result.tracking?.last_checked)box.append(el('small','chat-trace','Last checked '+new Date(result.tracking.last_checked*1000).toLocaleTimeString()));
        if(done||expired){trackingTimer=null;return;}
      } catch(e) {
        box.replaceChildren(el('h3','','Tracking continues'),el('p','muted','The status connection is unavailable. The server keeps the original order; this page will reconnect automatically.'));
      }
      trackingTimer=setTimeout(poll,7000);
    }
    poll();
  }
  async function wallet() {
    if(!C.getWallet()){C.signIn();throw new Error('Reconnect your signing wallet, then continue this action.');}
    const state=await WalletBridge.accountState(C.getWallet());
    if(state.chain_id!==42161 || state.address.toLowerCase()!==C.state.identity?.address?.toLowerCase())throw new Error('Reconnect the reviewed wallet on Arbitrum One.');
    return state.address;
  }
  async function swapStep(step,body,fuelId) {
    if(!fuelId)return fetchSwap(step,body);
    const r=await api('/fuel/requests/'+fuelId+'/'+(step==='status'?'reconcile':step),body.signature?{signature:body.signature}:{});
    return step==='status'?r.settlement:r;
  }
  function checkPending() {
    const held=savedSwap();
    if(held&&!['USER_REJECTED','FILLED_FINALIZED','EXPIRED_UNFILLED'].includes(held.status))
      throw new Error('A swap is already pending. Check the original settlement before signing another.');
  }
  function reviewSwap(reviewed,owner,atoms,fuelId=null) {
    SkewSwapWallet.validBase(reviewed,owner,atoms);
    const version=++quoteVersion, raw=SkewSwapWallet.formatUnits(atoms);
    const current=()=>{checkPending();if(version!==quoteVersion)throw new Error('A newer quote is open. Review that quote before signing.');};
    const quote=card('Review your quote','No ETH is required for the two wallet signatures.');
    details(quote,[['You spend',raw+' USDC'],['Estimated receipt',SkewSwapWallet.formatUnits(reviewed.preview_buy_wei,18)+' ETH'],['Routing cost',SkewSwapWallet.formatUnits(reviewed.preview_fee_atoms)+' USDC'],['Network','Arbitrum One']]);
    async function finalReview(approved,signedPermit) {
      SkewSwapWallet.validateOrder(approved,owner,atoms,signedPermit,SkewSwapCrypto);
      const final=card('Confirm swap','The next signature authorizes this trade.');
      details(final,[['Maximum spend',raw+' USDC'],['Minimum receipt',SkewSwapWallet.formatUnits(approved.minimum_buy_wei,18)+' ETH'],['Receive at',owner]]);
      final.append(action('2. Sign and submit swap',async()=>{
        current();await wallet();
        const persist=value=>saveSwap({...value,fuel_id:fuelId});
        const signature=await SkewSwapWallet.signOrder(C.getWallet(),approved,owner,atoms,signedPermit,SkewSwapCrypto,persist);
        const result=await swapStep('submit',{id:approved.id,signature},fuelId);
        saveSwap({...savedSwap(),status:result.status});
        final.replaceChildren(el('h3','','Order submitted'),el('p','','Tracking settlement automatically. You can leave this page.'),action('View progress',()=>startTracking()));
        startTracking();
        return true;
      }));
      add('assistant','The route was simulated. Review the final minimum before signing.',final);
    }
    quote.append(action(reviewed.order?'Review existing approval':'1. Approve USDC in wallet',async()=>{
      current();await wallet();
      const signedPermit=reviewed.order?SkewSwapCrypto.signatureFromHook(reviewed.app_data):await SkewSwapWallet.signPermit(C.getWallet(),reviewed,owner,atoms,SkewSwapCrypto);
      const approved=reviewed.order?reviewed:await swapStep('order',{id:reviewed.id,signature:signedPermit},fuelId);
      await finalReview(approved,signedPermit);return true;
    }));
    add('assistant','A live route is ready for your review.',quote);
  }
  function offerSwap(amount) {
    const node=card('Get ETH for gas','Pay with USDC on Arbitrum One. Your wallet approves the exact amount.');
    const label=el('label','','USDC to swap'),input=el('input');input.type='text';input.inputMode='decimal';input.value=amount||'';input.placeholder='USDC amount';input.setAttribute('aria-label','USDC to swap');label.append(input);node.append(label);
    node.append(action('Get live quote',async()=>{
      const owner=await wallet(), previous=savedSwap();
      if(previous&&!['USER_REJECTED','FILLED_FINALIZED','EXPIRED_UNFILLED'].includes(previous.status))return startTracking();
      const atoms=SkewSwapWallet.usdcAtoms(input.value.trim());
      const reviewed=await fetchSwap('quote',{owner,amount_atoms:atoms});reviewSwap(reviewed,owner,atoms);
    }));
    const held=savedSwap();if(held?.id)node.append(action('Track previous swap',()=>startTracking()));
    return node;
  }
  function boundRequest() {
    const fid=new URLSearchParams(location.search).get('fuel');
    if(boundShown||!/^fuel-[0-9a-f]{24}$/.test(fid||''))return;
    boundShown=true;
    const n=card('Review agent gas request','This request keeps the original shared budget and purchase reservation.');
    n.append(action('Load agent request',async()=>{
      const owner=await wallet(), bound=await api('/fuel/requests/'+fid), reviewed=bound.swap;
      if(owner.toLowerCase()!==reviewed.owner.toLowerCase())throw new Error('Connect the wallet assigned to this request.');
      const held=savedSwap();
      if(held&&!['USER_REJECTED','FILLED_FINALIZED','EXPIRED_UNFILLED'].includes(held.status)&&held.id!==reviewed.id)return startTracking();
      if(['SUBMITTED','UNKNOWN_RECONCILE_ONLY','FILLED_FINALIZED','EXPIRED_UNFILLED'].includes(reviewed.status)||held?.id===reviewed.id){
        saveSwap({...held,id:reviewed.id,owner,status:reviewed.status,fuel_id:fid});return startTracking();
      }
      reviewSwap(reviewed,owner,reviewed.amount_atoms,fid);return true;
    }));add('assistant','An agent is requesting gas. Review it here with your wallet.',n);
  }
  function proposal(result, accepted) {
    const p=result.proposal;let n;
    if(p.action==='swap')n=offerSwap(p.amount);
    else if(p.action==='task'&&p.task?.budget!==null){
      n=card(p.task.title,'Review the task before adding it to your workspace. Purchases require a separate approval.');
      details(n,[['Budget cap',p.task.budget+' USD'],['Work',p.task.kind.replaceAll('_',' ')],['Conditions',p.task.preference_id?'Use last confirmed conditions':Object.entries(p.task.constraints).map(([k,v])=>k.replaceAll('_',' ')+': '+v).join(' · ')||'Needs clarification']]);
      if(!accepted)n.append(action('Create this task',async()=>{const task=await api('/assistant/'+result.request_id+'/task',{});n.replaceChildren(el('h3','','Task created'),el('p','',task.id||'Saved to your workspace'),go('Open tasks','tasks'));}));
      else n.append(go('Open saved task','tasks'));
    }else if(p.action==='status'){
      n=card('Your workspace');n.append(action('Check connected accounts',async()=>{
        const live=await api('/overview');n.replaceChildren(el('h3','','Account overview'));
        for(const conn of live.connections||[]){n.append(el('p','',`${conn.name||conn.id} · ${conn.status}`));}
        if(!live.connections?.length)n.append(el('p','','No accounts connected yet.'));
        n.append(go('Manage connections','connections'),go('Orders and positions','execution'));
      }));
    }else if(p.action==='mining'){
      n=card('Mining','Check available jobs, search results and reward status.');
      n.append(action('Check mining jobs',async()=>{
        const m=await api('/mining');n.replaceChildren(el('h3','','Your mining jobs'));
        n.append(el('p','',`${m.jobs.length} saved jobs · confirmed reward ${m.confirmed_reward}`));
        for(const j of m.jobs.slice(0,4)){n.append(el('p','',j.snapshot?.title||j.id),action('Search this job',async()=>{
          const r=await api('/mining/jobs/'+j.id+'/solve',{budget:10000});
          const proof=card('Search result','Local candidate verification. This is not an on-chain token reward.');
          proof.append(el('pre','receipt-json',JSON.stringify(r,null,2)));add('assistant','The engine completed the bounded search.',proof);
        }));}n.append(go('Mining controls','mining'));
      }));
    }else if(p.action==='data'){n=card('Data licenses');n.append(go('Browse data','data'),go('Buy services','market'));}
    else if(p.action==='connections'){n=card('Connect your tools');n.append(go('Manage connections','connections'));}
    const row=add('assistant',p.reply,n);
    const meta=el('small','chat-trace',`${result.trace.model} · ${result.trace.latency_ms} ms · proposal`);
    row.append(meta);
  }
  async function send(text) {
    if(busy||!text.trim())return;
    if(!C.state.identity&&!C.LOCAL_ENGINE){pending=text;C.signIn();return;}
    busy=true;const input=document.getElementById('chat-input'),submit=document.getElementById('chat-send');
    input.value='';submit.disabled=true;add('user',text);
    const indicator=add('assistant','Working on your request…');indicator.classList.add('chat-pending');
    try{const result=await api('/assistant',{request_id:crypto.randomUUID(),message:text});indicator.remove();proposal(result);}
    catch(e){indicator.querySelector('.chat-text').textContent=friendly(e);indicator.classList.remove('chat-pending');}
    finally{busy=false;submit.disabled=false;input.focus();}
  }
  function localPlanCard(job) {
    const n=card('Choose how to clean your data','Both options run in this workspace. No external service fee; local compute is not priced.');
    const options=el('div','chat-plan-grid');n.append(options);
    for(const p of job.plans){
      const option=card(p.title);details(option,[['External charge','$0.00'],['Output',p.format.toUpperCase()]]);
      option.append(action('Approve this plan',async()=>{
        const done=await api('/local-work/'+job.id+'/run',{plan_hash:p.hash});
        if(done.status!=='DELIVERED')throw new Error('The result is not verified yet.');
        const result=await api('/local-work/'+job.id+'/result');
        n.replaceChildren(el('h3','','Done. Your result is ready.'),el('p','',`${result.rows} rows delivered · ${result.removed_rows} exact duplicates removed.`));
        resultButton(n,'/local-work/'+job.id+'/result');return true;
      }));options.append(option);
    }return n;
  }
  function resultButton(node,path){node.append(action('Download result',async()=>{
    const result=await api(path);const url=URL.createObjectURL(new Blob([result.content],{type:result.content_type}));
    const a=el('a');a.href=url;a.download=result.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }));}
  async function workProgress(){
    const state=await api('/assistant/work'),n=card('Your saved work','Status comes from the execution records, without a new model call.');
    for(const t of state.tasks.slice(0,10))n.append(el('p','',t.brief.title+' · '+t.status.replaceAll('_',' ').toLowerCase()));
    for(const job of state.local){
      if(job.status==='DELIVERED'){const item=card('Completed data task','Result saved and verified.');resultButton(item,'/local-work/'+job.id+'/result');n.append(item);}
      else n.append(localPlanCard(job));
    }
    for(const p of state.purchases){const item=card(p.plan.service.name,p.status.replaceAll('_',' ').toLowerCase());
      if(p.status==='DELIVERED')resultButton(item,'/task-purchases/'+p.id+'/result');
      else item.append(go('Review payment or recovery','tasks'));n.append(item);}
    if(!state.tasks.length)n.append(el('p','','No saved work yet.'));
    n.append(go('Manage tasks','tasks'));add('assistant','Here is the latest saved state of your work.',n);
  }
  function officeWork(){
    const n=card('Clean a customer or supplier list','Paste CSV with a header. Compare keeping all rows with removing exact duplicates. No payment or AI call is needed.'),source=el('textarea');
    source.rows=5;source.maxLength=20000;source.placeholder='name,email\nAlex,alex@example.com';source.setAttribute('aria-label','CSV to clean');n.append(source);
    let draftId=crypto.randomUUID();source.addEventListener('input',()=>{draftId=crypto.randomUUID();});
    n.append(action('Compare two plans',async()=>{
      const columns=source.value.split(/\r?\n/)[0].split(',').map(v=>v.trim());
      if(!columns.length||columns.some(c=>!c))throw new Error('Add a CSV header with named columns.');
      const task=await api('/tasks',{request_id:draftId,kind:'data_cleanup',title:'Clean business data',instructions:'Trim cells. Review whether exact duplicate rows should be removed.',budget:{currency:'USD',maximum:'0'},deadline_at:null,constraints:{output_format:'csv',required_fields:columns},preference_id:null,connection_ids:[]});
      const job=await api('/tasks/'+task.id+'/local-plans',{request_id:draftId+'-plans',expected_revision:task.revision,input:{csv:source.value}});
      add('assistant','Choose the transformation you want. Neither plan sends a payment.',localPlanCard(job));return true;
    }));add('assistant','Let’s finish a practical data task.',n);
  }
  function init() {
    if(ready)return;ready=true;root.replaceChildren();
    const welcome=el('div','chat-welcome');welcome.id='chat-welcome';
    welcome.append(el('span','chat-wordmark','skew'),el('h1','','What would you like to get done?'),el('p','','Your accounts, agents and work. One conversation.'));
    const suggestions=el('div','chat-suggestions');
    for(const [label,prompt]of [['Get ETH for gas','Swap 2 USDC to ETH for gas on Arbitrum One.'],['Check my accounts','Check my balances, positions and recent orders.'],['Run a mining job','Show my mining jobs and help me run a bounded search.'],['Create a work task','Help me prepare a vendor comparison for my team.']])suggestions.append(button(label,'chat-suggestion',()=>{document.getElementById('chat-input').value=prompt;document.getElementById('chat-input').focus();}));
    suggestions.append(button('Clean business data','chat-suggestion',()=>officeWork()),button('Resume my work','chat-suggestion',()=>workProgress().catch(e=>C.notify(friendly(e),true))));
    const conversation=el('div','conversation');conversation.id='conversation';conversation.setAttribute('aria-live','polite');conversation.setAttribute('aria-label','Conversation');
    const composer=el('form','chat-composer');composer.id='chat-composer';
    const input=el('textarea');input.id='chat-input';input.rows=2;input.maxLength=1800;input.placeholder='Ask Skew to plan, check or do something…';input.setAttribute('aria-label','Message Skew');
    const bar=el('div','composer-bar'),hint=el('span','composer-hint','You approve spending. Skew tracks the rest.');
    const submit=button('Send','chat-send');submit.type='submit';submit.id='chat-send';
    const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
    if(Recognition){const voice=button('Dictate','button secondary',()=>{
      const recognition=new Recognition();recognition.lang=navigator.language||'en-US';recognition.interimResults=false;recognition.maxAlternatives=1;
      recognition.onresult=event=>{input.value=(input.value+' '+event.results[0][0].transcript).trim().slice(0,1800);input.focus();};
      recognition.onerror=()=>{C.notify('Voice input was unavailable. You can type instead.',true);};
      recognition.onend=()=>{voice.disabled=false;voice.textContent='Dictate';};
      voice.disabled=true;voice.textContent='Listening…';
      try{recognition.start();}catch{voice.disabled=false;voice.textContent='Dictate';}
    });voice.title='Browser voice input. Review the transcript before sending. This is not an Alexa device connection.';bar.append(voice);}
    bar.append(hint,submit);composer.append(input,bar);composer.onsubmit=e=>{e.preventDefault();send(input.value);};
    input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send(input.value);}};
    root.append(welcome,suggestions,conversation,composer);boundRequest();
  }
  async function connected() {
    init();const owner=C.state.identity?.address;
    if(owner&&owner!==historyOwner){
      if(historyOwner){document.getElementById('conversation').replaceChildren();conversationRevision++;}
      historyOwner=owner;const revision=conversationRevision;
      try{const h=await api('/assistant');
        if(C.state.identity?.address!==owner)return;
        turns=h.turns;
        if(revision===conversationRevision&&!document.getElementById('conversation').childElementCount){document.getElementById('conversation').replaceChildren();for(const t of turns){add('user',t.message);if(t.result)proposal(t.result,t.action_result);else add('assistant',t.status==='RUNNING'?'This request is pending. Check again before retrying.':'This request did not complete. No transaction was sent.');}}
      }catch(e){C.notify(friendly(e),true);}
    }
    try{const p=await api('/assistant/provider');if(p.status==='ORGANIZATION_DENY')add('assistant','Amazon Bedrock is connected in configuration, but AWS organization policy is blocking model access. Your direct workspace actions still work.');}catch{}
    boundShown=false;boundRequest();
    startTracking();
    if(pending){const value=pending;pending='';await send(value);}
  }
  window.AssistantConsole={render:()=>{init();document.body.dataset.view='overview';if(C.state.identity&&historyOwner!==C.state.identity.address)connected();},connected,
    fuel:()=>{C.setView('overview');init();add('assistant','Choose how much USDC to exchange for gas.',offerSwap(null));}};
  document.addEventListener('click',e=>{if(e.target.closest('[data-fuel]'))window.AssistantConsole.fuel();});
})();
