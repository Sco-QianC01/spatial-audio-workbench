// Web Audio: x right, y up, z rear; azimuth increases toward the listener's left.
export const STEREO_GEOMETRY='great-circle-width-v1';
export function directionToVector(azimuth, elevation){
  const a=azimuth*Math.PI/180,e=elevation*Math.PI/180;
  return [-Math.sin(a)*Math.cos(e),Math.sin(e),-Math.cos(a)*Math.cos(e)];
}

export function stereoDirectionToVector(azimuth,elevation,offset){
  const center=directionToVector(azimuth,elevation),a=azimuth*Math.PI/180,w=offset*Math.PI/180;
  if(offset===0)return center;
  const tangent=[-Math.cos(a),0,Math.sin(a)];
  return center.map((value,i)=>Math.cos(w)*value+Math.sin(w)*tangent[i]);
}
