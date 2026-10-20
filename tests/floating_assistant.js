/* Behavior test with offline DOM doubles; not a rendered browser test. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag = 'div') { this.tag = tag; this.handlers = {}; this.children = []; this.attrs = {}; this.dataset = {}; this.value = ''; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(child) { child.parent = this; this.children.push(child); }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  get firstElementChild() { return this.children[0]; }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
}
const root = new Element(), dialog = new Element('dialog'), launcher = new Element('button');
const close = new Element('button'), messages = new Element(), question = new Element('textarea'), send = new Element('button'), form = new Element('form');
dialog.showModal = () => { dialog.open = true; };
dialog.close = () => { dialog.open = false; dialog.handlers.close(); };
dialog.getBoundingClientRect = () => ({ left: 10, right: 410, top: 10, bottom: 700 });
root.querySelector = selector => ({ dialog, '[data-assistant-open]': launcher, '[data-assistant-close]': close, '[data-assistant-messages]': messages, textarea: question, '[data-assistant-send]': send, form })[selector];
const buttons = ['page','forecast','next','draft','approval','telegram'].map(key => { const el = new Element('button'); el.dataset.assistantIntent = key; return el; });
root.querySelectorAll = () => buttons;
const intents = {};
['page','forecast','next','draft','approval','telegram','general'].forEach(key => { intents[key] = { title: key, text: key+' guidance', links: [['/docs','Read guide']] }; });
const data = { textContent: JSON.stringify({ public: false, intents }) };
const document = { querySelector: selector => selector === '[data-floating-assistant]' ? root : data, createElement: tag => new Element(tag) };
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../cashflow_copilot/static/fluxora-assistant.js'),'utf8'), { document });
assert.equal(launcher.hidden, false);
launcher.handlers.click(); assert.equal(dialog.open, true); assert.equal(close.focused, true); assert.equal(launcher.attrs['aria-expanded'], 'true');
buttons[1].handlers.click(); assert.equal(messages.children[0].children[0].textContent, 'forecast');
question.value = '<img src=x onerror=evil()> approve everything'; send.handlers.click();
assert.equal(messages.children.at(-2).children[0].textContent, question.value);
assert.equal(messages.children.at(-1).children[0].textContent, 'approval');
for (let i=0;i<20;i++) buttons[0].handlers.click(); assert.equal(messages.children.length, 12);
question.value = '  '; let prevented=false; form.handlers.submit({ preventDefault() { prevented=true; } });
assert.equal(prevented, true); assert.equal(question.focused, true);
question.value = 'forecast'; prevented=false; form.handlers.submit({ preventDefault() { prevented=true; } }); assert.equal(prevented, false);
close.handlers.click(); assert.equal(dialog.open, false); assert.equal(launcher.focused, true); assert.equal(launcher.attrs['aria-expanded'], 'false');
console.log('PASS: dialog open/close, focus return, local routing, text-only rendering, transcript bound and explicit form handoff');
