// Release-only recovery. Never restores a callable token, retries a tool, or
// publishes/resumes an Agent. Physical persistence reads exclude read-side closers.
import { createHash } from 'node:crypto';
const canon = v => Array.isArray(v) ? v.map(canon) : v && typeof v === 'object'
  ? Object.fromEntries(Object.keys(v).sort().map(k => [k, canon(v[k])])) : v;
const encoded = v => JSON.stringify(canon(v));
const digest = v => createHash('sha256').update(encoded(v)).digest('hex');

export async function readRawNativeCut(ctx, policy) {
  const agent = ctx.agents.get(policy.session_id), session = ctx.sessions.get(policy.session_id);
  if (agent && agent.status !== 'idle') throw Error('RECOVERY_AGENT_NOT_IDLE');
  if (session && (!agent || agent.session !== session)) throw Error('RECOVERY_SESSION_OWNER_UNKNOWN');
  if (session && !await ctx.sessions.flush(session)) throw Error('RECOVERY_DURABILITY_REQUIRED');
  if (typeof ctx.sessionPersistence?.open !== 'function') throw Error('RECOVERY_READ_HANDLE_UNAVAILABLE');
  const handle = await ctx.sessionPersistence.open(policy.session_id, 'read');
  let header, events;
  try { header = handle.header; events = (await handle.read(0, undefined)).events; }
  finally { await handle.close(); }
  if (ctx.agents.get(policy.session_id) !== agent || ctx.sessions.get(policy.session_id) !== session || agent && agent.status !== 'idle') throw Error('RECOVERY_SESSION_CHANGED');
  if (header.id !== policy.session_id || !Array.isArray(events)) throw Error('RECOVERY_SESSION_IDENTITY');
  return { header, events, cursor: events.at(-1)?.seq ?? -1, sha256: digest({ header, events }) };
}

function foldTerminal(policy, cut) {
  const inbox = { 'next-turn': [], 'next-step': [] };
  let turn = null, user = null, terminal = null, open = false, calls = [], expectedSeq = 0;
  for (const event of cut.events) {
    if (event.seq !== expectedSeq++) throw Error('RECOVERY_EVENT_SEQUENCE');
    const d = event.data;
    if (event.type === 'agent/inbox/spliced') {
      const values = inbox[d.target], removedCount = d.removedCount ?? 0;
      if (!values || !Number.isSafeInteger(d.start) || !Number.isSafeInteger(removedCount) || removedCount < 0 || d.start < 0 || d.start + removedCount > values.length || !Array.isArray(d.inserted)) throw Error('RECOVERY_INBOX_SHAPE');
      values.splice(d.start, removedCount, ...d.inserted);
    } else if (event.type === 'turn/start') {
      if (open) throw Error('RECOVERY_OVERLAPPING_TURN');
      turn = d.turn; open = true; user = null; terminal = null; calls = [];
    } else if (event.type === 'user/message' && d.source?.kind === 'user') {
      user = d.source.rpcId === policy.request_id && d.content?.some(b => b.type === 'text' && b.text === `[[learning-generation:${policy.run_id}]]\n`)
        ? { seq: event.seq, rpcId: d.source.rpcId } : null;
    } else if (event.type === 'tool/call') {
      if (!open || d.turn !== turn) throw Error('RECOVERY_TOOL_OWNER');
      if (!user) continue; // Historical turn only. This creates no executable owner.
      calls.push({ callSeq: event.seq, callId: d.callId, name: d.name, turn: d.turn, step: d.step,
        argsSha256: digest(JSON.parse(d.arguments || '{}')), resultSeq: null, errorCode: null });
    } else if (event.type === 'tool/result') {
      if (!user) continue; // Prior runs are checked by their persisted released scopes.
      const call = calls.findLast(c => c.turn === d.turn && c.step === d.step && c.callId === d.message.toolCallId && c.resultSeq === null);
      if (!call) throw Error('RECOVERY_RESULT_WITHOUT_CALL');
      call.resultSeq = event.seq; call.errorCode = d.error?.code || null;
    } else if (event.type === 'turn/end') {
      if (!open || d.turn !== turn) throw Error('RECOVERY_TERMINAL_POSITION');
      open = false; terminal = { turn, seq: event.seq, reason: d.reason, runId: user ? policy.run_id : null };
    }
  }
  if (open) throw Error('RECOVERY_RAW_TURN_OPEN');
  if (!user || !terminal || terminal.runId !== policy.run_id) throw Error('RECOVERY_ORIGINAL_REQUEST_REQUIRED');
  if (!['completed', 'max-tokens', 'error'].includes(terminal.reason.kind)
    && !(terminal.reason.kind === 'aborted' && terminal.reason.reason?.kind === 'user')) throw Error('RECOVERY_TERMINAL_UNKNOWN');
  if (inbox['next-turn'].length || inbox['next-step'].length) throw Error('RECOVERY_PENDING_INPUT');
  if (calls.some(c => c.resultSeq === null || c.errorCode === 'TOOL_OUTCOME_UNKNOWN')) throw Error('RECOVERY_TOOL_OUTCOME_UNKNOWN');
  return { terminal, calls };
}

export async function recoverReadonlyLease(ctx, leases, attempts, policy) {
  leases.assertPolicyActive(policy);
  const cut = await readRawNativeCut(ctx, policy), folded = foldTerminal(policy, cut);
  const nativeTerminal = { turn: folded.terminal.turn, seq: folded.terminal.seq, reason: folded.terminal.reason };
  const evidence = await leases.assertBusinessRelease({ ...Object.fromEntries(['session_id','run_id','request_id','epoch'].map(k => [k, policy[k]])), native_terminal: nativeTerminal });
  if (evidence.tool_stop) verifyToolStopCut(evidence.tool_stop, cut, nativeTerminal);
  if (!Array.isArray(evidence.attempts) || evidence.attempts.length === 0 || !Array.isArray(evidence.native_calls)) throw Error('RECOVERY_JOURNAL_REQUIRED');
  if (evidence.native_calls.length !== folded.calls.filter(c => c.name !== 'skill').length) throw Error('RECOVERY_CALL_JOURNAL_COUNT');
  for (const call of folded.calls) {
    if (call.name === 'skill') continue; // own static native loader only, no business IO
    const entry = evidence.native_calls.find(c => c.native_call_seq === call.callSeq);
    if (!entry || entry.session_id !== policy.session_id || entry.run_id !== policy.run_id || entry.request_id !== policy.request_id || entry.epoch !== policy.epoch
      || entry.native_turn !== call.turn || entry.native_step !== call.step || entry.call_id !== call.callId || entry.tool_name !== call.name || entry.args_sha256 !== call.argsSha256) throw Error('RECOVERY_CALL_JOURNAL_MISMATCH');
    const attempt = evidence.attempts.find(a => a.attempt_id === entry.attempt_id), provider = attempt?.calls?.[entry.tool_ordinal];
    if (!attempt || attempt.dispatch_id !== entry.dispatch_id || !provider || provider.call_id !== call.callId
      || provider.name !== call.name || provider.args_sha256 !== call.argsSha256) throw Error('RECOVERY_PROVIDER_CALL_MISMATCH');
  }
  for (const a of evidence.attempts) {
    if (a.run_id !== policy.run_id || a.session_id !== policy.session_id || a.request_id !== policy.request_id || a.epoch !== policy.epoch
      || !['completed','closed_cancelled','closed_error'].includes(a.state) || a.network?.downstream_closed !== true || a.network?.upstream_closed !== true || a.network?.watcher_closed !== true) throw Error('RECOVERY_ATTEMPT_UNKNOWN');
    if (a.state !== 'completed') {
      const actual = { turn: folded.terminal.turn, seq: folded.terminal.seq, reason: folded.terminal.reason };
      if (encoded(a.reconciliation?.native_terminal) !== encoded(actual)) throw Error('RECOVERY_TERMINAL_RECEIPT_MISMATCH');
    }
  }
  leases.assertPolicyActive(policy);
  const recovery = Object.freeze({ cursor: cut.cursor, sha256: cut.sha256, terminal: folded.terminal,
    evidenceSha256: digest(evidence), attemptCount: evidence.attempts.length, callCount: folded.calls.length });
  // A release projection, explicitly not a Session or an executable dispatch.
  leases.projections.set(policy.session_id, { session: ctx.sessions.get(policy.session_id) || null, invalid: false, terminal: folded.terminal,
    calls: [], recovery, ownerPolicy: policy });
  attempts.readonlyRecovery.set(policy.run_id, recovery);
  return { recovered: true, source: 'physical_read_handle', native_cursor: cut.cursor, native_cut_sha256: cut.sha256,
    native_terminal: nativeTerminal, attempts: recovery.attemptCount, calls: recovery.callCount,
    live_agent: Boolean(ctx.agents.get(policy.session_id)), live_session: Boolean(ctx.sessions.get(policy.session_id)) };
}

export async function verifyReadonlyCut(ctx, policy, recovery) {
  const cut = await readRawNativeCut(ctx, policy);
  if (cut.cursor !== recovery.cursor || cut.sha256 !== recovery.sha256) throw Error('RECOVERY_NATIVE_CUT_CHANGED');
}

export async function proveToolStop(ctx, leases, attempts, policy) {
  leases.assertPolicyActive(policy);
  if (!ctx.agents.get(policy.session_id)) throw Error('TOOL_STOP_LIVE_AGENT_REQUIRED');
  attempts.assertSettled(policy);
  const cut = await readRawNativeCut(ctx, policy), folded = foldTerminal(policy, cut);
  const t = folded.terminal, call = folded.calls.at(-1);
  if (t.reason.kind !== 'aborted' || t.reason.reason?.kind !== 'user' || !call || call.name === 'skill'
    || call.resultSeq === null || cut.events.some(e => e.seq > call.callSeq
      && ['assistant/message', 'assistant/attempt', 'turn/start'].includes(e.type))) throw Error('TOOL_STOP_PHASE_UNKNOWN');
  return { session_id: policy.session_id, run_id: policy.run_id, request_id: policy.request_id, epoch: policy.epoch,
    native_terminal: { turn: t.turn, seq: t.seq, reason: t.reason },
    native_cursor: cut.cursor, native_cut_sha256: cut.sha256, native_events_sha256: digest(cut.events), native_header_sha256: digest(cut.header),
    call: { native_turn: call.turn, native_step: call.step, native_call_seq: call.callSeq,
      call_id: call.callId, tool_name: call.name, args_sha256: call.argsSha256 }, result_seq: call.resultSeq };
}

export function verifyToolStopCut(proof, cut, terminal) {
  const prefix = cut.events.filter(e => e.seq <= proof.native_cursor);
  const suffix = cut.events.filter(e => e.seq > proof.native_cursor);
  // The fixed runtime appends an empty seed boundary when loading an existing
  // session. It cannot carry an inbox, call, result, inherited fork or message.
  if (proof.native_events_sha256 !== digest(prefix) || prefix.at(-1)?.seq !== proof.native_cursor
    || encoded(proof.native_terminal) !== encoded(terminal)
    || suffix.some(e => e.type !== 'session/end-seed' || !e.data || typeof e.data !== 'object'
      || Array.isArray(e.data) || Object.keys(e.data).length !== 0)) throw Error('RECOVERY_TOOL_STOP_CUT_CHANGED');
}
