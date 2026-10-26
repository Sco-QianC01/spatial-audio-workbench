import test from 'node:test';
import assert from 'node:assert/strict';
import {pointAt,upsertKeyframe,moveKeyframeTime,removeKeyframe,createAutomation} from '../spatial-audio/automation.mjs';

const object=()=>({id:'vocals',label:'人声',azimuthDeg:20,elevationDeg:10,widthDeg:30,gainDb:-3,muted:true,keyframes:[]});
const animated=()=>({...object(),keyframes:[{timeSeconds:0,azimuthDeg:179,elevationDeg:0},{timeSeconds:1,azimuthDeg:-179,elevationDeg:30}]});

test('pointAt keeps shortest arc, negative half-turn tie and endpoint clamps',()=>{
  assert.deepEqual(pointAt(object(),.5),{azimuthDeg:20,elevationDeg:10});
  assert.deepEqual(pointAt(animated(),.5),{azimuthDeg:-180,elevationDeg:15});
  assert.deepEqual(pointAt(animated(),-1),{azimuthDeg:179,elevationDeg:0});
  assert.deepEqual(pointAt(animated(),2),{azimuthDeg:-179,elevationDeg:30});
  for(const end of [-180,180]){
    const item=animated();item.keyframes[0].azimuthDeg=0;item.keyframes[1].azimuthDeg=end;
    assert.equal(pointAt(item,.5).azimuthDeg,-90);
  }
});

test('first insertion adds static endpoints, rounds only interior time and preserves object fields',()=>{
  const source=object(),before=structuredClone(source),result=upsertKeyframe(source,.4567,1.23456);
  assert.deepEqual(source,before);assert.notEqual(result,source);
  assert.deepEqual(result.keyframes.map(frame=>frame.timeSeconds),[0,.457,1.23456]);
  assert.ok(result.keyframes.every(frame=>frame.azimuthDeg===20&&frame.elevationDeg===10));
  assert.equal(result.muted,true);assert.equal(result.gainDb,-3);assert.equal(result.widthDeg,30);
  assert.equal(upsertKeyframe(source,1.23456,1.23456).keyframes.length,2);
});

test('insertion uses current interpolated position and updating an existing time does not duplicate',()=>{
  const result=upsertKeyframe(animated(),.5,1);
  assert.deepEqual(result.keyframes[1],{timeSeconds:.5,azimuthDeg:-180,elevationDeg:15});
  assert.equal(upsertKeyframe(result,.5001,1).keyframes.length,3);
  assert.equal(animated().keyframes.length,2);
});

test('nonfinite/out-of-range times, close spacing and more than 32 nodes are rejected',()=>{
  for(const time of [NaN,Infinity,-.001,1.001,'0',true])assert.throws(()=>upsertKeyframe(object(),time,1),RangeError);
  for(const duration of [NaN,Infinity,0,-1])assert.throws(()=>upsertKeyframe(object(),0,duration),RangeError);
  assert.throws(()=>upsertKeyframe(object(),.1001,.1003),/1毫秒/);
  const item={...object(),keyframes:Array.from({length:32},(_,i)=>({timeSeconds:i/31,azimuthDeg:0,elevationDeg:0}))};
  assert.throws(()=>upsertKeyframe(item,.5,1),/32/);
  assert.equal(upsertKeyframe(item,0,1).keyframes.length,32);
});

test('move preserves positions, fixes endpoints and enforces sorted 1ms spacing',()=>{
  const source=upsertKeyframe(animated(),.5,1),moved=moveKeyframeTime(source,1,.7004,1);
  assert.equal(moved.keyframes[1].timeSeconds,.7);assert.equal(source.keyframes[1].timeSeconds,.5);
  assert.equal(moved.keyframes[1].azimuthDeg,-180);
  for(const time of [0,.9999,1,NaN])assert.throws(()=>moveKeyframeTime(source,1,time,1),RangeError);
  assert.throws(()=>moveKeyframeTime(source,0,.2,1),/首尾/);
  assert.throws(()=>moveKeyframeTime(source,2,.8,1),/首尾/);
  assert.equal(moveKeyframeTime(source,2,1,1).keyframes.at(-1).timeSeconds,1);
  assert.throws(()=>moveKeyframeTime(source,99,.5,1),/不存在/);
});

test('remove only changes the interior array, retaining fields and two endpoints',()=>{
  const source=upsertKeyframe(animated(),.5,1),result=removeKeyframe(source,1);
  assert.equal(result.keyframes.length,2);assert.equal(source.keyframes.length,3);
  assert.equal(result.gainDb,source.gainDb);assert.equal(result.muted,source.muted);
  for(const index of [0,2,-1,1.5])assert.throws(()=>removeKeyframe(source,index),RangeError);
});

// Minimal DOM records attribute writes so time-only updates can prove path reuse.
function fakeDocument(){
  class Element{
    constructor(tag,doc){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.dataset={};this.style={};this.attributes={};this.listeners=new Map();this.writes=0;this.classList={toggle:()=>{}};}
    append(...nodes){for(const node of nodes){node.parentNode=this;this.children.push(node);}}
    setAttribute(key,value){this.attributes[key]=String(value);this.writes++;}
    getAttribute(key){return this.attributes[key];}
    addEventListener(name,fn){this.listeners.set(name,fn);}
    removeEventListener(name,fn){if(this.listeners.get(name)===fn)this.listeners.delete(name);}
    remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(child=>child!==this);}
    getBoundingClientRect(){return {left:100,width:1000};}
  }
  const doc={createElement:tag=>new Element(tag,doc),createElementNS:(_,tag)=>new Element(tag,doc)};
  return doc;
}

test('timeline caches paths during playback, splits rear seam, delegates selection/seek and disposes',()=>{
  const doc=fakeDocument(),container=doc.createElement('div'),selected=[],seek=[];
  const scene={durationSeconds:1,objects:[animated()]};
  const ui=createAutomation({container,onSelect:(...args)=>selected.push(args),onSeek:t=>seek.push(t)});
  const state={scene,selectedId:'vocals',selectedTime:0,time:0,disabled:false};
  ui.update(state);
  const [name,lane]=container.children[0].children,[svg,cursor,label,start,end]=lane.children,[az,el]=svg.children;
  assert.equal(name.textContent,'轨道 04');
  assert.match(lane.getAttribute('aria-label'),/^轨道 04：/);
  assert.match(start.getAttribute('aria-label'),/^轨道 04 /);
  assert.equal(scene.objects[0].label,'人声');
  assert.equal((az.getAttribute('d').match(/M/g)||[]).length,2);
  assert.equal((el.getAttribute('d').match(/M/g)||[]).length,1);
  const writes=az.writes,path=az.getAttribute('d');ui.update({...state,time:.5});
  assert.equal(az.writes,writes);assert.equal(az.getAttribute('d'),path);assert.equal(cursor.style.left,'50%');
  assert.equal(label.textContent,'0:00.5 / 0:01.0');assert.equal(lane.children.length,5);
  container.listeners.get('click')({target:end});assert.deepEqual(selected,[['vocals',1]]);
  container.listeners.get('click')({target:lane,clientX:350});assert.deepEqual(seek,[.25]);
  ui.update({...state,disabled:true});container.listeners.get('click')({target:start});assert.equal(selected.length,1);assert.equal(name.disabled,true);
  ui.dispose();ui.dispose();assert.equal(container.children.length,0);assert.equal(container.listeners.size,0);
});

test('azimuth ending at +180 splits its wrapped -180 endpoint rather than drawing a false jump',()=>{
  const doc=fakeDocument(),container=doc.createElement('div'),item=animated();item.keyframes[1].azimuthDeg=180;
  const ui=createAutomation({container});ui.update({scene:{durationSeconds:1,objects:[item]},time:0});
  const path=container.children[0].children[1].children[0].children[0].getAttribute('d');
  assert.equal((path.match(/M/g)||[]).length,2);ui.dispose();
});

test('editing keyframe positions reuses existing timeline buttons and updates their coordinates',()=>{
  const doc=fakeDocument(),container=doc.createElement('div'),item=animated(),scene={durationSeconds:1,objects:[item]};
  const ui=createAutomation({container});ui.update({scene});
  const lane=container.children[0].children[1],start=lane.children[3],end=lane.children[4],before=end.style.top;
  for(let i=0;i<100;i++){item.keyframes[1].azimuthDeg=i;ui.update({scene});}
  assert.equal(lane.children[3],start);assert.equal(lane.children[4],end);assert.equal(lane.children.length,5);assert.notEqual(end.style.top,before);
  item.keyframes.pop();ui.update({scene});assert.equal(lane.children.length,4);assert.equal(lane.children[3],start);ui.dispose();
});

test('timeline names preserve playback while real keyframe nodes seek',()=>{
  const doc=fakeDocument(),container=doc.createElement('div');
  const audio={currentTime:.6,paused:false},calls=[];
  const ui=createAutomation({container,getState:()=>({time:audio.currentTime,selectedTime:0}),onSelect:(id,at,move=true)=>{calls.push([id,at,move]);if(move)audio.currentTime=at;}});
  ui.update({scene:{durationSeconds:1,objects:[animated()]},selectedTime:0,time:.6});
  const [name,lane]=container.children[0].children;
  container.listeners.get('click')({target:name});
  assert.deepEqual(calls,[['vocals',.6,false]]);
  assert.deepEqual(audio,{currentTime:.6,paused:false});
  container.listeners.get('click')({target:lane.children.at(-1)});
  assert.deepEqual(calls.at(-1),['vocals',1,true]);
  assert.deepEqual(audio,{currentTime:1,paused:false});ui.dispose();
});
