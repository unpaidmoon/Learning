import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { NativeLease } from '../plugins/native-lease.mjs';

function fixture() {
  const policy = Object.freeze({ session_id: 'sid', run_id: 'run', request_id: 'request', epoch: 1 });
  const leases = new NativeLease(new Map()); leases.accept(policy);
  const session = { id: 'sid' }, cancellations = [];
  const agent = { id: 'sid', session, cancel: (...args) => cancellations.push(args) };
  const exec = { agent, token: Symbol(), name: 'read_nearby_subtitles', callId: 'call', arguments: { anchor_id: null } };
  const owner = Object.freeze({ policy, session, agent, token: exec.token, toolName: exec.name, callId: exec.callId,
    argsSha256: createHash('sha256').update('{"anchor_id":null}').digest('hex') });
  leases.calls.set(exec.token, owner);
  const ctx = { agents: { get: () => agent }, sessions: { get: () => session } };
  return { policy, leases, session, agent, exec, owner, ctx, cancellations };
}

test('explicit original business stop synchronously cancels only its own actor', () => {
  const f = fixture();
  assert.equal(f.leases.forwardBusinessStop(f.owner, f.exec, 409, 'NATIVE_RUN_CANCEL_REQUESTED', f.ctx), true);
  assert.deepEqual(f.cancellations, [[{ kind: 'user' }, { keepInbox: true }]]);
});

for (const [status, code] of [[409, 'NATIVE_CHAT_NOT_ACTIVE'], [409, 'NATIVE_TASK_NOT_ACTIVE'],
  [403, 'NATIVE_RUN_CANCEL_REQUESTED'], [200, 'NATIVE_RUN_CANCEL_REQUESTED'], [500, 'OTHER']]) {
  test(`generic or inconsistent response ${status}/${code} grants no cancellation`, () => {
    const f = fixture();
    assert.equal(f.leases.forwardBusinessStop(f.owner, f.exec, status, code, f.ctx), false);
    assert.equal(f.cancellations.length, 0);
  });
}

for (const [name, mutate] of [
  ['released original owner', f => { f.leases.history.get('run').state = 'released'; }],
  ['same actor reused for a later run', f => { f.leases.active.set('sid', { ...f.policy, run_id: 'next', epoch: 2 }); }],
  ['replaced live actor', f => { f.ctx.agents.get = () => ({ ...f.agent }); }],
  ['replaced session', f => { f.ctx.sessions.get = () => ({ id: 'sid' }); }],
  ['changed tool arguments', f => { f.exec.arguments = { anchor_id: 'different' }; }],
  ['changed private call token', f => { f.exec.token = Symbol(); }],
]) test(`late stop response cannot cancel ${name}`, () => {
  const f = fixture(); mutate(f);
  assert.throws(() => f.leases.forwardBusinessStop(f.owner, f.exec, 409, 'NATIVE_RUN_CANCEL_REQUESTED', f.ctx));
  assert.equal(f.cancellations.length, 0);
});
