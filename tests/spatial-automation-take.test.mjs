import test from 'node:test';
import assert from 'node:assert/strict';
import {createAutomationTake,MAX_KEYFRAMES} from '../spatial-audio/automation-take.mjs';
import {pointAt} from '../spatial-audio/automation.mjs';

const source=()=>({id:'other',azimuthDeg:0,elevationDeg:0,widthDeg:30,gainDb:-3,muted:false,keyframes:[]});
const p=(az,el=0)=>({azimuthDeg:az,elevationDeg:el});
const error=(a,b)=>Math.abs(((a-b+180)%360+360)%360-180);

test('write records forward/back motion on the audio timeline and replays positions',()=>{
  const object=source(),take=createAutomationTake(object,0,120);
  for(let i=1;i<=2400;i++)take.record(i/20,p(i<=1200?i/1200*170:170, i<=1200?i/1200*25:25));
  const next=take.finish(120,p(170,25));
  assert.deepEqual(object,source());assert.equal(next.gainDb,-3);assert.equal(next.widthDeg,30);
  assert.ok(next.keyframes.length<10,'constant holds and straight movements should not create thousands of nodes');
  assert.ok(error(pointAt(next,30).azimuthDeg,85)<.25);
  assert.ok(error(pointAt(next,60).azimuthDeg,170)<.25);
  assert.ok(error(pointAt(next,110).azimuthDeg,170)<.25);
});

test('a short punch-in preserves earlier and later automation with 1ms boundary guards',()=>{
  const object={...source(),keyframes:[{timeSeconds:0,...p(0)},{timeSeconds:100,...p(100,40)}]};
  const original=structuredClone(object),take=createAutomationTake(object,30,100);
  take.record(35,p(-90,20));take.record(40,p(-120,30));
  const next=take.finish(40,p(-120,30));
  for(const at of [0,10,29.99,40.01,60,99])assert.deepEqual(pointAt(next,at),pointAt(original,at));
  assert.deepEqual(pointAt(next,35),p(-90,20));assert.deepEqual(object,original);
  assert.ok(next.keyframes.every((f,i)=>!i||f.timeSeconds>next.keyframes[i-1].timeSeconds));
});

test('a first middle-of-track write keeps its final position through the remaining audio',()=>{
  const object={...source(),...p(25,10)},take=createAutomationTake(object,30,100);
  take.record(35,p(-90,20));
  const result=take.finish(40,p(-120,30));
  for(const at of [0,10,29.99])assert.deepEqual(pointAt(result,at),p(25,10));
  assert.deepEqual(pointAt(result,35),p(-90,20));
  for(const at of [40,40.001,60,100])assert.deepEqual(pointAt(result,at),p(-120,30));
  assert.deepEqual(object,{...source(),...p(25,10)});
  assert.ok(result.keyframes.every((f,i)=>!i||f.timeSeconds>result.keyframes[i-1].timeSeconds));
});

test('seam crossing and a whole revolution are not flattened into the wrong rotation',()=>{
  for(const start of [0,170]){
    const object={...source(),...p(start)},take=createAutomationTake(object,0,10);
    for(let i=1;i<=200;i++)take.record(i/20,p(start+i/200*360));
    const next=take.finish(10,p(start+360));
    for(let i=0;i<=200;i++)assert.ok(error(pointAt(next,i/20).azimuthDeg,start+i/200*360)<=.251);
  }
});

test('duplicate millisecond samples replace, zero-length writes preserve original and invalid inputs reject',()=>{
  const object=source(),take=createAutomationTake(object,1,10);
  take.record(1.2,p(12));take.record(1.2001,p(15));assert.equal(take.count,2);
  assert.throws(()=>take.record(1.1,p(20)),/不能向前/);
  for(const at of [NaN,Infinity,-1,11])assert.throws(()=>take.record(at,p(0)),RangeError);
  assert.throws(()=>take.record(2,p(0,91)),RangeError);
  assert.deepEqual(createAutomationTake(object,1,10).finish(1,p(90)),object);
  assert.throws(()=>createAutomationTake(object,10,10),RangeError);
  assert.equal(MAX_KEYFRAMES,12002);
});

test('entering Write at a sub-millisecond playback time does not reject its own first sample',()=>{
  const take=createAutomationTake(source(),1.2346,10);
  take.record(1.2346,p(30));take.record(1.3,p(40));
  const result=take.finish(1.4,p(50));
  assert.deepEqual(pointAt(result,1.235),p(30));
});

test('a complete 600-second take at 20Hz preserves its final position',()=>{
  const object=source(),take=createAutomationTake(object,0,600);
  for(let i=1;i<=12000;i++)take.record(i/20,p(i/12000*150));
  const result=take.finish(600,p(160,25));
  assert.deepEqual(pointAt(result,600),p(160,25));
  assert.ok(result.keyframes.length<=MAX_KEYFRAMES);
  assert.deepEqual(object,source());
});

test('sample capacity failure preserves the take and reserves a final sample for finishing',()=>{
  const object=source(),take=createAutomationTake(object,0,601);
  for(let i=1;i<MAX_KEYFRAMES-1;i++)take.record(i/20,p(10));
  const count=take.count,last=take.lastTime;
  assert.throws(()=>take.record(last+.05,p(20)),/上限/);
  assert.equal(take.count,count);assert.equal(take.lastTime,last);
  const result=take.finish(last+.05,p(20,15));
  assert.deepEqual(pointAt(result,last+.05),p(20,15));
  assert.deepEqual(object,source());
});
