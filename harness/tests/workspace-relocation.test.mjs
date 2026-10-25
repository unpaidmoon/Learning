import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { workspaceRelocator } from '../plugins/workspace-relocation.mjs';

const old = path.resolve('owned-original-workspace');
const current = path.resolve('owned-moved-workspace');
const record = { format: 1, origins: [old], target: current };

test('closed migration appends the actual directory event once without model notice or header rewrite', () => {
  let directory = old;
  const events = [];
  const session = { header: { cwd: old }, append: (type, value) => { events.push({ type, value }); directory = value.cwd; } };
  const relocate = workspaceRelocator(record, current);
  const service = { get: () => directory };
  assert.equal(relocate({ session }, service), true);
  assert.equal(relocate({ session }, service), false);
  assert.deepEqual(events, [{ type: 'working-directory/change', value: { cwd: current } }]);
  assert.equal(session.header.cwd, old);
});

test('unmapped or unchanged directory receives no mutation', () => {
  const session = { append: () => { throw Error('unexpected mutation'); } };
  assert.equal(workspaceRelocator(null, current)({ session }), false);
  assert.equal(workspaceRelocator(record, current)({ session }, { get: () => path.resolve('different-course-location') }), false);
});

test('mapping cannot choose another destination or smuggle unknown state', () => {
  for (const invalid of [
    { ...record, target: old }, { ...record, target: 'relative' },
    { ...record, origins: ['relative'] }, { ...record, origins: [current] },
    { ...record, command: 'ignored' }, { ...record, format: 2 },
  ]) assert.throws(() => workspaceRelocator(invalid, current), /LEARNING_WORKSPACE_RELOCATION_INVALID/);
});
