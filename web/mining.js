'use strict';
(() => {
  const {el, button: makeButton, notify} = window.MachineConsole;
  const button = (text, action) => makeButton(text, 'button secondary', action);
  let selected = null, identity = null, busy = false;
  const download = (value, name) => {
    const blob = new Blob([JSON.stringify(value, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  function render(root, record, {request, refresh, writable}) {
    const owner = record?.credential_namespace || (record?.read_only ? 'readonly' : 'local');
    if (identity !== owner) {selected = null; identity = owner; root.replaceChildren();}
    if (busy || root.querySelector('textarea:focus')) return;
    root.replaceChildren();
    const data = record?.mining;
    root.append(el('h2','','Useful work earns tokens'), el('p','task-hint','Find a better execution route. C++ searches; the contract verifies the frozen calculation.'));
    if (!writable() || !data) {
      root.append(el('div','task-empty','Sign in to create a work request or run the native solver. API keys stay in your environment.'));
      return;
    }
    const current = data.jobs.find(j => j.id === selected), layout = el('div','task-layout');
    const panel = el('section','task-editor'), side = el('aside','task-history'); layout.append(panel,side); root.append(layout);
    panel.append(el('div','status-chip','Draft workspace · no public mining deployment'),
      el('p','task-hint','Rewards come from the requester’s escrow. Creating or solving a draft does not pay a bond or move funds.'));
    if (!current) {
      panel.append(el('h3','','Create a work request'), el('p','task-hint','Start with a sample, or paste your own frozen job. Amounts are exact integer strings; costs use the final asset’s units.'));
      const form = el('form','task-form');
      const field = (name, title, value) => {const label=el('label','task-field',title),input=el('input');input.id='mining-'+name;input.value=value;input.required=true;label.append(input);form.append(label);return input;};
      const title=field('title','Work name',data.example.title), reward=field('reward','Reward · test USDC','0.01'), bond=field('bond','Participant bond · test USDC','0.001');
      const advanced=el('details'), label = el('label','task-field','Frozen pool snapshot · JSON'), text = el('textarea');
      advanced.append(el('summary','','Review or replace the sample snapshot'));
      text.id = 'mining-job-json'; text.rows = 13; text.value = JSON.stringify(data.example,null,2); label.append(text);advanced.append(label);form.append(advanced);
      form.append(el('p','task-hint','Sample inputs are synthetic. They do not describe live pools or expected investment returns.'));
      const save = button('Freeze work request',()=>{}); save.type='submit'; form.append(save);
      form.addEventListener('submit',async event=>{
        event.preventDefault(); if(busy)return;busy=true;save.disabled=true;
        try {
          const units = value => {if(!/^(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?$/.test(value))throw new Error('Use an exact amount with up to six decimals.');const [whole,decimals='']=value.split('.');return (BigInt(whole)*1000000n+BigInt(decimals.padEnd(6,'0'))).toString();};
          const raw=JSON.parse(text.value);raw.title=title.value;raw.reward_units=units(reward.value);raw.bond_units=units(bond.value);
          const job=await request('/mining/jobs',raw);selected=job.id;notify('Frozen draft saved. No funds moved.');
        }
        catch(error){notify(error.message,true);}finally{busy=false;await refresh();}
      });panel.append(form);
    } else {
      panel.append(el('h3','',current.snapshot.title), el('p','task-hint',current.snapshot.source_description));
      const facts = el('dl','ops-facts');
      for(const [key,value] of [['Reward · token base units',current.snapshot.reward_units],['Participant bond · token base units',current.snapshot.bond_units],['Confirmed earnings',current.confirmed_reward],['Frozen input SHA-256',current.input_sha256]]) {
        facts.append(el('dt','',key),el('dd','',value));
      }panel.append(facts);
      const run=button(data.enabled?'Run native solver':'Native solver unavailable',async()=>{
        if(busy)return;busy=true;run.disabled=true;
        try{await request('/mining/jobs/'+current.id+'/solve',{budget:70000});notify('Search complete. Review the candidate before any chain action.');}
        catch(error){notify(error.message,true);}finally{busy=false;await refresh();}
      });run.disabled=!data.enabled;panel.append(run,button('Download frozen job',()=>download(current.snapshot,current.id+'.json')));
      if(current.result){
        const r=current.result;
        panel.append(el('h3','',r.valid?'Verified native candidate':'No feasible candidate'),
          el('p','task-hint',r.valid?`Route ${r.path.map(i=>i+1).join(' → ')} · net ${r.net_output} · cost ${r.cost} · ${r.expansions} expansions`:'Try different conditions or a larger search budget.'),
          el('p','task-hint',r.search_complete?'Complete search of the allowed frozen paths. No language-model calls.':'Bounded search stopped early. Optimality is not established.'),
          button('Download candidate receipt',()=>download(r,current.id+'-result.json')));
      }
      panel.append(el('h3','','Participate from your environment'),
        el('p','task-hint','Use the open-source client with your CPU or your own agent’s candidate. It verifies the result and keeps the commitment salt on your machine. Public-chain publication and wallet approval are separate steps.'),
        el('code','','python scripts/mining_cli.py solve --job frozen-job.json'));
    }
    side.append(el('h3','','Your work requests'),button('New work request',async()=>{selected=null;await refresh();}));
    for(const job of data.jobs){
      const pick=button(job.snapshot.title,async()=>{selected=job.id;await refresh();});pick.classList.add('task-row');
      pick.append(el('small','',job.result?'Candidate ready · unpublished':'Frozen draft · not funded'));side.append(pick);
    }
  }
  window.MiningConsole = {render};
})();
