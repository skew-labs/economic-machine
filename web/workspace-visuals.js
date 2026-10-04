/* Visual navigation and observed state. This module never signs or submits. */
(() => {
  'use strict';
  const C=window.MachineConsole,{el,button}=C, prefix=C.API_PREFIX||'';
  const mark=name=>{const img=el('img','tool-mark');img.src=prefix+'/assets/app-'+name+'.svg?v=visual-1';img.alt='';img.width=44;img.height=44;return img;};
  const token=()=>{const img=el('img','token-mark');img.src=prefix+'/assets/skew-token.svg?v=visual-1';img.alt='SKEW token';img.width=80;img.height=80;return img;};
  function illustration(kind){
    const art=el('span','tool-art '+kind);art.setAttribute('aria-hidden','true');
    if(kind==='fuel'){art.append(el('span','currency usdc','USDC'),el('span','flow-arrow','→'),el('span','currency eth','ETH'));}
    else if(kind==='mining'){const cluster=el('span','node-cluster');for(let i=0;i<6;i++)cluster.append(el('i'));art.append(cluster,el('span','flow-arrow','→'),token());}
    else if(kind==='data-pass'){const doc=el('span','license-paper');doc.append(el('b','','DataPass'),el('i'),el('i'),el('span','license-seal','✓'));art.append(doc);}
    else{const plugs=el('span','connection-orbit');for(const name of ['Wallet','AI','API'])plugs.append(el('span','',name));art.append(plugs);}
    return art;
  }
  function launcher(){
    const section=el('section','work-launcher');section.setAttribute('aria-label','Workspace tools');
    for(const [key,title,description,fn]of [
      ['data-pass','Buy data','Compare access. Receive a licensed report.',()=>C.setView('data')],
      ['fuel','Get gas','Swap USDC to ETH with your wallet.',()=>window.AssistantConsole.fuel()],
      ['mining','Explore mining','Search, verify and prepare useful work.',()=>C.setView('mining')],
      ['engine','Connect an agent','Your APIs, with a shared spending limit.',()=>C.setView('connections')]
    ]){
      const action=button('','work-tool',fn);action.dataset.tool=key;
      const top=el('span','work-tool-top');top.append(mark(key),el('span','launch-arrow','↗'));
      action.append(top,illustration(key),el('strong','',title),el('span','work-tool-description',description));section.append(action);
    }return section;
  }
  function observe(root,record){
    if(!root||document.getElementById('conversation')?.children.length)return;
    let bar=root.querySelector('.workspace-path');if(!bar){bar=el('div','workspace-path');root.append(bar);}
    bar.replaceChildren();
    const live=record&&!record.read_only&&C.state.identity;
    const connections=live?(record.connections||[]).length:0,policies=live?(record.control?.policies||[]).length:0;
    const states=[['Connect',connections>0,'connections'],['Set limits',policies>0,'agents'],['Run a task',false,'tasks'],['Track delivery',false,'activity']];
    bar.setAttribute('aria-label','Workspace setup');
    for(const [title,done,view]of states){const b=button('','path-step',()=>C.setView(view));b.append(el('span','step-dot',done?'✓':String(states.findIndex(v=>v[0]===title)+1)),el('span','',title));b.dataset.complete=String(Boolean(done));bar.append(b);}
  }
  const steps=[
    ['Frozen job','Choose the exact problem and a search limit. Your local worker keeps the same input for replay.','mining'],
    ['Search','C++ evaluates candidate routes. The search limit controls work; a candidate is not a token reward.','engine'],
    ['Commit & verify','Seal your result, reveal it after the deadline and let the contract check the calculation.','mining'],
    ['Publish DataPass','A licensed, eligible release ties the accepted result to a reusable data product.','data-pass'],
    ['Claim SKEW','The accepted winner can claim the protocol reward after publication. A local run does not mint tokens.','token']
  ];
  function mining(root,record,{tryWorker}){
    const layout=el('section','mining-hub'),workspace=el('div','mining-workbench');
    const head=el('div','mining-identity');head.append(mark('mining'));
    const title=el('div');title.append(el('h2','','Make work worth keeping.'),el('p','','Search a solution. Verify it. Turn it into data.'));head.append(title);workspace.append(head);
    const process=el('div','mining-process'),detail=el('div','mining-step-detail');detail.setAttribute('aria-live','polite');
    process.setAttribute('role','group');process.setAttribute('aria-label','Explore the mining process');
    let active=0;
    const show=i=>{active=i;for(const[b,n]of [...process.children].map((b,n)=>[b,n]))b.setAttribute('aria-pressed',String(n===active));
      detail.replaceChildren(el('span','process-number',String(i+1).padStart(2,'0')),el('div','process-explanation'));
      detail.lastChild.append(el('h3','',steps[i][0]),el('p','',steps[i][1]));};
    steps.forEach(([label,,icon],i)=>{const b=button('','mining-step',()=>show(i));b.append(icon==='token'?token():mark(icon),el('span','',label));process.append(b);});
    workspace.append(process,detail);show(0);
    const operations=el('div','mining-controls');operations.append(button('Open search worker','button primary',tryWorker),button('View data products','button secondary',()=>C.setView('data')));workspace.append(operations);
    const observed=el('div','mining-observed'),isLive=record&&!record.read_only&&C.state.identity;
    const jobs=isLive?record.mining?.jobs:undefined;
    for(const [name,value]of [['Saved jobs',jobs?String(jobs.length):'—'],['Local candidates',jobs?String(jobs.filter(j=>j.result?.valid).length):'—'],['Rewards claimed','Not observed']]){const item=el('div');item.append(el('span','',name),el('strong','',value));observed.append(item);}workspace.append(observed);
    const note=el('p','visual-note',isLive?'Counts are saved workspace records. Chain publication and rewards are separate.':'Connect your wallet to see your saved jobs and run the search worker.');workspace.append(note);
    const market=el('aside');layout.append(workspace,market);root.append(layout);window.SkewMarket.mount(market,{compact:true});
    const economics=el('div','mining-economics');
    for(const [value,label]of [['1 SKEW','Per accepted, published job'],['160,000','Maximum token supply'],['0','Premine in the contract design']]){const n=el('div');n.append(el('strong','',value),el('span','',label));economics.append(n);}root.append(economics);
    root.append(el('p','visual-note','Protocol design, not circulating supply or confirmed earnings. Rights admission and a finalized release are required.'));
  }
  function routeResult(root,result){
    if(!result?.valid||!Array.isArray(result.path))return;
    const card=el('section','candidate-visual'),header=el('div','candidate-heading');header.append(el('h3','','Verified route'),el('span','result-badge','Local calculation'));card.append(header);
    const path=el('ol','route-path');path.setAttribute('aria-label','Selected route edges');
    for(const edge of result.path){const item=el('li');item.append(el('span','route-point','✓'),el('strong','','Edge '+(edge+1)));path.append(item);}card.append(path);
    const stats=el('div','mining-observed');for(const[label,value]of [['Net output',result.net_output],['Execution cost',result.cost],['Paths explored',result.expansions]]){const cell=el('div');cell.append(el('span','',label),el('strong','',String(value)));stats.append(cell);}card.append(stats,el('p','visual-note','Amounts use the frozen job’s base units. No chain transaction was sent.'));root.append(card);
  }
  function solution(root,r){
    const card=el('div','solution-quality'),label=el('div');label.append(el('strong','',(Number(r.quality_bps)/100).toFixed(2)+'%'),el('span','','of total edge weight'));
    const meter=el('meter');meter.min=0;meter.max=10000;meter.value=Math.max(0,Math.min(10000,Number(r.quality_bps)));meter.setAttribute('aria-label','Verified cut score as a share of total edge weight');card.append(label,meter,el('p','visual-note',r.edge_visits+' edge visits · exact score '+r.score+' / '+r.total_weight));root.append(card);
  }
  function dataHeading(root){const heading=el('div','data-intro');heading.append(illustration('data-pass'));const copy=el('div');copy.append(el('h2','','Data you can put to work.'),el('p','','Choose a release. Review the rights. Verify delivery.'));heading.append(copy);root.append(heading);}
  function releaseCard(root,item){
    const ticket=el('div','data-ticket'),identity=el('div','data-ticket-identity');identity.append(mark('atlas'),el('strong','','Atlas release'));ticket.append(identity);
    const metrics=el('div','data-coverage');for(const [value,label]of [[item.coverage.rows,'observations'],[item.coverage.regions.length,'APAC regions']]){const c=el('div');c.append(el('strong','',String(value)),el('span','',label));metrics.append(c);}ticket.append(metrics);
    const regions=el('div','region-chips');for(const region of item.coverage.regions.slice(0,8))regions.append(el('span','',region));ticket.append(regions);root.prepend(ticket);
  }
  window.WorkspaceVisuals={launcher,observe,mining,routeResult,solution,dataHeading,releaseCard,mark};
  for(const [selector,icon]of [['[data-view=mining]','mining'],['[data-view=data]','data-pass'],['[data-fuel]','fuel']]){
    const target=document.querySelector('.sidebar '+selector);if(target){target.querySelector('svg')?.replaceWith(mark(icon));}
  }
})();
