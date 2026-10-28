export function createSceneHistory(initial,{limit=40}={}){
  if(!Number.isInteger(limit)||limit<1)throw new RangeError('历史记录上限须为正整数。');
  const encode=scene=>{
    if(!scene||typeof scene!=='object'||Array.isArray(scene))throw new TypeError('场景须为对象。');
    return JSON.stringify(scene);
  };
  let entries=[encode(initial)],cursor=0,lastKey=null,lastTime=0;
  const breakMerge=()=>{lastKey=null;};
  return {
    get canUndo(){return cursor>0;},
    get canRedo(){return cursor<entries.length-1;},
    get current(){return JSON.parse(entries[cursor]);},
    checkpoint(next,{mergeKey=null,now=Date.now()}={}){
      if(!Number.isFinite(now))throw new RangeError('记录时间须为有限数字。');
      const encoded=encode(next);
      if(encoded===entries[cursor])return false;
      const merge=cursor>0&&cursor===entries.length-1&&mergeKey!==null&&mergeKey===lastKey&&now>=lastTime&&now-lastTime<=400;
      entries.length=cursor+1;
      if(merge)entries[cursor]=encoded;
      else{entries.push(encoded);cursor++;}
      if(entries.length>limit+1){entries.shift();cursor--;}
      lastKey=mergeKey;lastTime=now;return true;
    },
    undo(){breakMerge();if(cursor===0)return null;cursor--;return JSON.parse(entries[cursor]);},
    redo(){breakMerge();if(cursor===entries.length-1)return null;cursor++;return JSON.parse(entries[cursor]);},
    reset(next){entries=[encode(next)];cursor=0;breakMerge();}
  };
}
