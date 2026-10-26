import {pointAt} from './automation.mjs';
import {directionToVector} from './coordinates.mjs';

export function sampleTrajectory(object,durationSeconds,segments=60){
  if(!object?.keyframes?.length||!Number.isFinite(durationSeconds)||durationSeconds<=0)return [];
  const count=Number.isInteger(segments)&&segments>0?segments:60;
  const times=new Set(Array.from({length:count+1},(_,i)=>durationSeconds*i/count));
  for(const frame of object.keyframes)if(Number.isFinite(frame.timeSeconds)&&frame.timeSeconds>=0&&frame.timeSeconds<=durationSeconds)times.add(frame.timeSeconds);
  return [...times].sort((a,b)=>a-b).map(timeSeconds=>{
    const point=pointAt(object,timeSeconds);
    return {timeSeconds,position:directionToVector(point.azimuthDeg,point.elevationDeg)};
  });
}

export function pickTrajectory(samples,pointer,{maxDistancePx=8,referenceTime=0,accept=()=>true}={}){
  if(!Number.isFinite(pointer?.x)||!Number.isFinite(pointer?.y)||!Number.isFinite(maxDistancePx)||maxDistancePx<0)return null;
  const reference=Number.isFinite(referenceTime)?referenceTime:0;
  const valid=sample=>sample?.visible&&[sample.x,sample.y,sample.timeSeconds].every(Number.isFinite);
  let best=null;
  for(let i=0;i<samples.length-1;i++){
    const a=samples[i],b=samples[i+1];if(!valid(a)||!valid(b))continue;
    const dx=b.x-a.x,dy=b.y-a.y,lengthSquared=dx*dx+dy*dy;if(lengthSquared===0)continue;
    const ratio=Math.max(0,Math.min(1,((pointer.x-a.x)*dx+(pointer.y-a.y)*dy)/lengthSquared));
    const distanceSquared=(pointer.x-a.x-ratio*dx)**2+(pointer.y-a.y-ratio*dy)**2;
    if(distanceSquared>maxDistancePx**2)continue;
    const timeSeconds=a.timeSeconds+(b.timeSeconds-a.timeSeconds)*ratio;
    const candidate={timeSeconds,distanceSquared,segmentIndex:i,ratio};
    if(!accept(candidate))continue;
    if(!best||distanceSquared<best.distanceSquared-1e-9||(Math.abs(distanceSquared-best.distanceSquared)<=1e-9&&Math.abs(timeSeconds-reference)<Math.abs(best.timeSeconds-reference)))
      best=candidate;
  }
  return best;
}
