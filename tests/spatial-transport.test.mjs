import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {moveKeyframeTime} from '../spatial-audio/automation.mjs';

const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
function extract(name){
  const match=new RegExp(`^(?:async )?function ${name}\\(`,'m').exec(source);
  assert.ok(match,`Missing actual application helper: ${name}`);
  const end=source.indexOf('\n}',match.index);
  assert.ok(end>match.index,`Missing function end: ${name}`);
  return source.slice(match.index,end+2);
}
function transport({playing=false,busy=false,withScene=true}={}){
  const create=new Function('options',`
    let busy=options.busy,scene=options.withScene?{durationSeconds:30}:null,mixControls;
    let playhead=12,playbackId='source-audio';
    const make=(id,src,duration)=>({id,duration,currentTime:12,paused:true,dataset:{},getAttribute:name=>name==='src'?src:null,pause(){this.paused=true},async play(){this.paused=false}});
    const nodes={'source-audio':make('source-audio','/original.wav',30),'preview':make('preview','/hrtf.wav',30.25),'stem-audio':make('stem-audio',null,30),'mix-audio':make('mix-audio','/compare.wav',30),seek:{},'time-current':{}};
    nodes['source-audio'].paused=!options.playing;
    const $=id=>nodes[id],time=String,draw=()=>{},updateTransport=()=>{},stopPlayhead=()=>{},startPlayhead=()=>{};
    const syncPlayhead=audio=>{playhead=audio.currentTime};
    ${extract('transportAudio')}
    ${extract('selectPlayback')}
    ${extract('seekTo')}
    return {nodes,selectPlayback,seekTo,state:()=>({playhead,playbackId})};
  `);
  return create({playing,busy,withScene});
}

test('explicit reference to HRTF switch preserves cursor, resumes only if playing and pauses competing audio',async()=>{
  for(const playing of [false,true]){
    const ui=transport({playing});ui.nodes['mix-audio'].paused=false;
    await ui.selectPlayback('preview');
    assert.equal(ui.state().playbackId,'preview');assert.equal(ui.nodes.preview.currentTime,12);
    assert.equal(ui.nodes.preview.paused,!playing);
    assert.equal(ui.nodes['source-audio'].paused,true);assert.equal(ui.nodes['mix-audio'].paused,true);
    await ui.selectPlayback('source-audio');
    assert.equal(ui.state().playbackId,'source-audio');assert.equal(ui.nodes['source-audio'].currentTime,12);
    assert.equal(ui.nodes['source-audio'].paused,!playing);assert.equal(ui.nodes.preview.paused,true);
  }
});

test('busy, unavailable and nontransport selection cannot change playback',async()=>{
  const locked=transport({playing:true,busy:true});await locked.selectPlayback('preview');
  assert.equal(locked.state().playbackId,'source-audio');assert.equal(locked.nodes['source-audio'].paused,false);
  const ui=transport();await ui.selectPlayback('stem-audio');await ui.selectPlayback('mix-audio');
  assert.equal(ui.state().playbackId,'source-audio');
});

test('seek includes actual HRTF tail and supports imported audio before scene exists',async()=>{
  const ui=transport();await ui.selectPlayback('preview');
  ui.seekTo(30.2);assert.equal(ui.nodes.preview.currentTime,30.2);assert.equal(ui.state().playhead,30.2);
  ui.seekTo(99);assert.equal(ui.nodes.preview.currentTime,30.25);assert.equal(ui.state().playhead,30.25);
  const imported=transport({withScene:false});imported.seekTo(18);
  assert.equal(imported.nodes['source-audio'].currentTime,18);assert.equal(imported.state().playhead,18);
});

function edits({dirty=true,conflict=false}={}){
  const create=new Function('options',`
    let scene={durationSeconds:30,objects:[{id:'other',azimuthDeg:45}]},revision=options.dirty?2:0,busy=false;
    let project={id:'a'.repeat(32),sceneHash:'original'};
    let begins=0,generation=0;const runtime={limits:{maxUploadBytes:100,maxNativeBundleBytes:100}};
    const begin=()=>{begins++;scene=null;revision=0;return ++generation};
    const initialScene=scene,requests=[],messages=[],nodes={'saved-projects':{value:'b'.repeat(32)},'source-route':{value:'native-stems-first-v1'}};
    const $=id=>nodes[id],clone=structuredClone,json=value=>({body:value});
    const setBusy=value=>{busy=value};
    const api=async(path,options_)=>{requests.push({path,...options_});if(options.conflict)throw Error('409 scene conflict');return {...project,scene:structuredClone(scene),sceneHash:'saved-hash'}};
    const accept=value=>{project=value;scene=structuredClone(value.scene);revision=0};
    const message=(text,error)=>messages.push({text,error});
    const projectOptions=()=>{nodes['saved-projects'].value=project.id};
    ${extract('saveEdits')}
    ${extract('restoreProject')}
    ${extract('loadInput')}
    ${extract('loadOnline')}
    return {saveEdits,restoreProject,loadInput,loadOnline,requests,messages,nodes,state:()=>({scene,revision,busy,project,begins,sameScene:scene===initialScene})};
  `);
  return create({dirty,conflict});
}

test('save conflict retains edited scene and revision, restores selection and unlocks',async()=>{
  const ui=edits({conflict:true});await assert.rejects(ui.saveEdits(),/409|未保存|冲突/);
  const state=ui.state();assert.equal(state.sameScene,true);assert.equal(state.scene.objects[0].azimuthDeg,45);
  assert.equal(state.revision,2);assert.equal(state.project.sceneHash,'original');assert.equal(state.busy,false);
  assert.equal(ui.nodes['saved-projects'].value,'a'.repeat(32));assert.ok(ui.messages.some(message=>message.error));
  assert.equal(ui.requests.length,1);assert.equal(ui.requests[0].body.expectedSceneHash,'original');
});

test('successful save updates hash via accept; unchanged scene does not post',async()=>{
  const ui=edits();await ui.saveEdits();assert.equal(ui.requests.length,1);
  assert.equal(ui.requests[0].path,`/projects/${'a'.repeat(32)}/scene`);
  assert.equal(ui.requests[0].body.scene.objects[0].azimuthDeg,45);
  assert.equal(ui.state().project.sceneHash,'saved-hash');assert.equal(ui.state().revision,0);assert.equal(ui.state().busy,false);
  const clean=edits({dirty:false});await clean.saveEdits();assert.equal(clean.requests.length,0);assert.equal(clean.state().busy,false);
});


test('save conflict blocks project restore, local import and online continuation before begin',async()=>{
  for(const action of ['restore','local','online']){
    const ui=edits({conflict:true});
    if(action==='restore')await ui.restoreProject('b'.repeat(32));
    else if(action==='local')await ui.loadInput({size:10});
    else await assert.rejects(ui.loadOnline('http://audio.example/new.wav',{jobId:'b'.repeat(32),autoPrepare:true}),/409|未保存|冲突/);
    const state=ui.state();
    assert.equal(state.begins,0,action);assert.equal(state.sameScene,true,action);
    assert.equal(state.scene.objects[0].azimuthDeg,45,action);assert.equal(state.revision,2,action);
    assert.equal(state.project.id,'a'.repeat(32),action);assert.equal(state.busy,false,action);
    assert.equal(ui.requests.length,1,action);assert.equal(ui.requests[0].path,`/projects/${'a'.repeat(32)}/scene`,action);
    assert.equal(ui.nodes['saved-projects'].value,'a'.repeat(32),action);
  }
});

test('oversized local input is rejected before saving or clearing current project',async()=>{
  const ui=edits();await ui.loadInput({size:101});
  const state=ui.state();assert.equal(state.begins,0);assert.equal(state.sameScene,true);
  assert.equal(state.revision,2);assert.equal(state.project.sceneHash,'original');assert.equal(state.busy,false);
  assert.equal(ui.requests.length,0);assert.ok(ui.messages.some(message=>message.error&&/限制|超过/.test(message.text)));
});

test('late play event from an already paused source cannot steal or stop current HRTF',()=>{
  const start=source.indexOf("\nfor(const id of ['preview','source-audio','stem-audio','mix-audio']){");
  assert.ok(start>=0,'Missing actual media event binding');
  const end=source.indexOf('\nfunction stopPlayhead()',start);
  const create=new Function(`
    let playbackId='preview',stops=0,starts=0,syncs=0,mixControls;
    const make=id=>({id,paused:id!=='preview',pauseCalls:0,listeners:{},getAttribute:()=>'/audio.wav',pause(){this.paused=true;this.pauseCalls++},addEventListener(event,fn){this.listeners[event]=fn}});
    const nodes=Object.fromEntries(['preview','source-audio','stem-audio','mix-audio'].map(id=>[id,make(id)]));
    const $=id=>nodes[id],stopPlayhead=()=>stops++,startPlayhead=()=>starts++,syncPlayhead=()=>syncs++,updateTransport=()=>{};
    ${extract('transportAudio')}
    ${source.slice(start,end)}
    return {nodes,state:()=>({playbackId,stops,starts,syncs})};
  `);
  const ui=create();ui.nodes['source-audio'].listeners.play();
  assert.equal(ui.nodes.preview.paused,false);assert.equal(ui.nodes.preview.pauseCalls,0);
  assert.deepEqual(ui.state(),{playbackId:'preview',stops:0,starts:0,syncs:0});
  ui.nodes['source-audio'].paused=false;ui.nodes['source-audio'].listeners.play();
  assert.equal(ui.state().playbackId,'source-audio');assert.equal(ui.nodes.preview.paused,true);
  assert.equal(ui.nodes.preview.pauseCalls,1);assert.equal(ui.nodes['source-audio'].paused,false);
  assert.equal(ui.state().starts,1);assert.equal(ui.state().syncs,1);
});

test('interrupted playback switch ignores only expected AbortError, not unsupported audio',async()=>{
  const paused=transport({playing:true});
  paused.nodes.preview.play=async()=>{paused.nodes.preview.paused=true;throw Object.assign(Error('interrupted by pause'),{name:'AbortError'});};
  await assert.doesNotReject(paused.selectPlayback('preview'));
  const superseded=transport({playing:true});let rejectOld;
  superseded.nodes.preview.play=()=>{superseded.nodes.preview.paused=false;return new Promise((_,reject)=>{rejectOld=reject;});};
  const old=superseded.selectPlayback('preview');await superseded.selectPlayback('source-audio');
  rejectOld(Object.assign(Error('superseded'),{name:'AbortError'}));await assert.doesNotReject(old);
  assert.equal(superseded.state().playbackId,'source-audio');assert.equal(superseded.nodes['source-audio'].paused,false);
  const invalid=transport({playing:true});
  invalid.nodes.preview.play=async()=>{throw Object.assign(Error('unsupported codec'),{name:'NotSupportedError'});};
  await assert.rejects(invalid.selectPlayback('preview'),{name:'NotSupportedError'});
});

test('source switch detects actual pending-event playback but never treats mix reference as transport',async()=>{
  for(const active of ['preview','stem-audio']){
    const ui=transport();
    assert.equal(ui.state().playbackId,'source-audio');assert.equal(ui.nodes['source-audio'].paused,true);
    ui.nodes['stem-audio'].getAttribute=name=>name==='src'?'/stem.wav':null;
    ui.nodes[active].paused=false;const target=active==='preview'?'stem-audio':'preview';
    await ui.selectPlayback(target);
    assert.equal(ui.nodes[target].paused,false,`${active} playback must continue despite a pending play event`);
    assert.equal(ui.nodes[active].paused,true);assert.equal(ui.nodes[target].currentTime,12);
  }
  const mixOnly=transport();mixOnly.nodes['mix-audio'].paused=false;
  await mixOnly.selectPlayback('preview');
  assert.equal(mixOnly.nodes.preview.paused,true,'independent mix playback must not start HRTF');
  assert.equal(mixOnly.nodes['mix-audio'].paused,true);assert.equal(mixOnly.state().playbackId,'preview');
});

test('keyframe time editing synchronizes the active reference audio and visual cursor',()=>{
  const handler=source.split('\n').find(line=>line.startsWith("$('keyframe-time').addEventListener('change'"));
  const run=new Function('moveKeyframeTime','playing',`
    let scene={durationSeconds:30,objects:[{id:'x',keyframes:[{timeSeconds:0},{timeSeconds:5},{timeSeconds:30}]}]};
    let selectedTime=5,playhead=5,selectedId='x',busy=false,revision=0,change;
    const audio={currentTime:5,paused:!playing,duration:30};
    const nodes={'keyframe-time':{value:'12',addEventListener(type,fn){change=fn}},seek:{},'time-current':{}};
    const $=id=>nodes[id],currentObject=()=>scene.objects[0],transportAudio=()=>audio;
    const clearPreview=()=>{playhead=0},renderStems=()=>{},renderParameters=()=>{},scheduleDraw=()=>{},message=()=>{},draw=()=>{},time=String;
    const replaceCurrent=next=>{scene.objects[0]=next};
    ${extract('seekTo')}
    ${extract('changed')}
    ${handler}
    change();return {selectedTime,playhead,audioTime:audio.currentTime,paused:audio.paused,revision};
  `);
  for(const playing of [false,true])assert.deepEqual(run(moveKeyframeTime,playing),{selectedTime:12,playhead:12,audioTime:12,paused:!playing,revision:1});
});
