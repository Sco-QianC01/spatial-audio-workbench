import {stereoDirectionToVector} from './coordinates.mjs';
import {pointAt} from './automation.mjs';

export function createLiveMonitor({
  contextFactory=()=>new (globalThis.AudioContext??globalThis.webkitAudioContext)(),
  fetchAudio=url=>fetch(url)
}={}){
  let context,disposed=false,epoch=0,prepared=null,voices=[],master=null,headroom=1,initializing=false;
  const cache=new Map();
  const signature=scene=>JSON.stringify(scene.objects.map(({id,audioUrl})=>[id,audioUrl]));
  function stop(){
    epoch++;
    for(const voice of voices){
      try{voice.source.stop();}catch{}
      voice.source.disconnect();voice.gain.disconnect();voice.panner.disconnect();
    }
    voices=[];master?.disconnect();master=null;
  }
  function set(param,value){
    const now=context.currentTime;
    param.cancelScheduledValues?.(now);
    if(initializing)param.value=value;
    else if(param.setTargetAtTime)param.setTargetAtTime(value,now,.015);
    else param.setValueAtTime(value,now);
  }
  function update(scene,time,overrides={}){
    if(!voices.length)return;
    let bound=0;
    for(const object of scene.objects){
      const entry=cache.get(object.audioUrl);
      if(entry&&!object.muted)bound+=entry.peak*10**((object.gainDb??0)/20)*Math.SQRT2;
    }
    headroom=bound>0?Math.min(1,.9/bound):1;
    set(master.gain,headroom);
    const objects=new Map(scene.objects.map(object=>[object.id,object]));
    for(const voice of voices){
      const object=objects.get(voice.id);
      if(!object){set(voice.gain.gain,0);continue;}
      const point=overrides[object.id]??pointAt(object,time);
      const xyz=stereoDirectionToVector(point.azimuthDeg,point.elevationDeg,voice.side*(object.widthDeg??0)/2);
      [voice.panner.positionX,voice.panner.positionY,voice.panner.positionZ].forEach((param,i)=>set(param,xyz[i]));
      set(voice.gain.gain,object.muted?0:10**((object.gainDb??0)/20)/Math.SQRT2);
    }
  }
  return {
    async prepare(scene){
      if(disposed)throw new Error('Live monitor disposed.');
      context??=contextFactory();
      const resumed=context.resume();
      const token=++epoch,key=signature(scene);
      prepared=null;
      await resumed;
      const urls=[...new Set(scene.objects.map(object=>object.audioUrl))];
      for(const url of cache.keys())if(!urls.includes(url))cache.delete(url);
      await Promise.all(urls.map(async url=>{
        if(cache.has(url))return;
        const response=await fetchAudio(url);
        if(!response.ok)throw new Error(`Live audio fetch failed (${response.status}).`);
        const input=await context.decodeAudioData(await response.arrayBuffer());
        let peak=0;
        const mono=[0,1].map(channel=>{
          const data=input.getChannelData(Math.min(channel,input.numberOfChannels-1));
          for(const sample of data){
            if(!Number.isFinite(sample))throw new Error('Invalid decoded audio.');
            peak=Math.max(peak,Math.abs(sample));
          }
          const buffer=context.createBuffer(1,input.length,input.sampleRate);
          buffer.copyToChannel(data,0);
          return buffer;
        });
        if(!disposed&&token===epoch)cache.set(url,{mono,peak});
      }));
      if(disposed||token!==epoch)return false;
      prepared=key;
      return true;
    },
    start(scene,offset,{rate=1}={}){
      if(disposed||!prepared||prepared!==signature(scene))throw new Error('Live audio is not prepared.');
      if(!Number.isFinite(offset)||offset<0||!Number.isFinite(rate)||rate<=0)throw new RangeError('Invalid live playback time or rate.');
      stop();
      master=context.createGain();master.connect(context.destination);
      const now=context.currentTime;
      try{
        for(const object of scene.objects)for(let channel=0;channel<2;channel++){
          const source=context.createBufferSource(),gain=context.createGain(),panner=context.createPanner();
          source.buffer=cache.get(object.audioUrl).mono[channel];source.playbackRate.value=rate;
          panner.panningModel='HRTF';panner.distanceModel='inverse';panner.rolloffFactor=0;
          source.connect(gain).connect(panner).connect(master);
          voices.push({id:object.id,side:channel===0?1:-1,source,gain,panner});
        }
        initializing=true;
        update(scene,offset);
        initializing=false;
        for(const {source} of voices)source.start(now,offset);
      }catch(error){initializing=false;stop();throw error;}
    },
    update,stop,
    dispose(){
      if(disposed)return;
      disposed=true;stop();prepared=null;cache.clear();
      return context?.close();
    },
    get ready(){return !disposed&&prepared!==null;},
    get active(){return voices.length>0;},
    // Conservative PCM bound for safe monitoring only, not final loudness matching.
    get headroomGain(){return headroom;}
  };
}
