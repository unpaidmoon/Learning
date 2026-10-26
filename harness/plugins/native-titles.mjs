// A separate, single-use title capability. Main model requests remain untouched.
import { AsyncLocalStorage } from 'node:async_hooks';

export class NativeTitles {
  constructor(endpoint, audit) {
    this.endpoint = endpoint; this.audit = audit;
    this.scopes = new Map(); this.routed = new WeakMap(); this.local = new AsyncLocalStorage();
  }
  register(scope) {
    if (!scope || !/^title_[a-f0-9]{32}$/.test(scope.request_id || '')
      || !/^learning-[a-f0-9]{32}$/.test(scope.session_id || '')
      || typeof scope.input !== 'string' || Buffer.byteLength(scope.input) > 70000
      || typeof scope.token !== 'string' || scope.token.length < 32
      || typeof scope.model !== 'string' || !['off','low','high','max'].includes(scope.thinking)
      || scope.max_tokens !== 4096 || this.scopes.has(scope.session_id)) throw Error('TITLE_SCOPE_INVALID');
    this.scopes.set(scope.session_id, { ...scope, claimed: false, fetches: 0 });
  }
  stream(ctx, options, next) {
    const accepted = this.routed.get(options);
    if (accepted) return this.wrap(next, accepted);
    const scope = this.scopes.get(options.sessionId);
    if (!scope || scope.claimed || options.model !== scope.model || options.tools?.length) throw Error('TITLE_SCOPE_REQUIRED');
    scope.claimed = true;
    const message = options.messages?.find(m => m.role === 'user');
    if (!message) throw Error('TITLE_HUMAN_MESSAGE_REQUIRED');
    const routed = { ...options, maxTokens: scope.max_tokens, reasoningEffort: scope.thinking,
      messages: [{ ...message, content: [{ type: 'text', text: scope.input }] }] };
    this.routed.set(routed, scope);
    this.audit('session-title-dispatch', { requestId: scope.request_id, sessionId: scope.session_id,
      model: scope.model, thinking: scope.thinking, maxTokens: scope.max_tokens });
    return ctx.llm.stream(routed);
  }
  wrap(next, scope) {
    const runtime = this;
    return (async function*() {
      const iterator = runtime.local.run(scope, () => next()[Symbol.asyncIterator]());
      try {
        while (true) {
          const item = await runtime.local.run(scope, () => iterator.next());
          if (item.done) break;
          yield item.value;
        }
      } finally { if(iterator.return) await runtime.local.run(scope, () => iterator.return()); }
    })();
  }
  decorate(input, init) {
    const scope = this.local.getStore();
    if (!scope) return null;
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.origin !== this.endpoint.origin || url.pathname !== '/v1/messages'
      || scope.fetches !== 0) throw Error('TITLE_SINGLE_DISPATCH_REQUIRED');
    scope.fetches++;
    const headers = new Headers(init?.headers);
    headers.set('x-learning-title-id', scope.request_id);
    headers.set('x-learning-title-token', scope.token);
    return { input, init: { ...init, headers } };
  }
}
