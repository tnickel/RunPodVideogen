const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Load only the pure comparison exports. No DOM, network, or application config.
const context = vm.createContext({ window: {} });
for (const file of ['gpu-performance.js', 'gpu-picker.js']) {
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'static', 'js', file), 'utf8'),
    context,
    { filename: file },
  );
}
const { metrics, compareRows } = context.window.GpuComparison;
const { seconds } = context.window.GpuBenchmarkData;
const A40 = 'NVIDIA A40';
const L40S = 'NVIDIA L40S';
const H100 = 'NVIDIA H100 80GB HBM3';
const RTX4090 = 'NVIDIA GeForce RTX 4090';

const gpu = (id, overrides = {}) => ({
  id, displayName: id.replace(/^NVIDIA /, ''), vram: 48,
  hourly_price: 0.5, availability: 'available', ...overrides,
});
const options = (overrides = {}) => ({
  reference: A40, search: '', minVram: 0, minSpeed: 0,
  availableOnly: false, sort: 'availability', ...overrides,
});
const ids = rows => Array.from(rows, row => row.id);
const near = (actual, expected) => assert.ok(
  Math.abs(actual - expected) < 1e-10,
  `Expected ${actual} to equal ${expected} within rounding tolerance`,
);

test('published FLUX times yield A40 = 1 and L40S = 2.12 times A40 speed', () => {
  assert.equal(seconds[A40], 32.79);
  assert.equal(seconds[L40S], 15.45);
  const reference = metrics(gpu(A40), A40, seconds);
  const candidate = metrics(gpu(L40S), A40, seconds);
  assert.equal(reference.speed, 1);
  near(candidate.speed, 2.122330097087379);
  assert.equal(candidate.seconds, 15.45);
});

test('efficiency is benchmark speed divided by the current hourly price', () => {
  const result = metrics(gpu(L40S, { hourly_price: 0.79 }), A40, seconds);
  near(result.efficiency, (32.79 / 15.45) / 0.79);
  const doublePrice = metrics(gpu(L40S, { hourly_price: 1.58 }), A40, seconds);
  near(result.efficiency / doublePrice.efficiency, 2);
});

test('absent, zero, negative, nonnumeric, and nonfinite prices cannot create efficiency', () => {
  for (const hourly_price of [undefined, null, 0, -0.5, '0.5', NaN, Infinity]) {
    const result = metrics(gpu(L40S, { hourly_price }), A40, seconds);
    assert.equal(result.price, null, `Invalid price ${hourly_price}`);
    assert.equal(result.efficiency, null, `Invalid price ${hourly_price}`);
    near(result.speed, 32.79 / 15.45);
  }
});

test('missing benchmarks stay unknown and never receive an estimated factor', () => {
  const result = metrics(gpu(RTX4090), A40, seconds);
  assert.equal(result.seconds, null);
  assert.equal(result.speed, null);
  assert.equal(result.efficiency, null);
  assert.equal(result.price, 0.5);
});

test('invalid benchmark durations or reference durations cannot produce factors', () => {
  for (const invalid of [undefined, null, 0, -1, '12', NaN, Infinity]) {
    const invalidCandidate = metrics(gpu(L40S), A40, { [A40]: 32.79, [L40S]: invalid });
    assert.equal(invalidCandidate.seconds, null);
    assert.equal(invalidCandidate.speed, null);
    assert.equal(invalidCandidate.efficiency, null);
    const invalidReference = metrics(gpu(L40S), A40, { [A40]: invalid, [L40S]: 15.45 });
    assert.equal(invalidReference.seconds, 15.45);
    assert.equal(invalidReference.speed, null);
    assert.equal(invalidReference.efficiency, null);
  }
});

test('2x filter includes measured faster GPUs and excludes missing benchmarks', () => {
  const candidates = [gpu(A40), gpu(L40S), gpu(H100), gpu(RTX4090)];
  assert.deepEqual(
    ids(compareRows(candidates, options({ minSpeed: 2, sort: 'speed' }), seconds)),
    [H100, L40S],
  );
  const unfiltered = ids(compareRows(candidates, options(), seconds));
  assert.ok(unfiltered.includes(RTX4090), 'Unknown benchmark is selectable without a speed filter');
  const atThreshold = compareRows([gpu('Boundary')], options({ minSpeed: 2 }), {
    [A40]: 10, Boundary: 5,
  });
  assert.equal(atThreshold.length, 1, 'Exactly 2x meets the minimum');
});

test('available-only includes limited stock and excludes unknown/unavailable stock', () => {
  const candidates = [
    gpu('Available', { availability: 'available' }),
    gpu('Limited', { availability: 'limited' }),
    gpu('Unknown', { availability: 'unknown' }),
    gpu('Unavailable', { availability: 'unavailable' }),
  ];
  assert.deepEqual(
    ids(compareRows(candidates, options({ availableOnly: true }), seconds)),
    ['Available', 'Limited'],
  );
  assert.equal(compareRows(candidates, options(), seconds).length, 4);
});

test('price sorting puts valid cheap offers first and all missing prices last', () => {
  const candidates = [
    gpu('Zero', { hourly_price: 0 }),
    gpu('Expensive', { hourly_price: 1.5 }),
    gpu('Absent', { hourly_price: null }),
    gpu('Cheap', { hourly_price: 0.2 }),
    gpu('Invalid', { hourly_price: NaN }),
  ];
  assert.deepEqual(
    ids(compareRows(candidates, options({ sort: 'price' }), seconds)),
    ['Cheap', 'Expensive', 'Absent', 'Invalid', 'Zero'],
  );
});

test('changing the reference recalculates speed and the 2x filter', () => {
  const candidates = [gpu(A40), gpu(L40S), gpu(H100)];
  const changed = compareRows(candidates, options({ reference: L40S, sort: 'speed' }), seconds);
  near(changed.find(row => row.id === A40).speed, 15.45 / 32.79);
  assert.equal(changed.find(row => row.id === L40S).speed, 1);
  near(changed.find(row => row.id === H100).speed, 15.45 / 5.86);
  assert.deepEqual(
    ids(compareRows(candidates, options({ reference: L40S, minSpeed: 2 }), seconds)),
    [H100],
  );
});

test('search and memory filters combine without modifying the input offers', () => {
  const candidates = [
    Object.freeze(gpu(L40S)),
    Object.freeze(gpu('NVIDIA L40', { vram: 24 })),
    Object.freeze(gpu(A40)),
  ];
  const originalOrder = candidates.map(row => row.id);
  Object.freeze(candidates);
  assert.deepEqual(
    ids(compareRows(candidates, options({ search: '  l40  ', minVram: 48 }), seconds)),
    [L40S],
  );
  assert.deepEqual(candidates.map(row => row.id), originalOrder);
  assert.ok(candidates.every(row => !Object.hasOwn(row, 'speed')));
});
