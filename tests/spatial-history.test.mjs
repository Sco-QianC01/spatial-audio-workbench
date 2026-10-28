import test from 'node:test';
import assert from 'node:assert/strict';
import {createSceneHistory} from '../spatial-audio/history.mjs';

test('history clones snapshots, supports undo/redo and removes abandoned future edits',()=>{
  const original={objects:[{id:'track01',azimuthDeg:0}]},history=createSceneHistory(original);
  original.objects[0].azimuthDeg=90;
  assert.equal(history.current.objects[0].azimuthDeg,0);
  history.checkpoint(original);assert.equal(history.canUndo,true);
  const previous=history.undo();previous.objects[0].azimuthDeg=-90;
  assert.equal(history.current.objects[0].azimuthDeg,0);
  assert.equal(history.redo().objects[0].azimuthDeg,90);
  history.undo();history.checkpoint({objects:[{id:'track01',azimuthDeg:30}]});
  assert.equal(history.redo(),null);assert.equal(history.current.objects[0].azimuthDeg,30);
});

test('only consecutive matching gestures merge; undo and time gaps start new edits',()=>{
  const history=createSceneHistory({value:0});
  history.checkpoint({value:1},{mergeKey:'drag',now:0});
  history.checkpoint({value:2},{mergeKey:'drag',now:200});
  assert.deepEqual(history.undo(),{value:0});
  history.redo();history.checkpoint({value:3},{mergeKey:'drag',now:300});
  assert.deepEqual(history.undo(),{value:2});
  history.redo();history.checkpoint({value:4},{mergeKey:'drag',now:1000});
  assert.deepEqual(history.undo(),{value:3});
});

test('bounded history retains recent changes and reset clears prior project state',()=>{
  const history=createSceneHistory({value:0},{limit:2});
  for(let value=1;value<=4;value++)history.checkpoint({value});
  assert.deepEqual(history.undo(),{value:3});assert.deepEqual(history.undo(),{value:2});
  assert.equal(history.undo(),null);history.reset({value:20});
  assert.equal(history.canUndo,false);assert.equal(history.canRedo,false);
  assert.deepEqual(history.current,{value:20});
  assert.throws(()=>history.checkpoint({value:30},{now:NaN}),RangeError);
  assert.throws(()=>createSceneHistory(null),TypeError);
});
