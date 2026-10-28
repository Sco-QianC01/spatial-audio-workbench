import {pointAt} from './automation.mjs';

export const MAX_KEYFRAMES=12002;
const wrap=value=>((value+180)%360+360)%360-180;
const position=value=>{
  if(!value||![value.azimuthDeg,value.elevationDeg].every(Number.isFinite)||Math.abs(value.elevationDeg)>90)
    throw new RangeError('空间坐标无效。');
  return {azimuthDeg:wrap(value.azimuthDeg),elevationDeg:value.elevationDeg};
};
const frame=(at,value)=>({timeSeconds:at,...position(value)});

function simplify(frames,tolerance=.25){
  if(frames.length<3)return frames;
  const keep=new Set([0,frames.length-1]),stack=[[0,frames.length-1]];
  while(stack.length){
    const [a,b]=stack.pop();
    if(b-a<2)continue;
    const source={keyframes:[frames[a],frames[b]]};
    let worst=tolerance,index=-1;
    for(let i=a+1;i<b;i++){
      const expected=pointAt(source,frames[i].timeSeconds);
      const error=Math.max(Math.abs(wrap(frames[i].azimuthDeg-expected.azimuthDeg)),Math.abs(frames[i].elevationDeg-expected.elevationDeg));
      if(error>worst){worst=error;index=i;}
    }
    if(index>=0){keep.add(index);stack.push([a,index],[index,b]);}
  }
  return [...keep].sort((a,b)=>a-b).map(index=>frames[index]);
}

export function createAutomationTake(object,start,duration){
  if(!object||!Number.isFinite(start)||!Number.isFinite(duration)||duration<=0||start<0||start>=duration)
    throw new RangeError('写入起点须位于音频范围内。');
  const original=structuredClone(object),samples=[];
  start=Math.round(start*1000)/1000;
  if(start>=duration)throw new RangeError('请将播放位置移到音频结束前。');
  const atTime=time=>{
    const at=time>=duration?duration:Math.min(duration,Math.round(time*1000)/1000);
    if(!Number.isFinite(time)||at<start-1e-9||time>duration+1e-6)throw new RangeError('写入时间须单调递增且位于音频范围内。');
    return at;
  };
  function append(time,value,limit){
    const at=atTime(time),last=samples.at(-1),next=frame(at,value);
    if(last&&at<last.timeSeconds)throw new RangeError('写入期间不能向前拖动播放位置。');
    if(last?.timeSeconds===at)samples[samples.length-1]=next;
    else{
      if(samples.length>=limit)throw new RangeError('轨迹记录已达上限，请停止本次写入。');
      samples.push(next);
    }
    return next;
  }
  // Reserve one sample so a full take can still capture its final position.
  const record=(time,value)=>append(time,value,MAX_KEYFRAMES-1);
  record(start,pointAt(original,start));
  return {
    get count(){return samples.length;},
    get lastTime(){return samples.at(-1).timeSeconds;},
    get previewFrames(){
      const stride=Math.max(1,Math.ceil(samples.length/128));
      return samples.filter((_,i)=>i%stride===0||i===samples.length-1);
    },
    record,
    finish(time,value){
      const end=atTime(time);
      if(end<samples.at(-1).timeSeconds)throw new RangeError('写入结束时间早于已记录位置。');
      append(end,value??samples.at(-1),MAX_KEYFRAMES);
      if(end===start)return original;
      const keys=original.keyframes?.length?original.keyframes:[
        frame(0,original),frame(duration,original)];
      const result=keys.filter(key=>key.timeSeconds<start).map(key=>({...key}));
      if(start>0){
        const guard=Math.max(0,start-.001);
        if(!result.length||result.at(-1).timeSeconds<guard)result.push(frame(guard,pointAt(original,guard)));
      }
      result.push(...simplify(samples));
      if(end<duration&&original.keyframes?.length){
        const guard=Math.min(duration,end+.001);
        result.push(frame(guard,pointAt(original,guard)));
        result.push(...keys.filter(key=>key.timeSeconds>guard).map(key=>({...key})));
      }
      if(result[0].timeSeconds!==0)result.unshift(frame(0,pointAt(original,0)));
      if(result.at(-1).timeSeconds!==duration)result.push(frame(duration,original.keyframes?.length?pointAt(original,duration):samples.at(-1)));
      if(result.length>MAX_KEYFRAMES)throw new RangeError('合并后的轨迹超过上限；本次写入尚未替换原轨迹。');
      return {...original,...position(result[0]),keyframes:result};
    }
  };
}
