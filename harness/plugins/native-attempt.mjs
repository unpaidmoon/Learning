// Private correlation, never business authorization. No prompt/body rewriting.
import { AsyncLocalStorage } from 'node:async_hooks';
import { randomUUID, createHash } from 'node:crypto';
const canon = value => Array.isArray(value) ? value.map(canon) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(k => [k, canon(value[k])])) : value;
const digest = value => createHash('sha256').update(JSON.stringify(canon(value))).digest('hex');
const callView = block => ({ id: block.id, name: block.name, argsSha256: digest(JSON.parse(block.arguments || '{}')) });

export class NativeAttempts {
  constructor(endpoint, leases) {
    this.endpoint = endpoint; this.leases = leases; this.local = new AsyncLocalStorage();
    this.readonlyRecovery = new Map(); this.positions = new Map(); this.pending = new Map(); this.records = new Map(); this.messages = new Map(); this.denials = [];
  }
  position(agent, payload) {
    if (!Number.isInteger(payload.turn) || !Number.isInteger(payload.step)) throw Error('ATTEMPT_NATIVE_POSITION_REQUIRED');
    this.positions.set(agent.id, Object.freeze({ turn: payload.turn, step: payload.step }));
  }
  decorate(input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.origin !== this.endpoint.origin || url.pathname !== '/v1/messages') return { input, init, record: null };
    const record = this.local.getStore();
    if (!record || record.fetches !== 0) throw Error('ATTEMPT_PRIVATE_CONTEXT_REQUIRED');
    this.leases.assertPolicyActive(record.policy);
    record.fetches++;
    const headers = new Headers(init?.headers);
    if (headers.has('x-learning-dispatch-id')) throw Error('ATTEMPT_HEADER_ALREADY_SET');
    headers.set('x-learning-dispatch-id', record.dispatchId);
    return { input, init: { ...init, headers }, record };
  }
  acceptResponse(record, response) {
    if (!record) return;
    const attempt = response.headers.get('x-learning-attempt-id');
    if (response.headers.get('x-learning-dispatch-id') !== record.dispatchId
      || !/^boundary-[a-f0-9]{32}$/.test(attempt || '')) throw Error('ATTEMPT_RESPONSE_BINDING_REQUIRED');
    record.attemptId = attempt;
  }
  wrap(options, next, policy) {
    const position = this.positions.get(policy.session_id);
    if (!position || this.pending.has(policy.session_id)) throw Error('ATTEMPT_PREVIOUS_UNSETTLED');
    const record = { policy, ...position, dispatchId: randomUUID(), attemptId: null, fetches: 0,
      state: 'running', tools: [], finishSeen: false, nativeMessageId: null };
    this.records.set(record.dispatchId, record); this.pending.set(policy.session_id, record);
    const runtime = this;
    return (async function*() {
      const iterator = runtime.local.run(record, () => next()[Symbol.asyncIterator]());
      let done = false;
      try {
        while (true) {
          const part = await runtime.local.run(record, () => iterator.next());
          if (part.done) { done = true; break; }
          if (!record.attemptId) throw Error('ATTEMPT_MISSING_BEFORE_FIRST_CHUNK');
          const chunk = part.value;
          if (chunk.type === 'block-end' && chunk.block?.type === 'tool-call') record.tools.push(callView(chunk.block));
          if (chunk.type === 'finish') record.finishSeen = true;
          yield chunk;
        }
        record.state = record.finishSeen ? 'returned' : 'unknown';
      } catch (error) { record.state = 'unknown'; throw error; }
      finally {
        if (!done && typeof iterator.return === 'function') await runtime.local.run(record, () => iterator.return());
        if (!done) record.state = 'unknown';
      }
    })();
  }
  observe(session, event) {
    if (!['assistant/message', 'assistant/attempt'].includes(event.type)) return;
    const record = this.pending.get(session.id);
    if (!record) { this.denials.push({ sessionId: session.id, seq: event.seq, code: 'ATTEMPT_NATIVE_UNBOUND' }); return; }
    const data = event.data;
    try {
      if (data.turn !== record.turn || data.step !== record.step || record.policy !== this.leases.active.get(session.id)) throw Error('ATTEMPT_NATIVE_POSITION_CONFLICT');
      if (event.type !== 'assistant/message' || data.interrupted === true || record.state !== 'returned') throw Error('ATTEMPT_NATIVE_OUTCOME_UNKNOWN');
      const tools = data.message.content.filter(b => b.type === 'tool-call').map(callView);
      if (JSON.stringify(tools) !== JSON.stringify(record.tools) || !record.attemptId) throw Error('ATTEMPT_NATIVE_TOOL_VECTOR_CONFLICT');
      record.nativeMessageId = data.message.id; record.state = 'committed';
      this.messages.set(data.message.id, Object.freeze({ runId: record.policy.run_id, requestId: record.policy.request_id,
        epoch: record.policy.epoch, attemptId: record.attemptId, dispatchId: record.dispatchId, turn: record.turn, step: record.step, tools: Object.freeze(tools) }));
      this.pending.delete(session.id);
    } catch (error) { record.state = 'unknown'; this.denials.push({ sessionId: session.id, seq: event.seq, code: error.message }); }
  }
  reconcileKnownTerminal(policy, terminal, receipts) {
    const actual = { turn: terminal.turn, seq: terminal.seq, reason: terminal.reason };
    const records = [...this.records.values()].filter(r => r.policy === policy && r.state !== 'committed');
    for (const record of records) {
      const receipt = receipts.find(v => v.attempt_id === record.attemptId && v.dispatch_id === record.dispatchId);
      const expected = terminal.reason.kind === 'aborted' && terminal.reason.reason?.kind === 'user' ? 'closed_cancelled'
        : terminal.reason.kind === 'error' ? 'closed_error' : null;
      if (!receipt || !expected || receipt.state !== expected || record.tools.length || record.turn !== terminal.turn
        || JSON.stringify(canon(receipt.native_terminal)) !== JSON.stringify(canon(actual))) throw Error('ATTEMPT_TERMINAL_RECEIPT_REQUIRED');
    }
    for (const record of records) {
      const receipt = receipts.find(v => v.attempt_id === record.attemptId);
      record.state = receipt.state; record.terminalReceipt = receipt;
      if (this.pending.get(policy.session_id) === record) this.pending.delete(policy.session_id);
    }
  }
  assertSettled(policy) {
    if (this.readonlyRecovery.has(policy.run_id)) return;
    if ([...this.records.values()].some(r => r.policy === policy && !['committed', 'closed_cancelled', 'closed_error'].includes(r.state))) throw Error('ATTEMPT_OUTCOME_UNKNOWN');
  }
  publicState() {
    return { records: [...this.records.values()].map(r => ({ runId: r.policy.run_id, requestId: r.policy.request_id,
      epoch: r.policy.epoch, turn: r.turn, step: r.step, dispatchId: r.dispatchId, attemptId: r.attemptId,
      state: r.state, fetches: r.fetches, nativeMessageId: r.nativeMessageId, tools: r.tools })), denials: this.denials };
  }
}
