import test from 'node:test';
import assert from 'node:assert/strict';
import {sampleTrajectory,pickTrajectory} from '../spatial-audio/trajectory.mjs';

const moving=()=>({azimuthDeg:179,elevationDeg:0,keyframes:[
  {timeSeconds:0,azimuthDeg:179,elevationDeg:0},
  {timeSeconds:1,azimuthDeg:-179,elevationDeg:30}
]});
const projected=(timeSeconds,x,y=0,visible=true)=>({timeSeconds,x,y,visible});

test('sampling retains a 1ms interval, sorted unique times, finite unit sphere and shortest rear arc',()=>{
  const object=moving();object.keyframes.splice(1,0,{timeSeconds:.001,azimuthDeg:179,elevationDeg:0});
  const samples=sampleTrajectory(object,1);
  assert.ok(samples.some(sample=>sample.timeSeconds===.001));
  assert.equal(new Set(samples.map(sample=>sample.timeSeconds)).size,samples.length);
  assert.ok(samples.every((sample,i)=>!i||sample.timeSeconds>samples[i-1].timeSeconds));
  assert.ok(samples.every(sample=>sample.position.every(Number.isFinite)&&Math.abs(Math.hypot(...sample.position)-1)<1e-10));
  const middle=sampleTrajectory(moving(),1,2)[1];
  assert.equal(middle.timeSeconds,.5);assert.ok(Math.abs(middle.position[0])<1e-10);assert.ok(middle.position[2]>.96);
  assert.equal(sampleTrajectory(moving(),1)[0].timeSeconds,0);
  assert.equal(sampleTrajectory(moving(),1).at(-1).timeSeconds,1);
});

test('static sources and invalid duration produce no trajectory',()=>{
  assert.deepEqual(sampleTrajectory({keyframes:[]},1),[]);
  for(const duration of [NaN,Infinity,0,-1,'1'])assert.deepEqual(sampleTrajectory(moving(),duration),[]);
});

test('picking uses CSS pixel threshold independent of projected scale and interpolates time',()=>{
  for(const scale of [1,3]){
    const samples=[projected(0,0),projected(10,100*scale)];
    const hit=pickTrajectory(samples,{x:25*scale,y:8});
    assert.equal(hit.timeSeconds,2.5);assert.equal(hit.distanceSquared,64);assert.equal(hit.ratio,.25);assert.equal(hit.segmentIndex,0);
    assert.equal(pickTrajectory(samples,{x:25*scale,y:8.01}),null);
  }
  assert.equal(pickTrajectory([projected(0,0),projected(1,100)],{x:50,y:5},{maxDistancePx:4}),null);
});

test('picking skips stationary, invisible and nonfinite segments',()=>{
  assert.equal(pickTrajectory([projected(0,1),projected(1,1)],{x:1,y:0}),null);
  assert.equal(pickTrajectory([projected(0,0),projected(1,100,0,false)],{x:50,y:0}),null);
  assert.equal(pickTrajectory([projected(0,NaN),projected(1,100)],{x:50,y:0}),null);
  assert.equal(pickTrajectory([],{x:0,y:0}),null);
  assert.equal(pickTrajectory([projected(0,0),projected(1,100)],{x:NaN,y:0}),null);
});

test('overlapping paths choose the nearest reference time while closer geometry wins',()=>{
  const samples=[projected(0,0),projected(1,100),projected(2,0)];
  assert.equal(pickTrajectory(samples,{x:50,y:0},{referenceTime:0}).timeSeconds,.5);
  const later=pickTrajectory(samples,{x:50,y:0},{referenceTime:2});
  assert.equal(later.timeSeconds,1.5);assert.equal(later.segmentIndex,1);
  const separated=[projected(0,0),projected(1,100),projected(2,100,4),projected(3,0,4)];
  assert.equal(pickTrajectory(separated,{x:50,y:0},{referenceTime:3}).timeSeconds,.5);
});

test('rejected closest or equally close segments leave other accepted candidates selectable',()=>{
  const accept=candidate=>candidate.segmentIndex===2;
  for(const offset of [0,4]){
    const samples=[projected(0,0),projected(1,100),projected(2,100,offset),projected(3,0,offset)];
    const hit=pickTrajectory(samples,{x:50,y:0},{referenceTime:0,accept});
    assert.deepEqual(hit,{timeSeconds:2.5,distanceSquared:offset**2,segmentIndex:2,ratio:.5});
  }
  assert.equal(pickTrajectory([projected(0,0),projected(1,100)],{x:50,y:0},{accept:()=>false}),null);
});
