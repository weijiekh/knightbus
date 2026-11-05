import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const ctx = vm.createContext({});
vm.runInContext(readFileSync(new URL('./walking-navigation.js',import.meta.url),'utf8'),ctx);
const nav = ctx.WalkNavigation;
const route = {geometry:[[3,101],[3.001,101],[3.001,101.001]],duration_s:180,steps:[
  {instruction:'Head north',way_points:[0,1],type:11},
  {instruction:'Turn right',way_points:[1,2],type:1},
  {instruction:'Arrive',way_points:[2,2],type:10},
]};
const dest={lat:3.001,lon:101.001};
const fix=(lat,lon,time,accuracy=5)=>({lat,lon,timestamp:time,accuracy});

test('progress follows the polyline and advances instructions through a turn',()=>{
  const model=nav.build(route);
  const a=nav.update(model,fix(3.0005,101,10000),null,dest,10000);
  assert.equal(a.stepIndex,0);
  assert.ok(a.turnDistance>50 && a.turnDistance<60);
  const b=nav.update(model,fix(3.001,101.0002,40000),a,dest,40000);
  assert.equal(b.stepIndex,1);
  assert.ok(b.remaining<a.remaining);
});
test('poor, stale, future, out-of-order GPS and route jumps cannot advance progress',()=>{
  const model=nav.build(route), previous=nav.update(model,fix(3,101,10000),null,dest,10000);
  for(const bad of [fix(3.001,101.001,11000,100),fix(3,101,0),fix(3,101,50000),fix(3,101,9999),fix(3.001,101.001,11000)]) {
    assert.equal(nav.update(model,bad,previous,dest,30000).status,'weak');
  }
});
test('off-route detection respects accuracy and arrival needs proximity to the actual stop',()=>{
  const model=nav.build(route);
  assert.equal(nav.update(model,fix(3.0005,101.002,10000),null,dest,10000).status,'offroute');
  const atEnd=fix(3.001,101.001,10000);
  assert.equal(nav.update(model,atEnd,null,dest,10000).nearArrival,true);
  assert.equal(nav.update(model,atEnd,null,{lat:3.002,lon:101.001},10000).nearArrival,false);
});
test('missing or invalid turn indices allow GPS tracking without invented turn locations',()=>{
  assert.equal(nav.build({...route,steps:[{instruction:'Turn',way_points:[0,99]}]}).steps.length,0);
  assert.equal(nav.build({...route,steps:[]}).steps.length,0);
  assert.equal(nav.build({geometry:[]}),null);
  assert.equal(nav.build({geometry:[[3,101],[NaN,101]]}),null);
});
test('crossing a loop does not jump from the beginning to the end',()=>{
  const model=nav.build({geometry:[[3,101],[3.001,101],[3.001,101.001],[3,101.001],[3,101]],steps:[]});
  const result=nav.update(model,fix(3,101,10000),{along:10,timestamp:5000}, {lat:3,lon:101},10000);
  assert.ok(result.along<20);
  assert.equal(result.nearArrival,false);
});

test('malformed route data degrades to unavailable guidance without throwing',()=>{
  for(const geometry of [{},'not a path',[null,null],[[3,101],null]]) {
    assert.equal(nav.build({geometry}),null);
  }
  assert.equal(nav.build({...route,steps:[null]}).steps.length,0);
  assert.equal(nav.build({...route,steps:{}}).steps.length,0);
});
