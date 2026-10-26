import * as THREE from '../public/vendor/three/three.module.min.js';
import {OrbitControls} from '../public/vendor/three/OrbitControls.js';
import {directionToVector} from './coordinates.mjs';
import {trackLabel} from './automation.mjs';
import {pickTrajectory} from './trajectory.mjs';
import {Line2} from '../public/vendor/three/lines/Line2.js';
import {LineGeometry} from '../public/vendor/three/lines/LineGeometry.js';
import {LineMaterial} from '../public/vendor/three/lines/LineMaterial.js';

export const SPEAKER_LAYOUT=Object.freeze([
  ['L',30,0],['R',-30,0],['C',0,0],['Ls',110,0],['Rs',-110,0],['Tml',90,80],['Tmr',-90,80]
].map(([id,azimuthDeg,elevationDeg])=>Object.freeze({id,azimuthDeg,elevationDeg,position:Object.freeze(directionToVector(azimuthDeg,elevationDeg))})));

export const SPEAKER_LAYOUTS=Object.freeze({
  '5.1.2':SPEAKER_LAYOUT,
  '7.1.4':Object.freeze([
    ['L',30,0],['R',-30,0],['C',0,0],['Ls',100,0],['Rs',-100,0],['Lrs',145,0],['Rrs',-145,0],['Ltf',45,45],['Rtf',-45,45],['Ltr',135,45],['Rtr',-135,45]
  ].map(([id,azimuthDeg,elevationDeg])=>Object.freeze({id,azimuthDeg,elevationDeg,position:Object.freeze(directionToVector(azimuthDeg,elevationDeg))})))
});

const VIEWS={perspective:[2.7,1.8,-3.3],top:[0,4,-.001],front:[0,.12,-4],right:[4,.12,0]};
const validPosition=p=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite);
function disposeGroup(group){
  const geometries=new Set(),materials=new Set();
  group.traverse(item=>{if(item.geometry)geometries.add(item.geometry);for(const material of Array.isArray(item.material)?item.material:[item.material])if(material)materials.add(material);});
  for(const geometry of geometries)geometry.dispose();for(const material of materials)material.dispose();
}
function line(points,color,opacity=1,dashed=false){
  const geometry=new THREE.BufferGeometry().setFromPoints(points.map(p=>new THREE.Vector3(...p)));
  const material=dashed?new THREE.LineDashedMaterial({color,transparent:true,opacity,dashSize:.035,gapSize:.025,depthWrite:false}):new THREE.LineBasicMaterial({color,transparent:true,opacity,depthWrite:false});
  const result=new THREE.Line(geometry,material);if(dashed)result.computeLineDistances();return result;
}
function circle(radius,y=0){return Array.from({length:97},(_,i)=>{const a=i/96*Math.PI*2;return [Math.sin(a)*radius,y,Math.cos(a)*radius];});}
function trajectoryLine(points,color,opacity){
  const geometry=new LineGeometry();geometry.setPositions(points.flat());
  const result=new Line2(geometry,new LineMaterial({color,linewidth:1.2,transparent:true,opacity,depthWrite:false,dashSize:.045,gapSize:.03}));
  result.computeLineDistances();return result;
}
const clock=seconds=>{const ms=Math.round(seconds*1000);return `${Math.floor(ms/60000)}:${((ms%60000)/1000).toFixed(3).padStart(6,'0')}`;};

export function createSceneView({canvas,labels,onSelect=()=>{},onKeyframeSelect=()=>{},onSeek=()=>{},onOrbit=()=>{}}){
  let renderer;
  try{renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});}
  catch{
    const notice=document.createElement('p');notice.className='scene-3d-fallback scene-fallback';notice.textContent='当前图形环境不支持三维视窗，请使用下方俯视与高度编辑。';labels.replaceChildren(notice);
    return {available:false,update(){},setView(){},resize(){},dispose(){notice.remove();}};
  }
  const scene=new THREE.Scene(),camera=new THREE.OrthographicCamera(-2,2,2,-2,.01,100);
  renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio||1,2));renderer.outputColorSpace=THREE.SRGBColorSpace;
  renderer.setClearColor(0x191c21,0);renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1;
  const controls=new OrbitControls(camera,canvas);controls.enableDamping=false;controls.enablePan=false;controls.minZoom=.75;controls.maxZoom=1.65;
  controls.minPolarAngle=.001;controls.maxPolarAngle=Math.PI-.001;controls.target.set(0,-.05,0);
  const fixed=new THREE.Group(),objects=new THREE.Group();scene.add(fixed,objects);
  scene.add(new THREE.HemisphereLight(0xdfe4e6,0x252a2f,1.8));
  const key=new THREE.DirectionalLight(0xf0f0eb,2);key.position.set(-3,4,-4);scene.add(key);
  const rim=new THREE.DirectionalLight(0xb2bcc3,.7);rim.position.set(3,1.5,2);scene.add(rim);
  const box=new THREE.EdgesGeometry(new THREE.BoxGeometry(2.36,2.36,2.36));
  fixed.add(new THREE.LineSegments(box,new THREE.LineBasicMaterial({color:0x76818b,transparent:true,opacity:.35})));
  const grid=[];for(let i=-2;i<=2;i++){const n=i*1.18/2;grid.push([-1.18,-1.18,n],[1.18,-1.18,n],[n,-1.18,-1.18],[n,-1.18,1.18]);}
  fixed.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(grid.map(p=>new THREE.Vector3(...p))),new THREE.LineBasicMaterial({color:0x6a747e,transparent:true,opacity:.13})));
  fixed.add(line(circle(1),0x71808b,.16,true));
  fixed.add(line([[-1.18,0,0],[1.18,0,0]],0x71838c,.1),line([[0,0,-1.18],[0,0,1.18]],0x71838c,.1));
  fixed.add(line([[-.12,-1.177,-.91],[0,-1.177,-1.06],[.12,-1.177,-.91]],0xc5d8d3,.75));

  const head=new THREE.Group();head.name='listener-head';fixed.add(head);
  const surface=new THREE.MeshStandardMaterial({color:0x76848d,roughness:.84,metalness:.12});
  const body=new THREE.Mesh(new THREE.BufferGeometry(),surface);head.add(body);
  canvas.dataset.listenerState='loading';
  new THREE.BufferGeometryLoader().load(new URL('./assets/listener-head.json',import.meta.url).href,geometry=>{
    if(disposed){geometry.dispose();return;}
    body.geometry.dispose();geometry.computeBoundingBox();body.geometry=geometry;canvas.dataset.listenerState='ready';render();
  },undefined,()=>{if(!disposed){canvas.dataset.listenerState='unavailable';canvas.title='听者轮廓未载入；方向与轨迹编辑仍可使用。';}});
  head.add(line([[0,-.22,-.3],[0,-.22,-.39],[-.018,-.22,-.369],[0,-.22,-.39],[.018,-.22,-.369]],0xb9c9ca,.6));
  canvas.dataset.listenerStyle='sculpted-matte-profile';

  labels.replaceChildren();labels.style.pointerEvents='none';
  const leaders=document.createElementNS('http://www.w3.org/2000/svg','svg');leaders.classList.add('scene-label-leaders');leaders.style.cssText='position:absolute;inset:0;width:100%;height:100%;overflow:hidden;pointer-events:none';leaders.setAttribute('aria-hidden','true');labels.append(leaders);
  const axisSpecs=[['前 · 0°',[0,-1.18,-1.25]],['后 · ±180°',[0,-1.18,1.25]],['左 · +90°',[-1.25,-1.18,0]],['右 · −90°',[1.25,-1.18,0]]];
  const axes=axisSpecs.map(([text,position])=>{const element=document.createElement('span');element.className='scene-axis-label scene-axis';element.textContent=text;element.style.cssText='position:absolute;transform:translate(-50%,-50%);white-space:nowrap;pointer-events:none;font:10px ui-monospace,"NR CJK",sans-serif;color:#82939c';labels.append(element);return {element,position};});
  let speakerLabels=[],activeSpeakerLayout=null;
  function setSpeakerLayout(layout){
    if(activeSpeakerLayout===layout)return;
    for(const speaker of speakerLabels){fixed.remove(speaker.marker);disposeGroup(speaker.marker);speaker.element.remove();speaker.leader.remove();}
    const speakerMarkers=SPEAKER_LAYOUTS[layout].map(speaker=>{
      const marker=new THREE.Group();marker.name=`speaker-${speaker.id}`;marker.position.set(...speaker.position);
      const geometry=new THREE.BoxGeometry(.065,.095,.045);
      const body=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:0x3c444b,roughness:1}));
      const edges=new THREE.LineSegments(new THREE.EdgesGeometry(geometry),new THREE.LineBasicMaterial({color:0xa2adb5,transparent:true,opacity:.65}));
      marker.add(body,edges);marker.lookAt(0,0,0);fixed.add(marker);
      return {...speaker,marker};
    });
    speakerLabels=speakerMarkers.map(speaker=>{
      const element=document.createElement('span');element.className='scene-speaker-label';element.dataset.speakerId=speaker.id;element.textContent=speaker.id;
      element.title=`${speaker.id} · 方位${speaker.azimuthDeg}° · 仰角${speaker.elevationDeg}°`;
      element.style.cssText='position:absolute;pointer-events:none;padding:2px 4px;border:1px solid #55616a88;border-radius:2px;background:#1d2227d9;color:#aeb9c2;font:10px ui-monospace,Consolas,monospace;line-height:15px;white-space:nowrap';
      const leader=document.createElementNS('http://www.w3.org/2000/svg','line');leader.setAttribute('stroke','#82919a');leader.setAttribute('stroke-opacity','.35');leader.setAttribute('stroke-width','1');leaders.append(leader);
      labels.append(element);return {...speaker,element,leader};
    });
    activeSpeakerLayout=layout;canvas.dataset.speakerLayout=layout;canvas.dataset.speakerCount=String(speakerLabels.length);
  }
  setSpeakerLayout('5.1.2');
  const notice=document.createElement('p');notice.className='scene-3d-fallback scene-fallback';notice.hidden=true;labels.append(notice);
  const hint=document.createElement('span');hint.className='scene-time-hint';hint.hidden=true;labels.append(hint);
  const records=new Map(),raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2();
  let selectedId=null,selectedTime=0,currentTime=0,editable=false,disposed=false,lost=false,press=null,width=1,height=1,settingView=false;
  const screen=p=>{const v=new THREE.Vector3(...p).project(camera);return {x:(v.x+1)*width/2,y:(1-v.y)*height/2,visible:v.z>=-1&&v.z<=1};};
  function placeLabels(){
    const sizes=new Map([...speakerLabels.map(s=>s.element),...Array.from(records.values(),r=>r.label)].map(element=>[element,{w:element.offsetWidth,h:element.offsetHeight}]));
    const axisPositions=[];
    for(const axis of [...axes].sort((a,b)=>camera.position.distanceToSquared(new THREE.Vector3(...a.position))-camera.position.distanceToSquared(new THREE.Vector3(...b.position)))){
      const p=screen(axis.position),x=Math.max(34,Math.min(width-34,p.x)),y=Math.max(14,Math.min(height-14,p.y));
      axis.element.hidden=!p.visible||axisPositions.some(other=>Math.abs(other.x-x)<72&&Math.abs(other.y-y)<16);
      axis.element.style.left=`${x}px`;axis.element.style.top=`${y}px`;
      if(!axis.element.hidden)axisPositions.push({x,y});
    }
    const occupied=axisPositions.map(p=>({x:p.x-36,y:p.y-8,w:72,h:16})),bounds=body.geometry.boundingBox;
    if(bounds){
      const corners=[];for(const x of [bounds.min.x,bounds.max.x])for(const y of [bounds.min.y,bounds.max.y])for(const z of [bounds.min.z,bounds.max.z])corners.push(screen([x,y,z]));
      const left=Math.min(...corners.map(p=>p.x))-5,top=Math.min(...corners.map(p=>p.y))-5;
      occupied.push({x:left,y:top,w:Math.max(...corners.map(p=>p.x))-left+5,h:Math.max(...corners.map(p=>p.y))-top+5});
    }
    const center=screen([0,0,0]);
    for(const speaker of speakerLabels){
      const p=screen(speaker.position),element=speaker.element,w=Math.max(24,sizes.get(element).w||30),h=21;
      const dx=p.x-center.x,dy=p.y-center.y,length=Math.max(1,Math.hypot(dx,dy));
      const offsetX=speaker.id==='Tml'?-22:speaker.id==='Tmr'?22:dx/length*19;
      const offsetY=speaker.id.startsWith('Tm')?-15:dy/length*19;
      let x=Math.max(7,Math.min(width-w-7,p.x+offsetX-w/2)),y=Math.max(7,Math.min(height-h-7,p.y+offsetY-h/2));
      for(let i=0;i<6&&occupied.some(r=>x<r.x+r.w+3&&x+w+3>r.x&&y<r.y+r.h+3&&y+h+3>r.y);i++)y=Math.max(7,Math.min(height-h-7,y+(y<center.y?-23:23)));
      element.style.left=`${x}px`;element.style.top=`${y}px`;element.hidden=!p.visible;occupied.push({x,y,w,h});
      speaker.leader.setAttribute('x1',p.x);speaker.leader.setAttribute('y1',p.y);speaker.leader.setAttribute('x2',Math.max(x,Math.min(x+w,p.x)));speaker.leader.setAttribute('y2',Math.max(y,Math.min(y+h,p.y)));speaker.leader.style.display=p.visible?'':'none';
    }
    const entries=[...records.values()].sort((a,b)=>(b.id===selectedId)-(a.id===selectedId));
    for(const record of entries){
      const p=screen(record.position),element=record.label,w=Math.min(width-16,Math.max(54,sizes.get(element).w||80)),h=Math.max(30,sizes.get(element).h);
      const clamp=(n,max)=>Math.max(8,Math.min(max-8,n));
      const candidates=[];
      for(const dy of [0,-34,34,-68,68,-102,102,-136,136])for(const dx of [13,-w-13]){
        const x=clamp(p.x+dx,width-w),y=clamp(p.y-h/2+dy,height-h);
        const overlap=occupied.reduce((sum,r)=>sum+Math.max(0,Math.min(x+w+5,r.x+r.w)-Math.max(x-5,r.x))*Math.max(0,Math.min(y+h+4,r.y+r.h)-Math.max(y-4,r.y)),0);
        candidates.push({x,y,score:overlap*100+Math.hypot(x+w/2-p.x,y+h/2-p.y)});
      }
      const {x,y}=candidates.sort((a,b)=>a.score-b.score)[0];
      occupied.push({x,y,w,h});element.style.left=`${x}px`;element.style.top=`${y}px`;element.hidden=!p.visible;
      const endX=Math.max(x,Math.min(x+w,p.x)),endY=Math.max(y,Math.min(y+h,p.y));
      record.leader.setAttribute('x1',p.x);record.leader.setAttribute('y1',p.y);record.leader.setAttribute('x2',endX);record.leader.setAttribute('y2',endY);record.leader.style.display=p.visible&&Math.hypot(endX-p.x,endY-p.y)>8?'':'none';
      record.ring.quaternion.copy(camera.quaternion);
    }
  }
  function render(){if(disposed||lost)return;camera.updateMatrixWorld();placeLabels();renderer.render(scene,camera);}
  function replaceLine(record,name,points,color,opacity,dashed){
    const key=JSON.stringify(points);if(record[`${name}Key`]===key)return;record[`${name}Key`]=key;
    if(record[name]&&record[`${name}Count`]===points.length){
      const geometry=record[name].geometry;
      if(name==='path')geometry.setPositions(points.flat());
      else{const position=geometry.getAttribute('position');points.forEach((point,i)=>position.setXYZ(i,...point));position.needsUpdate=true;geometry.computeBoundingSphere();}
      if(dashed)record[name].computeLineDistances();return;
    }
    if(record[name]){objects.remove(record[name]);disposeGroup(record[name]);record[name]=null;}
    record[`${name}Count`]=points.length;
    if(points.length>1){record[name]=name==='path'?trajectoryLine(points,color,opacity):line(points,color,opacity,dashed);objects.add(record[name]);}
  }
  function makeRecord(object){
    const mesh=new THREE.Mesh(new THREE.SphereGeometry(.045,18,12),new THREE.MeshStandardMaterial({color:object.color||'#c5d8d3',metalness:0,roughness:.75}));
    const hit=new THREE.Mesh(new THREE.SphereGeometry(.115,10,8),new THREE.MeshBasicMaterial({transparent:true,opacity:0,depthWrite:false}));hit.userData.objectId=object.id;
    const ring=new THREE.Mesh(new THREE.TorusGeometry(.069,.0035,6,40),new THREE.MeshBasicMaterial({color:0xf0f4f4,transparent:true,opacity:.95,depthTest:false}));ring.renderOrder=3;
    const keyframes=new THREE.Group();objects.add(mesh,hit,ring,keyframes);
    const label=document.createElement('button');label.type='button';label.className='scene-object-label scene-label';label.dataset.objectId=object.id;
    label.style.cssText='position:absolute;pointer-events:auto;min-height:30px;min-width:54px;max-width:calc(100% - 16px);padding:4px 8px;border:1px solid #46525b;border-radius:3px;background:#1b2228eb;font:11px "Segoe UI","NR CJK",sans-serif;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer';
    label.addEventListener('click',()=>{if(editable)onSelect(object.id);});labels.append(label);
    const leader=document.createElementNS('http://www.w3.org/2000/svg','line');leader.setAttribute('stroke-width','1');leader.setAttribute('vector-effect','non-scaling-stroke');leader.setAttribute('stroke-linecap','round');leaders.append(leader);
    return {id:object.id,mesh,hit,ring,label,leader,keyframes,nodes:[],trajectory:[],position:[0,0,0]};
  }
  function update(value={}){
    if(disposed)return;setSpeakerLayout(value.speakerLayout==='7.1.4'?'7.1.4':'5.1.2');if(selectedId!==value.selectedId||currentTime!==(value.time??0)||!value.editable){hint.hidden=true;canvas.style.cursor='';}selectedId=value.selectedId??null;selectedTime=value.selectedTime??0;currentTime=value.time??0;editable=Boolean(value.editable);
    const incoming=(value.objects??[]).filter(o=>typeof o.id==='string'&&validPosition(o.position)),ids=new Set(incoming.map(o=>o.id));
    for(const [id,record] of records)if(!ids.has(id)){for(const key of ['mesh','hit','ring','path','spread','keyframes'])if(record[key]){objects.remove(record[key]);disposeGroup(record[key]);}record.label.remove();record.leader.remove();records.delete(id);}
    for(const object of incoming){
      let record=records.get(object.id);if(!record){record=makeRecord(object);records.set(object.id,record);}
      record.position=object.position;for(const key of ['mesh','hit','ring'])record[key].position.set(...object.position);
      const selected=object.id===selectedId,color=object.color||'#c5d8d3';
      record.mesh.material.color.set(color);record.mesh.material.transparent=Boolean(object.muted);record.mesh.material.opacity=object.muted?.35:1;
      record.mesh.scale.setScalar(selected?1.15:1);record.ring.visible=selected;
      record.label.textContent=`${trackLabel(object.id)}${object.muted?' · M':''}`;record.label.disabled=!editable;record.label.setAttribute('aria-pressed',String(selected));
      record.leader.setAttribute('stroke',color);record.leader.setAttribute('opacity',object.muted?'.2':selected?'.7':'.45');
      record.label.style.color=object.muted?'#9ba8ad':color;record.label.style.borderColor=selected?'#b6c5cb':'#46525b';record.label.style.opacity=object.muted?'.65':'1';
      record.trajectory=(object.trajectory??[]).filter(p=>Number.isFinite(p.timeSeconds)&&validPosition(p.position));
      replaceLine(record,'path',record.trajectory.map(p=>p.position),color,selected?.7:.2,true);
      replaceLine(record,'spread',(object.spread??[]).filter(validPosition),color,selected?.72:.3,false);
      const frames=(object.keyframes??[]).filter(p=>Number.isFinite(p.timeSeconds)&&validPosition(p.position)),frameKey=JSON.stringify(frames);
      if(record.frameKey!==frameKey){
        record.frameKey=frameKey;
        while(record.nodes.length>frames.length){const node=record.nodes.pop();record.keyframes.remove(node.mesh);disposeGroup(node.mesh);}
        frames.forEach((frame,i)=>{
          let node=record.nodes[i];
          if(!node){const mesh=new THREE.Mesh(new THREE.SphereGeometry(.021,12,8),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.8}));record.keyframes.add(mesh);node={mesh};record.nodes.push(node);}
          Object.assign(node,frame);node.mesh.position.set(...frame.position);
        });
      }
      record.keyframes.visible=selected;
      for(const node of record.nodes){const active=Math.abs(node.timeSeconds-selectedTime)<1e-8;node.mesh.material.color.set(active?0xffffff:color);node.mesh.material.opacity=object.muted?.3:active?1:.8;node.mesh.scale.setScalar(active?1.4:1);}
      if(record.path){record.path.material.opacity=object.muted?.12:selected?.7:.2;record.path.material.linewidth=selected?2.2:1.1;record.path.material.dashed=!selected;}
      if(record.spread)record.spread.material.opacity=object.muted?.12:selected?.72:.3;
    }
    canvas.dataset.objectCount=String(records.size);canvas.dataset.selectedObject=selectedId??'';canvas.dataset.keyframeCount=String(records.get(selectedId)?.nodes.length??0);canvas.dataset.selectedTime=String(selectedTime);render();
  }
  function resize(){
    if(disposed)return;const rect=canvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;width=rect.width;height=rect.height;
    renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio||1,2));renderer.setSize(width,height,false);
    const aspect=width/height,halfY=Math.max(1.85,1.8/aspect);camera.left=-halfY*aspect;camera.right=halfY*aspect;camera.top=halfY;camera.bottom=-halfY;camera.updateProjectionMatrix();render();
  }
  function setView(name='perspective'){
    if(disposed)return;const position=VIEWS[name]??VIEWS.perspective;settingView=true;camera.position.set(...position);camera.up.set(0,1,0);camera.zoom=1;camera.updateProjectionMatrix();controls.target.set(0,-.05,0);controls.update();settingView=false;canvas.dataset.view=name in VIEWS?name:'perspective';hint.hidden=true;render();
  }
  function locate(event){
    const rect=canvas.getBoundingClientRect(),p={x:event.clientX-rect.left,y:event.clientY-rect.top},record=records.get(selectedId);
    pointer.set(p.x/rect.width*2-1,-p.y/rect.height*2+1);raycaster.setFromCamera(pointer,camera);scene.updateMatrixWorld(true);
    const block=body.geometry.attributes.position?raycaster.intersectObject(body,false)[0]:null;
    const visibleAt=position=>!block||block.distance>=new THREE.Vector3(...position).sub(raycaster.ray.origin).dot(raycaster.ray.direction)-.03;
    const limit=event.pointerType==='touch'?14:9;
    const nodes=(record?.nodes??[]).map(node=>({...node,screen:screen(node.position)})).filter(node=>node.screen.visible&&Math.hypot(node.screen.x-p.x,node.screen.y-p.y)<=limit&&visibleAt(node.position));
    nodes.sort((a,b)=>Math.hypot(a.screen.x-p.x,a.screen.y-p.y)-Math.hypot(b.screen.x-p.x,b.screen.y-p.y)||Math.abs(a.timeSeconds-currentTime)-Math.abs(b.timeSeconds-currentTime));
    if(nodes.length)return {...nodes[0],kind:'keyframe',id:record.id,p};
    const objectHit=raycaster.intersectObjects([...records.values()].map(r=>r.hit),false).find(hit=>!block||hit.distance<=block.distance+.03);
    if(objectHit)return {kind:'object',id:objectHit.object.userData.objectId,p};
    if(!record)return null;
    const positionAt=hit=>{const a=record.trajectory[hit.segmentIndex].position,b=record.trajectory[hit.segmentIndex+1].position;return a.map((n,i)=>n+(b[i]-n)*hit.ratio);};
    const hit=pickTrajectory(record.trajectory.map(sample=>({...sample,...screen(sample.position)})),p,{maxDistancePx:limit,referenceTime:currentTime,accept:hit=>visibleAt(positionAt(hit))});
    return hit?{...hit,kind:'trajectory',id:record.id,p}:null;
  }
  const down=event=>{press={x:event.clientX,y:event.clientY,id:event.pointerId};};
  const up=event=>{
    const start=press;press=null;hint.hidden=true;if(!editable||lost||!start||start.id!==event.pointerId||Math.hypot(event.clientX-start.x,event.clientY-start.y)>5)return;
    const target=locate(event);
    if(target?.kind==='keyframe')onKeyframeSelect(target.id,target.timeSeconds);
    else if(target?.kind==='object')onSelect(target.id);
    else if(target)onSeek(target.timeSeconds);
  };
  const hover=event=>{
    if(!editable||lost||event.buttons){hint.hidden=true;return;}
    const target=locate(event);hint.hidden=!target||target.kind==='object';canvas.style.cursor=target?'pointer':'';
    if(!hint.hidden){hint.textContent=`${target.kind==='keyframe'?'节点':'轨迹'} ${clock(target.timeSeconds)}`;hint.style.left=`${Math.max(8,Math.min(width-120,target.p.x+12))}px`;hint.style.top=`${Math.max(28,target.p.y-12)}px`;}
  };
  const leave=()=>{hint.hidden=true;canvas.style.cursor='';};
  const cancel=()=>{press=null;leave();};
  const orbit=()=>{canvas.dataset.view='custom';onOrbit();};
  const controlsChanged=()=>{if(!settingView)orbit();hint.hidden=true;render();};
  const contextLost=event=>{event.preventDefault();lost=true;cancel();notice.textContent='三维图形环境已中断；二维编辑仍可使用。';notice.hidden=false;};
  const contextRestored=()=>{lost=false;notice.hidden=true;resize();};
  canvas.addEventListener('pointerdown',down);canvas.addEventListener('pointerup',up);canvas.addEventListener('pointercancel',cancel);canvas.addEventListener('pointermove',hover);canvas.addEventListener('pointerleave',leave);
  canvas.addEventListener('webglcontextlost',contextLost);canvas.addEventListener('webglcontextrestored',contextRestored);
  controls.addEventListener('change',controlsChanged);
  const observer=new ResizeObserver(resize);observer.observe(canvas);setView('perspective');resize();
  return {get available(){return !disposed&&!lost;},update,setView,resize,dispose(){
    if(disposed)return;disposed=true;observer.disconnect();controls.removeEventListener('change',controlsChanged);controls.dispose();
    for(const [event,handler] of [['pointerdown',down],['pointerup',up],['pointercancel',cancel],['pointermove',hover],['pointerleave',leave],['webglcontextlost',contextLost],['webglcontextrestored',contextRestored]])canvas.removeEventListener(event,handler);
    disposeGroup(scene);renderer.dispose();labels.replaceChildren();records.clear();
  }};
}
