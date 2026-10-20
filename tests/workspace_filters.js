/* Offline DOM doubles: behaviour checks, not a browser/layout simulation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(text = '') { this.textContent = text; this.hidden = false; this.events = {}; this.attrs = {}; this.value = ''; }
  addEventListener(name, callback) { this.events[name] = callback; }
  dispatchEvent(event) { if (this.events[event.type]) this.events[event.type](event); }
  setAttribute(key, value) { this.attrs[key] = value; }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  remove() { this.removed = true; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
}

function fixture() {
  const input = new Element(); input.dataset = { tableFilter: 'table-1' };
  const label = new Element(); label.hidden = true;
  const status = new Element(); status.hidden = true;
  const holder = new Element(); holder.querySelector = () => status;
  input.closest = selector => selector === 'label' ? label : holder;
  const rows = ['USD 100 payroll', 'USD 200 invoice', 'USD 300 bill', 'EUR 100 invoice', 'USD 400 receipt', 'USD 500 adjustment'].map(text => {
    const row = new Element(text); row.querySelector = selector => selector === 'td' ? {} : null; return row;
  });
  const table = new Element(); table.querySelectorAll = () => rows;
  const host = new Element(); host.querySelector = () => table;
  const blocked = new Element(); blocked.dataset = { tableFilter: 'table-editable' };
  const blockedLabel = new Element(); blockedLabel.hidden = true; blocked.closest = () => blockedLabel;
  const editable = new Element(); editable.querySelector = () => ({ name: 'confirmed' });
  const blockedHost = new Element(); blockedHost.querySelector = () => editable;
  const feature = new Element(); const featureLabel = new Element(); featureLabel.hidden = true;
  feature.closest = () => featureLabel;
  const tiles = ['Payroll funding', 'Receipt review', 'Cash forecast'].map(text => new Element(text));
  const groups = [new Element(), new Element()];
  groups[0].querySelectorAll = () => tiles.slice(0, 2); groups[1].querySelectorAll = () => tiles.slice(2);
  const empty = new Element(); empty.hidden = true;
  const directoryStatus = new Element(); directoryStatus.hidden = true;
  const form = new Element(); form.checkValidity = () => true;
  const button = new Element('Confirm'); button.disabled = false;
  let loading = null;
  form.querySelector = selector => selector === '.loading-note' ? loading : button;
  form.append = note => { loading = note; };
  const doc = new Element();
  doc.getElementById = id => id === 'table-1' ? host : blockedHost;
  doc.querySelector = selector => ({ '[data-feature-filter]': feature, '[data-directory-empty]': empty, '[data-directory-status]': directoryStatus }[selector] || null);
  doc.querySelectorAll = selector => ({
    '[data-table-filter]': [input, blocked], '.directory-group': groups, 'form': [form],
    '[data-table-filter],[data-feature-filter]': [input, blocked, feature],
    'form[aria-busy]': form.attrs['aria-busy'] ? [form] : [], '.loading-note': loading ? [loading] : [],
  }[selector] || []);
  doc.createElement = () => new Element();
  const win = new Element();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../cashflow_copilot/static/fluxora.js'), 'utf8'), {
    document: doc, window: win, Event: class Event { constructor(type) { this.type = type; } },
    fetch: () => { throw new Error('Unexpected network request'); },
  });
  return { input, label, rows, status, blocked, blockedLabel, feature, featureLabel, tiles, groups, empty, directoryStatus, form, button, getLoading: () => loading, win };
}

const t = fixture();
assert.equal(t.label.hidden, false);
assert.equal(t.blockedLabel.hidden, true, 'editable table must never get a filter');
assert.equal(t.blocked.events.input, undefined);
t.input.value = '  UsD 300 '; t.input.dispatchEvent({ type: 'input' });
assert.deepEqual(t.rows.map(row => row.hidden), [true, true, false, true, true, true]);
assert.match(t.status.textContent, /^1 of 6 rows match/);
assert.match(t.status.textContent, /does not change totals/);
t.input.value = 'no such value'; t.input.dispatchEvent({ type: 'input' });
assert.equal(t.rows.filter(row => !row.hidden).length, 0);
assert.match(t.status.textContent, /^0 of 6/);
t.feature.value = 'receipt'; t.feature.dispatchEvent({ type: 'input' });
assert.deepEqual(t.tiles.map(tile => tile.hidden), [true, false, true]);
assert.deepEqual(t.groups.map(group => group.hidden), [false, true]);
assert.equal(t.empty.hidden, true);
assert.match(t.directoryStatus.textContent, /^1 workspace pages match/);
t.feature.value = 'unmatched'; t.feature.dispatchEvent({ type: 'input' });
assert.equal(t.empty.hidden, false);
t.form.dispatchEvent({ type: 'submit' });
assert.equal(t.form.attrs['aria-busy'], 'true');
assert.equal(t.button.disabled, false, 'submission controls must keep their values');
assert.ok(t.getLoading());
assert.equal(t.button.attrs['aria-disabled'], 'true');
let duplicateBlocked = false;
t.form.dispatchEvent({ type: 'submit', preventDefault() { duplicateBlocked = true; } });
assert.equal(duplicateBlocked, true, 'second submit is blocked without disabling successful controls');
assert.match(t.getLoading().textContent, /completion is not confirmed/i);
t.win.dispatchEvent({ type: 'pageshow' });
assert.equal(t.input.value, ''); assert.equal(t.feature.value, '');
assert.ok(t.rows.every(row => !row.hidden)); assert.ok(t.tiles.every(tile => !tile.hidden));
assert.equal(t.status.hidden, true); assert.equal(t.directoryStatus.hidden, true);
assert.equal(t.empty.hidden, true); assert.ok(t.groups.every(group => !group.hidden));
assert.equal(t.form.attrs['aria-busy'], undefined); assert.equal(t.getLoading().removed, true);
assert.equal(t.button.attrs['aria-disabled'], undefined);
const cancelled = fixture(); cancelled.form.dispatchEvent({ type: 'submit', defaultPrevented: true });
assert.equal(cancelled.form.attrs['aria-busy'], undefined, 'cancelled submissions stay editable');
const invalid = fixture(); invalid.form.checkValidity = () => false; invalid.form.dispatchEvent({ type: 'submit' });
assert.equal(invalid.form.attrs['aria-busy'], undefined, 'invalid form never becomes busy');
console.log('PASS: local filtering, editable-table exclusion, unchanged order/controls, no requests, and back-navigation reset. DOM doubles only; no visual/browser claim.');
