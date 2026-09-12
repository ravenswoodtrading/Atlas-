// Exercise delegated handlers without a server, browser or database.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const template = name => readFileSync(path.join(__dirname, 'app/templates', name), 'utf8').replace(/\r\n/g, '\n');
const listeners = {}, requests = [], alerts = [];
let removed = false, hidden = false, copied = '';
const body = {innerHTML: '', querySelector: () => null};
const document = {
  addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
  getElementById(id) { return id === 'qi-detail-body' ? body : {}; },
  querySelectorAll() { return [{remove() { removed = true; }}]; },
};
const context = vm.createContext({document, URLSearchParams, console, setTimeout() {},
  bootstrap: {Offcanvas: class {show() {} hide() {hidden = true;}}, Toast: class {show() {}}},
  alert(message) {alerts.push(message);},
  navigator: {clipboard: {writeText(asin) {copied = asin; return Promise.resolve();}}},
  fetch(url, options) {
    requests.push({url, options});
    return Promise.resolve({ok: false, json: async () => ({detail: 'Reason required'}), text: async () => ''});
  },
});
vm.runInContext(template('_review_queue_scripts.html').replace(/<\/?script>/g, ''), context);
listeners.DOMContentLoaded[0]();
const base = template('base.html');
const copyStart = base.indexOf('(function() {\n    // Real bug, 2026-09-07');
vm.runInContext(base.slice(copyStart, base.indexOf('})();', copyStart) + 5), context);
const dispatch = (type, target, extra = {}) => {
  for (const listener of listeners[type]) listener({target, preventDefault() {}, ...extra});
};
async function run() {
  const icon = {className: 'bi bi-clipboard'};
  const copy = {dataset: {asin: 'B000000001'}, querySelector: () => icon};
  dispatch('click', {closest: selector => selector === '.copy-asin-btn' ? copy : {dataset: {asin: 'B000000001'}}});
  await new Promise(setImmediate);
  assert.equal(copied, 'B000000001');
  assert.equal(requests.length, 0, 'Copy must not open the detail panel');
  assert.match(icon.className, /clipboard-check/);
  assert(!template('_competitors_opportunities.html').includes('stopPropagation'));

  const fields = {'asin': {value: 'B000000001'}, 'reason': {value: '', focus() {}}, 'reason_category': {value: '', focus() {}}};
  const form = {dataset: {}, querySelector(selector) {return fields[selector.match(/name="([^"]+)"/)[1]] || null;}};
  const target = {closest: selector => selector === '.qi-detail-actions-form' ? form : null};
  dispatch('submit', target, {submitter: {value: 'rejected'}});
  assert.equal(requests.length, 0);
  fields.reason_category.value = 'OTHER';
  fields.reason.value = '   ';
  dispatch('submit', target, {submitter: {value: 'rejected'}});
  assert.equal(requests.length, 0);
  fields.reason.value = 'Wrong size';
  dispatch('submit', target, {submitter: {value: 'rejected'}});
  await new Promise(setImmediate);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.body.get('reason'), 'Wrong size');
  assert.equal(removed, false, 'Failed saves must retain the queue row');
  assert.equal(hidden, false, 'Failed saves must retain the panel');
  assert.equal(alerts.at(-1), 'Reason required');
  console.log('PASS: copy propagation, required reasons, valid payload, failed-save preservation');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
