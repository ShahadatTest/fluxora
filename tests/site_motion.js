/* Offline behavior doubles only; no rendered browser or performance claim. */
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../cashflow_copilot/static/fluxora-motion.js'), 'utf8');
class Element {
  constructor(classes = []) { this.classes = new Set(classes); this.events = {}; this.attrs = {}; this.styles = {}; this.hidden = true; this.disabled = false;
    this.classList = { contains: c => this.classes.has(c), add: c => this.classes.add(c), remove: c => this.classes.delete(c), toggle: (c,on) => on ? this.classes.add(c) : this.classes.delete(c) };
    this.style = { setProperty: (k,v) => this.styles[k] = v, removeProperty: k => delete this.styles[k] };
  }
  addEventListener(k, f) { this.events[k] = f; }
  setAttribute(k,v) { this.attrs[k] = v; }
  contains(target) { return target === this; }
}
function setup({ reduced = false, saved = null, storageFails = false, pointer = true, observerAvailable = true, site = true } = {}) {
  const body = new Element(site ? ['site'] : ['app']), button = new Element(), scene = new Element(), stage = new Element(), heading = new Element();
  const reduce = { matches: reduced, addEventListener(_,f) { this.change = f; } }, fine = { matches: pointer, addEventListener(_,f) { this.change = f; } };
  scene.querySelector = () => stage;
  scene.getBoundingClientRect = () => ({ left:0, top:0, width:400, height:400 });
  const doc = new Element(); doc.body = body; doc.hidden = false;
  doc.querySelectorAll = s => s === '[data-motion-toggle]' ? [button] : s === '[data-depth-scene]' ? [scene] : [heading,scene];
  let frame = null, cancelled = 0, observer = null, writes = [];
  const win = new Element();
  Object.assign(win, { matchMedia: q => q.includes('reduced-motion') ? reduce : fine,
    localStorage: { getItem(k) { assert.equal(k,'fluxora.motion'); if(storageFails) throw Error('storage blocked'); return saved; }, setItem(k,v) { if(storageFails) throw Error('storage blocked'); writes.push([k,v]); } },
    requestAnimationFrame(f) { frame = f; return 1; }, cancelAnimationFrame() { cancelled++; frame = null; }
  });
  if (observerAvailable) win.IntersectionObserver = class { constructor(callback) { this.callback = callback; this.observed = []; observer = this; } observe(n) { this.observed.push(n); } unobserve(n) { this.observed = this.observed.filter(i => i !== n); } disconnect() { this.observed = []; } };
  vm.runInNewContext(code, { document:doc, window:win, WeakSet });
  return { body,button,scene,stage,heading,reduce,fine,doc,win,writes, observer:() => observer, frame:() => frame, cancelled:() => cancelled };
}
let t=setup();
assert.equal(t.button.hidden,false); assert.equal(t.button.attrs['aria-pressed'],'true'); assert.ok(t.body.classes.has('motion-enabled'));
t.observer().callback([{isIntersecting:true,target:t.heading}]); assert.ok(t.heading.classes.has('motion-enter'));
t.doc.events.focusin({target:t.heading}); assert.ok(!t.heading.classes.has('motion-enter'), 'focused content never fades');
const move={pointerType:'mouse',clientX:400,clientY:400};
t.scene.events.pointermove(move); assert.ok(t.frame()); t.frame()(); assert.equal(t.stage.styles['--depth-y'],'-6.00deg');
t.scene.events.pointerleave(); assert.deepEqual(t.stage.styles,{});
t.scene.events.pointermove(move); const queuedObserver=t.observer();
t.button.events.click(); assert.equal(t.button.attrs['aria-pressed'],'false'); assert.equal(t.frame(),null); assert.ok(t.cancelled()>0); assert.deepEqual(t.writes,[['fluxora.motion','off']]);
queuedObserver.callback([{isIntersecting:true,target:t.scene}]); assert.ok(!t.scene.classes.has('motion-enter'), 'stale callback after opt-out is harmless');
t.scene.events.pointermove(move); assert.equal(t.frame(),null);
t.button.events.click(); assert.equal(t.button.attrs['aria-pressed'],'true');
t.reduce.matches=true; t.reduce.change(); assert.equal(t.button.disabled,true); assert.equal(t.button.textContent,'Motion off'); assert.deepEqual(t.stage.styles,{});
t=setup({reduced:true}); assert.equal(t.button.disabled,true); assert.ok(t.body.classes.has('motion-off')); t.scene.events.pointermove(move); assert.equal(t.frame(),null);
t=setup({saved:'off'}); assert.equal(t.button.attrs['aria-pressed'],'false');
t=setup({storageFails:true,observerAvailable:false}); t.button.events.click(); assert.equal(t.button.textContent,'Motion off'); t.button.events.click(); assert.equal(t.button.textContent,'Motion on');
t.scene.events.pointermove(move); t.doc.hidden=true; t.doc.events.visibilitychange(); assert.equal(t.frame(),null); assert.deepEqual(t.stage.styles,{});
t=setup({pointer:false}); t.scene.events.pointermove(move); assert.equal(t.frame(),null);
t=setup(); t.scene.events.pointermove({...move,pointerType:'touch'}); assert.equal(t.frame(),null);
t.scene.events.pointermove(move); t.win.events.blur(); assert.equal(t.frame(),null);
t=setup({site:false}); assert.equal(t.button.hidden,true); assert.equal(t.scene.events.pointermove,undefined);
console.log('PASS: reduced motion, opt-out, optional storage/observer, stale callbacks, focus, fine-pointer-only tilt, cancellation, hidden page and app exclusion. DOM doubles only.');
