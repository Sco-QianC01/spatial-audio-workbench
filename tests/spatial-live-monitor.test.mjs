import test from 'node:test';
import assert from 'node:assert/strict';
import {createLiveMonitor} from '../spatial-audio/live-monitor.mjs';
import {stereoDirectionToVector} from '../spatial-audio/coordinates.mjs';

function fixture(fetchAudio){
  const param=()=>({value:0,events:[],cancelScheduledValues(){this.events=[];},setTargetAtTime(value,time,tau){this.value=value;this.events.push([value,time,tau]);}});
  const node=()=>({connect(target){return target;},disconnect(){this.disconnected=true;}});
  const context={
    currentTime:12,destination:{},sources:[],panners:[],gains:[],resumes:0,
    resume(){this.resumes++;return Promise.resolve();},close(){this.closed=true;},
    async decodeAudioData(){return {length:2,sampleRate:48000,numberOfChannels:2,getChannelData:()=>new Float32Array([1,-.5])};},
    createBuffer(){return {copyToChannel(){}};},
    createBufferSource(){const source={...node(),playbackRate:param(),start(...args){this.started=args;},stop(){this.stopped=true;}};this.sources.push(source);return source;},
    createGain(){const gain={...node(),gain:param()};this.gains.push(gain);return gain;},
    createPanner(){const panner={...node(),positionX:param(),positionY:param(),positionZ:param()};this.panners.push(panner);return panner;}
  };
  const fetched=[];
  const monitor=createLiveMonitor({contextFactory:()=>context,fetchAudio:async url=>{fetched.push(url);return fetchAudio?fetchAudio(url):{ok:true,arrayBuffer:async()=>new ArrayBuffer(0)};}});
  const scene={objects:[{id:'a',audioUrl:'/a',azimuthDeg:0,elevationDeg:0,widthDeg:60,gainDb:0,keyframes:[{timeSeconds:0,azimuthDeg:0,elevationDeg:0},{timeSeconds:10,azimuthDeg:90,elevationDeg:20}]}]};
  return {monitor,context,scene,fetched};
}

test('live stereo voices share a clock and seek restarts with cached mono buffers',async()=>{
  const {monitor,context,scene,fetched}=fixture();
  const preparing=monitor.prepare(scene);
  assert.equal(context.resumes,1);
  assert.equal(monitor.ready,false);
  await preparing;
  monitor.start(scene,2,{rate:1.5});
  assert.equal(monitor.active,true);
  assert.deepEqual(context.sources.map(source=>source.started),[[12,2],[12,2]]);
  assert.ok(context.sources.every(source=>source.playbackRate.value===1.5));
  assert.equal(context.panners[0].panningModel,'HRTF');
  assert.equal(context.panners[0].rolloffFactor,0);
  assert.equal(context.gains[1].gain.value,1/Math.SQRT2);
  assert.equal(monitor.headroomGain,.9/Math.SQRT2);
  monitor.stop();assert.equal(context.sources[0].stopped,true);
  context.currentTime=20;monitor.start(scene,7);
  assert.deepEqual(context.sources[2].started,[20,7]);
  await monitor.prepare(scene);assert.deepEqual(fetched,['/a']);
  await monitor.dispose();assert.equal(context.closed,true);assert.equal(monitor.ready,false);assert.equal(monitor.active,false);
});

test('read positions, write overrides, mute, and gain update without accumulating events',async()=>{
  const {monitor,context,scene}=fixture();
  scene.objects[0].muted=true;
  await monitor.prepare(scene);monitor.start(scene,0);
  assert.equal(context.gains[1].gain.value,0);
  scene.objects[0].muted=false;
  monitor.update(scene,5);
  const panner=context.panners[0],coords=()=>[panner.positionX.value,panner.positionY.value,panner.positionZ.value];
  assert.deepEqual(coords(),stereoDirectionToVector(45,10,30));
  monitor.update(scene,5,{a:{azimuthDeg:-90,elevationDeg:40}});
  assert.deepEqual(coords(),stereoDirectionToVector(-90,40,30));
  scene.objects[0].muted=true;monitor.update(scene,6);assert.equal(context.gains[1].gain.value,0);
  scene.objects[0].muted=false;scene.objects[0].gainDb=-6;monitor.update(scene,7);
  assert.equal(context.gains[1].gain.value,10**(-6/20)/Math.SQRT2);
  assert.equal(panner.positionX.events.length,1);
});

test('URL changes require fresh preparation and errors never mark ready',async()=>{
  const {monitor,scene,fetched}=fixture();
  await monitor.prepare(scene);scene.objects[0].audioUrl='/new';
  assert.throws(()=>monitor.start(scene,0),/not prepared/);
  await monitor.prepare(scene);assert.deepEqual(fetched,['/a','/new']);
  const failed=fixture(async()=>({ok:false,status:500}));
  await assert.rejects(failed.monitor.prepare(failed.scene),/500/);
  assert.equal(failed.monitor.ready,false);assert.equal(failed.monitor.active,false);
  const decode=fixture();decode.context.decodeAudioData=async()=>{throw new Error('decode');};
  await assert.rejects(decode.monitor.prepare(decode.scene),/decode/);assert.equal(decode.monitor.ready,false);
});

test('stop or dispose during pending preparation cannot activate stale playback',async()=>{
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  const {monitor,context,scene}=fixture(async()=>{await pending;return {ok:true,arrayBuffer:async()=>new ArrayBuffer(0)};});
  const preparing=monitor.prepare(scene);monitor.stop();release();
  assert.equal(await preparing,false);assert.equal(monitor.ready,false);
  assert.throws(()=>monitor.start(scene,0),/not prepared/);assert.equal(context.sources.length,0);
  const other=fixture();const disposed=other.monitor.prepare(other.scene);await other.monitor.dispose();
  assert.equal(await disposed,false);assert.equal(other.monitor.active,false);
});
