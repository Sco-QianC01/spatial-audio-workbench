import {initMix} from './mix.mjs';
import {createSceneView} from './scene-view.mjs';
import {STEREO_GEOMETRY,directionToVector as coords,stereoDirectionToVector as stereoCoords} from './coordinates.mjs';
import {sampleTrajectory} from './trajectory.mjs';
import {pointAt,upsertKeyframe,moveKeyframeTime,removeKeyframe,createAutomation,trackLabel,setTrackOrder} from './automation.mjs';
import {initOnlineLink} from './model-link.mjs';
const $=id=>document.getElementById(id);
const COLORS={drums:'#e39b86',bass:'#9dd0b6',other:'#a9bbd6',vocals:'#d2b3cc'};
const LAST_PROJECT_KEY='nr-spatial-last-project-v1';
let project=null,scene=null,selectedId=null,generation=0,revision=0,busy=false,editingEnd=false,runtime=null,pollTimer=null,playhead=0,dragging=null;
let mixControls;
let sceneView, automationControls, onlineControls, selectedTime=0, playbackFrame=0, lastFrame=0, playbackId=null;
const trajectoryCache=new WeakMap();
let drawFrame=0;
function scheduleDraw(){if(!drawFrame)drawFrame=requestAnimationFrame(()=>{drawFrame=0;draw();});}
function trajectoryFor(object,duration){
  const key=JSON.stringify([duration,object.azimuthDeg,object.elevationDeg,object.keyframes]),cached=trajectoryCache.get(object);
  if(cached?.key===key)return cached.samples;
  const samples=sampleTrajectory(object,duration);trajectoryCache.set(object,{key,samples});return samples;
}
const time=n=>`${Math.floor((n||0)/60)}:${String(Math.floor((n||0)%60)).padStart(2,'0')}`;
const clone=value=>structuredClone(value);
const wrap=n=>((n+180)%360+360)%360-180;
const angleDelta=(a,b)=>wrap(b-a);
const projectName=value=>`${value.nativeStems?'ACE 原生分轨':value.source?.workflow==='separated-stereo-v1'?'本地估计分轨':value.source?.generation?.label??value.input?.name??'音频'} · ${value.id.slice(0,8)}`;
const speakerLayout=()=>scene?.speakerLayout??'7.1.4';
const currentObject=()=>scene?.objects.find(o=>o.id===selectedId);
function message(text,error=false,result=null){
  if(!error&&result?.artifactWarnings?.length){
    text=`${result.artifacts?.surroundUrl?'扬声器通道可用。':'项目已载入。'} 导出校验提示：${result.artifactWarnings.join('；')}。请按提示重新处理候选或重新渲染，以恢复对应输出。`;error=true;
  }
  $('status').textContent=text;$('status').classList.toggle('error',error);
}
async function api(path,options={}){
  const response=await fetch(`/api/spatial${path}`,options);
  let body;try{body=await response.json();}catch{throw new Error(`本地服务返回无效数据（${response.status}）。`);}
  if(!response.ok)throw Object.assign(new Error(typeof body.error==='string'?body.error:body.error?.message??body.message??`请求失败（${response.status}）`),{status:response.status});
  return body;
}
function json(body){return {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
function setBusy(value){busy=value;renderStems();updateControls();draw();}
function updateControls(){
  const editable=Boolean(scene)&&!busy;
  $('file').disabled=busy;$('demo').disabled=busy||runtime?.demo?.available===false;
  $('saved-projects').disabled=busy||!runtime?.projects?.length;
  $('source-route').disabled=busy;
  $('separate').hidden=$('source-route').value!=='separated-stereo-v1';
  $('separate').disabled=busy||project?.state!=='imported'||runtime?.separation?.ready!==true;
  $('render').disabled=!editable;$('render-speakers').disabled=!editable;$('speaker-layout').disabled=!editable;
  const layout=speakerLayout(),channels=layout==='7.1.4'?12:8;
  $('speaker-layout').value=layout;
  for(const id of ['session-layout','stage-layout','output-layout'])$(id).textContent=layout;
  $('session-channels').textContent=`48 kHz / ${channels} ch`;$('output-channels').textContent=`${channels} 声道 WAV`;
  $('download-surround').textContent=`${layout} WAV ↓`;
  $('render-speakers').textContent=layout==='7.1.4'?'渲染棚内直出包':'仅渲染扬声器通道';
  $('download-studio-package').hidden=layout!=='7.1.4';
  $('speaker-routing').textContent=`${layout} 扬声器馈送：${layout==='7.1.4'?'L / R / C / LFE / Ls / Rs / Lrs / Rrs / Ltf / Rtf / Ltr / Rtr':'L / R / C / LFE / Ls / Rs / Tml / Tmr'}。`;
  for(const id of ['azimuth','elevation','width','gain','motion','reset','edit-start'])$(id).disabled=!editable;
  $('edit-end').disabled=!editable||!currentObject()?.keyframes?.length;
  updateTransport();
  $('add-keyframe').disabled=!editable;
  const frames=currentObject()?.keyframes??[],index=frames.findIndex(f=>Math.abs(f.timeSeconds-selectedTime)<1e-8);
  $('remove-keyframe').disabled=!editable||index<=0||index>=frames.length-1;
  $('keyframe-time').disabled=!editable||index<=0||index>=frames.length-1;
  onlineControls?.refresh(busy);
  $('input-badge').textContent=project?'已导入':'未导入';
  $('scene-state').textContent=scene?(revision?'有未渲染修改':'场景就绪'):'等待分轨';
  mixControls?.refresh();
}
function clearPreview(){
  if($('preview').getAttribute('src')){$('preview').pause();$('preview').removeAttribute('src');$('preview').load();}
  for(const format of ['binaural','foa','scene','package','quality','surround','surround-report','studio-package']){
    const node=$(`download-${format}`);node.removeAttribute('href');node.setAttribute('aria-disabled','true');
  }
  $('preview-label').textContent='等待渲染';$('output-state').textContent='未渲染';$('seek').value=0;$('time-current').textContent='0:00';playhead=0;
}
function artifacts(value){
  const links={'studio-package':value?.studioPackageUrl,binaural:value?.binauralUrl,foa:value?.foaUrl,scene:value?.sceneUrl,package:value?.packageUrl,quality:value?.binauralReportUrl,surround:value?.surroundUrl,'surround-report':value?.surroundReportUrl};
  for(const [key,url] of Object.entries(links)){
    const node=$(`download-${key}`);
    if(url){node.href=url;node.setAttribute('aria-disabled','false');}else{node.removeAttribute('href');node.setAttribute('aria-disabled','true');}
  }
  if(value?.binauralUrl){playbackId='preview';$('preview').src=value.binauralUrl;$('preview-label').textContent='HRTF · 双耳';$('output-state').textContent=value.binauralReportUrl?'已渲染 · 真峰已复测':'已保存 · 真峰待复测';}
  else if(value?.surroundUrl)$('output-state').textContent=`${speakerLayout()} 扬声器通道已导出`;
  updateControls();
}
function showAudioWarnings(value){
  let node=$('audio-warnings');
  if(!node){node=document.createElement('p');node.id='audio-warnings';node.className='hint';node.setAttribute('role','status');$('status').after(node);}
  const warnings=value?.audioWarnings??[];node.hidden=!warnings.length;
  node.textContent=warnings.length?`空间渲染听辨提示：${warnings.map(text=>text.replace(/\b(drums|bass|other|vocals)\b/g,trackLabel)).join('；')}。请结合原曲检查轨道定位与折叠听感。`:'';
}
function accept(value,{replaceScene=true}={}){
  project=value;showAudioWarnings(value);
  if(value.nativeStems||value.source?.workflow==='separated-stereo-v1'){$('source-route').value=value.nativeStems?'native-stems-first-v1':'separated-stereo-v1';sourceRouteChanged();}
  if(value.scene){setTrackOrder(value.scene.objects.map(o=>o.id));const palette=['#e39b86','#9dd0b6','#a9bbd6','#d2b3cc','#d1c393','#8dc8ca','#b4c29b','#c3afe0','#e3b18c','#9cbfdf','#c9d4cf','#d1a8ad'];value.scene.objects.forEach((o,i)=>{COLORS[o.id]=palette[i%palette.length];});}
  try{localStorage.setItem(LAST_PROJECT_KEY,value.id);}catch{}
  if(runtime){
    const entry={id:value.id,input:value.input,state:value.state,source:value.source},index=runtime.projects?.findIndex(p=>p.id===value.id)??-1;
    if(index<0)(runtime.projects??=[]).unshift(entry);else runtime.projects[index]=entry;
    projectOptions();
  }
  if(replaceScene&&value.scene){scene=clone(value.scene);if(!currentObject()?.keyframes?.some(f=>Math.abs(f.timeSeconds-selectedTime)<1e-8))selectedTime=0;if(!scene.objects.some(o=>o.id===selectedId))selectedId=scene.objects[0]?.id;revision=0;}
  $('input-name').textContent=projectName(value);$('input-name').title=value.input?.name??'';
  $('input-meta').textContent=`${time(value.input?.durationSeconds)} · ${value.input?.sampleRate??'—'} Hz · ${value.input?.channels??'—'} ch`;
  if(value.input?.audioUrl&&$('source-audio').getAttribute('src')!==value.input.audioUrl)$('source-audio').src=value.input.audioUrl;
  $('project-id').textContent=`项目 ${value.id}`;
  const previewDuration=$('preview').getAttribute('src')&&Number.isFinite($('preview').duration)?$('preview').duration:value.input?.durationSeconds;
  $('time-total').textContent=time(previewDuration);
  $('seek').max=previewDuration??1;
  $('trajectory-duration').textContent=`0:00 → ${time(value.input?.durationSeconds)}`;
  const fraction=Math.max(0,Math.min(1,value.progress?.fraction??(scene?1:0)));
  $('progress').value=fraction;$('progress-value').textContent=`${Math.round(fraction*100)}%`;
  renderStems();renderParameters();updateControls();draw();
  if(value.state==='error')throw new Error(value.error??value.progress?.message??'本地处理失败。');
}
function begin(){
  showAudioWarnings(null);generation++;clearTimeout(pollTimer);stopPlayhead();playbackId=null;for(const id of ['source-audio','stem-audio','mix-audio'])$(id).pause();clearPreview();
  $('source-audio').removeAttribute('src');$('source-audio').load();
  $('stem-audio').removeAttribute('src');$('stem-audio').hidden=true;
  project=null;scene=null;selectedId=null;revision=0;editingEnd=false;selectedTime=0;renderStems();renderParameters();setBusy(true);draw();return generation;
}
function projectOptions(){
  const select=$('saved-projects'),placeholder=document.createElement('option');
  placeholder.value='';placeholder.textContent=runtime?.projects?.length?'选择已保存项目':'暂无已保存项目';
  const states={imported:'已导入',separating:'分轨中',ready:'就绪',rendering:'渲染中',analyzing:'分析中',mixing:'混音中',error:'处理失败'};
  select.replaceChildren(placeholder,...(runtime?.projects??[]).map(value=>{
    const option=document.createElement('option');option.value=value.id;
    option.textContent=`${projectName(value)} · ${time(value.input?.durationSeconds)} · ${states[value.state]??value.state}`;return option;
  }));
  select.value=project?.id??'';
}
async function saveEdits(){
  if(!project||!scene||!revision)return;
  setBusy(true);
  try{
    message('正在保存当前轨迹…');
    accept(await api(`/projects/${project.id}/scene`,json({scene:clone(scene),expectedSceneHash:project.sceneHash})));
  }catch(error){
    $('saved-projects').value=project.id;
    const reason=`当前轨迹未保存，已保留编辑并暂停项目切换：${error.message}`;
    message(reason,true);throw new Error(reason);
  }finally{setBusy(false);}
}
async function restoreProject(id){
  if(!id||busy)return;
  try{await saveEdits();}catch{return;}
  const token=begin();
  try{
    message('正在恢复本机保存的项目…');
    let value=await api(`/projects/${encodeURIComponent(id)}`);
    if(token!==generation)return;accept(value);
    if(['separating','rendering','analyzing','mixing'].includes(value.state)){
      message(value.progress?.message??'继续等待本地处理…');
      value=await waitForProject(token,p=>!['separating','rendering','analyzing','mixing'].includes(p.state));
    }
    if(token!==generation)return;
    artifacts(value.artifacts);
    message(value.artifacts?.binauralUrl?'已恢复本机保存的声场与渲染结果。':value.scene?'已恢复本机声场，可继续编辑或渲染。':'已恢复本机音频，可开始分轨。',false,value);
  }catch(error){if(token===generation)message(error.message,true);}finally{if(token===generation)setBusy(false);}
}
async function loadInput(file=null){
  if(busy)return;
  const route=$('source-route').value,native=route==='native-stems-first-v1';
  const limit=native?runtime?.limits?.maxNativeBundleBytes:runtime?.limits?.maxUploadBytes;
  if(file&&limit&&file.size>limit){message('文件超过本地服务的上传限制。',true);return;}
  try{await saveEdits();}catch{return;}
  const token=begin();
  try{
    if(!file||native&&!file.name.toLowerCase().endsWith('.zip'))throw new Error('原生路线请选择带无损声部与生成回执的ZIP；立体声音频请选择本地自动分离路线。');
    message(native?'正在校验原生分轨、生成回执与共同时间线…':'正在导入立体声；导入后可明确启动本地自动分离。');
    const form=new FormData();if(file)form.append('file',file);
    const value=await api(native?'/native/import':'/import',{method:'POST',body:form});
    if(token!==generation)return;accept(value);message(native?'原生分轨文件与回执已校验；可进行混音、轨迹编排和7.1.4导出。':'立体声已导入。点击本地自动分离，保留原曲并生成估计声部。');
  }catch(error){if(token===generation)message(error.message,true);}finally{if(token===generation)setBusy(false);}
}
async function waitForProject(token,done){
  const deadline=Date.now()+65*60*1000;
  while(Date.now()<deadline){
    await new Promise(resolve=>{pollTimer=setTimeout(resolve,700);});
    if(token!==generation)throw new Error('项目已切换。');
    const value=await api(`/projects/${encodeURIComponent(project.id)}`);
    if(token!==generation)throw new Error('项目已切换。');
    accept(value);message(value.progress?.message??'正在本地处理…');
    if(done(value))return value;
  }
  throw new Error('等待超过65分钟；请重新载入项目查看实际状态。');
}
async function mixAction(action,body){
  if(!project||busy)throw new Error('请等待当前本地任务完成。');
  const token=++generation;setBusy(true);
  try{
    if(scene&&(action==='apply'||revision>0)){
      const saved=await api(`/projects/${project.id}/scene`,json({scene:clone(scene),expectedSceneHash:project.sceneHash}));
      project=saved;
    }
    let value=await api(`/projects/${project.id}/mix/${action}`,json(action==='apply'?{...body,expectedSceneHash:project.sceneHash}:body));
    if(token!==generation)throw new Error('项目已切换。');
    accept(value);
    if(['analyzing','mixing'].includes(value.state))value=await waitForProject(token,p=>!['analyzing','mixing'].includes(p.state));
    if(action==='apply'){clearPreview();artifacts(value.artifacts);}
    message(action==='apply'?(body.mixId?'已应用本地处理候选，可渲染当前声场。':'已恢复应用前的声场。'):value.progress?.message??'本地混音提案已就绪。');
    return value;
  }finally{if(token===generation)setBusy(false);}
}
async function separate({propagate=false}={}){
  if(project?.state!=='imported'||busy)return;
  const token=++generation;setBusy(true);clearPreview();
  try{
    message('本地分轨处理中…');
    const value=await api(`/projects/${encodeURIComponent(project.id)}/separate`,json({workflow:'separated-stereo-v1'}));
    if(token!==generation)return;accept(value);
    if(value.state==='separating')await waitForProject(token,p=>p.state!=='separating');
    if(token===generation)message('本地估计分轨完成。先试听各轨串音与瞬态，再编排声场。');
  }catch(error){if(token===generation)message(error.message,true);if(propagate)throw error;}finally{if(token===generation)setBusy(false);}
}
function renderStems(){
  const rows=Array.from($('stems').children);
  if(scene&&rows.length===scene.objects.length&&rows.every((row,i)=>row.dataset.objectId===scene.objects[i].id)){
    rows.forEach((row,i)=>{
      const object=scene.objects[i];row.classList.toggle('selected',object.id===selectedId);
      row.querySelector('small').textContent=`${object.azimuthDeg}° / ${object.elevationDeg}°${object.keyframes?.length?' · 轨迹':''}`;
      const buttons=row.querySelectorAll('button');buttons[0].disabled=busy;buttons[1].disabled=busy||!object.audioUrl;buttons[2].disabled=busy;buttons[2].setAttribute('aria-pressed',String(Boolean(object.muted)));
    });return;
  }
  $('stems').replaceChildren();$('stem-count').textContent=`${scene?.objects.length??0} 轨`;
  if(!scene){const p=document.createElement('p');p.className='empty';p.textContent='导入原生分轨或完成本地分离后显示声部。';$('stems').append(p);return;}
  for(const object of scene.objects){
    const row=document.createElement('div');row.className=`stem${object.id===selectedId?' selected':''}`;row.style.setProperty('--stem-color',COLORS[object.id]??'#c5d8d3');
    row.dataset.objectId=object.id;
    const select=document.createElement('button');select.className='stem-select';select.disabled=busy;
    const dot=document.createElement('span');dot.className='stem-dot';
    const text=document.createElement('span');text.textContent=trackLabel(object.id);
    const meta=document.createElement('small');meta.textContent=`${object.azimuthDeg}° / ${object.elevationDeg}°${object.keyframes?.length?' · 轨迹':''}`;text.append(meta);select.append(dot,text);
    select.addEventListener('click',()=>selectKeyframe(object.id,playhead,false));
    const listen=document.createElement('button');listen.className='icon-button';listen.textContent='▷';listen.title=`试听${trackLabel(object.id)}`;listen.setAttribute('aria-label',listen.title);listen.disabled=busy||!object.audioUrl;
    listen.addEventListener('click',async()=>{const latest=scene?.objects.find(value=>value.id===object.id);if(!latest)return;mixControls?.cancelPendingPlayback();const audio=$('stem-audio');$('preview').pause();$('source-audio').pause();audio.src=latest.audioUrl;audio.dataset.referenceLabel=trackLabel(object.id);audio.hidden=false;try{await audio.play();}catch(error){if(error.name!=='AbortError'||(!audio.paused&&audio===transportAudio()))message(`分轨试听失败：${error.message}`,true);}});
    const mute=document.createElement('button');mute.className='icon-button';mute.textContent='M';mute.title='静音';mute.setAttribute('aria-label',`${trackLabel(object.id)}静音`);mute.setAttribute('aria-pressed',String(Boolean(object.muted)));mute.disabled=busy;
    mute.addEventListener('click',()=>{const latest=scene?.objects.find(value=>value.id===object.id);if(latest){latest.muted=!latest.muted;changed();}});row.append(select,listen,mute);$('stems').append(row);
  }
}
function point(object,t=playhead){return pointAt(object,t);}
function editPoint(){const object=currentObject();return object?.keyframes?.find(f=>Math.abs(f.timeSeconds-selectedTime)<1e-8)??object;}
function renderParameters(){
  const object=currentObject(),p=editPoint();
  $('selected-label').textContent=object?trackLabel(object.id):'未选择轨道';
  for(const [id,key,unit,fallback] of [['azimuth','azimuthDeg','°',0],['elevation','elevationDeg','°',0],['width','widthDeg','°',30],['gain','gainDb',' dB',0]]){
    const value=(id==='azimuth'||id==='elevation'?p:object)?.[key]??fallback;
    $(id).value=value;$(`${id}-value`).textContent=`${Number(value.toFixed(1))}${unit}`;
  }
  $('motion').checked=Boolean(object?.keyframes?.length);
  const keys=object?.keyframes??[],frameIndex=keys.findIndex(f=>Math.abs(f.timeSeconds-selectedTime)<1e-8);
  $('keyframe-time').max=scene?.durationSeconds??0;$('keyframe-time').value=Number(selectedTime.toFixed(3));
  $('keyframe-info').textContent=keys.length?`节点 ${Math.max(1,frameIndex+1)} / ${keys.length} · 首尾时间固定`:'在时间线上定位后添加节点';
  $('edit-start').classList.toggle('active',selectedTime===0);$('edit-end').classList.toggle('active',Boolean(keys.length)&&Math.abs(selectedTime-scene.durationSeconds)<1e-8);updateControls();
}
function changed(){
  const cursor=playhead;revision++;clearPreview();playhead=cursor;$('seek').value=cursor;$('time-current').textContent=time(cursor);renderStems();renderParameters();scheduleDraw();message('轨迹已修改，渲染后试听与导出。');
}
function setPosition(azimuth,elevation){
  const object=currentObject();if(!object||busy)return;
  const p=editPoint();p.azimuthDeg=Math.round(wrap(azimuth));p.elevationDeg=Math.round(Math.max(-90,Math.min(90,elevation)));
  if(object.keyframes?.length&&selectedTime===0){Object.assign(object,{azimuthDeg:p.azimuthDeg,elevationDeg:p.elevationDeg});Object.assign(object.keyframes[0],{azimuthDeg:p.azimuthDeg,elevationDeg:p.elevationDeg});}
  changed();
}
async function renderBinaural(snapshot,token){
  const duration=snapshot.durationSeconds,tail=snapshot.renderTailSeconds??0;
  if(!Number.isFinite(duration)||duration<=0)throw new Error('场景时长无效。');
  if(!Number.isFinite(tail)||tail<0||tail>.25)throw new Error('渲染尾音时长无效。');
  if(!window.OfflineAudioContext)throw new Error('当前浏览器不支持离线 HRTF 渲染。');
  const context=new OfflineAudioContext(2,Math.round(duration*48000)+Math.round(tail*48000),48000);
  const buffers=await Promise.all(snapshot.objects.map(async object=>{
    const response=await fetch(object.audioUrl);if(!response.ok)throw new Error(`${trackLabel(object.id)}音频读取失败（${response.status}）。`);
    return context.decodeAudioData(await response.arrayBuffer());
  }));
  if(token!==generation)throw new Error('项目已切换。');
  snapshot.objects.forEach((object,index)=>{
    if(object.muted)return;
    const input=buffers[index];
    for(let channel=0;channel<2;channel++){
      const buffer=context.createBuffer(1,input.length,input.sampleRate);
      buffer.copyToChannel(input.getChannelData(Math.min(channel,input.numberOfChannels-1)),0);
      const source=context.createBufferSource();source.buffer=buffer;
      const gain=context.createGain();gain.gain.value=10**(object.gainDb/20)/Math.sqrt(2);
      const panner=context.createPanner();panner.panningModel='HRTF';panner.distanceModel='inverse';panner.rolloffFactor=0;
      const times=new Set([0,duration,...(object.keyframes??[]).map(frame=>frame.timeSeconds)]);
      for(let i=1;i*.05<duration;i++)times.add(i*.05);
      for(const at of [...times].sort((a,b)=>a-b)){
        const p=point(object,at),xyz=stereoCoords(p.azimuthDeg,p.elevationDeg,(channel===0?1:-1)*object.widthDeg/2);
        for(const [axis,value] of xyz.entries()){const param=[panner.positionX,panner.positionY,panner.positionZ][axis];if(at===0)param.setValueAtTime(value,0);else param.linearRampToValueAtTime(value,at);}
      }
      source.connect(gain).connect(panner).connect(context.destination);source.start(0);source.stop(duration);
    }
  });
  message('正在渲染浏览器 HRTF 双耳音频…');
  const output=await context.startRendering();
  if(token!==generation)throw new Error('项目已切换。');
  if(tail){
    let peak=0;
    for(let c=0;c<output.numberOfChannels;c++){
      const data=output.getChannelData(c);
      for(let i=output.length-2400;i<output.length;i++)peak=Math.max(peak,Math.abs(data[i]));
    }
    // shortcut: bounded browser HRTF tail; require a larger measured interval before adding room IRs.
    if(!Number.isFinite(peak)||peak>1/32768)throw new Error('耳模尾音尚未衰减，当前输出未保存。');
  }
  return encodeWav(output);
}
function encodeWav(buffer){
  const channels=buffer.numberOfChannels,frames=buffer.length,data=new ArrayBuffer(44+frames*channels*2),view=new DataView(data);
  const text=(offset,s)=>{for(let i=0;i<s.length;i++)view.setUint8(offset+i,s.charCodeAt(i));};
  text(0,'RIFF');view.setUint32(4,data.byteLength-8,true);text(8,'WAVE');text(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,channels,true);view.setUint32(24,buffer.sampleRate,true);view.setUint32(28,buffer.sampleRate*channels*2,true);view.setUint16(32,channels*2,true);view.setUint16(34,16,true);text(36,'data');view.setUint32(40,frames*channels*2,true);
  const samples=Array.from({length:channels},(_,i)=>buffer.getChannelData(i));let peak=0;
  for(const channel of samples)for(let i=0;i<frames;i++){if(!Number.isFinite(channel[i]))throw new Error('HRTF 渲染包含无效采样。');peak=Math.max(peak,Math.abs(channel[i]));}
  const scale=peak>0?Math.min(1,.95/peak):1;
  for(let i=0;i<frames;i++)for(let c=0;c<channels;c++){const sample=Math.max(-1,Math.min(1,samples[c][i]*scale));view.setInt16(44+(i*channels+c)*2,Math.round(sample*(sample<0?32768:32767)),true);}
  return new Blob([data],{type:'audio/wav'});
}
async function finishBinaural(value,token){
  if(!value.scene||!value.sceneHash)throw new Error('保存的场景缺少渲染参数或校验值。');
  const wav=await renderBinaural(clone(value.scene),token);
  if(token!==generation)throw new Error('项目已切换。');
  message('正在保存 HRTF 音频与完整导出包…');
  const result=await api(`/projects/${encodeURIComponent(value.id)}/binaural`,{method:'POST',headers:{'Content-Type':'audio/wav','X-Scene-Hash':value.sceneHash,'X-Spatial-Geometry':STEREO_GEOMETRY},body:wav});
  if(token!==generation)throw new Error('项目已切换。');
  accept(result);return result;
}
async function render({speakersOnly=false}={}){
  if(!scene||busy)return;
  const token=++generation,submittedRevision=revision;setBusy(true);clearPreview();$('stem-audio').pause();$('source-audio').pause();
  try{
    message('正在保存并校验当前声场…');
    let value=await api(`/projects/${encodeURIComponent(project.id)}/scene`,json({scene:clone(scene),expectedSceneHash:project.sceneHash}));
    if(token!==generation||revision!==submittedRevision)return;accept(value);
    if(!value.sceneHash)throw new Error('本地服务未返回场景校验值。');
    value=await api(`/projects/${encodeURIComponent(project.id)}/render`,json({expectedSceneHash:value.sceneHash}));
    if(token!==generation)return;accept(value);
    if(value.state==='rendering')value=await waitForProject(token,p=>p.state!=='rendering');
    if(!speakersOnly)value=await finishBinaural(value,token);
    if(token!==generation)return;accept(value);artifacts(value.artifacts);message(`已完成 ${speakerLayout()} ${value.artifacts?.studioPackageUrl?'棚内直出包':'扬声器通道'}${speakersOnly?'':'、双耳试听'}与声场文件导出。`,false,value);$('progress').value=1;$('progress-value').textContent='100%';
  }catch(error){if(token===generation){clearPreview();if(revision===0)artifacts(project?.artifacts);message(error.message,true);}}finally{if(token===generation)setBusy(false);}
}
function canvasContext(id){
  const canvas=$(id),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  const w=Math.max(1,rect.width),h=Math.max(1,rect.height);
  if(canvas.width!==Math.round(w*ratio)||canvas.height!==Math.round(h*ratio)){canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio);}
  const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);return {ctx,w,h};
}
function draw(){
  if(sceneView){
    const objects=(scene?.objects??[]).map(object=>{
      const p=point(object);
      return {id:object.id,label:trackLabel(object.id),color:COLORS[object.id],muted:object.muted,
        position:coords(p.azimuthDeg,p.elevationDeg),
        trajectory:trajectoryFor(object,scene.durationSeconds),
        keyframes:(object.keyframes??[]).map(frame=>({timeSeconds:frame.timeSeconds,position:coords(frame.azimuthDeg,frame.elevationDeg)})),
        spread:Array.from({length:25},(_,i)=>stereoCoords(p.azimuthDeg,p.elevationDeg,(i/24-.5)*object.widthDeg))};
    });
    sceneView.update({speakerLayout:speakerLayout(),objects,selectedId,selectedTime,time:playhead,editable:!busy});
    const object=currentObject(),p=object?point(object):null;
    $('scene-coordinate').textContent=p?`方位 ${Math.round(p.azimuthDeg)}°  /  仰角 ${Math.round(p.elevationDeg)}°`:'未选择对象';
  }
  automationControls?.update({scene,selectedId,selectedTime,time:playhead,disabled:busy});
  const colors=COLORS;
  for(const row of document.querySelectorAll('.automation-track'))row.style.setProperty('--track-color',colors[row.querySelector('.automation-track-name')?.dataset.objectId]??'#b8d4cc');
  if(!document.querySelector('.projection-panel').open)return;
  for(const id of ['top-canvas','side-canvas']){
    const {ctx,w,h}=canvasContext(id),top=id==='top-canvas',cx=w/2,cy=h/2,r=Math.min(w,h)*.36;
    ctx.font='10px "Segoe UI","NR CJK",sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.strokeStyle='#30373a';ctx.fillStyle='#929b99';ctx.lineWidth=1;
    if(top){
      for(const f of [.5,1]){ctx.beginPath();ctx.arc(cx,cy,r*f,0,Math.PI*2);ctx.stroke();}
      ctx.beginPath();ctx.moveTo(cx,cy-r);ctx.lineTo(cx,cy+r);ctx.moveTo(cx-r,cy);ctx.lineTo(cx+r,cy);ctx.stroke();
      ctx.fillText('前 0°',cx,cy-r-17);ctx.fillText('后 ±180°',cx,cy+r+18);ctx.fillText('左',cx-r-17,cy);ctx.fillText('右',cx+r+17,cy);
      ctx.fillStyle='#c5d8d3';ctx.beginPath();ctx.moveTo(cx,cy-8);ctx.lineTo(cx-5,cy+5);ctx.lineTo(cx+5,cy+5);ctx.closePath();ctx.fill();
    }else{
      ctx.beginPath();ctx.moveTo(cx,25);ctx.lineTo(cx,h-25);ctx.moveTo(15,cy);ctx.lineTo(w-15,cy);ctx.stroke();ctx.fillText('+90°',cx,14);ctx.fillText('−90°',cx,h-13);ctx.fillText('0°',cx+19,cy+13);
    }
    for(const object of scene?.objects??[]){
      const isSelected=object.id===selectedId,p=point(object),color=COLORS[object.id]??'#c5d8d3';
      const location=p=>top?{x:cx-Math.sin(p.azimuthDeg*Math.PI/180)*r,y:cy-Math.cos(p.azimuthDeg*Math.PI/180)*r}:{x:cx,y:cy-p.elevationDeg/90*(h/2-35)};
      if(top&&object.keyframes?.length){
        ctx.strokeStyle=color;ctx.globalAlpha=.35;ctx.setLineDash([3,4]);ctx.beginPath();
        for(let i=0;i<=60;i++){const at=location(point(object,scene.durationSeconds*i/60));if(i===0)ctx.moveTo(at.x,at.y);else ctx.lineTo(at.x,at.y);}ctx.stroke();ctx.setLineDash([]);
      }
      const {x,y}=location(p);ctx.globalAlpha=object.muted?.3:1;
      if(isSelected){ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.stroke();}
      ctx.fillStyle=color;ctx.beginPath();ctx.arc(x,y,isSelected?5:4,0,Math.PI*2);ctx.fill();
      if(top){ctx.fillStyle=color;ctx.font=`${isSelected?'600 ':''}10px "Segoe UI","NR CJK",sans-serif`;ctx.fillText(trackLabel(object.id),Math.max(40,Math.min(w-40,x)),Math.max(12,y-17));}
      ctx.globalAlpha=1;
    }
    if(!scene){ctx.fillStyle='#757e7b';ctx.fillText(top?'等待音源对象':'—',cx,top?cy+28:cy+25);}
  }
}
function pointer(event,id){
  const canvas=$(id),rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
  if(!scene||busy)return;
  if(event.type==='pointerdown'){
    if(id==='top-canvas'){
      let closest=null,distance=24;
      for(const object of scene.objects){const p=point(object),r=Math.min(rect.width,rect.height)*.36,px=rect.width/2-Math.sin(p.azimuthDeg*Math.PI/180)*r,py=rect.height/2-Math.cos(p.azimuthDeg*Math.PI/180)*r,d=Math.hypot(x-px,y-py);if(d<distance){closest=object;distance=d;}}
      if(closest)selectKeyframe(closest.id,selectedTime);
    }
    dragging=id;canvas.setPointerCapture(event.pointerId);
  }
  if(dragging!==id)return;
  const p=editPoint();if(!p)return;
  if(id==='top-canvas')setPosition(Math.atan2(-(x-rect.width/2),-(y-rect.height/2))*180/Math.PI,p.elevationDeg);
  else setPosition(p.azimuthDeg,(rect.height/2-y)/(rect.height/2-35)*90);
}
function selectKeyframe(id,at,movePlayhead=true){
  const object=scene?.objects.find(o=>o.id===id);if(busy||!object)return;
  selectedId=id;selectedTime=object.keyframes?.length?object.keyframes.reduce((nearest,frame)=>Math.abs(frame.timeSeconds-at)<Math.abs(nearest-at)?frame.timeSeconds:nearest,object.keyframes[0].timeSeconds):0;editingEnd=Math.abs(selectedTime-scene.durationSeconds)<1e-8;
  if(movePlayhead)seekTo(selectedTime);renderStems();renderParameters();draw();
}
function seekTo(at){
  if(busy||!Number.isFinite(at))return;
  const audio=transportAudio(),duration=Number.isFinite(audio?.duration)?audio.duration:scene?.durationSeconds;
  if(!Number.isFinite(duration))return;
  playhead=Math.max(0,Math.min(duration,at));
  if(audio)audio.currentTime=playhead;
  $('seek').value=playhead;$('time-current').textContent=time(playhead);draw();
}
function replaceCurrent(next){scene.objects[scene.objects.findIndex(o=>o.id===selectedId)]=next;}
$('add-keyframe').addEventListener('click',()=>{const object=currentObject();if(!object||busy)return;try{const next=upsertKeyframe(object,Math.min(scene.durationSeconds,playhead),scene.durationSeconds);replaceCurrent(next);selectedTime=next.keyframes.reduce((a,f)=>Math.abs(f.timeSeconds-playhead)<Math.abs(a-playhead)?f.timeSeconds:a,0);changed();}catch(error){message(error.message,true);}});
$('remove-keyframe').addEventListener('click',()=>{const object=currentObject();if(!object||busy)return;try{const index=object.keyframes.findIndex(f=>Math.abs(f.timeSeconds-selectedTime)<1e-8);replaceCurrent(removeKeyframe(object,index));selectedTime=object.keyframes[Math.max(0,index-1)].timeSeconds;changed();}catch(error){message(error.message,true);}});
$('keyframe-time').addEventListener('change',()=>{const object=currentObject();if(!object||busy)return;try{const index=object.keyframes.findIndex(f=>Math.abs(f.timeSeconds-selectedTime)<1e-8),next=moveKeyframeTime(object,index,Number($('keyframe-time').value),scene.durationSeconds);replaceCurrent(next);selectedTime=next.keyframes[index].timeSeconds;changed();seekTo(selectedTime);}catch(error){message(error.message,true);renderParameters();}});
async function loadOnline(audioUrl,{jobId,workflow}={}){
  if(busy)throw new Error('请等待当前本地任务完成。');
  await saveEdits();
  const token=begin();
  try{
    message('正在下载到本机并校验立体声文件…');
    const value=await api('/online/import',json({audioUrl,...(jobId?{jobId}:{})}));
    if(token!==generation)throw new Error('项目已切换。');
    accept(value);
    if(workflow==='separated-stereo-v1'){$('source-route').value=workflow;sourceRouteChanged();}
    message('线上立体声已下载并导入；可明确启动本地自动分离。');
  }catch(error){if(token===generation)message(error.message,true);throw error;}
  finally{if(token===generation)setBusy(false);}
  if(workflow==='separated-stereo-v1')await separate({propagate:true});
  if(workflow==='separated-stereo-v1'&&!project?.scene)throw new Error('分轨尚未完成，请查看本地处理状态。');
  return project;
}
function sourceRouteChanged(){
  const native=$('source-route').value==='native-stems-first-v1';
  $('file').accept=native?'application/zip,.zip':'audio/wav,audio/flac,audio/mpeg,audio/mp4,.wav,.flac,.mp3,.m4a';
  $('import-label').textContent=native?'导入分轨包':'导入立体声';
  $('import-title').textContent=native?'拖入原生分轨包':'拖入立体声音频';
  if(!project)$('input-meta').textContent=native?'ACE Base · WAV + 生成回执 · ZIP':'本地 HDemucs · 原曲保留 · 估计声部';
  updateControls();
}
$('source-route').addEventListener('change',sourceRouteChanged);

$('file').addEventListener('change',event=>{const file=event.target.files[0];if(file)loadInput(file);event.target.value='';});
$('demo').addEventListener('click',()=>loadInput());$('separate').addEventListener('click',separate);$('render').addEventListener('click',()=>render());
$('render-speakers').addEventListener('click',()=>render({speakersOnly:true}));
$('speaker-layout').addEventListener('change',event=>{if(!scene||busy)return;scene.speakerLayout=event.target.value;changed();message(`扬声器布局已改为 ${speakerLayout()}，请重新渲染导出。`);});
$('saved-projects').addEventListener('change',event=>restoreProject(event.target.value));
for(const type of ['dragenter','dragover'])$('drop-zone').addEventListener(type,event=>{event.preventDefault();if(!busy)$('drop-zone').classList.add('dragging');});
for(const type of ['dragleave','drop'])$('drop-zone').addEventListener(type,event=>{event.preventDefault();$('drop-zone').classList.remove('dragging');if(type==='drop'&&event.dataTransfer.files[0])loadInput(event.dataTransfer.files[0]);});
for(const id of ['azimuth','elevation'])$(id).addEventListener('input',()=>setPosition(Number($('azimuth').value),Number($('elevation').value)));
for(const [id,key] of [['width','widthDeg'],['gain','gainDb']])$(id).addEventListener('input',()=>{const object=currentObject();if(object&&!busy){object[key]=Number($(id).value);changed();}});
$('motion').addEventListener('change',()=>{const object=currentObject();if(!object||busy)return;object.keyframes=$('motion').checked?upsertKeyframe(object,0,scene.durationSeconds).keyframes:[];selectedTime=0;editingEnd=false;changed();});
for(const [id,end] of [['edit-start',false],['edit-end',true]])$(id).addEventListener('click',()=>{selectKeyframe(selectedId,end?scene.durationSeconds:0);});
$('reset').addEventListener('click',()=>{const object=currentObject();if(!object)return;Object.assign(object,{azimuthDeg:0,elevationDeg:0,widthDeg:30,gainDb:0,muted:false,keyframes:[]});editingEnd=false;selectedTime=0;changed();});
for(const id of ['top-canvas','side-canvas']){
  for(const type of ['pointerdown','pointermove'])$(id).addEventListener(type,event=>pointer(event,id));
  for(const type of ['pointerup','pointercancel'])$(id).addEventListener(type,()=>{dragging=null;});
  $(id).addEventListener('keydown',event=>{const p=editPoint();if(!p||busy)return;const step=event.shiftKey?10:1;if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();setPosition(p.azimuthDeg+(event.key==='ArrowLeft'?step:event.key==='ArrowRight'?-step:0),p.elevationDeg+(event.key==='ArrowUp'?step:event.key==='ArrowDown'?-step:0));}});
}
function transportAudio(){
  if(playbackId&&$(playbackId).getAttribute('src'))return $(playbackId);
  return ['preview','source-audio'].map($).find(audio=>audio.getAttribute('src'))??null;
}
async function selectPlayback(id){
  if(busy||!['preview','source-audio','stem-audio'].includes(id)||!$(id).getAttribute('src'))return;
  mixControls?.cancelPendingPlayback();
  const playing=['preview','source-audio','stem-audio'].some(source=>!$(source).paused);
  stopPlayhead();playbackId=id;
  for(const other of ['preview','source-audio','stem-audio','mix-audio'])$(other).pause();
  seekTo(playhead);updateTransport();
  if(playing){
    const audio=$(id);
    try{await audio.play();}catch(error){if(error.name!=='AbortError'||(!audio.paused&&audio===transportAudio()))throw error;}
  }
}
function updateTransport(){
  const audio=transportAudio(),label=audio?.id==='preview'?'HRTF · 双耳试听':audio?.id==='stem-audio'?`声部参考 · ${audio.dataset.referenceLabel??'分轨'}（非空间渲染）`:'原曲参考（非空间渲染）';
  const select=$('playback-source');select.disabled=!audio||busy;
  for(const option of select.options){option.disabled=!$(option.value).getAttribute('src');if(option.value==='stem-audio')option.textContent=`${$('stem-audio').dataset.referenceLabel??'分轨'}参考`;}
  select.value=audio?.id??'source-audio';
  $('preview-label').textContent=audio?label:'等待音频';
  $('seek').disabled=!audio||busy;
  $('play-pause').disabled=!audio||busy;$('play-pause').textContent=!audio||audio.paused?'▶':'Ⅱ';
  $('play-pause').setAttribute('aria-label',`${!audio||audio.paused?'播放':'暂停'}${audio?label:'音频'}`);
  if(audio){const duration=Number.isFinite(audio.duration)?audio.duration:scene?.durationSeconds??project?.input?.durationSeconds??1;$('seek').max=duration;$('time-total').textContent=time(duration);}
}
function syncPlayhead(audio){
  if(audio!==transportAudio()||!audio.getAttribute('src'))return;
  playhead=audio.currentTime;$('seek').value=playhead;$('time-current').textContent=time(playhead);draw();
}
$('seek').addEventListener('input',()=>seekTo(Number($('seek').value)));
$('playback-source').addEventListener('change',async event=>{try{await selectPlayback(event.target.value);}catch(error){updateTransport();message(`试听失败：${error.message}`,true);}});
$('play-pause').addEventListener('click',async()=>{const audio=transportAudio();if(!audio)return;try{if(audio.paused){mixControls?.cancelPendingPlayback();audio.currentTime=Math.min(playhead,Number.isFinite(audio.duration)?audio.duration:playhead);await audio.play();}else audio.pause();}catch(error){if(error.name!=='AbortError'||(!audio.paused&&audio===transportAudio()))message(`试听失败：${error.message}`,true);}});
for(const id of ['preview','source-audio','stem-audio','mix-audio']){
  const audio=$(id);
  audio.addEventListener('play',()=>{
    if(audio.paused)return;
    if(id!=='mix-audio')mixControls?.cancelPendingPlayback();
    stopPlayhead();if(id!=='mix-audio')playbackId=id;
    for(const other of ['preview','source-audio','stem-audio','mix-audio'])if(other!==id)$(other).pause();
    if(id!=='mix-audio'){syncPlayhead(audio);startPlayhead();}updateTransport();
  });
  if(id==='mix-audio')continue;
  audio.addEventListener('timeupdate',()=>{if(!playbackFrame)syncPlayhead(audio);});
  audio.addEventListener('loadedmetadata',()=>{if(audio===transportAudio())updateTransport();});
  for(const event of ['pause','ended','emptied'])audio.addEventListener(event,()=>{
    if(audio!==transportAudio())return;stopPlayhead();if(event!=='emptied')syncPlayhead(audio);updateTransport();
  });
}
function stopPlayhead(){cancelAnimationFrame(playbackFrame);playbackFrame=0;}
function trackPlayhead(stamp){
  const audio=transportAudio();if(!audio||audio.paused||document.hidden){stopPlayhead();return;}
  lastFrame=stamp;syncPlayhead(audio);
  playbackFrame=requestAnimationFrame(trackPlayhead);
}
function startPlayhead(){const audio=transportAudio();if(!playbackFrame&&!document.hidden&&audio&&!audio.paused)playbackFrame=requestAnimationFrame(trackPlayhead);}
document.addEventListener('visibilitychange',()=>document.hidden?stopPlayhead():startPlayhead());
sceneView=createSceneView({canvas:$('scene-canvas'),labels:$('scene-labels'),
  onSelect:id=>selectKeyframe(id,playhead,false),onKeyframeSelect:selectKeyframe,onSeek:seekTo,
  onOrbit:()=>{for(const button of document.querySelectorAll('button[data-view]'))button.setAttribute('aria-pressed','false');}});
function updateViewAvailability(){
  $('scene-canvas').dataset.webglState=sceneView.available?'ready':'unavailable';
  for(const button of document.querySelectorAll('button[data-view]'))button.disabled=!sceneView.available;
  if(!sceneView.available)document.querySelector('.projection-panel').open=true;
}
updateViewAvailability();
for(const event of ['webglcontextlost','webglcontextrestored'])$('scene-canvas').addEventListener(event,updateViewAvailability);
document.querySelector('.projection-panel').addEventListener('toggle',draw);
for(const button of document.querySelectorAll('button[data-view]')){
  button.addEventListener('click',()=>{sceneView.setView(button.dataset.view);for(const other of document.querySelectorAll('button[data-view]'))other.setAttribute('aria-pressed',String(other===button));});
}
window.addEventListener('pagehide',event=>{stopPlayhead();cancelAnimationFrame(drawFrame);drawFrame=0;if(!event.persisted){sceneView?.dispose();automationControls?.dispose();onlineControls?.dispose();}});
new ResizeObserver(draw).observe($('top-canvas'));new ResizeObserver(draw).observe($('side-canvas'));
async function init(){
  try{
    runtime=await api('/status');$('runtime-dot').classList.toggle('ready',runtime.ready===true);
    $('runtime-status').textContent='分轨 → 混音 → 7.1.4';
    $('runtime-status').title=runtime.workflow?.steps?.join(' → ')??'';
    $('native-workflow-state').textContent=runtime.workflow?.reason??'原生分轨服务能力尚未验证。';
    if(runtime.limits)$('limits').textContent=`最长 ${Math.round(runtime.limits.maxDurationSeconds/60)} 分钟 · 分轨包最大 ${Math.round(runtime.limits.maxNativeBundleBytes/1048576)} MB`;
    projectOptions();sourceRouteChanged();
    let lastId=null;try{lastId=localStorage.getItem(LAST_PROJECT_KEY);}catch{}
    if(lastId&&runtime.projects?.some(value=>value.id===lastId))await restoreProject(lastId);
  }catch(error){$('runtime-status').textContent='本地服务未连接';message(error.message,true);$('demo').disabled=true;}
  draw();
}
automationControls=createAutomation({container:$('automation-tracks'),onSelect:selectKeyframe,onSeek:seekTo,getState:()=>({time:playhead,selectedTime})});
onlineControls=initOnlineLink({container:$('online-link'),api,onImport:loadOnline});
mixControls=initMix({getState:()=>({project,scene,runtime,busy}),run:mixAction,render,message});
init();
document.fonts.ready.then(draw);
