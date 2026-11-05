import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
function setup() {
  const elements = Object.fromEntries(['#walk-bar','#walk-text','#walk-btn','#walk-maps','#walk-directions'].map(id => [id,{hidden:false,innerHTML:'',textContent:''}]));
  const ctx = vm.createContext({$:id=>elements[id], stopState:{id:'s1',mode:'map'}, S:{view:'stop',loc:{lat:3.15,lon:101.7}}, stopCoords:()=>({lat:3.16,lon:101.71}),stopDisplay:()=>({name:'LRT Station'}), fmtDist:d=>`${d} m`,URLSearchParams,API_BASE:'',window:{},stopMap:null,fitFillCards(){}});
  vm.runInContext(html.slice(html.indexOf('function esc(str)'),html.indexOf('\n}',html.indexOf('function esc(str)'))+2),ctx);
  vm.runInContext(html.slice(html.indexOf('const walkState ='),html.indexOf('view.addEventListener("pointerup"')),ctx);
  const run = source=>vm.runInContext(source,ctx);
  ctx.route = {duration_s:240,distance_m:150,geometry:[],steps:[{instruction:'Head north',distance_m:120,type:11},{instruction:'Turn left onto <Main Street>',distance_m:30,type:0},{instruction:'Arrive at your destination',distance_m:0,type:10}]};
  ctx.fetch = async()=>({ok:true,status:200,json:async()=>ctx.route});
  return {ctx,run,elements};
}

test('Show walk reveals ordered turn instructions, distances and navigation link',async()=>{
  const {run,elements} = setup();
  await run('showWalkRoute()');
  const panel=elements['#walk-directions'];
  assert.equal(panel.hidden,false);
  assert.match(panel.innerHTML,/<ol class="walk-steps">/);
  assert.ok(panel.innerHTML.indexOf('Head north') < panel.innerHTML.indexOf('Turn left'));
  assert.match(panel.innerHTML,/Continue for 120 m/);
  assert.match(panel.innerHTML,/Turn left onto &lt;Main Street&gt;/);
  assert.match(panel.innerHTML,/Arrive at your destination/);
  assert.match(panel.innerHTML,/travelmode=walking/);
  assert.equal(elements['#walk-text'].textContent,'4 min walk · 150 m');
});

test('geometry-only responses keep a useful Maps fallback and do not invent turns',async()=>{
  const {ctx,run,elements} = setup();
  delete ctx.route.steps;
  await run('showWalkRoute()');
  assert.match(elements['#walk-directions'].innerHTML,/Turn instructions are unavailable/);
  assert.doesNotMatch(elements['#walk-directions'].innerHTML,/<li>/);
  run('stopState.mode="list"; renderWalkBar()');
  assert.equal(elements['#walk-directions'].hidden,true);
});

test('a delayed route for a previous stop cannot replace the current walking directions',async()=>{
  const {ctx,run,elements} = setup();
  let finish;
  ctx.fetch=()=>new Promise(resolve=>{finish=resolve;});
  const previous=run('showWalkRoute()');
  run('stopState.id="s2"; walkState.request++; walkState.busy=false; walkState.route=null');
  ctx.route.steps[0].instruction='Start at the new stop';
  ctx.fetch=async()=>({ok:true,status:200,json:async()=>ctx.route});
  await run('showWalkRoute()');
  finish({ok:true,status:200,json:async()=>({steps:[{instruction:'Old stop',distance_m:1}]})});
  await previous;
  assert.match(elements['#walk-directions'].innerHTML,/Start at the new stop/);
  assert.doesNotMatch(elements['#walk-directions'].innerHTML,/Old stop/);
});
