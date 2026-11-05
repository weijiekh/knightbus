import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  let callback;
  const markers=[],circles=[],views=[],cleared=[];
  const layer=coords=>({coords,addTo(){return this;},setLatLng(value){this.coords=value;},setRadius(value){this.radius=value;}});
  const L={divIcon:x=>x,marker:(coords,options)=>{const marker=layer(coords);marker.options=options;markers.push(marker);return marker;},circle:(coords,options)=>{const circle=layer(coords);circle.radius=options.radius;circles.push(circle);return circle;}};
  const context=vm.createContext({window:{L},L,stopMap:{setView:(...args)=>views.push(args),getZoom:()=>16},
    navigator:{geolocation:{watchPosition:fn=>{callback=fn;return 0;},clearWatch:id=>cleared.push(id)}},
    S:{view:'stop',loc:null},stopState:{id:'s',mode:'map'},document:{visibilityState:'visible'},$:()=>null,
    walkState:{id:'s',route:{geometry:[[3,101],[3.001,101]],steps:[],duration_s:80}},stopCoords:()=>({lat:3.001,lon:101}),
  });
  for(const file of ['walking-navigation.js','walking-live.js']) vm.runInContext(readFileSync(new URL(`./${file}`,import.meta.url),'utf8'),context);
  const run=code=>vm.runInContext(code,context);
  const send=(lat,lon,timestamp,accuracy=5)=>callback({coords:{latitude:lat,longitude:lon,accuracy},timestamp});
  return {context,run,send,markers,circles,views,cleared};
}

test('GPS moves one existing marker and accuracy circle; exploration and recenter controls work',()=>{
  const {run,send,markers,circles,views}=setup();
  run('startWalkingNavigation()');
  const now=Date.now();
  send(3,101,now);
  send(3.0001,101,now+500);
  assert.equal(markers.length,1);assert.equal(circles.length,1);
  assert.deepEqual(Array.from(markers[0].coords),[3.0001,101]);
  assert.equal(circles[0].radius,5);
  const followed=views.length;
  run('walkNav.follow=false');
  send(3.00015,101,now+1000);
  assert.equal(views.length,followed);
  run('recenterWalkingNavigation()');
  assert.equal(views.length,followed+1);
});

test('switching from map to list ends GPS tracking through the actual stop-view handler',()=>{
  const {context,run,send,cleared}=setup();
  const html=readFileSync(new URL('./index.html',import.meta.url),'utf8');
  context.document.querySelectorAll=()=>[];
  context.$=id=>id==='#walk-nav'?null:{hidden:false};
  context.destroyStopMap=()=>{};
  context.renderWalkBar=()=>{};
  const from=html.indexOf('function setStopView(mode)');
  vm.runInContext(html.slice(from,html.indexOf('function drawStopMap()',from)),context);
  run('startWalkingNavigation()');
  send(3,101,Date.now());
  run('setStopView("list")');
  assert.equal(run('walkNav.active'),false);
  assert.ok(cleared.includes(0));
});

test('leaving a stop cancels GPS and invalidates pending walking requests through the router',()=>{
  const {context,run,send,cleared}=setup();
  const html=readFileSync(new URL('./index.html',import.meta.url),'utf8');
  context.routeSearchTimer=null;context.routeSearchController=null;context.clearTimeout=()=>{};
  context.currentRoute={name:'stop',id:'s'};
  context.parseHash=()=>({name:'routes'});
  context.document.querySelectorAll=()=>[];context.window.scrollTo=()=>{};
  context.destroyMap=()=>{};
  for(const name of ['renderHome','renderStop','renderTrip','renderSearch','renderSaved','renderRoutes','renderRoute']) context[name]=()=>{};
  const from=html.indexOf('function route()');
  vm.runInContext(html.slice(from,html.indexOf('window.addEventListener("hashchange"',from)),context);
  run('walkState.request=3;walkState.busy=true;startWalkingNavigation()');
  send(3,101,Date.now());
  run('route()');
  assert.equal(run('walkNav.active'),false);
  assert.equal(context.walkState.request,4);
  assert.equal(context.walkState.busy,false);
  assert.ok(cleared.includes(0));
  send(3.0001,101,Date.now()+1000,5);
  assert.equal(context.S.loc.lat,3);
});
