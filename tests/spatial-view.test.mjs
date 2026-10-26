import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as THREE from '../public/vendor/three/three.module.min.js';
import {STEREO_GEOMETRY,directionToVector,stereoDirectionToVector} from '../spatial-audio/coordinates.mjs';
import {trackLabel} from '../spatial-audio/automation.mjs';

test('viewer and HRTF share listener-relative cardinal directions',()=>{
  for(const [a,e,expected] of [[0,0,[0,0,-1]],[90,0,[-1,0,0]],[-90,0,[1,0,0]],[180,0,[0,0,1]],[0,90,[0,1,0]],[0,-90,[0,-1,0]]]){
    directionToVector(a,e).forEach((n,i)=>assert.ok(Math.abs(n-expected[i])<1e-10));
  }
});

test('great-circle stereo width stays finite at both poles and preserves horizontal positions',()=>{
  assert.equal(STEREO_GEOMETRY,'great-circle-width-v1');
  for(const a of [-180,-90,0,25,179])for(const e of [-90,-89,-45,0,45,89,90]){
    const left=stereoDirectionToVector(a,e,30),right=stereoDirectionToVector(a,e,-30);
    assert.ok(Math.abs(Math.hypot(...left)-1)<1e-10);
    assert.ok(Math.abs(Math.hypot(...right)-1)<1e-10);
    assert.ok(Math.abs(left.reduce((sum,n,i)=>sum+n*right[i],0)-.5)<1e-10);
    assert.deepEqual(stereoDirectionToVector(a,e,0),directionToVector(a,e));
    if(e===0)left.forEach((n,i)=>assert.ok(Math.abs(n-directionToVector(a+30,0)[i])<1e-10));
  }
});

test('bundled listener mesh is finite, bounded and faces the console front',()=>{
  const asset=JSON.parse(readFileSync(new URL('../spatial-audio/assets/listener-head.json',import.meta.url),'utf8'));
  const geometry=new THREE.BufferGeometryLoader().parse(asset);
  geometry.computeBoundingBox();
  const {min,max}=geometry.boundingBox,positions=geometry.attributes.position,normals=geometry.attributes.normal;
  assert.ok(positions.count>5000&&positions.count<12000);
  assert.equal(positions.count,normals.count);
  assert.ok(positions.array.every(Number.isFinite)&&normals.array.every(Number.isFinite));
  assert.ok(geometry.index.array.every(index=>index<positions.count));
  assert.ok(min.x>-.2&&max.x<.2&&min.y>-.2&&max.y<.3);
  assert.ok(min.z<-.24&&max.z>.1&&max.z<.16);
  assert.ok(Math.abs(min.z)>max.z+.09,'The nose must extend toward -Z, not the back of the scene');
  geometry.dispose();
});

test('direction has unit radius at every height and across the rear seam',()=>{
  for(const a of [-180,-179,-90,0,90,179,180])for(const e of [-90,-45,0,45,90]){
    assert.ok(Math.abs(Math.hypot(...directionToVector(a,e))-1)<1e-10);
  }
  const left=directionToVector(179,30),right=directionToVector(-179,30);
  assert.ok(Math.hypot(...left.map((n,i)=>n-right[i]))<.04);
});

test('5.1.2 room markers use the renderer speaker angles without inventing an LFE position',async()=>{
  const {SPEAKER_LAYOUT}=await import('../spatial-audio/scene-view.mjs');
  assert.deepEqual(SPEAKER_LAYOUT.map(({id,azimuthDeg,elevationDeg})=>[id,azimuthDeg,elevationDeg]),[
    ['L',30,0],['R',-30,0],['C',0,0],['Ls',110,0],['Rs',-110,0],['Tml',90,80],['Tmr',-90,80]
  ]);
  for(const speaker of SPEAKER_LAYOUT){
    assert.deepEqual(speaker.position,directionToVector(speaker.azimuthDeg,speaker.elevationDeg));
    assert.ok(Math.abs(Math.hypot(...speaker.position)-1)<1e-10);
  }
  assert.ok(SPEAKER_LAYOUT.find(s=>s.id==='L').position[0]<0);
  assert.ok(SPEAKER_LAYOUT.find(s=>s.id==='R').position[0]>0);
  assert.equal(SPEAKER_LAYOUT.find(s=>s.id==='C').position[2],-1);
  assert.ok(SPEAKER_LAYOUT.filter(s=>s.elevationDeg>0).every(s=>s.position[1]>.98));
});

test('7.1.4 exposes eleven directional markers at the speaker renderer angles',async()=>{
  const {SPEAKER_LAYOUT,SPEAKER_LAYOUTS}=await import('../spatial-audio/scene-view.mjs');
  assert.equal(SPEAKER_LAYOUTS['5.1.2'],SPEAKER_LAYOUT);
  assert.deepEqual(SPEAKER_LAYOUTS['7.1.4'].map(({id,azimuthDeg,elevationDeg})=>[id,azimuthDeg,elevationDeg]),[
    ['L',30,0],['R',-30,0],['C',0,0],['Ls',100,0],['Rs',-100,0],['Lrs',145,0],['Rrs',-145,0],['Ltf',45,45],['Rtf',-45,45],['Ltr',135,45],['Rtr',-135,45]
  ]);
  for(const speaker of SPEAKER_LAYOUTS['7.1.4'])assert.deepEqual(speaker.position,directionToVector(speaker.azimuthDeg,speaker.elevationDeg));
  assert.ok(!SPEAKER_LAYOUTS['7.1.4'].some(s=>s.id==='LFE'));
});

test('speaker-only render saves selected layout and completes without browser HRTF',async()=>{
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const renderSource=source.slice(source.indexOf('async function render({'),source.indexOf('\nfunction canvasContext('));
  const create=new Function('api','finishBinaural',`
    let scene={speakerLayout:'7.1.4'},busy=false,generation=0,revision=1;
    let project={id:'test',sceneHash:'before'};
    const clone=structuredClone,json=body=>({body}),speakerLayout=()=>scene.speakerLayout;
    const $=()=>({pause(){},value:0,textContent:''});
    const setBusy=value=>{busy=value},clearPreview=()=>{},message=()=>{},artifacts=()=>{};
    const accept=value=>{project=value};
    ${renderSource}
    return render;
  `);
  const requests=[];let binauralCalls=0;
  const render=create(async(path,options)=>{
    requests.push({path,body:options.body});
    return {id:'test',sceneHash:'saved',scene:{speakerLayout:'7.1.4'},state:'ready',artifacts:{studioPackageUrl:'/studio.zip'}};
  },async value=>{binauralCalls++;return value;});
  await render({speakersOnly:true});
  assert.equal(requests[0].body.scene.speakerLayout,'7.1.4');
  assert.equal(requests[1].path,'/projects/test/render');
  assert.equal(requests[1].body.expectedSceneHash,'saved');
  assert.equal(binauralCalls,0);
  await render();
  assert.equal(binauralCalls,1,'existing combined render still renders HRTF');
});

test('render failure never restores stale edits but retains saved speaker results after HRTF failure',async()=>{
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const renderSource=source.slice(source.indexOf('async function render({'),source.indexOf('\nfunction canvasContext('));
  const create=new Function('failSave',`
    let scene={speakerLayout:'7.1.4'},busy=false,generation=0,revision=1;
    let project={id:'test',sceneHash:'old',artifacts:{surroundUrl:'old_5_1_2.wav'}};
    const restored=[],errors=[],clone=structuredClone,json=body=>body;
    const $=()=>({pause(){}}),setBusy=value=>{busy=value},clearPreview=()=>{},message=(text,error)=>{if(error)errors.push(text)},artifacts=value=>restored.push(value);
    const accept=value=>{project=value;revision=0};
    const api=async path=>{if(failSave)throw Error('scene save conflict');return {id:'test',sceneHash:'saved',artifacts:{surroundUrl:'new_7_1_4.wav'}}};
    const finishBinaural=async()=>{throw Error('HRTF unavailable')};
    ${renderSource}
    return render().then(()=>({restored,errors,busy}));
  `);
  assert.deepEqual(await create(true),{restored:[],errors:['scene save conflict'],busy:false});
  assert.deepEqual(await create(false),{restored:[{surroundUrl:'new_7_1_4.wav'}],errors:['HRTF unavailable'],busy:false});
});

test('HRTF schedules real 1ms keyframe movements as well as grid and endpoint',async()=>{
  const {pointAt}=await import('../spatial-audio/automation.mjs');
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const renderSource=source.slice(source.indexOf('async function renderBinaural('),source.indexOf('\nfunction encodeWav('));
  const panners=[];
  class OfflineContext{
    constructor(){this.destination={};}
    async decodeAudioData(){return {length:100,sampleRate:48000,numberOfChannels:1,getChannelData:()=>new Float32Array(100)}}
    createBuffer(){return {copyToChannel(){}}}
    createBufferSource(){return {connect:node=>node,start(){},stop(){}}}
    createGain(){return {gain:{},connect:node=>({connect(){}})}}
    createPanner(){const axis=()=>({calls:[],setValueAtTime(v,t){this.calls.push([t,v])},linearRampToValueAtTime(v,t){this.calls.push([t,v])}});const p={positionX:axis(),positionY:axis(),positionZ:axis()};panners.push(p);return p;}
    async startRendering(){return {rendered:true}}
  }
  const create=new Function('OfflineAudioContext','stereoCoords','point',`
    const window={OfflineAudioContext},generation=1,message=()=>{},encodeWav=value=>value;
    const fetch=async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(1)});
    ${renderSource}
    return renderBinaural;
  `);
  const frames=[[0,0],[.024,0],[.025,90],[.026,0],[.103,0]].map(([timeSeconds,azimuthDeg])=>({timeSeconds,azimuthDeg,elevationDeg:0}));
  const renderBinaural=create(OfflineContext,stereoDirectionToVector,pointAt);
  await renderBinaural({durationSeconds:.103,objects:[{audioUrl:'/stem',gainDb:0,widthDeg:0,keyframes:frames}]},1);
  assert.equal(panners.length,2);
  for(const panner of panners){
    const calls=panner.positionX.calls;
    assert.deepEqual(calls.map(([t])=>t),[0,.024,.025,.026,.05,.1,.103]);
    assert.equal(calls.find(([t])=>t===.025)[1],-1);
    assert.ok(Math.abs(calls.find(([t])=>t===.026)[1])<1e-10);
  }
  panners.length=0;
  await renderBinaural({durationSeconds:.103,objects:[{audioUrl:'/stem',gainDb:0,widthDeg:60,keyframes:[],azimuthDeg:0,elevationDeg:90}]},1);
  assert.equal(panners.length,2);
  assert.ok(Math.abs(panners[0].positionX.calls[0][1]+.5)<1e-10);
  assert.ok(Math.abs(panners[1].positionX.calls[0][1]-.5)<1e-10);
});

test('HRTF publication identifies the geometry version to reject stale browser clients',async()=>{
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const functionSource=source.slice(source.indexOf('async function finishBinaural('),source.indexOf('\nasync function render({'));
  const requests=[];
  const create=new Function('api','STEREO_GEOMETRY',`
    const generation=1,clone=structuredClone,renderBinaural=async()=>new Blob(),message=()=>{},accept=()=>{};
    ${functionSource}
    return finishBinaural;
  `);
  await create(async(path,options)=>{requests.push({path,options});return {};},STEREO_GEOMETRY)({id:'test',scene:{},sceneHash:'a'.repeat(64)},1);
  assert.equal(requests[0].options.headers['X-Spatial-Geometry'],STEREO_GEOMETRY);
});

test('stem name click selects at current playhead without seeking or pausing playback',()=>{
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const renderSource=source.slice(source.indexOf('function renderStems(){'),source.indexOf('\nfunction point('));
  const create=new Function('trackLabel',`
    const nodes=[];
    const node=()=>({children:[],dataset:{},classList:{toggle(){}},style:{setProperty(){}},handlers:{},append(...items){this.children.push(...items)},addEventListener(event,fn){this.handlers[event]=fn},setAttribute(){},replaceChildren(){this.children=[]},querySelector(tag){return this.querySelectorAll(tag)[0]},querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...child.querySelectorAll(tag)])}});
    const document={createElement:tag=>{const element=node();element.tagName=tag;nodes.push(element);return element}};
    const elements={stems:node(),'stem-count':node()},$=id=>elements[id];
    const scene={objects:[{id:'vocals',label:'Voice',azimuthDeg:0,elevationDeg:0}]},COLORS={},LABELS={};
    let selectedId=null,busy=false,selectedTime=0,playhead=30;
    const audio={currentTime:30,paused:false},selections=[];
    const selectKeyframe=(id,at,move=true)=>{selections.push([id,at,move]);if(move)audio.currentTime=0};
    ${renderSource}
    renderStems();
    const row=elements.stems.children[0],button=row.children[0];
    for(let i=0;i<100;i++){scene.objects[0].azimuthDeg=i;renderStems();}
    if(elements.stems.children[0]!==row||row.children[0]!==button)throw new Error('stem row was replaced during parameter input');
    elements.stems.children[0].children[0].handlers.click();
    return {selections,audio};
  `);
  assert.deepEqual(create(trackLabel),{selections:[['vocals',30,false]],audio:{currentTime:30,paused:false}});
});

test('moving spread updates the existing Three geometry without disposing or replacing its material',()=>{
  const source=readFileSync(new URL('../spatial-audio/scene-view.mjs',import.meta.url),'utf8');
  const functionSource=source.slice(source.indexOf('  function replaceLine('),source.indexOf('  function makeRecord('));
  const group=new THREE.Group();let disposed=0;
  const line=points=>new THREE.Line(new THREE.BufferGeometry().setFromPoints(points.map(p=>new THREE.Vector3(...p))),new THREE.LineBasicMaterial());
  const replace=new Function('objects','line','disposeGroup',`${functionSource};return replaceLine;`)(group,line,()=>disposed++);
  const record={};
  replace(record,'spread',[[0,0,0],[1,0,0]],'#fff',1,false);
  const object=record.spread,geometry=object.geometry,material=object.material,attribute=geometry.getAttribute('position');
  for(let i=1;i<=100;i++)replace(record,'spread',[[0,i,0],[1,i,0]],'#fff',1,false);
  assert.equal(record.spread,object);assert.equal(record.spread.geometry,geometry);assert.equal(record.spread.material,material);
  assert.equal(geometry.getAttribute('position'),attribute);assert.equal(attribute.getY(0),100);assert.equal(disposed,0);
});

test('static trajectory samples are reused across playback and gain changes but invalidate on keyframe edits',()=>{
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const functionSource=source.slice(source.indexOf('function trajectoryFor('),source.indexOf('\nconst time='));
  let calls=0;
  const cached=new Function('sampleTrajectory',`const trajectoryCache=new WeakMap();${functionSource};return trajectoryFor;`)(()=>{calls++;return [];});
  const object={azimuthDeg:0,elevationDeg:0,keyframes:[{timeSeconds:0,azimuthDeg:0,elevationDeg:0}]};
  const first=cached(object,10);for(let i=0;i<100;i++){object.gainDb=i;assert.equal(cached(object,10),first);}
  assert.equal(calls,1);object.keyframes[0].azimuthDeg=10;assert.notEqual(cached(object,10),first);assert.equal(calls,2);
});

test('artifact warnings replace completion claims without masking actual errors or progress',()=>{
  const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
  const messageSource=source.slice(source.indexOf('function message('),source.indexOf('\nasync function api('));
  const status={textContent:'',error:false,classList:{toggle(_,value){status.error=value}}};
  const message=new Function('$',`${messageSource};return message;`)(()=>status);
  message('全部恢复完成',false,{artifactWarnings:['双耳文件损坏'],artifacts:{surroundUrl:'/speaker.wav'}});
  assert.match(status.textContent,/扬声器通道可用/);assert.match(status.textContent,/双耳文件损坏/);assert.match(status.textContent,/重新渲染/);
  assert.doesNotMatch(status.textContent,/全部恢复完成/);assert.equal(status.error,true);
  message('全部恢复完成',false,{artifactWarnings:['扬声器缓存不完整'],artifacts:{}});
  assert.match(status.textContent,/项目已载入/);assert.doesNotMatch(status.textContent,/扬声器通道可用/);
  message('HTTP 409',true,{artifactWarnings:['旧警告']});assert.equal(status.textContent,'HTTP 409');
  message('正在渲染…');assert.equal(status.textContent,'正在渲染…');assert.equal(status.error,false);
  message('已导出',false,{artifactWarnings:[]});assert.equal(status.textContent,'已导出');
});
