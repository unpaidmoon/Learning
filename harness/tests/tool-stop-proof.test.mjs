import test from 'node:test';
import assert from 'node:assert/strict';
import { proveToolStop, verifyToolStopCut } from '../plugins/native-recovery.mjs';

function fixture(mutate = () => {}) {
  const policy = { session_id: 'sid', run_id: 'run', request_id: 'request', epoch: 1 };
  const events = [
    { type: 'turn/start', data: { turn: 1 } },
    { type: 'user/message', data: { source: { kind: 'user', rpcId: 'request' }, content: [{ type: 'text', text: '[[learning-generation:run]]\n' }] } },
    { type: 'assistant/message', data: { turn: 1, step: 2 } },
    { type: 'tool/call', data: { turn: 1, step: 2, callId: 'call', name: 'read_nearby_subtitles', arguments: '{"anchor_id":null}' } },
    { type: 'tool/result', data: { turn: 1, step: 2, message: { toolCallId: 'call' }, error: { code: 'ABORTED' } } },
    { type: 'turn/end', data: { turn: 1, reason: { kind: 'aborted', reason: { kind: 'user' } } } },
  ];
  events.forEach((event, seq) => { event.seq = seq; });
  const session = {}, agent = { status: 'idle', session };
  const reads = [];
  const ctx = { agents: { get: () => agent }, sessions: { get: () => session, flush: async () => true },
    sessionPersistence: { open: async (id, mode) => {
      reads.push([id, mode]);
      return { header: { id }, read: async () => ({ events }), close: async () => {} };
    } } };
  const leases = { assertPolicyActive: value => assert.equal(value, policy) };
  const attempts = { assertSettled: value => assert.equal(value, policy) };
  mutate({ events, ctx, agent, attempts });
  return { ctx, leases, attempts, policy, reads };
}

test('physical original tool stop yields only bound metadata, without a new dispatch', async () => {
  const f = fixture();
  const value = await proveToolStop(f.ctx, f.leases, f.attempts, f.policy);
  assert.deepEqual(f.reads, [['sid', 'read']]);
  assert.equal(value.call.native_call_seq, 3);
  assert.equal(value.result_seq, 4);
  assert.equal(value.native_terminal.seq, 5);
  assert.match(value.native_cut_sha256, /^[a-f0-9]{64}$/);
});

for (const [name, mutate] of [
  ['active actor', ({ agent }) => { agent.status = 'working'; }],
  ['unknown provider record', ({ attempts }) => { attempts.assertSettled = () => { throw Error('unknown'); }; }],
  ['missing live actor', ({ ctx }) => { ctx.agents.get = () => undefined; }],
  ['wrong original request', ({ events }) => { events[1].data.source.rpcId = 'other'; }],
  ['missing tool result', ({ events }) => { events[4].type = 'metadata'; }],
  ['unknown tool outcome', ({ events }) => { events[4].data.error.code = 'TOOL_OUTCOME_UNKNOWN'; }],
  ['non-user terminal', ({ events }) => { events[5].data.reason = { kind: 'error' }; }],
  ['later native assistant activity', ({ events }) => { events.push({ seq: 6, type: 'assistant/attempt', data: {} }); }],
]) test(`rejects ${name}`, async () => {
  const f = fixture(mutate);
  await assert.rejects(proveToolStop(f.ctx, f.leases, f.attempts, f.policy));
});

test('cold verification accepts only an unchanged prefix and empty runtime seed boundaries', async () => {
  const f = fixture();
  const proof = await proveToolStop(f.ctx, f.leases, f.attempts, f.policy);
  const handle = await f.ctx.sessionPersistence.open('sid', 'read');
  const events = (await handle.read()).events;
  const cut = { events: [...events, { seq: 6, type: 'session/end-seed', data: {} }] };
  verifyToolStopCut(proof, cut, proof.native_terminal);
  for (const extra of [{ seq: 6, type: 'session/end-seed', data: { inherited: true } },
    { seq: 6, type: 'assistant/attempt', data: {} }, { seq: 6, type: 'user/message', data: {} }]) {
    assert.throws(() => verifyToolStopCut(proof, { events: [...events, extra] }, proof.native_terminal));
  }
  const changed = structuredClone(events);
  changed[3].data.name = 'changed_tool';
  assert.throws(() => verifyToolStopCut(proof, { events: changed }, proof.native_terminal));
});
