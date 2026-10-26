import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {initMix} from '../spatial-audio/mix.mjs';

const source=readFileSync(new URL('../spatial-audio/app.mjs',import.meta.url),'utf8');
function extract(name){
  const match=new RegExp(`^(?:async )?function ${name}\\(`,'m').exec(source);
  return source.slice(match.index,source.indexOf('\n}',match.index)+2);
}
function setup(){
  const originalDocument=globalThis.document,originalOption=globalThis.Option;
  const nodes=new Map();
  const make=()=>({
    children:[],listeners:{},dataset:{},checked:false,value:'',paused:true,currentTime:8,duration:30,playCalls:0,
    classList:{toggle(){}},setAttribute(){},getAttribute(name){return name==='src'?this.src:null;},
    removeAttribute(name){if(name==='src')this.src=null;},load(){},pause(){this.paused=true;},
    async play(){this.paused=false;this.playCalls++;for(const fn of this.listeners.play??[])fn();},
    addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);},
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
    querySelectorAll(){return [];},
    async emit(name){for(const fn of this.listeners[name]??[])await fn();}
  });
  const $=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
  globalThis.document={getElementById:$,createElement:make};
  globalThis.Option=function(text,value){return {text,value};};
  const metrics={baseline:{},candidate:{}};
  const state={busy:false,runtime:{projects:[]},project:{id:'project',mixRuns:[{id:'run',urls:{baseline:'/a.wav',candidate:'/b.wav'},metrics,loudnessMatch:{status:'matched'}}]}};
  const errors=[];
  const mix=initMix({getState:()=>state,run(){},render(){},message:text=>errors.push(text)});
  mix.refresh();
  const audio=$('mix-audio');
  const start=source.indexOf("\nfor(const id of ['preview','source-audio','stem-audio','mix-audio']){");
  const end=source.indexOf('\nfunction stopPlayhead()',start);
  const selectPlayback=new Function('mixControls','$',`
    let busy=false,playbackId='source-audio',playhead=8,playbackFrame=0,scene={durationSeconds:30};
    const stopPlayhead=()=>{},startPlayhead=()=>{},syncPlayhead=()=>{},updateTransport=()=>{},draw=()=>{},time=String;
    ${extract('transportAudio')}
    ${extract('seekTo')}
    ${extract('selectPlayback')}
    ${source.slice(start,end)}
    return selectPlayback;
  `)(mix,$);
  $('source-audio').src='/original.wav';
  return {$,audio,mix,state,selectPlayback,errors,async ready(){await audio.onloadedmetadata();},
    dispose(){globalThis.document=originalDocument;globalThis.Option=originalOption;}};
}

for(const pending of [false,true]){
  test(`removing all candidates clears ${pending?'loading':'playing'} comparison and permits a replacement`,async()=>{
    const ui=setup();
    try{
      await ui.ready();await ui.audio.play();
      if(pending)await ui.$('mix-b').emit('click');
      const staleMetadata=ui.audio.onloadedmetadata,original=ui.state.project.mixRuns[0];
      ui.state.project.mixRuns=[];ui.mix.refresh();
      assert.equal(ui.audio.paused,true);assert.equal(ui.audio.getAttribute('src'),null);
      assert.equal(ui.$('mix-comparison').hidden,true);
      const plays=ui.audio.playCalls;
      await staleMetadata();
      assert.equal(ui.audio.playCalls,plays,'invalidated metadata cannot restart a removed candidate');
      ui.state.project.mixRuns=[{...original,id:'replacement',urls:{baseline:'/new-a.wav',candidate:'/new-b.wav'}}];
      ui.mix.refresh();
      assert.equal(ui.audio.getAttribute('src'),'/new-a.wav');
      await ui.ready();assert.equal(ui.$('mix-b').disabled,false);
      await ui.audio.play();await ui.$('mix-b').emit('click');
      await staleMetadata();
      assert.equal(ui.audio.paused,true,'old callback cannot finish a replacement load');
      await ui.ready();
      assert.equal(ui.audio.getAttribute('src'),'/new-b.wav');assert.equal(ui.audio.paused,false);
      assert.equal(ui.$('mix-b').disabled,false);assert.deepEqual(ui.errors,[]);
    }finally{ui.dispose();}
  });
}

test('A/B switching resumes active comparison after metadata and unlocks controls',async()=>{
  const ui=setup();
  try{
    await ui.ready();await ui.audio.play();
    await ui.$('mix-b').emit('click');
    assert.equal(ui.audio.paused,true);assert.equal(ui.$('mix-b').disabled,true);
    await ui.ready();
    assert.equal(ui.audio.paused,false);assert.equal(ui.audio.currentTime,8);
    assert.equal(ui.$('mix-b').disabled,false);assert.deepEqual(ui.errors,[]);
  }finally{ui.dispose();}
});

for(const action of ['select paused source','play another source']){
  test(`pending A/B resume yields to ${action}, unlocks and supports subsequent playback`,async()=>{
    const ui=setup();
    try{
      await ui.ready();await ui.audio.play();await ui.$('mix-b').emit('click');
      if(action==='select paused source')await ui.selectPlayback('source-audio');
      else await ui.$('source-audio').play();
      await ui.ready();
      assert.equal(ui.audio.paused,true,'late metadata must not reclaim playback');
      assert.equal(ui.$('mix-a').disabled,false,'loading still completes after cancellation');
      assert.equal(ui.$('mix-versions').disabled,false);
      await ui.audio.play();await ui.$('mix-a').emit('click');await ui.ready();
      assert.equal(ui.audio.paused,false,'new A/B intent can resume normally');
      assert.equal(ui.$('source-audio').paused,true);assert.deepEqual(ui.errors,[]);
    }finally{ui.dispose();}
  });
}
