'use strict';
(() => {
  const {el, button, notify} = window.MachineConsole;
  let selected = null, owner = null, busy = false, pending = null;
  const labels = {vendor_comparison:'Compare vendors', research_brief:'Research a topic', document_draft:'Draft a document', data_cleanup:'Clean a dataset', content_localization:'Translate and adapt'};
  const fields = {regions:'Regions', output_language:'Output language', output_format:'Deliverable', source_language:'Source language', comparison_fields:'Comparison criteria', required_fields:'Required columns', tone:'Tone', excluded_providers:'Excluded vendors'};
  const status = {READY_TO_PLAN:'Ready to plan', NEEDS_INPUT:'Needs details', EXPIRED:'Deadline passed', CANCELLED_UNSENT:'Cancelled'};
  const languages = [['','Use saved condition'],['en','English'],['ko','Korean'],['ja','Japanese'],['zh','Chinese'],['es','Spanish'],['fr','French'],['de','German'],['pt','Portuguese'],['id','Indonesian'],['vi','Vietnamese'],['th','Thai']];
  const id = () => crypto.randomUUID();
  const list = value => value.split(',').map(s => s.trim()).filter(Boolean);
  function input(form, name, label, value='', type='text') {
    const wrap = el('label', 'task-field', label), node = el(type === 'textarea' ? 'textarea' : 'input');
    node.name = name; node.id = 'task-' + name;
    if (type !== 'textarea') node.type = type;
    node.value = value; wrap.append(node); form.append(wrap); return node;
  }
  function select(form, name, label, choices, value) {
    const wrap = el('label','task-field',label), node = el('select'); node.name = name; node.id = 'task-' + name;
    choices.forEach(([val, title]) => {const opt = el('option','',title); opt.value = val; node.append(opt);});
    if (value && !choices.some(([val]) => val === value)) {const opt = el('option','',value);opt.value=value;node.append(opt);}
    node.value = value; wrap.append(node); form.append(wrap); return node;
  }
  function render(root, record, {request, refresh, writable}) {
    const identity = record?.credential_namespace || (record?.read_only ? 'readonly' : 'local');
    if (identity !== owner) {selected = null; pending = null; owner = identity; root.replaceChildren();}
    if (root.querySelector('form[data-editing="true"]') || busy) return;
    root.replaceChildren();
    const data = record?.tasks;
    if (!writable() || !data) {
      root.append(el('div','task-empty','Sign in to save work, budgets and confirmed preferences in your workspace.'));
      return;
    }
    const task = data.tasks.find(t => t.id === selected);
    if (selected && !task) selected = null;
    const layout = el('div','task-layout'), editor = el('section','task-editor'), side = el('aside','task-history');
    editor.append(el('h2','',task ? task.brief.title : 'What needs doing?'), el('p','task-hint','Save a clear brief first. Review the plan and any purchase before execution.'));
    const form = el('form','task-form'); form.dataset.editing = 'false';
    const draft = task?.brief, cond = draft?.explicit_constraints || {};
    const kind = select(form,'kind','Work type',Object.entries(labels),draft?.kind || 'vendor_comparison');
    const title = input(form,'title','Task name',draft?.title || ''); title.required = true; title.maxLength = 160;
    const instructions = input(form,'instructions','Brief',draft?.instructions || '', 'textarea'); instructions.required = true; instructions.maxLength = 12000;
    instructions.placeholder = data.templates.find(t => t.kind === kind.value)?.example || '';
    const pair = el('div','task-pair'); form.append(pair);
    const budget = input(pair,'budget','Maximum cost · USD',draft?.budget.maximum || '10.00'); budget.required = true; budget.inputMode = 'decimal'; budget.pattern = '(0|[1-9][0-9]{0,4})(\\.[0-9]{1,2})?';
    const deadline = input(pair,'deadline','Deadline · your local time','', 'datetime-local');
    if (draft?.deadline_at) {
      const d = new Date(draft.deadline_at * 1000); deadline.value = new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,16);
    }
    const prefs = select(form,'preference','Reuse confirmed conditions', [['','No saved conditions'],['last','Last confirmed for this work type'], ...data.preferences.filter(p=>p.status==='CONFIRMED' && p.kind===kind.value).map(p=>[p.id,p.name+' · v'+p.version])], draft?.preference_id || '');
    const details = el('div','task-pair'); form.append(details);
    const language = select(details,'output_language','Output language',languages,cond.output_language || (draft ? '' : 'en'));
    const format = select(details,'output_format','Deliverable',[['','Use saved condition'], ...data.templates.find(t=>t.kind===kind.value).formats.map(v=>[v,v.charAt(0).toUpperCase()+v.slice(1)])],cond.output_format || (draft ? '' : 'table'));
    const source = select(form,'source_language','Source language',languages,cond.source_language || ''); source.parentElement.hidden = kind.value !== 'content_localization';
    const columns = input(form,'criteria','Criteria or columns · comma separated',(cond.comparison_fields || cond.required_fields || []).join(', '));
    columns.placeholder = 'Price, delivery time, support'; columns.parentElement.hidden = !['vendor_comparison','data_cleanup'].includes(kind.value);
    const extras = el('details','task-extra'); extras.append(el('summary','','More conditions')); form.append(extras);
    const regions = input(extras,'regions','Regions · optional, comma separated',(cond.regions || []).join(', ')); regions.placeholder = 'SG, JP, KR';
    const tone = select(extras,'tone','Tone',[['','Unspecified'],['plain','Plain'],['formal','Formal'],['friendly','Friendly']],cond.tone || '');
    const excluded = input(extras,'excluded_providers','Excluded vendors · comma separated',(cond.excluded_providers || []).join(', '));
    const connections = el('fieldset','task-connections'); connections.append(el('legend','','Connected APIs · optional'));
    (record.connections || []).filter(c=>c.status!=='DISCONNECTED').forEach(c=>{
      const label = el('label','task-check'), check = el('input'); check.type='checkbox'; check.value=c.id; check.name='connection'; check.checked=!!draft?.connection_ids.includes(c.id); label.append(check,el('span','',c.name)); connections.append(label);
    });
    if (connections.childElementCount>1) extras.append(connections);
    kind.addEventListener('change',()=>{
      instructions.placeholder = data.templates.find(t=>t.kind===kind.value).example;
      format.replaceChildren(el('option','','Use saved condition')); format.firstChild.value='';
      data.templates.find(t=>t.kind===kind.value).formats.forEach(v=>{const opt=el('option','',v);opt.value=v;format.append(opt);});
      format.value=data.templates.find(t=>t.kind===kind.value).formats[0]; prefs.value='';
      [...prefs.options].slice(2).forEach(o=>o.remove()); data.preferences.filter(p=>p.status==='CONFIRMED'&&p.kind===kind.value).forEach(p=>{const o=el('option','',p.name+' · v'+p.version);o.value=p.id;prefs.append(o);});
      source.parentElement.hidden=kind.value!=='content_localization'; columns.parentElement.hidden=!['vendor_comparison','data_cleanup'].includes(kind.value);
    });
    prefs.addEventListener('change',()=>{if(prefs.value){[language,format,source,columns,regions,tone,excluded].forEach(node=>node.value='');}});
    form.addEventListener('input',()=>{form.dataset.editing='true'; pending=null;});
    form.addEventListener('change',()=>{form.dataset.editing='true'; pending=null;});
    const actions=el('div','task-actions'), save=el('button','button primary',task?'Save revision':'Save task'); save.type='submit';
    const discard=button(task?'Discard edits':'Clear','button secondary',()=>{form.dataset.editing='false';selected=null;pending=null;render(root,record,{request,refresh,writable});});
    actions.append(save,discard);form.append(actions);
    form.addEventListener('submit',async(event)=>{
      event.preventDefault(); if(busy)return;
      const constraints={}; [language,format,tone].forEach(node=>{if(node.value)constraints[node.name]=node.value;});
      if(kind.value==='content_localization'&&source.value)constraints.source_language=source.value;
      if(columns.value&&['vendor_comparison','data_cleanup'].includes(kind.value))constraints[kind.value==='vendor_comparison'?'comparison_fields':'required_fields']=list(columns.value);
      if(regions.value)constraints.regions=list(regions.value); if(excluded.value)constraints.excluded_providers=list(excluded.value);
      const raw={kind:kind.value,title:title.value,instructions:instructions.value,budget:{currency:'USD',maximum:budget.value}, deadline_at:deadline.value?Math.floor(new Date(deadline.value).getTime()/1000):null,constraints,preference_id:prefs.value||null,connection_ids:[...connections.querySelectorAll('input:checked')].map(c=>c.value)};
      pending ||= task?{path:'/tasks/'+task.id+'/revise',body:{request_id:id(),expected_revision:task.revision,draft:raw}}:{path:'/tasks',body:{...raw,request_id:id()}};
      busy=true;save.disabled=true;
      try{const result=await request(pending.path,pending.body);selected=result.id;pending=null;form.dataset.editing='false';notify('Task saved. No purchase or execution has been authorized.');}
      catch(error){notify(error.message,true);}
      finally{busy=false;save.disabled=false;await refresh();}
    });
    editor.append(form,el('p','task-hint','Your maximum is a planning limit. This step does not charge an account.'));
    if(task){
      const detail=el('section','task-current');detail.append(el('span','tag',status[task.status]),el('p','task-hint','Revision '+task.revision+' · '+new Date(task.updated_at*1000).toLocaleString()));
      if(task.missing_information.length)detail.append(el('p','task-missing','Add: '+task.missing_information.map(f=>fields[f]||({confirmed_preferences:'confirmed conditions (none available or no longer valid)',active_connections:'an active connection'}[f]||f)).join(', ')));
      const effective=el('dl','task-effective');Object.entries(task.brief.constraints).forEach(([key,value])=>effective.append(el('dt','',fields[key]||key),el('dd','',Array.isArray(value)?value.join(', '):value)));detail.append(effective);
      if(task.status==='READY_TO_PLAN'){
        const remember=el('details','task-extra');remember.append(el('summary','','Save conditions for next time'));
        const rf=el('form'); const name=input(rf,'preference_name','Preference name',labels[task.brief.kind]);
        const choices=el('fieldset','task-connections');choices.append(el('legend','','Choose what to remember'));
        Object.keys(task.brief.constraints).forEach(key=>{const l=el('label','task-check'),c=el('input');c.type='checkbox';c.value=key;c.checked=true;l.append(c,el('span','',fields[key]||key));choices.append(l);});rf.append(choices,el('p','task-hint','Valid for 30 days. Budget, approvals and API permissions are never remembered.'));
        const confirm=el('button','button secondary','Confirm selected conditions');confirm.type='submit';rf.append(confirm);
        rf.addEventListener('submit',async e=>{e.preventDefault();if(busy)return;busy=true;confirm.disabled=true;try{await request('/tasks/'+task.id+'/remember',{request_id:id(),expected_revision:task.revision,name:name.value,fields:[...choices.querySelectorAll('input:checked')].map(n=>n.value),expires_at:Math.floor(Date.now()/1000)+30*86400});notify('Conditions confirmed for this work type.');}catch(error){notify(error.message,true);}finally{busy=false;await refresh();}});remember.append(rf);detail.append(remember);
      }
      if(!['CANCELLED_UNSENT'].includes(task.status))detail.append(button('Cancel unsent task','button secondary',async()=>{if(busy)return;busy=true;try{await request('/tasks/'+task.id+'/cancel',{request_id:id(),expected_revision:task.revision});notify('Unsent task cancelled.');}catch(e){notify(e.message,true);}finally{busy=false;form.dataset.editing='false';await refresh();}}));
      const receipt=el('details','task-extra');receipt.append(el('summary','','Revision record'),el('code','',task.brief_hash));detail.append(receipt);editor.append(detail);
      if(task.status==='CANCELLED_UNSENT'){form.querySelectorAll('input,select,textarea,button').forEach(n=>n.disabled=true);discard.disabled=false;}
    }
    side.append(el('h2','','Your work'),button('New task','button secondary',()=>{selected=null;pending=null;form.dataset.editing='false';render(root,record,{request,refresh,writable});}));
    data.tasks.forEach(t=>{const row=button('','task-row',()=>{selected=t.id;pending=null;form.dataset.editing='false';render(root,record,{request,refresh,writable});});row.append(el('strong','',t.brief.title),el('span','',status[t.status]+' · $'+t.brief.budget.maximum));row.setAttribute('aria-pressed',String(t.id===selected));side.append(row);});
    if(!data.tasks.length)side.append(el('p','task-hint','Vendor shortlists. Customer proposals. Meeting briefs. Start with one clear task.'));
    const memories=el('section','task-memories');memories.append(el('h3','','Confirmed conditions'));
    data.preferences.filter(p=>p.status==='CONFIRMED').forEach(p=>{const row=el('div','task-memory');row.append(el('strong','',p.name),el('small','',labels[p.kind]+' · v'+p.version),button('Forget','quiet',async()=>{if(busy)return;busy=true;try{await request('/task-preferences/'+p.id+'/revoke',{});notify('Conditions revoked.');}catch(e){notify(e.message,true);}finally{busy=false;await refresh();}}));memories.append(row);});
    if(memories.childElementCount===1)memories.append(el('p','task-hint','Only conditions you confirm appear here.'));
    side.append(memories); layout.append(editor,side);root.append(layout);
  }
  window.TasksConsole={render};
})();
