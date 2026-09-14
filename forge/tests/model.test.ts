import { test } from 'node:test';
import assert from 'node:assert/strict';
import { blankRequest, duplicateRow, readForgeFieldContext, removeRow, validateRequest } from '../src/model';

function valid() {
  const value = blankRequest();
  value.justification = 'Потрібен HTTPS';
  value.trafficLines[0].source = { kind: 'subnet', value: '203.0.113.0/24' };
  value.trafficLines[0].destination = { kind: 'ip', value: '192.0.2.10' };
  return value;
}
test('one subnet remains one item and object v1 matches bus schema', () => {
  const result = validateRequest(valid());
  assert.equal(result.schemaVersion, 1);
  assert.equal(result.trafficLines.length, 1);
  assert.equal(result.trafficLines[0].source.value, '203.0.113.0/24');
});
test('duplicate row is independent and last row cannot be deleted', () => {
  const original = valid();
  const copied = duplicateRow(original, 0);
  copied.trafficLines[1].destination.value = '192.0.2.20';
  assert.equal(original.trafficLines[0].destination.value, '192.0.2.10');
  assert.equal(copied.trafficLines[0].destination.value, '192.0.2.10');
  assert.equal(removeRow(original, 0).trafficLines.length, 1);
});
test('valid hostname and range survive without DNS resolution', () => {
  const value = valid();
  value.trafficLines[0].source = { kind: 'range', value: '198.51.100.1–198.51.100.10' };
  value.trafficLines[0].destination = { kind: 'hostname', value: 'app.example.local' };
  assert.equal(validateRequest(value).trafficLines[0].destination.kind, 'hostname');
});
test('invalid addresses, networks, ranges and ports are refused', () => {
  for (const source of [{ kind: 'ip', value: '999.1.1.1' }, { kind: 'subnet', value: '198.51.100.2/24' },
    { kind: 'range', value: '192.0.2.9-192.0.2.1' }, { kind: 'hostname', value: 'bad name' }]) {
    const value = valid();
    value.trafficLines[0].source = source as any;
    assert.throws(() => validateRequest(value));
  }
  const value = valid();
  value.trafficLines[0].services[0].port = 0;
  assert.throws(() => validateRequest(value));
});
test('unsupported versions and temporary duration are not silently overwritten', () => {
  assert.throws(() => validateRequest({ ...valid(), schemaVersion: 2 }));
  assert.throws(() => validateRequest({ ...valid(), duration: { kind: 'temporary' } }));
});
test('row limit prevents oversized requests', () => {
  const value = valid();
  value.trafficLines = Array.from({ length: 101 }, () => structuredClone(value.trafficLines[0]));
  assert.throws(() => validateRequest(value));
});

test('UI Kit field context enables blur submission in issue create', () => {
  const existing = valid();
  assert.deepEqual(readForgeFieldContext({
    extensionContext: { fieldValue: existing, renderContext: 'issue-create' },
  }), { fieldValue: existing, renderContext: 'issue-create' });
});

test('bridge field context remains supported for issue view', () => {
  const existing = valid();
  assert.deepEqual(readForgeFieldContext({
    extension: { fieldValue: existing, renderContext: 'issue-view' },
  }), { fieldValue: existing, renderContext: 'issue-view' });
});
