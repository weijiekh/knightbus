import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html=readFileSync(new URL('./index.html',import.meta.url),'utf8');
function setup() {
  let now=100000, nextWatch=0, nextTimer=0;
  const callbacks=new Map(), cleared=[], timers=new Map();
  class Clock extends Date {static now(){return now;}}
  const elements=new Map();
  const $=id=>{
    if(!elements.has(id)) elements.set(id,{innerHTML:'',hidden:false,contains:()=>false});
    return elements.get(id);
  };
  const geo={watchPosition(success,error,options){const id=nextWatch++;callbacks.set(id,{success,error,options});return id;},clearWatch:id=>cleared.push(id)};
  const document={visibilityState:'visible',activeElement:null,querySelectorAll:()=>[]};
  const ctx=vm.createContext({$,Date:Clock,document,navigator:{geolocation:geo},window:{},stopMap:null,
    stopState:{id:'s',mode:'map'},S:{view:'stop',loc:null},API_BASE:'',URLSearchParams,AbortController,
    stopCoords:()=>({lat:3.001,lon:101.001}),stopDisplay:()=>({name:'Bus stop'}),fmtDist:d=>`${Math.round(d)} m`,fitFillCards(){},
    setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id),
  });
  for(const name of ['walking-navigation.js','walking-live.js']) vm.runInContext(readFileSync(new URL(`./${name}`,import.meta.url),'utf8'),ctx);
  vm.runInContext(html.slice(html.indexOf('function esc(str)'),html.indexOf('const ACRONYMS')),ctx);
  vm.runInContext(html.slice(html.indexOf('const walkState ='),html.indexOf('view.addEventListener("pointerup"')),ctx);
  vm.runInContext('drawWalkLayer=()=>{}',ctx);
  ctx.route={geometry:[[3,101],[3.001,101],[3.001,101.001]],duration_s:180,distance_m:222,steps:[
    {instruction:'Head north',type:11,way_points:[0,1],distance_m:111},
    {instruction:'Turn right',type:1,way_points:[1,2],distance_m:111},
    {instruction:'Arrive',type:10,way_points:[2,2],distance_m:0},
  ]};
  const run=code=>vm.runInContext(code,ctx);
  run('walkState.id="s";walkState.route=route');
  const requests=[];
  ctx.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>ctx.route};};
  function send(lat,lon,at,accuracy=5,watch=nextWatch-1){now=at;callbacks.get(watch).success({coords:{latitude:lat,longitude:lon,accuracy},timestamp:at});}
  const settle=()=>new Promise(setImmediate);
  return {ctx,run,send,requests,callbacks,cleared,timers,$,settle,setTime:time=>{now=time;}};
}

test('explicit start watches high-accuracy GPS and advances the live turn',()=>{
  const {run,send,callbacks,$}=setup();
  assert.equal(callbacks.size,0);
  run('startWalkingNavigation()');
  assert.equal(callbacks.get(0).options.maximumAge,0);
  send(3.0007,101,100000);
  assert.match($('#walk-nav').innerHTML,/In 33 m · Turn right/);
  send(3.001,101.0002,120000);
  assert.equal(run('walkNav.progress.stepIndex'),1);
  assert.match($('#walk-nav').innerHTML,/remaining/);
});
test('end clears even watch ID zero and obsolete callbacks cannot update the location',()=>{
  const {run,send,cleared,ctx}=setup();
  run('startWalkingNavigation()');send(3,101,100000);
  run('endWalkingNavigation()');
  assert.ok(cleared.includes(0));
  send(3.0005,101,120000,5,0);
  assert.equal(ctx.S.loc.lat,3);
  assert.equal(run('walkNav.active'),false);
});
test('backgrounding stops GPS; resume waits for fresh GPS while retaining its matching anchor',()=>{
  const {ctx,run,send,cleared,$}=setup();
  run('startWalkingNavigation()');send(3,101,100000);
  ctx.document.visibilityState='hidden';run('walkingVisibilityChanged()');
  assert.ok(cleared.includes(0));assert.equal(run('walkNav.status'),'paused');
  ctx.document.visibilityState='visible';run('walkingVisibilityChanged()');
  assert.equal(run('walkNav.progress.along'),0);
  assert.equal(run('walkNav.status'),'locating');
  assert.match($('#walk-nav').innerHTML,/Finding your location/);
  send(3.0005,101,120000,5,0);
  assert.equal(run('walkNav.progress.along'),0);
  assert.equal(run('walkNav.status'),'locating');
  send(3.0005,101,120000);
  assert.equal(run('walkNav.status'),'onroute');
});
test('arrival needs sustained accurate proximity and releases the GPS watch',()=>{
  const {run,send,cleared,$}=setup();
  run('startWalkingNavigation()');
  send(3.001,101.001,100000);
  assert.equal(run('walkNav.active'),true);
  send(3.001,101.001,104000);
  assert.equal(run('walkNav.active'),false);
  assert.equal(run('walkNav.status'),'arrived');
  assert.ok(cleared.includes(0));assert.match($('#walk-nav').innerHTML,/See bus arrivals/);
});
test('inaccurate fixes and short off-route jitter do not spend routing requests',()=>{
  const {run,send,requests}=setup();
  run('startWalkingNavigation()');send(3,101,100000);
  send(3.0005,101.002,110000,100);send(3.0005,101.002,116000,100);
  assert.equal(requests.length,0);
  send(3.0005,101.002,120000);send(3.0005,101,124000);
  assert.equal(requests.length,0);
});
test('sustained off-route GPS reroutes and repeated failures are throttled',async()=>{
  const {ctx,run,send,requests,settle,$}=setup();
  ctx.fetch=async(url,options)=>{requests.push({url,options});return {ok:false};};
  run('startWalkingNavigation()');send(3,101,100000);
  send(3.0005,101.002,110000);send(3.0005,101.002,116000);
  await settle();
  assert.equal(requests.length,1);
  assert.match($('#walk-nav').innerHTML,/Could not reroute/);
  send(3.0005,101.002,120000);send(3.0005,101.002,130000);
  assert.equal(requests.length,1);
  assert.match($('#walk-nav').innerHTML,/Could not reroute/);
  send(3.0005,101.002,147000);await settle();
  assert.equal(requests.length,2);
});
test('ending navigation aborts a pending reroute; its response cannot replace the route',async()=>{
  const {ctx,run,send,settle}=setup();
  let finish, signal;
  ctx.fetch=(url,options)=>{signal=options.signal;return new Promise(resolve=>{finish=resolve;});};
  run('startWalkingNavigation()');send(3,101,100000);
  send(3.0005,101.002,110000);send(3.0005,101.002,116000);
  run('endWalkingNavigation()');assert.equal(signal.aborted,true);
  finish({ok:true,json:async()=>({...ctx.route,distance_m:999})});await settle();
  assert.equal(run('walkState.route.distance_m'),222);
  assert.equal(run('walkNav.active'),false);
});
test('permission denial ends tracking; a silent GPS timeout pauses turn updates',()=>{
  const {run,send,callbacks,cleared,setTime,$}=setup();
  run('startWalkingNavigation()');send(3,101,100000);
  setTime(121000);run('checkWalkingGPS()');assert.equal(run('walkNav.status'),'weak');
  assert.match($('#walk-nav').innerHTML,/Waiting for GPS/);
  callbacks.get(0).error({code:1});
  assert.equal(run('walkNav.active'),false);assert.ok(cleared.includes(0));
  assert.match($('#walk-nav').innerHTML,/Location access was denied/);
});

test('returning to the current route cancels a pending reroute and ignores its late response',async()=>{
  const {ctx,run,send,settle,$}=setup();
  let finish, signal;
  ctx.fetch=(url,options)=>{signal=options.signal;return new Promise(resolve=>{finish=resolve;});};
  run('startWalkingNavigation()');send(3,101,100000);
  send(3.0005,101.002,110000);send(3.0005,101.002,116000);
  send(3.0005,101,120000);
  assert.equal(signal.aborted,true);
  assert.equal(run('walkNav.rerouting'),false);
  finish({ok:true,json:async()=>({...ctx.route,distance_m:999})});await settle();
  assert.equal(run('walkState.route.distance_m'),222);
  assert.equal(run('walkNav.status'),'onroute');
  assert.doesNotMatch($('#walk-nav').innerHTML,/Could not reroute/);
});

test('a browser GPS startup exception leaves tracking stopped with a useful error',()=>{
  const {ctx,run,$}=setup();
  ctx.navigator.geolocation.watchPosition=()=>{throw new Error('blocked by permissions policy');};
  assert.doesNotThrow(()=>run('startWalkingNavigation()'));
  assert.equal(run('walkNav.active'),false);
  assert.equal(run('walkNav.watchId'),null);
  assert.match($('#walk-nav').innerHTML,/could not start/);
});

test('resuming at a loop crossing preserves progress instead of resetting to the beginning',()=>{
  const {ctx,run,send,$}=setup();
  ctx.route={geometry:[[3,101],[3.001,101],[3.001,101.001],[3,101.001],[3,101]],duration_s:360,steps:[]};
  run('walkState.route=route;startWalkingNavigation()');
  send(3,101,100000);
  send(3.001,101,140000);
  send(3.001,101.001,180000);
  send(3,101.001,220000);
  send(3,101.0001,250000);
  assert.ok(run('walkNav.progress.along')>400);
  ctx.document.visibilityState='hidden';run('walkingVisibilityChanged()');
  ctx.document.visibilityState='visible';run('walkingVisibilityChanged()');
  assert.match($('#walk-nav').innerHTML,/Finding your location/);
  assert.equal(run('walkNav.fix'),null);
  send(3,101,260000);
  assert.ok(run('walkNav.progress.along')>400);
  assert.equal(run('walkNav.status'),'onroute');
});

test('a successful reroute replaces geometry and advances turns using the existing GPS watch',async()=>{
  const {ctx,run,send,requests,callbacks,timers,settle}=setup();
  const rerouted={geometry:[[3.0005,101.002],[3.001,101.002],[3.001,101.001]],duration_s:150,distance_m:166,steps:[
    {instruction:'Head north',type:11,way_points:[0,1],distance_m:55},
    {instruction:'Turn left',type:0,way_points:[1,2],distance_m:111},
    {instruction:'Arrive',type:10,way_points:[2,2],distance_m:0},
  ]};
  ctx.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>rerouted};};
  run('startWalkingNavigation()');send(3,101,100000);
  send(3.0005,101.002,110000);send(3.0005,101.002,116000);
  await settle();
  assert.equal(run('walkState.route.distance_m'),166);
  assert.equal(run('walkNav.rerouting'),false);
  assert.equal(run('walkNav.progress'),null);
  assert.equal(run('walkNav.status'),'locating');
  assert.equal(callbacks.size,1);
  assert.equal(timers.size,0);
  send(3.0005,101.002,120000);
  assert.equal(run('walkNav.status'),'onroute');
  assert.equal(run('walkNav.progress.stepIndex'),0);
  send(3.001,101.002,140000);
  assert.equal(run('walkNav.progress.stepIndex'),1);
  assert.equal(run('walkNav.model.steps[1].instruction'),'Turn left');
  assert.equal(requests.length,1);
});
