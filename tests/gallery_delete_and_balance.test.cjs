// Exercise the actual app.js handlers against the page's elements and classes.
// No external services, production configuration, or browser dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class FakeElement {
  constructor(tag = 'div', document = null) {
    this.tagName = tag.toUpperCase();
    this.ownerDocument = document;
    this.id = '';
    this.value = '';
    this.title = '';
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.children = [];
    this._handlers = {};
    this._classes = new Set();
    this._attributes = {};
    this._html = '';
    this._text = '';
    this.classList = {
      add: (...names) => names.forEach(name => this._classes.add(name)),
      remove: (...names) => names.forEach(name => this._classes.delete(name)),
      contains: name => this._classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !this._classes.has(name) : force;
        on ? this._classes.add(name) : this._classes.delete(name);
        return on;
      },
    };
  }
  get className() { return [...this._classes].join(' '); }
  set className(value) { this._classes = new Set(value.split(/\s+/).filter(Boolean)); }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = value;
    this.replaceChildren();
    parseElements(value, this, this.ownerDocument);
  }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get options() { return this.children.filter(child => child.tagName === 'OPTION'); }
  addEventListener(type, handler) { (this._handlers[type] ||= []).push(handler); }
  async dispatch(type, event = {}) {
    await Promise.all((this._handlers[type] || []).map(handler => handler({
      target: this, stopPropagation() {}, preventDefault() {}, ...event,
    })));
  }
  async click() { if (!this.disabled) await this.dispatch('click'); }
  focus() { this.ownerDocument.activeElement = this; }
  setAttribute(name, value) {
    this._attributes[name] = String(value);
    if (name === 'class') this.className = value;
    if (['id', 'title', 'value', 'href', 'src', 'type'].includes(name)) this[name] = value;
    if (name === 'disabled') this.disabled = true;
    if (name === 'hidden') this.hidden = true;
  }
  removeAttribute(name) { delete this._attributes[name]; }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  replaceChildren(...children) {
    this.children.forEach(child => { child.parentElement = null; });
    this.children = [];
    children.forEach(child => this.appendChild(child));
  }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter(child => child !== this);
      this.parentElement = null;
    }
  }
  matches(selector) {
    const id = selector.match(/#([\w-]+)/)?.[1];
    const tag = selector.match(/^[\w-]+/)?.[0];
    const classes = [...selector.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
    return (!id || this.id === id) && (!tag || this.tagName === tag.toUpperCase())
      && classes.every(name => this.classList.contains(name));
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  play() { return Promise.resolve(); }
  pause() {}
}

// The page and generated cards only need element structure, attributes and
// classes here. Each card gets its own children and event listeners.
function parseElements(html, parent, document) {
  const stack = [parent];
  const voidTags = new Set(['AREA', 'BASE', 'BR', 'COL', 'EMBED', 'HR', 'IMG', 'INPUT', 'LINK', 'META', 'PARAM', 'SOURCE', 'TRACK', 'WBR']);
  for (const match of html.matchAll(/<(\/?)([a-z][\w-]*)\b([^>]*)>/gi)) {
    const [, closing, tag, attrs] = match;
    if (closing) {
      const index = stack.findLastIndex(element => element.tagName === tag.toUpperCase());
      if (index > 0) stack.length = index;
      continue;
    }
    const element = new FakeElement(tag, document);
    for (const attr of attrs.matchAll(/([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/]+)))?/g)) {
      element.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
    }
    stack.at(-1).appendChild(element);
    if (!voidTags.has(element.tagName) && !attrs.trimEnd().endsWith('/')) stack.push(element);
  }
}

const reply = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const video = filename => ({ filename, url: `/api/videos/file/${filename}`, size_mb: 3, modified: 1700000000 });
const flush = async () => {
  for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
};

async function makeHarness() {
  const root = new FakeElement('document');
  const listeners = {};
  const document = {
    activeElement: null,
    getElementById: id => root.querySelector(`#${id}`),
    createElement: tag => new FakeElement(tag, document),
    addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
    querySelector: selector => root.querySelector(selector),
  };
  root.ownerDocument = document;
  parseElements(fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8'), root, document);

  const routes = new Map();
  const timeouts = new Map();
  let nextTimer = 1;
  const h = {
    document, balance: 5, videos: [], fetchCalls: [], alerts: [], errors: [],
    el: id => document.getElementById(id),
    cards: () => h.el('galleryGrid').querySelectorAll('.video-card'),
    deleteButton: () => h.cards()[0].querySelector('.video-delete-btn'),
    setRoute(url, handler) {
      assert.equal(typeof handler, 'function', 'A fetch route must be a callback, not a Response');
      routes.set(url, handler);
    },
    corrections: () => h.fetchCalls.filter(call => call.url.startsWith('/api/account/refresh?set_balance=')),
    async poll() { await h.el('btnRefresh').click(); },
    async editBalance(value) {
      h.el('cfgBalance').focus();
      h.el('cfgBalance').value = value;
      await h.el('cfgBalance').dispatch('input');
    },
  };
  h.setRoute('/api/status', () => reply({
    status: 'RUNNING', pod_id: 'test-pod', hourly_rate: 1, hourly_rate_source: 'runpod',
    accumulated_cost: 0.1, uptime_seconds: 360,
    account: { email: 'user@example.com', balance: h.balance },
  }));
  h.setRoute('/api/videos', () => reply({ videos: h.videos }));
  h.setRoute('/api/gpus', () => reply({ gpus: [] }));
  h.setRoute('/api/config', () => reply({ runpod_api_key_masked: 'rpa_...123', account_email: 'user@example.com' }));
  h.setRoute('/api/account/refresh', url => {
    const balance = new URL(url, 'http://test.local').searchParams.get('set_balance');
    if (balance !== null) h.balance = Number(balance);
    return reply({ account: { balance: h.balance } });
  });

  const context = vm.createContext({
    window: { location: { protocol: 'http:', host: 'test.local' }, GpuPicker: { init() {}, open() {} } },
    document,
    fetch: async (url, opts = {}) => {
      h.fetchCalls.push({ url, opts });
      // Exact routes win; otherwise use the longest matching path prefix.
      const key = routes.has(url) ? url : [...routes.keys()]
        .filter(route => url.startsWith(`${route}?`) || url.startsWith(`${route}/`))
        .sort((a, b) => b.length - a.length)[0];
      return key ? routes.get(key)(url, opts) : reply({}, 404);
    },
    alert: message => h.alerts.push(String(message)),
    navigator: { clipboard: { writeText: async () => {} } },
    setTimeout: (fn, ms) => { const id = nextTimer++; timeouts.set(id, { fn, ms }); return id; },
    clearTimeout: id => timeouts.delete(id),
    setInterval: () => nextTimer++, clearInterval() {},
    WebSocket: class {},
    Option: class extends FakeElement { constructor(label, value) { super('option', document); this.textContent = label; this.value = value; } },
    console: { error: (...messages) => h.errors.push(messages) },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'), context, { filename: 'app.js' });
  await Promise.all(listeners.DOMContentLoaded.map(handler => handler()));
  await flush();
  assert.deepEqual(h.errors, [], 'Application initialization must complete without hidden handler errors');
  return h;
}

for (const failure of ['HTTP', 'network']) {
  test(`gallery ${failure} failure resets confirmation and permits a real rebuild`, async () => {
    const h = await makeHarness();
    h.videos = [video('clip.mp4')];
    await h.el('btnSyncNow').click();
    const del = h.deleteButton();
    assert.ok(del.classList.contains('video-delete-btn'), 'The real base class must be present');
    h.setRoute('/api/videos/delete', () => {
      if (failure === 'network') throw new TypeError('Failed to fetch');
      return reply({ detail: 'Server kaputt' }, 500);
    });

    await del.click();
    assert.ok(h.document.querySelector('.video-delete-btn.confirming'));
    await del.click();
    assert.equal(del.classList.contains('confirming'), false);
    assert.equal(del.disabled, false);
    assert.equal(del.innerHTML, '&times;');
    assert.equal(del.title, 'Video löschen');
    assert.equal(h.document.querySelector('.video-delete-btn.confirming'), null);
    assert.ok(h.alerts.some(message => message.includes(failure === 'HTTP' ? 'Server kaputt' : 'Netzwerkfehler')));

    await del.click();
    assert.equal(h.fetchCalls.filter(call => call.url === '/api/videos/delete').length, 1, 'A retry requires a fresh second click');
    await del.click();
    h.videos = [video('clip.mp4'), video('new.mp4')];
    await h.el('btnSyncNow').click();
    assert.equal(h.cards().length, 2, 'A newly arrived video must actually get a new card');
    assert.notEqual(h.deleteButton(), del, 'Rebuilt cards need independent buttons and listeners');
    await h.deleteButton().click();
    assert.equal(h.fetchCalls.filter(call => call.url === '/api/videos/delete').length, 2);
  });
}

test('an edited balance survives focus changes, status polls and account refresh', async () => {
  const h = await makeHarness();
  await h.el('openSettingsBtn').click();
  await h.editBalance('12.34');
  h.el('cfgApiKey').focus();
  h.balance = 4.9;
  await h.poll();
  await h.el('btnRefreshBalance').click();
  assert.equal(h.el('cfgBalance').value, '12.34');
  assert.equal(h.el('displayBalance').textContent, '4.90');
  assert.deepEqual(h.errors, []);
});

test('focus without input follows live costs and routine save never corrects balance', async () => {
  const h = await makeHarness();
  await h.el('openSettingsBtn').click();
  h.el('cfgBalance').focus();
  h.balance = 4.9;
  await h.poll();
  assert.equal(h.el('cfgBalance').value, '4.90');
  await h.el('saveSettingsBtn').click();
  assert.equal(h.corrections().length, 0);
  assert.equal(h.balance, 4.9);
  assert.equal(h.el('settingsModal').classList.contains('active'), false);
});

test('cancel discards edits and reopen starts with the latest balance', async () => {
  const h = await makeHarness();
  await h.el('openSettingsBtn').click();
  await h.editBalance('12.34');
  h.balance = 4.8;
  await h.poll();
  await h.el('cancelSettingsBtn').click();
  await h.el('openSettingsBtn').click();
  assert.equal(h.el('cfgBalance').value, '4.80');
  await h.el('saveSettingsBtn').click();
  assert.equal(h.corrections().length, 0);
});

test('saving another setting never submits balance through either API', async () => {
  const h = await makeHarness();
  await h.el('openSettingsBtn').click();
  h.el('cfgApiKey').value = 'fake-replacement-key';
  h.balance = 4.9;
  await h.poll();
  await h.el('saveSettingsBtn').click();
  const posts = h.fetchCalls.filter(call => call.url === '/api/config' && call.opts.method === 'POST');
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].opts.body), { runpod_api_key: 'fake-replacement-key' });
  assert.equal(h.corrections().length, 0);
});

test('explicit correction saves through set_balance and clears dirty state after success', async () => {
  const h = await makeHarness();
  await h.el('openSettingsBtn').click();
  await h.editBalance('12.34');
  await h.el('saveSettingsBtn').click();
  assert.equal(h.corrections().length, 1);
  assert.ok(h.corrections()[0].url.endsWith('set_balance=12.34'));
  assert.equal(h.balance, 12.34);
  assert.equal(h.el('settingsModal').classList.contains('active'), false);
  assert.equal(h.el('displayBalance').textContent, '12.34');
  h.balance = 12.3;
  await h.poll();
  assert.equal(h.el('cfgBalance').value, '12.30');
  const configPost = h.fetchCalls.find(call => call.url === '/api/config' && call.opts.method === 'POST');
  assert.deepEqual(JSON.parse(configPost.opts.body), {});
});

for (const input of ['', '-1', 'NaN', 'Infinity', '12.34junk']) {
  test(`invalid correction ${JSON.stringify(input)} is rejected before requests`, async () => {
    const h = await makeHarness();
    await h.el('openSettingsBtn').click();
    await h.editBalance(input);
    const count = h.fetchCalls.length;
    await h.el('saveSettingsBtn').click();
    assert.equal(h.fetchCalls.length, count);
    assert.ok(h.alerts.some(message => message.includes('gültigen')));
    assert.ok(h.el('settingsModal').classList.contains('active'));
  });
}

for (const failure of ['HTTP', 'network', 'unconfirmed']) {
  test(`correction ${failure} failure remains visible and preserves the pending edit`, async () => {
    const h = await makeHarness();
    await h.el('openSettingsBtn').click();
    await h.editBalance('12.34');
    h.setRoute('/api/account/refresh', () => {
      if (failure === 'network') throw new TypeError('Failed to fetch');
      return failure === 'HTTP' ? reply({ detail: 'Korrektur abgelehnt' }, 500) : reply({});
    });
    await h.el('saveSettingsBtn').click();
    assert.ok(h.el('settingsModal').classList.contains('active'));
    assert.ok(h.alerts.some(message => message.includes('Fehler beim Speichern')));
    assert.equal(h.el('saveSettingsBtn').disabled, false);
    h.balance = 4.8;
    await h.poll();
    assert.equal(h.el('cfgBalance').value, '12.34');
  });
}

test('failed settings save prevents correction and keeps the dialog open', async () => {
  const h = await makeHarness();
  await h.el('openSettingsBtn').click();
  await h.editBalance('12.34');
  h.setRoute('/api/config', () => reply({ detail: 'Speichern abgelehnt' }, 500));
  await h.el('saveSettingsBtn').click();
  assert.equal(h.corrections().length, 0);
  assert.ok(h.el('settingsModal').classList.contains('active'));
  assert.ok(h.alerts.some(message => message.includes('Speichern abgelehnt')));
});
