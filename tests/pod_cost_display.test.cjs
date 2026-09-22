const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Evaluate the pure price projection without starting the app, network or timers.
const context = vm.createContext({
  window: {},
  document: { addEventListener() {} },
});
vm.runInContext(
  fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'),
  context,
  { filename: 'app.js' },
);
const { project } = context.window.PodCostDisplay;
const { canPause } = context.window.PodActionsDisplay;
const pod = overrides => ({
  pod_id: 'current-pod', hourly_rate: 0.79, hourly_rate_source: 'runpod', ...overrides,
});

test('new pod uses its own RunPod rate, without adding a guessed storage tariff', () => {
  assert.equal(project(pod({ hourly_rate: 0.49 }), 18.44).hourlyRate, 0.49);
  const current = project(pod({ config: { volume_disk_gb: 75 } }), 18.44);
  assert.equal(current.hourlyRate, 0.79);
  assert.equal(current.dailyRate, 0.79 * 24);
  assert.equal(current.weeklyRate, 0.79 * 168);
  assert.equal(current.hoursRemaining, 18.44 / 0.79);
});

test('booking a new pod without a confirmed rate leaves every projection unknown', () => {
  for (const overrides of [
    { hourly_rate: null },
    { hourly_rate_source: 'unknown' },
    { hourly_rate_source: undefined },
  ]) {
    const current = project(pod({ status: 'STARTING', ...overrides }), 18.44);
    assert.equal(current.hourlyRate, null);
    assert.equal(current.dailyRate, null);
    assert.equal(current.weeklyRate, null);
    assert.equal(current.hoursRemaining, null);
  }
});

test('a stale rate without a current pod is not displayed or projected', () => {
  for (const status of ['STARTING', 'ERROR', 'TERMINATED', 'OFFLINE']) {
    const current = project(pod({ pod_id: null, status, hourly_rate: 0.49 }), 18.44);
    assert.equal(current.hourlyRate, null);
    assert.equal(current.hoursRemaining, null);
  }
});

test('choosing another GPU for the next pod does not change the current pod rate', () => {
  const current = project(pod({ config: { gpu_type_id: 'NVIDIA H100 80GB HBM3' } }), 18.44);
  assert.equal(current.hourlyRate, 0.79);
});

test('invalid prices cannot yield plausible fallback costs', () => {
  for (const hourly_rate of [undefined, '0.79', NaN, Infinity, -0.79]) {
    const current = project(pod({ hourly_rate }), 18.44);
    assert.equal(current.hourlyRate, null);
    assert.equal(current.hoursRemaining, null);
  }
});

test('a real zero price stays zero without division by zero or a replacement price', () => {
  const current = project(pod({ hourly_rate: 0 }), 18.44);
  assert.equal(current.hourlyRate, 0);
  assert.equal(current.dailyRate, 0);
  assert.equal(current.weeklyRate, 0);
  assert.equal(current.hoursRemaining, null);
});

test('invalid balances do not create an invented remaining runtime', () => {
  for (const balance of [undefined, null, NaN, Infinity, -1, '18.44']) {
    assert.equal(project(pod(), balance).hoursRemaining, null);
  }
  assert.equal(project(pod(), 0).hoursRemaining, 0);
});

test('a failed setup on an existing pod still permits stopping the billed GPU', () => {
  assert.equal(canPause(pod({ status: 'ERROR' })), true);
  assert.equal(canPause(pod({ status: 'RUNNING' })), true);
  assert.equal(canPause(pod({ status: 'ERROR', pod_id: null })), false);
  assert.equal(canPause(pod({ status: 'RUNNING', pod_id: null })), false);
  for (const status of ['STARTING', 'BOOTSTRAPPING', 'STOPPED', 'OFFLINE', 'TERMINATED']) {
    assert.equal(canPause(pod({ status })), false);
  }
});

test('error status suppresses the session estimate without suppressing the known GPU rate', () => {
  const current = project(pod({ status: 'ERROR', uptime_seconds: 0, accumulated_cost: 0.2251 }), 18.44);
  assert.equal(current.hourlyRate, 0.79);
  assert.equal(current.sessionCost, null);
  assert.equal(project(pod({ status: 'RUNNING', accumulated_cost: 0.2251 }), 18.44).sessionCost, 0.2251);
});
