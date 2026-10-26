const ACTIVE=new Set(['submitting','queued','running']);
const STATES={submitting:'正在提交',queued:'排队中',running:'生成中',succeeded:'生成完成',failed:'生成失败',submission_unknown:'提交结果待确认'};
const POLL_LIMIT=45*60*1000;
export function initOnlineLink({container,api,onImport}){
  const el=(tag,props={})=>Object.assign(document.createElement(tag),props);
  const field=(name,control)=>{const label=el('label',{textContent:name});label.htmlFor=control.id;const wrap=el('div',{className:'online-field'});wrap.append(label,control);return wrap;};
  const description=el('p',{className:'online-description',textContent:'可导入 ACE Base 原生生成分轨，也可在以下模型生成立体声后，点击“下载并本地分离”进入混音。分离声部来自成曲估计，与模型原生生成声部不同；生成完成不会自动切换路线。'});
  const connectForm=el('form',{className:'online-connect'});
  const username=el('input',{id:'online-username',type:'text',required:true,placeholder:'研究用户名',maxLength:120,autocomplete:'off'});
  const connect=el('button',{id:'online-connect',type:'submit',className:'secondary',textContent:'连接研究账号'});
  const connection=el('p',{className:'online-state',textContent:'读取研究账号状态…'});connection.setAttribute('role','status');
  connectForm.append(field('研究用户名',username),connect);
  const form=el('form',{className:'online-generation'});
  const model=el('select',{id:'online-model',required:true});
  const prompt=el('textarea',{id:'online-prompt',required:true,rows:3,maxLength:4000,placeholder:'描述音乐风格、乐器、情绪与结构'});
  const duration=el('input',{id:'online-duration',type:'number',value:'30',step:'1',required:true});
  const durationHint=el('small',{className:'hint'});
  const seed=el('input',{id:'online-seed',type:'number',value:'0',min:'0',max:'2147483647',step:'1',required:true});
  const score=el('textarea',{id:'online-score',rows:5,placeholder:'粘贴你提供的 ABC 乐谱',maxLength:60000});
  const example=el('button',{id:'online-score-example',type:'button',className:'secondary',textContent:'填入技术测试示例（非你的作品）'});
  const scoreField=field('YuE · 用户提供的 ABC 乐谱',score);scoreField.append(example,el('small',{className:'hint',textContent:'仅 YuE 需要乐谱。示例只用于接口测试；请核对或替换后再提交。'}));scoreField.hidden=true;
  const prepareStudio=el('input',{id:'online-prepare-studio',type:'checkbox',checked:false,disabled:true});
  const prepareLabel=el('label',{className:'checkbox'});prepareLabel.append(prepareStudio,el('span',{textContent:'生成完成后不自动切换制作路线'}));
  const generate=el('button',{id:'online-generate',type:'submit',textContent:'生成立体声参考'});
  const durationField=field('目标时长（秒）',duration);durationField.append(durationHint);
  const numbers=el('div',{className:'online-numbers'});numbers.append(durationField,field('Seed',seed));
  form.append(field('生成模型',model),field('音乐提示词',prompt),numbers,scoreField,prepareLabel,generate);
  const history=el('select',{id:'online-jobs'});const historyField=field('生成任务 / 历史结果',history);
  const status=el('p',{id:'online-generation-state',className:'online-state'});status.setAttribute('role','status');
  const refresh=el('button',{id:'online-refresh',type:'button',className:'secondary',textContent:'刷新任务状态'});
  const results=el('div',{className:'online-results'});
  const manual=el('form',{className:'online-import'});
  const input=el('input',{type:'url',id:'online-audio-url',placeholder:'http://10.100.16.56:8189/audio/…',required:true,autocomplete:'off',spellcheck:false});
  const button=el('button',{type:'submit',className:'secondary',textContent:'下载到本机并导入'});
  const localStatus=el('p',{className:'online-state'});localStatus.setAttribute('role','status');
  manual.append(field('或粘贴 Studio 音频下载地址',input),button);
  container.append(description,connectForm,connection,form,historyField,status,refresh,results,manual,localStatus);
  let config={connected:false,models:[]},jobs=[],selected='',externalBusy=false,localBusy=false,requestBusy=false,enabled=false,disposed=false,timer,checking=false;
  const imported=new Set(),pollStarted=new Map();
  const busy=()=>externalBusy||localBusy||requestBusy;
  const current=()=>jobs.find(job=>job.id===selected);
  const active=()=>jobs.find(job=>ACTIVE.has(job.status)||job.status==='submission_unknown');
  const settings=()=>config.models.find(item=>item.id===model.value);
  const post=(path,data)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  function modelSettings(){
    const item=settings();scoreField.hidden=!item?.requiresScore;score.required=!!item?.requiresScore;
    duration.min=String(item?.minDuration??1);duration.max=String(item?.maxDuration??600);
    duration.value=String(Math.min(Number(duration.max),Math.max(Number(duration.min),Number(duration.value)||30)));
    const modelDuration=item?.durationMode==='model'||item?.requiresScore;
    durationField.children[0].textContent=modelDuration?'时长（模型决定）':'目标时长（秒）';
    durationHint.textContent=modelDuration?(item?.requiresScore?'YuE 实际时长由乐谱和模型决定；提交值仅为参考，不控制实际生成长度。':'时长由模型决定；提交值不保证实际生成长度。'):'按模型允许范围设置目标时长。';
    controls();
  }
  function controls(){
    const locked=busy();for(const control of [username,connect,model,prompt,duration,seed,score,example,input,prepareStudio])control.disabled=locked;
    prepareStudio.disabled=true;prepareStudio.checked=false;
    duration.disabled=locked||settings()?.durationMode==='model'||!!settings()?.requiresScore;
    const owner=active()?.username||config.username;if(active()&&owner)username.value=owner;
    username.disabled=locked||!!active();connect.disabled=locked;generate.disabled=locked||!config.connected||!settings()||!!active();
    button.disabled=locked||!enabled;refresh.disabled=checking||requestBusy||disposed;
    for(const control of results.querySelectorAll('button'))control.disabled=locked||!enabled||imported.has(control.dataset.key);
  }
  function upsert(job){const index=jobs.findIndex(item=>item.id===job.id);if(index<0)jobs.unshift(job);else jobs[index]=job;}
  function render(){
    const previous=selected;history.replaceChildren();
    for(const job of jobs)history.append(el('option',{value:job.id,textContent:`${job.label||job.model} · ${STATES[job.status]||job.status} · ${job.prompt?.slice(0,32)||job.id.slice(0,8)}`}));
    selected=jobs.some(job=>job.id===previous)?previous:(active()?.id||jobs[0]?.id||'');history.value=selected;history.disabled=!jobs.length;
    const job=current();status.textContent=job?`${STATES[job.status]||job.status}${job.error?`：${job.error}`:''}${job.status==='submission_unknown'?'。请刷新确认同一任务；不要重复提交。':''}`:'尚无生成任务。';
    results.replaceChildren();
    if(job?.status==='succeeded')for(const [index,url] of (job.audioUrls||[]).entries()){
      let parsed;try{parsed=new URL(url);}catch{continue;}if(!['http:','https:'].includes(parsed.protocol))continue;
      const row=el('div',{className:'online-result'}),key=`${job.id}:${url}`;
      const download=el('a',{href:parsed.href,target:'_blank',rel:'noopener noreferrer',textContent:`下载结果 ${index+1}`});
      const separate=el('button',{type:'button',className:'secondary',textContent:'下载并本地分离'});separate.dataset.key=key;
      separate.addEventListener('click',()=>importAudio(url,{jobId:job.id,workflow:'separated-stereo-v1'},key));
      row.append(download,separate,el('small',{textContent:'立体声成曲；本地分离声部为估计结果，不标记为原生生成分轨。'}));
      results.append(row);
    }
    controls();
  }
  async function importAudio(url,options,key){
    if(disposed||busy()||!enabled||imported.has(key))return;localBusy=true;localStatus.textContent=options?'下载并执行本地分离中，请等待…':'下载并导入中，请等待…';controls();
    try{
      await onImport(url,options,key);
      localStatus.textContent=options?'已完成本地导入与分离；声部来源为立体声成曲估计。':'已导入本机。';
      if(key)imported.add(key);
    }
    catch(error){localStatus.textContent=`本地处理未完成：${error.message}`;}
    finally{localBusy=false;render();}
  }
  function schedule(){clearTimeout(timer);if(disposed)return;const job=active();if(!job||!ACTIVE.has(job.status))return;if(!pollStarted.has(job.id))pollStarted.set(job.id,Date.now());if(Date.now()-pollStarted.get(job.id)>=POLL_LIMIT){status.textContent+='；已暂停自动查询，请手动刷新。';return;}timer=setTimeout(()=>refreshJob(job.id),5000);}
  async function refreshJob(id){
    if(disposed||checking)return;checking=true;controls();
    try{upsert(await api(`/online/jobs/${id}`));render();}
    catch(error){status.textContent=`状态查询失败：${error.message}。保留任务 ${id.slice(0,8)}，请刷新确认。`;}
    finally{checking=false;controls();schedule();}
  }
  function apply(value){
    config=value;jobs=value.jobs||[];const chosen=model.value;model.replaceChildren();for(const item of value.models||[])model.append(el('option',{value:item.id,textContent:item.label}));
    if(value.models?.some(item=>item.id===chosen))model.value=chosen;
    if(value.username||active()?.username)username.value=value.username||active().username;connection.textContent=value.connected?`已连接：${value.username}`:'输入研究用户名建立连接；此处不接收或保存 Token。';
    modelSettings();render();schedule();
  }
  connectForm.addEventListener('submit',async event=>{event.preventDefault();if(busy())return;const owner=active()?.username||username.value.trim();requestBusy=true;controls();try{apply(await post('/online/connect',{username:owner}));}catch(error){connection.textContent=`连接失败：${error.message}`;}finally{requestBusy=false;controls();}});
  model.addEventListener('change',modelSettings);
  example.addEventListener('click',()=>{score.value='X:1\nT:Technical interface test - not user composition\nM:4/4\nL:1/4\nQ:1/4=90\nK:C\nC D E G | G E D C |';});
  history.addEventListener('change',()=>{selected=history.value;render();});
  async function refreshReadiness(){
    try{const value=await api('/online/status');enabled=value.downloadEnabled===true;controls();}
    catch{enabled=false;controls();localStatus.textContent='下载入口暂不可用，仍可导入本机已下载的音频。';}
  }
  refresh.addEventListener('click',async()=>{await refreshReadiness();const job=current();if(job)return refreshJob(job.id);try{apply(await api('/online/generation'));}catch(error){status.textContent=error.message;}});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy()||active()||!config.connected||!settings())return;
    const id=crypto.randomUUID().replaceAll('-','');const payload={requestId:id,model:model.value,prompt:prompt.value.trim(),duration:Number(duration.value),seed:Number(seed.value)};
    if(settings().requiresScore)payload.abcScore=score.value.trim();
    if(!payload.prompt||(settings().requiresScore&&!payload.abcScore)){status.textContent='请填写提示词及所需的 ABC 乐谱。';return;}
    const pending={id,...payload,username:config.username,label:settings().label,status:'submitting',audioUrls:[]};
    requestBusy=true;upsert(pending);selected=id;render();
    try{upsert(await post('/online/generate',payload));}
    catch(error){upsert({...pending,status:error.status>=400&&error.status<500?'failed':'submission_unknown',error:error.message});}
    finally{requestBusy=false;render();schedule();}
  });
  manual.addEventListener('submit',event=>{event.preventDefault();if(enabled)importAudio(input.value.trim());});
  try{const source=new URLSearchParams(location.hash.slice(1)).get('audio');if(source&&source.startsWith('http://10.100.16.56:8189/audio/')){input.value=source;const details=container.closest('details');if(details)details.open=true;}}catch{}
  controls();
  api('/online/generation').then(value=>{if(!disposed)apply(value);}).catch(error=>{connection.textContent=`生成入口暂不可用：${error.message}`;});
  void refreshReadiness();
  return {refresh(value){externalBusy=!!value;controls();},dispose(){disposed=true;clearTimeout(timer);}};
}
