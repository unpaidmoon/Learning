// Project-owned per-run native ownership; business binding epochs stay separate.
import { createHash } from 'node:crypto';
import { verifyReadonlyCut } from './native-recovery.mjs';
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const encode = value => JSON.stringify(canonical(value));
const digest = value => createHash('sha256').update(encode(value)).digest('hex');
const marker = policy => `[[learning-generation:${policy.run_id}]]\n`;
const marked = (message, policy) => message?.source?.kind === 'user'
  && message.source.rpcId === policy.request_id
  && message.content?.some(block => block.type === 'text' && block.text === marker(policy));

export class NativeLease {
  constructor(active) {
    this.active = active; this.history = new Map(); this.projections = new Map();
    this.calls = new Map(); this.claims = []; this.denials = []; this.bodyChecks = [];
  }
  accept(policy) {
    const known = this.history.get(policy.run_id);
    if (known) {
      if (encode(known.policy) !== encode(policy)) throw Error('LEASE_RUN_IMMUTABLE');
      return; // Reconciliation replay never reactivates a released run.
    }
    const previous = this.active.get(policy.session_id);
    if (previous) {
      if (this.history.get(previous.run_id)?.state !== 'released') throw Error('LEASE_PREVIOUS_NOT_RELEASED');
      if (policy.epoch <= previous.epoch) throw Error('LEASE_EPOCH_NOT_INCREASING');
      // New authority is frozen for the next run only after known release.
      for (const key of ['course_id', 'purpose', 'task_id'])
        if (encode(policy[key]) !== encode(previous[key])) throw Error('LEASE_QA_SCOPE_IMMUTABLE');
    }
    this.history.set(policy.run_id, { policy, state: 'active', receipt: null });
    this.active.set(policy.session_id, policy);
    if (this.projections.get(policy.session_id)?.recovery) this.projections.delete(policy.session_id);
  }
  assertPolicyActive(policy) {
    if (!policy || this.history.get(policy.run_id)?.state !== 'active'
      || this.active.get(policy.session_id) !== policy) throw Error('LEASE_NOT_ACTIVE');
  }
  assertActive(owner) { this.assertPolicyActive(owner.policy); }
  observe(session, event) {
    let p = this.projections.get(session.id);
    if (!p) { p = { session, turn: null, user: null, assistant: null, calls: [], terminal: null, invalid: false }; this.projections.set(session.id, p); }
    try {
      if (p.session !== session) throw Error('LEASE_SESSION_IDENTITY_CHANGED');
      const data = event.data;
      if (event.type === 'turn/start') {
        p.turn = data.turn; p.user = null; p.assistant = null; p.terminal = null; p.calls = []; p.invalid = false;
      } else if (event.type === 'user/message' && data.source?.kind === 'user') {
        const policy = this.active.get(session.id);
        p.user = marked(data, policy) ? Object.freeze({ policy, rpcId: data.source.rpcId, seq: event.seq }) : null;
      } else if (event.type === 'assistant/message') {
        p.assistant = { turn: data.turn, step: data.step, message: data.message, attempt: this.attempts.messages.get(data.message.id) };
      } else if (event.type === 'tool/call') {
        const policy = p.user?.policy;
        this.assertPolicyActive(policy);
        if (p.invalid || data.turn !== p.turn || p.assistant?.turn !== data.turn || p.assistant?.step !== data.step) throw Error('LEASE_CALL_POSITION');
        const attribution = p.assistant.attempt;
        if (!attribution || attribution.runId !== policy.run_id || attribution.requestId !== policy.request_id || attribution.epoch !== policy.epoch) throw Error('LEASE_EXACT_ATTEMPT_REQUIRED');
        const nativeTools = p.assistant.message.content.filter(b => b.type === 'tool-call');
        const toolOrdinal = nativeTools.findIndex(b => b.id === data.callId);
        const matching = p.assistant.message.content.filter(b => b.type === 'tool-call' && b.id === data.callId);
        const block = matching.length === 1 ? matching[0] : null;
        const args = JSON.parse(data.arguments || '{}');
        if (!block || block.name !== data.name || digest(JSON.parse(block.arguments || '{}')) !== digest(args)) throw Error('LEASE_ASSISTANT_CALL_MISMATCH');
        p.calls.push({ policy, turn: data.turn, step: data.step, callSeq: event.seq, callId: data.callId,
          toolName: data.name, argsSha256: digest(args), attemptId: attribution.attemptId, dispatchId: attribution.dispatchId, toolOrdinal, userSeq: p.user.seq, sourceRpcId: p.user.rpcId,
          assistantMessageId: p.assistant.message.id, session, claimed: false, resultSeq: null, unknown: false });
      } else if (event.type === 'tool/result') {
        const id = data.message.toolCallId;
        const call = p.calls.findLast(c => c.callId === id && c.turn === data.turn && c.step === data.step && c.resultSeq === null);
        if (!call) throw Error('LEASE_RESULT_WITHOUT_CALL');
        call.resultSeq = event.seq; call.unknown = data.error?.code === 'TOOL_OUTCOME_UNKNOWN';
      } else if (event.type === 'turn/end') {
        p.terminal = { turn: data.turn, seq: event.seq, reason: data.reason, runId: p.user?.policy.run_id };
      }
    } catch (error) { p.invalid = true; this.denials.push({ stage: 'observe', code: error.message, eventType: event.type, seq: event.seq }); }
  }
  claim(exec) {
    const p = this.projections.get(exec.agent?.session?.id);
    if (!p || p.invalid || p.session !== exec.agent.session || p.terminal || typeof exec.token !== 'symbol') throw Error('LEASE_CALL_SOURCE_REQUIRED');
    const candidates = p.calls.filter(c => !c.claimed && c.callId === exec.callId && c.toolName === exec.name && c.argsSha256 === digest(exec.arguments));
    if (candidates.length !== 1) throw Error('LEASE_CALL_SOURCE_AMBIGUOUS');
    const call = candidates[0]; this.assertActive(call);
    if (this.calls.has(exec.token)) throw Error('LEASE_CALL_TOKEN_REUSED');
    call.claimed = true;
    const owner = Object.freeze({ ...call, agent: exec.agent, token: exec.token });
    this.calls.set(exec.token, owner);
    this.claims.push({ ...this.publicOwner(owner), tokenOrdinal: this.calls.size, tokenType: typeof exec.token, rootCallId: exec.rootCallId,
      execKeys: Object.keys(exec).sort(), agentId: exec.agent.id, sameAgentSessionId: exec.agent.id === exec.agent.session.id,
      sourcePresent: Object.hasOwn(exec, 'source'), rpcIdPresent: Object.hasOwn(exec, 'rpcId'), controllerPresent: Object.hasOwn(exec, 'controller') });
    return owner;
  }
  owner(exec) {
    const owner = this.calls.get(exec.token);
    if (!owner || owner.agent !== exec.agent || owner.session !== exec.agent.session
      || owner.callId !== exec.callId || owner.toolName !== exec.name || owner.argsSha256 !== digest(exec.arguments)) throw Error('LEASE_FROZEN_OWNER_REQUIRED');
    this.bodyChecks.push({ runId: owner.policy.run_id, callSeq: owner.callSeq, callId: owner.callId,
      guardTokenMatched: true, active: this.history.get(owner.policy.run_id)?.state === 'active' && this.active.get(owner.policy.session_id) === owner.policy });
    return owner;
  }
  forwardBusinessStop(owner, exec, status, code, ctx) {
    // Only the authenticated backend's explicit, owner-bound stop intent. A
    // generic inactive/error response cannot authorize cancelling an actor.
    if (status !== 409 || code !== 'NATIVE_RUN_CANCEL_REQUESTED') return false;
    this.assertActive(owner);
    if (this.owner(exec) !== owner || ctx.agents.get(owner.policy.session_id) !== owner.agent
      || ctx.sessions.get(owner.policy.session_id) !== owner.session
      || typeof owner.agent.cancel !== 'function') throw Error('LEASE_STOP_OWNER_CHANGED');
    // The official session/cancel API uses this synchronous per-actor method.
    // Never await whenIdle from inside the tool that the actor is waiting for.
    owner.agent.cancel({ kind: 'user' }, { keepInbox: true });
    return true;
  }
  async release(value, ctx) {
    if (!value || Object.keys(value).sort().join() !== ['epoch', 'request_id', 'run_id', 'session_id'].join()) throw Error('LEASE_RELEASE_SHAPE');
    const policy = this.active.get(value.session_id);
    if (!policy || ['run_id', 'request_id', 'epoch'].some(key => value[key] !== policy[key])) throw Error('LEASE_RELEASE_OWNER');
    const lease = this.history.get(policy.run_id);
    if (lease.state === 'released') return lease.receipt;
    const p = this.projections.get(policy.session_id);
    if (!p || p.invalid || p.terminal?.runId !== policy.run_id || !['completed', 'max-tokens', 'aborted', 'error'].includes(p.terminal?.reason.kind)
      || p.calls.some(c => c.policy !== policy || c.resultSeq === null || c.unknown)) throw Error('LEASE_TERMINAL_NOT_KNOWN');
    const terminal = p.terminal;
    const evidence = await this.assertBusinessRelease({ ...Object.fromEntries(['session_id', 'run_id', 'request_id', 'epoch'].map(k => [k, policy[k]])),
      ...(p.recovery ? { native_terminal: { turn: terminal.turn, seq: terminal.seq, reason: terminal.reason } } : {}) });
    if (p.recovery && digest(evidence) !== p.recovery.evidenceSha256) throw Error('RECOVERY_JOURNAL_CHANGED');
    if (!['completed', 'max-tokens'].includes(terminal.reason.kind)) this.attempts.reconcileKnownTerminal(policy, terminal, evidence.terminal_attempts || []);
    this.attempts.assertSettled(policy);
    if (p.recovery) await verifyReadonlyCut(ctx, policy, p.recovery);
    else if (!await ctx.sessions.flush(p.session)) throw Error('LEASE_DURABILITY_REQUIRED');
    if (p.terminal !== terminal || this.active.get(policy.session_id) !== policy) throw Error('LEASE_RELEASE_RACE');
    const receipt = Object.freeze({ released: true, session_id: policy.session_id, run_id: policy.run_id,
      request_id: policy.request_id, epoch: policy.epoch, native_turn: terminal.turn, terminal_seq: terminal.seq, native_terminal_kind: terminal.reason.kind, durable: true,
      ...(p.recovery ? { recovered_from_persisted_cut: true, native_cursor: p.recovery.cursor, native_cut_sha256: p.recovery.sha256 } : {}) });
    lease.state = 'released'; lease.receipt = receipt; return receipt;
  }
  publicOwner(owner) {
    return { sessionId: owner.policy.session_id, runId: owner.policy.run_id, requestId: owner.sourceRpcId,
      epoch: owner.policy.epoch, turn: owner.turn, step: owner.step, callSeq: owner.callSeq, callId: owner.callId,
      toolName: owner.toolName, argsSha256: owner.argsSha256, attemptId: owner.attemptId, dispatchId: owner.dispatchId, toolOrdinal: owner.toolOrdinal, userSeq: owner.userSeq, assistantMessageId: owner.assistantMessageId };
  }
  publicState() {
    return { history: [...this.history.values()].map(v => ({ runId: v.policy.run_id, state: v.state, receipt: v.receipt })),
      claims: this.claims, bodyChecks: this.bodyChecks, denials: this.denials };
  }
}
