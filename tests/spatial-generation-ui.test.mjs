import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {initOnlineLink} from '../spatial-audio/model-link.mjs';

class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this._value='';}
  set value(value){this._value=value;}
  get value(){return this._value||(this.tagName==='select'?this.children[0]?.value||'':'');}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;this._value='';}
  setAttribute(name,value){this[name]=value;}
  closest(){return null;}
  querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...child.querySelectorAll(tag)]);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  async emit(name){await this.listeners[name]?.({preventDefault(){}});}
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('generation UI resumes safely, unlocks rejected requests, and budgets each task independently',async()=>{
  const originalNow=Date.now;let now=0;Date.now=()=>now;
  const originalCrypto=Object.getOwnPropertyDescriptor(globalThis,'crypto');
  const originals={document:globalThis.document,location:globalThis.location,setTimeout:globalThis.setTimeout,clearTimeout:globalThis.clearTimeout};
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:webcrypto});
  const nodes=[],timers=new Map();let timerId=0;
  globalThis.document={createElement(tag){const node=new Element(tag);nodes.push(node);return node;}};
  globalThis.location={hash:'',href:'http://localhost/spatial-audio/'};
  globalThis.setTimeout=fn=>{timers.set(++timerId,fn);return timerId;};globalThis.clearTimeout=id=>timers.delete(id);
  const find=id=>nodes.find(node=>node.id===id);
  const calls=[];let queryError=false,failPost=false,postErrorStatus,finishImport,rejectImport,importCalls=0,remoteStatus='succeeded';
  const running={id:'a'.repeat(32),model:'ace',label:'ACE',prompt:'test',username:'research',status:'queued',audioUrls:[]};
  const config={connected:false,username:null,models:[{id:'ace',label:'ACE',minDuration:10,maxDuration:60,durationMode:'target'},{id:'shao',label:'Shao',minDuration:60,maxDuration:60,durationMode:'model'},{id:'yue',label:'YuE',minDuration:10,maxDuration:600,durationMode:'model',requiresScore:true}],jobs:[running]};
  const api=async(path,options)=>{
    calls.push([path,options]);if(path==='/online/status')return {downloadEnabled:true};if(path==='/online/generation')return config;
    if(path==='/online/connect'){config.connected=true;config.username=JSON.parse(options.body).username;return config;}
    if(options?.method==='POST'){if(failPost)throw Object.assign(new Error('request rejected or unavailable'),{status:postErrorStatus});return {...running,id:JSON.parse(options.body).requestId};}
    if(queryError)throw Object.assign(new Error('Token expired'),{status:401});
    return {...running,id:path.split('/').at(-1),status:remoteStatus,audioUrls:['http://audio.example/result.wav','/audio/wrong-origin.wav']};
  };
  let ui;
  try{
    ui=initOnlineLink({container:new Element('div'),api,onImport:()=>{importCalls++;return new Promise((resolve,reject)=>{finishImport=resolve;rejectImport=reject;});}});
    await settle();assert.equal(find('online-generate').disabled,true);assert.equal(timers.size,1,'unfinished server job resumes polling');
    assert.equal(find('online-connect').disabled,false,'pending job can reconnect after backend restart');
    assert.equal(find('online-username').value,'research','restart restores owner from the active job when config username is null');assert.equal(find('online-username').disabled,true,'active task locks username');
    await nodes.find(node=>node.className==='online-connect').emit('submit');
    assert.equal(JSON.parse(calls.find(([path])=>path==='/online/connect')[1].body).username,'research');
    assert.equal(find('online-connect').disabled,false,'active job allows same-account reconnect');
    queryError=true;await find('online-refresh').emit('click');assert.match(find('online-generation-state').textContent,/Token expired/);
    assert.equal(find('online-connect').disabled,false,'expired token can reconnect without reloading');
    find('online-username').value='different-account';await nodes.find(node=>node.className==='online-connect').emit('submit');
    assert.equal(JSON.parse(calls.filter(([path])=>path==='/online/connect').at(-1)[1].body).username,'research','reconnect always uses the active job owner');
    assert.equal(find('online-username').value,'research');queryError=false;
    ui.refresh(true);assert.equal(find('online-connect').disabled,true,'local busy still locks reconnect');ui.refresh(false);
    const poll=timers.values().next().value;timers.clear();await poll();
    assert.equal(calls.filter(([path])=>path==='/online/generate').length,0,'resume only queries GET');
    assert.equal(nodes.filter(node=>node.tagName==='a').length,1,'relative result URLs cannot target localhost');
    assert.equal(nodes.find(node=>node.tagName==='a').href,'http://audio.example/result.wav');
    assert.equal(nodes.filter(node=>node.dataset.key).length,1,'valid stereo results expose an explicit separation action');
    assert.equal(importCalls,0);
    failPost=true;find('online-prompt').value='ambient';
    const generationForm=nodes.find(node=>node.className==='online-generation');await generationForm.emit('submit');
    const submitted=calls.find(([path])=>path==='/online/generate');const id=JSON.parse(submitted[1].body).requestId;
    assert.match(id,/^[a-f0-9]{32}$/);assert.equal(find('online-jobs').value,id);assert.match(find('online-generation-state').textContent,/提交结果待确认/);
    assert.equal(timers.size,0,'ambiguous submission does not automatically submit or poll');
    await generationForm.emit('submit');assert.equal(calls.filter(([path])=>path==='/online/generate').length,1,'unknown submission cannot be resubmitted');
    await find('online-refresh').emit('click');assert.equal(calls.at(-1)[0],`/online/jobs/${id}`);assert.equal(calls.filter(([path])=>path==='/online/generate').length,1);
    postErrorStatus=400;now=90*60*1000;await generationForm.emit('submit');
    assert.match(find('online-generation-state').textContent,/生成失败/);assert.equal(find('online-generate').disabled,false,'explicit 400 allows correction and a new request');
    find('online-model').value='shao';await find('online-model').emit('change');assert.equal(find('online-duration').disabled,true);assert.equal(find('online-duration').value,'60');
    find('online-model').value='yue';await find('online-model').emit('change');assert.equal(find('online-duration').disabled,true);assert.equal(find('online-score').required,true);
    find('online-model').value='ace';await find('online-model').emit('change');assert.equal(find('online-duration').disabled,false);
    postErrorStatus=503;await generationForm.emit('submit');assert.match(find('online-generation-state').textContent,/提交结果待确认/);assert.equal(find('online-generate').disabled,true);await find('online-refresh').emit('click');
    failPost=false;await generationForm.emit('submit');assert.equal(timers.size,1,'a new task gets its own budget even after the page has been open for 90 minutes');
    remoteStatus='running';now+=46*60*1000;await find('online-refresh').emit('click');assert.equal(timers.size,0,'the active task stops automatic polling after its own limit');
    remoteStatus='succeeded';await find('online-refresh').emit('click');await generationForm.emit('submit');assert.equal(timers.size,1,'a later task gets a fresh polling budget');
    ui.refresh(true);assert.equal(find('online-generate').disabled,true);ui.refresh(false);
  }finally{ui?.dispose();Date.now=originalNow;Object.assign(globalThis,originals);if(originalCrypto)Object.defineProperty(globalThis,'crypto',originalCrypto);else delete globalThis.crypto;}
});

test('legacy auto-prepare receipts never trigger a local import after restart or refresh',async()=>{
  const originals={document:globalThis.document,location:globalThis.location};
  const nodes=[];globalThis.document={createElement(tag){const node=new Element(tag);nodes.push(node);return node;}};globalThis.location={hash:''};
  let imports=0;const calls=[];
  const job={id:'a'.repeat(32),status:'succeeded',prepareStudio:true,audioUrls:['http://audio.example/result.wav']};
  const api=async(path,options)=>{calls.push([path,options]);return path==='/online/status'?{downloadEnabled:true}:{models:[],jobs:[job]};};
  const ui=initOnlineLink({container:new Element('div'),api,onImport:async()=>{imports++;}});
  try{
    await settle();ui.refresh(false);await settle();
    await nodes.find(node=>node.id==='online-refresh').emit('click');await settle();
    assert.equal(imports,0);assert.ok(nodes.some(node=>node.dataset.key),'historical receipts offer an explicit action without automatically importing');
    assert.equal(calls.some(([,options])=>options?.method==='POST'),false);
    assert.equal(nodes.find(node=>node.id==='online-prepare-studio').disabled,true);
    assert.equal(nodes.find(node=>node.id==='online-prepare-studio').checked,false);
  }finally{ui.dispose();Object.assign(globalThis,originals);}
});
test('new stereo-reference requests never request automatic separation or studio preparation',async()=>{
  const originalCrypto=Object.getOwnPropertyDescriptor(globalThis,'crypto');
  const originals={document:globalThis.document,location:globalThis.location};
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:webcrypto});
  const nodes=[];globalThis.document={createElement(tag){const node=new Element(tag);nodes.push(node);return node;}};globalThis.location={hash:''};
  const posts=[],imports=[];
  const api=async(path,options)=>{
    if(path==='/online/status')return {downloadEnabled:true};
    if(path==='/online/generation')return {connected:true,models:[{id:'ace',minDuration:10,maxDuration:60}],jobs:[]};
    const value=JSON.parse(options.body);posts.push(value);return {...value,id:value.requestId,status:'succeeded',audioUrls:['http://audio.example/new.wav']};
  };
  const ui=initOnlineLink({container:new Element('div'),api,onImport:async(...args)=>imports.push(args)});
  try{
    await settle();nodes.find(node=>node.id==='online-prompt').value='context only';
    nodes.find(node=>node.id==='online-prepare-studio').checked=true;
    await nodes.find(node=>node.className==='online-generation').emit('submit');await settle();
    assert.equal(posts.length,1);assert.equal(Object.hasOwn(posts[0],'prepareStudio'),false);assert.equal(imports.length,0);
  }finally{ui.dispose();Object.assign(globalThis,originals);if(originalCrypto)Object.defineProperty(globalThis,'crypto',originalCrypto);else delete globalThis.crypto;}
});

test('explicit separation click imports once, remains manual after refresh, and retries failures',async()=>{
  const originals={document:globalThis.document,location:globalThis.location};
  const nodes=[];globalThis.document={createElement(tag){const node=new Element(tag);nodes.push(node);return node;}};globalThis.location={hash:''};
  const job={id:'b'.repeat(32),status:'succeeded',prepareStudio:true,audioUrls:['http://audio.example/result.wav','/audio/relative.wav','javascript:alert(1)']};
  const container=new Element('div'),imports=[];let resolveImport,rejectImport;
  const api=async(path)=>path==='/online/status'?{downloadEnabled:true}:path.startsWith('/online/jobs/')?job:{models:[],jobs:[job]};
  const ui=initOnlineLink({container,api,onImport:(...args)=>{imports.push(args);return new Promise((resolve,reject)=>{resolveImport=resolve;rejectImport=reject;});}});
  const action=()=>container.querySelectorAll('button').find(node=>node.dataset.key);
  try{
    await settle();ui.refresh(false);await settle();
    assert.equal(imports.length,0,'loading a completed legacy receipt never imports');
    assert.equal(container.querySelectorAll('button').filter(node=>node.dataset.key).length,1,'only a legal absolute HTTP result offers separation');
    assert.equal(action().textContent,'下载并本地分离');
    const first=action();const pending=first.emit('click');await first.emit('click');
    assert.equal(imports.length,1,'busy lock prevents repeated click submissions');
    assert.equal(first.disabled,true);
    assert.deepEqual(imports[0],[job.audioUrls[0],{jobId:job.id,workflow:'separated-stereo-v1'},`${job.id}:${job.audioUrls[0]}`]);
    rejectImport(new Error('local worker unavailable'));await pending;
    assert.equal(action().disabled,false,'failed local separation can be retried explicitly');
    assert.ok(nodes.some(node=>/本地处理未完成：local worker unavailable/.test(node.textContent??'')));
    const retry=action().emit('click');assert.equal(imports.length,2);resolveImport({state:'ready'});await retry;
    assert.equal(action().disabled,true,'successful result is imported once per page session');
    await action().emit('click');assert.equal(imports.length,2,'successful clicks cannot resubmit even via a stale handler');
    ui.refresh(false);await nodes.find(node=>node.id==='online-refresh').emit('click');await settle();
    assert.equal(imports.length,2,'refresh never starts another import');
  }finally{ui.dispose();Object.assign(globalThis,originals);}
});
