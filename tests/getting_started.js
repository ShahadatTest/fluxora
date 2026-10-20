/* Offline navigation doubles. No rendered browser or human-user claim. */
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const code = fs.readFileSync(path.join(__dirname,'../cashflow_copilot/static/fluxora-guide.js'),'utf8');
function fixture({hash='',stored=null,blocked=false}={}) {
  const elements={};
  for(const key of ['name','progress','title','instruction','check','open','prev','next','exit']) elements['[data-guide-'+key+']']={textContent:'',hidden:false,addEventListener(k,f){this[k]=f;}};
  const rail={hidden:true,querySelector:k=>elements[k]}, main={focus(){this.focused=true;}};
  const guides={}; for(const mode of ['judge','owner']) guides[mode]={title:mode,steps:Array.from({length:5},(_,i)=>({title:'Step '+(i+1),time:'45 sec',path: i<3?'/app':'/telegram/demo',text:'Read before choosing a real control.',check:'Guide is not completion.'}))};
  const handlers={}, writes=[];
  const win={location:{hash,pathname:'/app',search:''},sessionStorage:{getItem(k){assert.equal(k,'fluxora.guide.v1');if(blocked)throw Error('blocked');return stored;},setItem(k,v){if(blocked)throw Error('blocked');writes.push([k,JSON.parse(v)]);stored=v;},removeItem(k){if(blocked)throw Error('blocked');stored=null;}},history:{replaceState(a,b,url){assert.equal(url,'/app');win.location.hash='';}},addEventListener(k,f){handlers[k]=f;}};
  const classes=new Set();
  const doc={body:{classList:{add:k=>classes.add(k),remove:k=>classes.delete(k)}},querySelector:()=>rail,getElementById:k=>k==='main'?main:{textContent:JSON.stringify(guides)}};
  vm.runInNewContext(code,{document:doc,window:win,Number,Array,JSON});
  return {classes,rail,elements,main,win,handlers,writes,stored:()=>stored};
}
let t=fixture();assert.equal(t.rail.hidden,true);
t=fixture({hash:'#tour-judge-1'});assert.equal(t.rail.hidden,false);assert.equal(t.classes.has('guide-active'),true);assert.equal(t.elements['[data-guide-prev]'].hidden,true);assert.equal(t.elements['[data-guide-next]'].href,'/app#tour-judge-2');assert.match(t.elements['[data-guide-progress]'].textContent,/Guide step 1 of 5/);
t.win.location.hash='#tour-judge-5';t.handlers.hashchange();assert.equal(t.elements['[data-guide-next]'].href,'/start');assert.equal(t.elements['[data-guide-next]'].textContent,'Return to guide hub');assert.equal(t.elements['[data-guide-prev]'].href,'/telegram/demo#tour-judge-4');
assert.equal(t.elements['[data-guide-open]'].hidden,false);assert.equal(t.elements['[data-guide-open]'].href,'/telegram/demo#tour-judge-5');
t.elements['[data-guide-exit]'].click();assert.equal(t.rail.hidden,true);assert.equal(t.classes.has('guide-active'),false);assert.equal(t.stored(),null);assert.equal(t.win.location.hash,'');assert.equal(t.main.focused,true);
t=fixture({stored:JSON.stringify({mode:'owner',step:2})});assert.equal(t.rail.hidden,false);assert.equal(t.elements['[data-guide-name]'].textContent,'owner');
for(const stored of ['not JSON','null','{"mode":"__proto__","step":0}','{"mode":"judge","step":5}','{"mode":"judge","step":"0"}','{"mode":"judge","step":-1}']){t=fixture({stored});assert.equal(t.rail.hidden,true);}
t=fixture({hash:'#tour-judge-2',blocked:true});assert.equal(t.rail.hidden,false);assert.equal(t.elements['[data-guide-next]'].href,'/app#tour-judge-3');t.elements['[data-guide-exit]'].click();assert.equal(t.rail.hidden,true);
t=fixture({hash:'#evidence-invoice',stored:JSON.stringify({mode:'owner',step:1})});t.elements['[data-guide-exit]'].click();assert.equal(t.win.location.hash,'#evidence-invoice');
t=fixture({hash:'#tour-judge-999'});assert.equal(t.rail.hidden,true);
assert.ok(t.writes.every(([key,value])=>key==='fluxora.guide.v1'&&Object.keys(value).sort().join(',')==='mode,step'));
console.log('PASS: explicit navigation, optional session preference, invalid input, step boundaries, no completion claims, exit/focus and evidence-anchor preservation. DOM doubles only.');
