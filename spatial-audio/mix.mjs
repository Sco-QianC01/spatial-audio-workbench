import {trackLabel} from './automation.mjs';
const $=id=>document.getElementById(id);
const fixed=(value,digits=1)=>Number.isFinite(value)?value.toFixed(digits):'—';

export function initMix({getState,run,render,message}){
  let draft=null,planKey='',projectId=null,versionId=null,latestRunId=null,side='a',switchToken=0,switching=false;
  let refreshState=[];
  const audio=$('mix-audio');
  const selected=()=>getState().project?.mixRuns?.find(value=>value.id===versionId);
  function playback(next=side,position=null){
    if(switching)return;
    const entry=selected(),mono=$('mix-mono').checked;
    const url=entry?.urls[(next==='a'?'baseline':'candidate')+(mono?'Mono':'')];
    if(audio.getAttribute('src')===url){if(position!==null)audio.currentTime=position;return;}
    const token=++switchToken,at=position??(audio.currentTime||0),resume=!audio.paused;
    audio.pause();side=next;
    for(const key of ['a','b']){$(`mix-${key}`).classList.toggle('active',side===key);$(`mix-${key}`).setAttribute('aria-pressed',String(side===key));}
    if(!url){audio.removeAttribute('src');audio.load();return;}
    switching=true;
    for(const id of ['mix-a','mix-b','mix-versions','mix-mono'])$(id).disabled=true;
    const finished=()=>{
      if(token!==switchToken)return;
      switching=false;
      for(const id of ['mix-a','mix-b','mix-versions'])$(id).disabled=getState().busy;
      $('mix-mono').disabled=getState().busy||!entry?.urls.baselineMono;
    };
    audio.src=url;
    audio.onloadedmetadata=async()=>{
      if(token!==switchToken)return;
      audio.currentTime=Math.min(at,Math.max(0,audio.duration-.01));
      finished();
      if(resume)try{await audio.play();}catch(error){message(`比较试听未启动：${error.message}`,true);}
    };
    audio.onerror=()=>{if(token!==switchToken)return;finished();message('比较音频读取失败，请重新载入项目。',true);};
    audio.load();
  }
  function result(){
    const entry=selected();if(!entry)return;
    const match=entry.loudnessMatch;
    $('mix-match-status').textContent=$('mix-mono').checked?'单声道 · 保留比较增益':match.status==='matched'?'立体声响度已匹配 · 待听辨':'匹配不可用 · 待检查';
    const before=entry.metrics.baseline,after=entry.metrics.candidate;
    $('mix-result').textContent=`原版 ${fixed(before.integratedLufs)} LUFS / ${fixed(before.truePeakDbTP)} dBTP → 候选 ${fixed(after.integratedLufs)} LUFS / ${fixed(after.truePeakDbTP)} dBTP。${match.status==='matched'?`比较目标 ${fixed(match.targetLufs)} LUFS。`:match.reason}`;
    $('mix-download').href=entry.urls.package;$('mix-report').href=entry.urls.report;
    playback();
  }
  function diagnostics(analysis){
    $('mix-diagnostics').hidden=!analysis;
    const list=$('mix-diagnostic-list');list.replaceChildren();
    if(!analysis)return;
    const risk=analysis.original.stereoTime,windows=risk?.windows?.filter(value=>value.monoRisk)??[];
    const intervals=[];
    for(const value of windows){
      const last=intervals.at(-1);
      if(last&&last.endSeconds===value.startSeconds)last.endSeconds=value.endSeconds;
      else intervals.push({...value});
    }
    const add=text=>{const li=document.createElement('li');li.textContent=text;list.append(li);return li;};
    if(!risk)add('此旧测量没有分段相位数据，请重新分析。');
    else if(!windows.length)add('本次未检出达到阈值的局部抵消；仍需单声道试听。');
    else for(const value of intervals.slice(0,6)){
      const row=add(`${fixed(value.startSeconds)}–${fixed(value.endSeconds)} 秒 · 原版单声道抵消风险 `);
      const button=document.createElement('button');button.className='text-button';button.textContent='定位 A';
      button.disabled=getState().busy||!selected()||switching;
      button.addEventListener('click',()=>playback('a',value.startSeconds));row.append(button);
    }
    if(intervals.length>6)add(`另有 ${intervals.length-6} 个区间，完整数据见测量记录。`);
    for(const pair of analysis.relationships?.overlapPairs??[]){
      add(`${pair.ids.map(trackLabel).join(' / ')} · ${pair.bands.join('、')} · 同时段占比 ${fixed(pair.overlapFraction*100,0)}%（能量重叠候选）`);
    }
  }
  function parameters(project){
    const key=project?.id+JSON.stringify(project?.mixPlan??null);
    if(key===planKey)return;
    planKey=key;draft=project?.mixPlan?structuredClone(project.mixPlan):null;
    $('mix-proposal').hidden=!draft;
    $('mix-stem-parameters').replaceChildren();$('mix-reasons').replaceChildren();
    if(!draft)return;
    for(const [id,value] of [['mix-master-gain',draft.masterGainDb],['mix-low-shelf',draft.masterEq.lowShelfDb],['mix-high-shelf',draft.masterEq.highShelfDb]])$(id).value=value;
    for(const stem of draft.stems){
      const measured=project.analysis.stems.find(value=>value.id===stem.id),row=document.createElement('tr');
      for(const text of [trackLabel(stem.id),fixed(measured.rmsDbFS),`${fixed(measured.activityRatio*100,0)}%`]){
        const cell=document.createElement('td');cell.textContent=text;row.append(cell);
      }
      for(const [name,value,min,max,step] of [['gainDb',stem.gainDb,-6,3,.1],['highpassHz',stem.highpassHz,0,60,1],['frequencyHz',stem.eq.frequencyHz,100,10000,10],['eqGainDb',stem.eq.gainDb,-3,3,.1]]){
        const cell=document.createElement('td'),input=document.createElement('input');
        input.type='number';input.value=value;input.min=min;input.max=max;input.step=step;
        input.setAttribute('aria-label',`${trackLabel(stem.id)} ${name}`);
        input.addEventListener('input',()=>{
          const value=input.valueAsNumber;
          if(name==='frequencyHz')stem.eq.frequencyHz=value;
          else if(name==='eqGainDb')stem.eq.gainDb=value;
          else stem[name]=value;
        });
        cell.append(input);row.append(cell);
      }
      $('mix-stem-parameters').append(row);
    }
    const reasons=draft.reasons.map(value=>value.explanation).concat(draft.warnings);
    for(const text of reasons){const li=document.createElement('li');li.textContent=text;$('mix-reasons').append(li);}
  }
  function refresh(){
    const {project,runtime,busy}=getState(),analysis=project?.analysis;
    const next=[project?.id,analysis,project?.mixPlan,project?.mixRuns,project?.mixResultId,project?.mixBeforeScene,project?.contentPlan,project?.sceneHash,project?.state,project?.progress?.message,runtime?.projects,runtime?.mixing?.ready,busy,versionId,planKey,switching];
    if(next.length===refreshState.length&&next.every((value,i)=>value===refreshState[i]))return;
    refreshState=next;
    if(projectId!==project?.id){
      projectId=project?.id;versionId=null;latestRunId=null;planKey='';side='a';switchToken++;switching=false;$('mix-mono').checked=false;audio.pause();audio.removeAttribute('src');audio.load();
    }
    const references=(runtime?.projects??[]).filter(value=>value.id!==project?.id),reference=$('mix-reference').value;
    $('mix-reference').replaceChildren(new Option('不使用参考',''),...references.map(value=>new Option(value.input?.name??value.id,value.id)));
    if(references.some(value=>value.id===reference))$('mix-reference').value=reference;
    const allowed=project?.nativeStems?.schema==='ace-native-stems-v1'||project?.source?.workflow==='separated-stereo-v1';
    const ready=allowed&&runtime?.mixing?.ready===true;
    $('mix-analyze').disabled=busy||!ready;$('mix-plan').disabled=busy||!ready||!analysis;
    for(const id of ['mix-reference','mix-intent'])$(id).disabled=busy||!ready;
    $('mix-status').textContent=['analyzing','mixing'].includes(project?.state)?project.progress.message:!allowed?'先导入原生分轨，或明确完成本地自动分离。':analysis?'测量用于定位问题；不足一分钟不使用 LRA 判断动态，计量不代表听感。':project?.nativeStems?'原生分轨已校验，可分析并生成可编辑混音候选。':'本地估计分轨可分析；请重点试听串音、瞬态和相位，不能按轨道编号认定乐器。';
    $('mix-metrics').replaceChildren();
    if(analysis)for(const [label,value,unit] of [['整体响度',analysis.original.integratedLufs,'LUFS'],['真峰值',analysis.original.truePeakDbTP,'dBTP'],['响度范围',analysis.original.durationSeconds<60?'短片段不适用':analysis.original.loudnessRangeLu,analysis.original.durationSeconds<60?'':'LU'],['左右相关性',analysis.original.correlation,'']]){
      const box=document.createElement('div'),name=document.createElement('span'),number=document.createElement('strong');
      name.textContent=label;number.textContent=typeof value==='string'?value:`${fixed(value,unit?1:2)} ${unit}`;if(typeof value==='string')number.className='mix-metric-note';box.append(name,number);$('mix-metrics').append(box);
    }
    parameters(project);
    const runs=project?.mixRuns??[];
    $('mix-comparison').hidden=!runs.length;
    const latest=runs.at(-1)?.id??null;
    if(latest!==latestRunId||!runs.some(value=>value.id===versionId))versionId=runs.find(value=>value.id===project?.mixResultId)?.id??latest;
    latestRunId=latest;
    $('mix-versions').replaceChildren(...runs.map((value,index)=>new Option(`候选 ${index+1} · ${{preserve:'保留平衡',clarity:'低频清晰度',reference:'参考平衡'}[value.intent]??value.intent}`,value.id)));
    $('mix-versions').value=versionId??'';
    $('mix-process').disabled=busy||!draft;
    for(const input of $('mix-proposal').querySelectorAll('input'))input.disabled=busy;
    for(const id of ['mix-versions','mix-layout','mix-apply','mix-a','mix-b'])$(id).disabled=busy||!runs.length;
    const monoAvailable=Boolean(selected()?.urls.baselineMono&&selected()?.urls.candidateMono);
    $('mix-mono').disabled=busy||!monoAvailable||switching;
    if(!monoAvailable)$('mix-mono').checked=false;
    if(switching)for(const id of ['mix-a','mix-b','mix-versions'])$(id).disabled=true;
    $('mix-revert').disabled=busy||!project?.mixBeforeScene;
    const content=project?.contentPlan,note=$('mix-content-note');
    note.hidden=!content||content.sceneHash!==project?.sceneHash;
    if(!note.hidden){
      const names={'front-low':'低频前方','front-primary':'主体前方','front-secondary':'前方陪衬','surround-secondary':'侧后候选','unchanged':'保留','residual-carrier':'残差载体前方'};
      const expansion=content.layout==='spacious'?Number.isInteger(content.expandedObjectCount)&&content.expandedObjectCount>=0?`实际展开 ${content.expandedObjectCount} 个对象。${content.expandedObjectCount===0?'未找到可安全展开的活跃陪衬，未人为填充侧后或高度声道。':''}`:'实际展开数量未记录。':'';
      note.textContent=content.objects.map(item=>`${trackLabel(item.id)}：${names[item.role]??item.role}`).join('；')+'。'+expansion+'基于声学测量的候选，尚未完成乐器识别或人工听辨。';
    }
    if(runs.length)result();
    diagnostics(analysis);
  }
  $('mix-master-gain').addEventListener('input',()=>{if(draft)draft.masterGainDb=$('mix-master-gain').valueAsNumber;});
  $('mix-low-shelf').addEventListener('input',()=>{if(draft)draft.masterEq.lowShelfDb=$('mix-low-shelf').valueAsNumber;});
  $('mix-high-shelf').addEventListener('input',()=>{if(draft)draft.masterEq.highShelfDb=$('mix-high-shelf').valueAsNumber;});
  const safe=fn=>async()=>{try{await fn();}catch(error){message(error.message,true);}};
  $('mix-analyze').addEventListener('click',safe(()=>run('analyze',$('mix-reference').value?{referenceProjectId:$('mix-reference').value}:{})));
  $('mix-plan').addEventListener('click',safe(async()=>{planKey='';await run('plan',{intent:$('mix-intent').value});}));
  $('mix-process').addEventListener('click',safe(async()=>{
    if(!Array.from($('mix-proposal').querySelectorAll('input')).every(input=>input.reportValidity()))return;
    const value=await run('process',{plan:draft});
    versionId=value.mixResultId;refresh();
  }));
  $('mix-versions').addEventListener('change',()=>{versionId=$('mix-versions').value;audio.pause();audio.currentTime=0;refresh();});
  $('mix-mono').addEventListener('change',result);
  for(const key of ['a','b'])$(`mix-${key}`).addEventListener('click',()=>playback(key));
  $('mix-apply').addEventListener('click',safe(async()=>{await run('apply',{mixId:versionId,layout:$('mix-layout').value});await render();}));
  $('mix-revert').addEventListener('click',safe(()=>run('apply',{mixId:null})));
  return {refresh};
}
