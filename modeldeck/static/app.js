'use strict';
const $ = id => document.getElementById(id);
let state = null, selected = null, review = null, busy = false;
const text = (tag, value, cls) => { const n = document.createElement(tag); n.textContent = value; if(cls) n.className = cls; return n; };
const fmt = (v, digits=1) => Number.isFinite(v) ? v.toFixed(digits) : '—';
const badge = status => text('span', status, 'badge ' + (status || 'unknown').replaceAll(' ', '-'));
const modelLabel = s => s.models?.map(m => m.id).join(', ') || 'No live model catalogue';
function renderHosts(){
  $('hosts').replaceChildren(...state.hosts.map(h=>{
    const a=text('article','','host');
    const top=text('div','','host-top'); top.append(text('h2',h.id),badge(h.status)); a.append(top);
    const ids=state.models.filter(s=>h.service_ids?.includes(s.id));
    if(h.gpu_total_gib != null){
      a.append(text('p','RTX 4080 · GPU memory'),text('p',`${fmt(h.gpu_used_gib)} / ${fmt(h.gpu_total_gib)} GiB`,'memory'));
    }else{
      a.append(text('p',h.memory_kind || 'Memory'),text('p',h.memory_total_gib == null || h.memory_available_gib == null ? 'Unavailable' : `${fmt(h.memory_total_gib-h.memory_available_gib)} / ${fmt(h.memory_total_gib)} GiB`,'memory'));
    }
    const names=ids.filter(s=>s.status==='online'&&s.models?.length).flatMap(s=>s.loaded_models!==undefined ? (s.loaded_models||[]) : s.models.map(m=>m.id));
    a.append(text('p',names.slice(0,2).join(' · ')+(names.length>2?` · +${names.length-2} more`:'') || (h.status==='offline' ? 'Host unreachable. Retrying automatically.' : 'No resident model confirmed.'),'host-model'));
    const live=ids.filter(s=>s.status==='online');
    const measurements=ids.find(s=>Number.isFinite(s.tok_s));
    a.append(text('p',`${live.length} service${live.length===1?'':'s'} responding · ${measurements?fmt(measurements.tok_s)+' tok/s':'speed unavailable'}`));
    const load=ids.find(s=>s.status==='online'&&Number.isFinite(s.running));
    a.append(text('p',`${fmt(load?.running,0)} running / ${fmt(load?.queue,0)} queued`));
    return a;
  }));
}
function inspect(service,model){
  selected=service.id+'|'+(model?.id||'');
  const models=model?[model]:[];
  const d=$('detail');d.replaceChildren(text('h3',model?.id||service.id));
  const dl=text('dl','');
  const facts=[['Service',service.id],['Endpoint',service.endpoint],['Role',service.kind],['Context',models.map(m=>m.context_length?Number(m.context_length).toLocaleString()+' tokens':'not reported').join(', ')||'Unavailable'],['Format',models.map(m=>m.format||'not reported').join(', ')||'Unavailable'],['Model VRAM',models.map(m=>m.gpu_gib != null ? fmt(m.gpu_gib)+' GiB':'not reported').join(', ')||'Unavailable'],['Observed',service.observed_at||'Unavailable']];
  if(service.loaded_models!==undefined) facts.push(['Resident models',service.loaded_models?.join(', ')||'None confirmed by /api/ps']);
  for(const [k,v] of facts)dl.append(text('dt',k),text('dd',v||'Unavailable'));
  d.append(dl);
  d.append(text('p',service.catalog_stale?`Cached catalogue, last seen ${service.last_seen}. This is not a live model claim.`:'Read-only endpoint evidence. Model listing is not a correctness or speed benchmark.'));
  if(service.error)d.append(text('p',service.error));
  renderModels();
}
function renderModels(){
  const q=$('search').value.toLowerCase();
  const rows=state.models.flatMap(s=>(s.models?.length?s.models:[null]).map(m=>({s,m}))).filter(({s,m})=>[s.id,s.host,s.kind,m?.id].join(' ').toLowerCase().includes(q));
  $('model-count').textContent=`${state.models.length} services · ${rows.length} rows`;
  $('models').replaceChildren(...rows.map(({s,m})=>{
    const tr=document.createElement('tr'); if(s.id+'|'+(m?.id||'')===selected)tr.className='selected';
    const td=document.createElement('td'), b=text('button','','row-button');b.type='button';
    b.append(text('span',m?.id||'No live model catalogue','model-name'),text('span',s.id + (s.catalog_stale?' · cached':''),'service-name'));
    b.onclick=()=>inspect(s,m);td.append(b);tr.append(td,text('td',s.host));
    const status=s.status==='online'&&s.loaded_models!==undefined&&!s.loaded_models?.includes(m?.id)?'catalogued':s.status;
    const stat=document.createElement('td');stat.append(badge(status));tr.append(stat);
    tr.append(text('td',status==='online'&&s.tok_s!=null?fmt(s.tok_s)+' tok/s':'—'),text('td',status==='online'?`${fmt(s.running,0)} / ${fmt(s.queue,0)}`:'— / —'));
    tr.onclick=e=>{if(!e.target.closest('button'))inspect(s,m);};return tr;
  }));
  if(!rows.length){const tr=document.createElement('tr'),td=text('td','No models match this filter.');td.colSpan=5;tr.append(td);$('models').append(tr);}
}
function renderSwap(){
  const c=state.cluster;
  $('cluster-badge').replaceWith(Object.assign(badge(c.online?'online':'offline'),{id:'cluster-badge'}));
  $('current').textContent=c.current||'Not available';
  $('controller').textContent=(c.profiles_fresh?'Live registry':'Unavailable registry')+' · '+(c.swap_state||'unknown state');
  const previous=$('profile').value;
  $('profile').replaceChildren(text('option',c.profiles?.length?'Select a target profile':'No live profiles available'));
  $('profile').firstChild.value='';
  for(const p of c.profiles||[]){const o=text('option',p.name+(p.id===c.current?' · current':'')+(!c.profiles_fresh?' · cached':''));o.value=p.id;o.disabled=p.id===c.current;$('profile').append(o);}
  if([...$('profile').options].some(o=>o.value===previous))$('profile').value=previous;
  const pending=c.pending_job&&!(c.action_job?.job===c.pending_job&&c.action_job?.terminal);
  const ready=c.online&&c.profiles_fresh&&c.gated_swap_available&&c.current&&c.swap_state==='idle'&&!state.stale&&!state.collector_error&&!pending;
  $('review-swap').disabled=!ready||!$('profile').value||busy;
  $('swap-reason').textContent=ready?'Two-step confirmation, then Jev safety gate, idle gate and rollback checks. Nothing swaps on refresh.':(!c.online?'The cluster is offline or no model answers. ':'')+(!c.profiles_fresh?'Live profiles are unavailable. ':'')+(!c.gated_swap_available?'The existing gated helper is not verified. ':'')+'Swap stays locked. Read-only checks retry automatically.';
  if(c.pending_job)$('swap-reason').textContent+=` Last submitted job: ${c.pending_job}. ${c.action_job?.state||'Outcome unknown'}; ${c.action_job?.result||'inspect the cluster journal before retrying'}.`;
  $('swaps').replaceChildren();
  if(!c.swaps?.length){$('swaps').append(text('p','No measured history is reachable while the cluster is offline. No timings or gate passes have been invented.','subtle'));return;}
  if(c.swaps_stale)$('swaps').append(text('p','Last observed history, not a fresh controller response.','subtle'));
  for(const s of [...c.swaps].reverse().slice(0,8)){
    const e=text('article','','swap-event'); e.append(text('strong',s.duration_s==null?'Duration not recorded':fmt(s.duration_s,0)+' seconds'),text('p',s.profile),text('p',`${s.status} · ${s.started_at||'time unknown'}`));
    const details=text('details','');details.append(text('summary','Gate evidence · '+s.source),text('pre',s.gates?JSON.stringify(s.gates,null,2):'No gate results recorded.'));e.append(details);$('swaps').append(e);
  }
}
function chart(id,keys,colors){
  const canvas=$(id),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  canvas.width=rect.width*ratio;canvas.height=140*ratio;const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
  const w=rect.width,h=140,pad=25,history=state.history||[];
  const values=history.flatMap(s=>keys.map(k=>s[k])).filter(Number.isFinite),max=values.length?Math.max(1,...values)*1.15:1;
  ctx.strokeStyle='#343a35';ctx.lineWidth=1;ctx.font='10px system-ui';ctx.fillStyle='#929c93';
  for(let i=0;i<3;i++){let y=10+(h-35)*i/2;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w,y);ctx.stroke();ctx.fillText(fmt(max*(1-i/2),max>10?0:1),0,y+4);}
  const minT=history[0]?.at||0,maxT=history.at(-1)?.at||minT+1;
  keys.forEach((key,index)=>{ctx.strokeStyle=colors[index];ctx.setLineDash(index?[4,3]:[]);ctx.lineWidth=1.8;ctx.beginPath();let pen=false,prev=0;
    for(const row of history){const v=row[key];if(!Number.isFinite(v)){pen=false;continue;}
      const x=pad+(w-pad-3)*(row.at-minT)/Math.max(1,maxT-minT),y=10+(h-35)*(1-v/max);
      if(!pen||row.at-prev>90)ctx.moveTo(x,y);else ctx.lineTo(x,y);pen=true;prev=row.at;
      if(history.length===1){ctx.moveTo(x-1,y);ctx.lineTo(x+1,y);}
    }ctx.stroke();});ctx.setLineDash([]);
  if(!values.length){ctx.fillStyle='#b2b9b0';ctx.fillText('No measured samples yet',pad+10,65);}
  ctx.fillStyle='#929c93';ctx.fillText(history.length?new Date(minT*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'—',pad,h-4);ctx.textAlign='right';ctx.fillText('now',w-2,h-4);
}
function renderCharts(){
  chart('decode-chart',['cluster_tok_s'],['#b9e5af']);chart('requests-chart',['running','queue'],['#b9e5af','#f0b39d']);chart('vram-chart',['gpu_used_gib'],['#c6cbd1']);
  const last=state.history?.at(-1)||{};
  $('decode-value').textContent=last.cluster_tok_s==null?'Unavailable':fmt(last.cluster_tok_s)+' tok/s';
  $('requests-value').textContent=last.running==null?'Unavailable':`${fmt(last.running,0)} running / ${fmt(last.queue,0)} queued`;
  $('vram-value').textContent=last.gpu_used_gib==null?'Unavailable':fmt(last.gpu_used_gib)+' GiB';
  $('history-range').textContent=`${state.history?.length||0} observed samples. Gaps mean missing measurements, not zero activity. Aggregate decode is not per-stream speed.`;
}
async function refresh(){
  try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw new Error('The local server did not return a sample.');state=await r.json();
    $('sample-state').textContent=state.loading?'Collecting first sample…':(state.stale?'Stale sample · ':'Sampled ')+new Date(state.sampled_at).toLocaleTimeString();
    $('notice').textContent=state.collector_error|| (state.stale?'The collector has not produced a fresh sample. Swap is locked.':!state.cluster.online&&!state.loading?'The cluster is offline. Other hosts remain visible. This app retries reads without restarting anything.':'');
    if(!state.loading){renderHosts();renderModels();renderSwap();renderCharts();}
  }catch(e){$('notice').textContent='The local monitor is unreachable. The view may be stale. Retrying in 10 seconds.';$('review-swap').disabled=true;}
}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-ModelDeck-CSRF':state.csrf},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw new Error(data.error||'Request rejected');return data;}
$('search').oninput=()=>state&&renderModels();$('refresh').onclick=refresh;$('profile').onchange=()=>state&&renderSwap();
$('review-swap').onclick=async()=>{if(busy)return;busy=true;try{review=await post('/api/swap/prepare',{profile:$('profile').value});$('confirm-warning').textContent=review.warning;$('confirm-phrase').textContent=review.confirmation;$('confirmation').value='';$('confirm-result').textContent='';$('confirm-dialog').showModal();$('confirmation').focus();}catch(e){$('notice').textContent=e.message;}finally{busy=false;renderSwap();}};
$('cancel-swap').onclick=()=>{$('confirm-dialog').close();review=null;};
$('confirm-form').onsubmit=async e=>{e.preventDefault();if(!review||busy)return;busy=true;$('commit-swap').disabled=true;try{const result=await post('/api/swap/commit',{ticket:review.ticket,confirmation:$('confirmation').value});review=null;$('confirm-dialog').close();$('notice').textContent=result.message+' Job: '+result.job;}catch(error){$('confirm-result').textContent=error.message;review=null;}finally{busy=false;$('commit-swap').disabled=false;refresh();}};
window.addEventListener('resize',()=>state&&!state.loading&&renderCharts());refresh();setInterval(refresh,10000);
