const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// No browser, configuration, network request or pod action is started here.
const context = vm.createContext({ window: {}, document: { addEventListener() {} } });
vm.runInContext(
  fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'),
  context,
  { filename: 'app.js' },
);
const { project } = context.window.StartupProgressDisplay;
const status = (overrides = {}, progressOverrides = {}) => ({
  status: 'BOOTSTRAPPING',
  startup_progress: {
    phase: 'install', label: 'Wan2GP installieren', detail: 'Installing collected packages …',
    started_at: 1000, updated_at: 1065, active: true, ...progressOverrides,
  },
  ...overrides,
});

test('real active startup shows the API phase/detail and elapsed time, without a percentage', () => {
  const view = project(status(), 1080);
  assert.equal(view.visible, true);
  assert.equal(view.active, true);
  assert.equal(view.label, 'Wan2GP installieren');
  assert.equal(view.detail, 'Installing collected packages …');
  assert.equal(view.elapsedSeconds, 80);
  assert.equal(view.feedbackAgeSeconds, 15);
  assert.equal(view.state, 'Läuft');
  assert.equal(Object.hasOwn(view, 'percent'), false);
});

test('provisioning and SSH use current server phase labels instead of invented install progress', () => {
  for (const phase of ['booking', 'container', 'ssh', 'launch']) {
    const view = project(status({ status: 'STARTING' }, { phase, label: phase }), 1080);
    assert.equal(view.visible, true);
    assert.equal(view.active, true);
    assert.equal(view.label, phase);
  }
});

test('failure stops animation and freezes elapsed startup time at the final update', () => {
  const data = status({ status: 'ERROR' }, {
    phase: 'error', label: 'Wan2GP installieren — fehlgeschlagen', active: false,
  });
  const view = project(data, 1200);
  assert.equal(view.visible, true);
  assert.equal(view.error, true);
  assert.equal(view.active, false);
  assert.equal(view.state, 'Fehler');
  assert.equal(view.elapsedSeconds, 65);
  assert.equal(view.feedbackAgeSeconds, 135);
  assert.equal(project(data, 1300).elapsedSeconds, 65);
});

test('stale active flag cannot keep an error or cancelled startup animating', () => {
  assert.equal(project(status({ status: 'ERROR' }), 1080).active, false);
  const cancelled = project(status({}, { phase: 'cancelled', active: true }), 1080);
  assert.equal(cancelled.active, false);
  assert.equal(cancelled.state, 'Abgebrochen');
});

test('panel is hidden after ready, stopping, deletion or when offline, even with old progress', () => {
  for (const current of ['RUNNING', 'STOPPED', 'TERMINATED', 'OFFLINE']) {
    const view = project(status({ status: current }), 1080);
    assert.equal(view.visible, false);
    assert.equal(view.active, false);
  }
});

test('missing progress and timestamps stay unknown instead of inventing elapsed time', () => {
  const view = project({ status: 'STARTING', status_detail: 'Warte auf RunPod' }, 1080);
  assert.equal(view.visible, true);
  assert.equal(view.active, false);
  assert.equal(view.elapsedSeconds, null);
  assert.equal(view.feedbackAgeSeconds, null);
  assert.equal(view.detail, 'Warte auf RunPod');
  for (const invalid of [null, undefined, NaN, Infinity, -1, '1065']) {
    const invalidTimes = project(status({}, { started_at: invalid, updated_at: invalid }), 1080);
    assert.equal(invalidTimes.elapsedSeconds, null);
    assert.equal(invalidTimes.feedbackAgeSeconds, null);
  }
});

test('small clock differences never show a negative duration', () => {
  const view = project(status({}, { started_at: 1100, updated_at: 1100 }), 1080);
  assert.equal(view.elapsedSeconds, 0);
  assert.equal(view.feedbackAgeSeconds, 0);
});
