const wrap=n=>((n+180)%360+360)%360-180;
const copyFrames=object=>(object.keyframes??[]).map(frame=>({...frame}));
const finite=(value,name)=>{if(!Number.isFinite(value))throw new RangeError(`${name}须为有限数字。`);return value;};
const TRACK_LABELS={drums:'轨道 01',bass:'轨道 02',other:'轨道 03',vocals:'轨道 04'};
export function setTrackOrder(ids){for(const key of Object.keys(TRACK_LABELS))delete TRACK_LABELS[key];ids.forEach((id,i)=>{TRACK_LABELS[id]=`轨道 ${String(i+1).padStart(2,'0')}`;});}
export const trackLabel=id=>TRACK_LABELS[id]??'轨道';

export function pointAt(object,time){
  finite(time,'时间');
  const frames=object.keyframes??[];
  if(frames.length<2)return {azimuthDeg:object.azimuthDeg,elevationDeg:object.elevationDeg};
  let a=frames[0],b=frames.at(-1);
  for(let i=1;i<frames.length;i++)if(time<=frames[i].timeSeconds){a=frames[i-1];b=frames[i];break;}
  const f=Math.max(0,Math.min(1,(time-a.timeSeconds)/(b.timeSeconds-a.timeSeconds||1)));
  return {azimuthDeg:wrap(a.azimuthDeg+wrap(b.azimuthDeg-a.azimuthDeg)*f),elevationDeg:a.elevationDeg+(b.elevationDeg-a.elevationDeg)*f};
}

function editTime(time,duration){
  finite(duration,'时长');finite(time,'时间');
  if(duration<=0||time<0||time>duration)throw new RangeError('时间须位于音频范围内。');
  return time===duration?duration:Math.min(duration,Math.round(time*1000)/1000);
}
function separated(frames){
  if(frames.some((frame,i)=>i&&frame.timeSeconds-frames[i-1].timeSeconds<.001-1e-10))
    throw new RangeError('相邻关键帧至少相隔1毫秒。');
}
function frameIndex(frames,index){
  if(!Number.isInteger(index)||index<0||index>=frames.length)throw new RangeError('关键帧不存在。');
}

export function upsertKeyframe(object,time,duration){
  const at=editTime(time,duration),position=pointAt(object,at);
  const frames=object.keyframes?.length?copyFrames(object):[
    {timeSeconds:0,azimuthDeg:object.azimuthDeg,elevationDeg:object.elevationDeg},
    {timeSeconds:duration,azimuthDeg:object.azimuthDeg,elevationDeg:object.elevationDeg}];
  const index=frames.findIndex(frame=>Math.abs(frame.timeSeconds-at)<1e-10);
  if(index>=0)frames[index]={...frames[index],...position};
  else{
    if(frames.length>=32)throw new RangeError('最多32个关键帧。');
    frames.push({timeSeconds:at,...position});frames.sort((a,b)=>a.timeSeconds-b.timeSeconds);
  }
  separated(frames);
  return {...object,keyframes:frames};
}

export function moveKeyframeTime(object,index,time,duration){
  const at=editTime(time,duration),frames=copyFrames(object);frameIndex(frames,index);
  if(index===0||index===frames.length-1){
    if(at!==frames[index].timeSeconds)throw new RangeError('首尾关键帧时间固定。');
  }else frames[index].timeSeconds=at;
  separated(frames);
  return {...object,keyframes:frames};
}

export function removeKeyframe(object,index){
  const frames=copyFrames(object);frameIndex(frames,index);
  if(index===0||index===frames.length-1)throw new RangeError('保留首尾关键帧；只能删除中间节点。');
  frames.splice(index,1);
  return {...object,keyframes:frames};
}

const clock=time=>{const ticks=Math.round(time*10);return `${Math.floor(ticks/600)}:${((ticks%600)/10).toFixed(1).padStart(4,'0')}`;};
const x=(time,duration)=>time/duration*1000;
const y=(angle,range)=>50-angle/range*45;
function paths(object,duration){
  const frames=object.keyframes?.length?object.keyframes:[
    {timeSeconds:0,...pointAt(object,0)},{timeSeconds:duration,...pointAt(object,duration)}];
  let az='',el='';
  frames.forEach((frame,i)=>{
    const a=wrap(frame.azimuthDeg),px=x(frame.timeSeconds,duration);
    if(!i)az=`M${px},${y(a,180)}`;
    else{
      const previous=frames[i-1],start=wrap(previous.azimuthDeg),delta=wrap(a-start),end=start+delta;
      if((end>=180&&a<0)||end< -180){
        const edge=end>=180?180:-180;
        const cross=x(previous.timeSeconds+(frame.timeSeconds-previous.timeSeconds)*(edge-start)/delta,duration);
        az+=` L${cross},${y(edge,180)} M${cross},${y(-edge,180)}`;
      }
      az+=` L${px},${y(a,180)}`;
    }
    el+=`${i?' L':'M'}${px},${y(frame.elevationDeg,90)}`;
  });
  return [az,el];
}

export function createAutomation({container,onSelect=()=>{},onSeek=()=>{},getState}){
  const doc=container.ownerDocument,rows=new Map();let state={},disposed=false;
  const element=(tag,className)=>{const node=doc.createElement(tag);node.className=className;return node;};
  const svgElement=(tag,attributes)=>{const node=doc.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attributes))node.setAttribute(key,value);return node;};
  function makeRow(object){
    const row=element('div','automation-track'),name=element('button','automation-track-name');
    name.type='button';name.dataset.objectId=object.id;
    const lane=element('div','automation-lane');lane.dataset.lane=object.id;lane.style.position='relative';
    const svg=svgElement('svg',{class:'automation-plot',viewBox:'0 0 1000 100',preserveAspectRatio:'none','aria-hidden':'true'});
    svg.style.width='100%';svg.style.height='100%';
    const az=svgElement('path',{class:'automation-azimuth',fill:'none',stroke:'currentColor','stroke-width':'2','vector-effect':'non-scaling-stroke'});
    const el=svgElement('path',{class:'automation-elevation',fill:'none',stroke:'currentColor','stroke-width':'1.5','stroke-dasharray':'5 4','vector-effect':'non-scaling-stroke'});
    const cursor=element('span','automation-cursor');cursor.style.position='absolute';cursor.style.top='0';cursor.style.bottom='0';cursor.style.pointerEvents='none';cursor.setAttribute('aria-hidden','true');
    const label=element('small','automation-time');
    svg.append(az,el);lane.append(svg,cursor);lane.append(label);row.append(name,lane);container.append(row);
    return {row,name,lane,az,el,cursor,label,nodes:[],signature:null};
  }
  function click(event){
    if(state.disabled||!state.scene)return;
    let target=event.target;
    while(target&&target!==container){
      if(target.dataset?.objectId){
        const object=state.scene.objects.find(item=>item.id===target.dataset.objectId);
        if(!object)return;
        const latest=getState?.()??state;
        const at=target.dataset.frameIndex===undefined?Math.max(0,Math.min(state.scene.durationSeconds,latest.time??0)):object.keyframes[Number(target.dataset.frameIndex)]?.timeSeconds;
        if(at!==undefined){
          if(target.dataset.frameIndex===undefined)onSelect(object.id,at,false);
          else onSelect(object.id,at);
        }
        return;
      }
      if(target.dataset?.lane){
        const rect=target.getBoundingClientRect();
        if(rect.width>0)onSeek(Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width))*state.scene.durationSeconds);
        return;
      }
      target=target.parentNode;
    }
  }
  container.addEventListener('click',click);
  return {
    update(next){
      if(disposed)return;
      state=next;
      const scene=next.scene,objects=scene?.objects??[],duration=scene?.durationSeconds??0;
      const empty=container.querySelector?.('.automation-empty');if(empty)empty.hidden=objects.length>0;
      const ids=new Set(objects.map(object=>object.id));
      for(const [id,row] of rows)if(!ids.has(id)){row.row.remove();rows.delete(id);}
      for(const object of objects){
        let row=rows.get(object.id);if(!row){row=makeRow(object);rows.set(object.id,row);}
        row.name.textContent=trackLabel(object.id);row.name.disabled=Boolean(next.disabled);
        row.name.setAttribute('aria-pressed',String(next.selectedId===object.id));
        row.lane.setAttribute('aria-label',`${trackLabel(object.id)}：实线方位角，虚线仰角；点击跳转时间`);
        const signature=JSON.stringify([duration,object.label,object.azimuthDeg,object.elevationDeg,object.keyframes]);
        if(signature!==row.signature){
          row.signature=signature;
          const [az,el]=duration>0?paths(object,duration):['',''];row.az.setAttribute('d',az);row.el.setAttribute('d',el);
          while(row.nodes.length>(object.keyframes?.length??0))row.nodes.pop().remove();
          (object.keyframes??[]).forEach((frame,index)=>{
            let node=row.nodes[index];
            if(!node){node=element('button','automation-node');node.type='button';node.dataset.objectId=object.id;row.lane.append(node);row.nodes.push(node);}
            node.dataset.frameIndex=String(index);
            node.style.position='absolute';node.style.left=`${frame.timeSeconds/duration*100}%`;node.style.top=`${y(wrap(frame.azimuthDeg),180)}%`;
            node.textContent='';node.setAttribute('aria-label',`${trackLabel(object.id)} ${clock(frame.timeSeconds)} 方位角${frame.azimuthDeg}° 仰角${frame.elevationDeg}°`);
            node.title=node.getAttribute('aria-label');
          });
        }
        row.nodes.forEach((node,index)=>{
          const active=next.selectedId===object.id&&Math.abs((object.keyframes[index].timeSeconds)-(next.selectedTime??-1))<1e-9;
          node.disabled=Boolean(next.disabled);node.classList.toggle('is-selected',active);node.setAttribute('aria-pressed',String(active));
        });
        const time=Math.max(0,Math.min(duration,next.time??0));
        row.cursor.style.left=`${duration>0?time/duration*100:0}%`;
        row.label.textContent=`${clock(time)} / ${clock(duration)}`;
      }
    },
    dispose(){if(disposed)return;disposed=true;container.removeEventListener('click',click);for(const row of rows.values())row.row.remove();rows.clear();}
  };
}
