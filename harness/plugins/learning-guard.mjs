// Project-owned policy for the pinned Harness host. No package dependencies.
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { syncBuiltinESMExports } from 'node:module';
import { createHash, timingSafeEqual } from 'node:crypto';

function required(name) {
  const value = process.env[`LEARNING_HARNESS_${name}`];
  if (!value) throw Error(`LEARNING_HARNESS_${name}_REQUIRED`);
  return value;
}
const endpoint = new URL(required('BOUNDARY_URL'));
const accountEnabled = process.env.LEARNING_HARNESS_ACCOUNT_ENABLED === '1';
const accountOrigin = 'https://platform.deepseek.com';
if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1'
  || !endpoint.port || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
  throw Error('LEARNING_HARNESS_BOUNDARY_INVALID');
}
const auditPath = required('AUDIT_PATH');
const controlToken = required('CONTROL_TOKEN');
const controlPort = Number(required('CONTROL_PORT'));
if (!Number.isInteger(controlPort) || controlPort < 1 || controlPort > 65535) {
  throw Error('LEARNING_HARNESS_CONTROL_PORT_INVALID');
}
const sha256 = value => createHash('sha256').update(value).digest('hex');
const state = { networkBlocked: 0, networkAllowed: 0, toolDenials: 0, agents: [], dispatches: 0 };
function audit(type, fields = {}) {
  // Failure to persist the audit is a host failure, never a silent success.
  fs.appendFileSync(auditPath, JSON.stringify({ time: Date.now(), type, ...fields }) + '\n');
}
function check(url, carrier) {
  if (accountEnabled && url.origin === accountOrigin && !url.username && !url.password) {
    state.networkAllowed += 1;
    audit('account-network-allowed', { carrier, path: url.pathname });
    return;
  }
  if (url.protocol !== 'http:' || url.hostname !== endpoint.hostname
    || url.port !== endpoint.port || url.username || url.password) {
    state.networkBlocked += 1;
    audit('network-blocked', { carrier, protocol: url.protocol, host: url.hostname, port: url.port });
    throw Error('LEARNING_HARNESS_NETWORK_DENIED');
  }
  state.networkAllowed += 1;
  audit('network-allowed', { carrier, path: url.pathname });
}

const originalFetch = globalThis.fetch;
globalThis.fetch = function(input, init) {
  try {
    check(new URL(typeof input === 'string' || input instanceof URL ? input : input.url), 'fetch');
  } catch (error) {
    return Promise.reject(error);
  }
  return originalFetch.call(this, input, init);
};
for (const [carrier, mod] of [['http', http], ['https', https]]) {
  for (const method of ['request', 'get']) {
    const original = mod[method];
    mod[method] = function(input, options, callback) {
      const base = typeof input === 'string' || input instanceof URL ? new URL(input) : undefined;
      const overrides = base ? (typeof options === 'object' && options !== null ? options : {}) : input;
      const protocol = overrides.protocol || base?.protocol || `${carrier}:`;
      const host = overrides.hostname || overrides.host || base?.hostname || 'localhost';
      const port = overrides.port || base?.port || (protocol === 'https:' ? '443' : '80');
      // Inspect effective options: request(URL, { hostname }) can override URL.
      check(new URL(`${protocol}//${host}:${port}${overrides.path || base?.pathname || '/'}`), `${carrier}.${method}`);
      return original.apply(this, arguments);
    };
  }
}
const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  const head = Array.isArray(args[0]) ? args[0][0] : args[0];
  const options = typeof head === 'object' && head !== null
    ? head : { port: head, host: typeof args[1] === 'string' ? args[1] : 'localhost' };
  if (accountEnabled && !options.path && String(options.port) === '443'
    && (options.host || options.hostname) === 'platform.deepseek.com') {
    return originalConnect.apply(this, args);
  }
  if (options.path || String(options.port) !== endpoint.port
    || (options.host || options.hostname || 'localhost') !== '127.0.0.1') {
    state.networkBlocked += 1;
    audit('network-blocked', { carrier: 'net.connect', host: options.host || options.hostname || null, port: options.port || null });
    throw Error('LEARNING_HARNESS_NETWORK_DENIED');
  }
  return originalConnect.apply(this, args);
};
syncBuiltinESMExports();
audit('network-guard-installed', { boundaryOrigin: endpoint.origin });

// 0.2.1rc2 SEA extractor recognizes C:/snapshot and V:/snapshot. Its VFS is
// drive-independent. Normalize only Windows virtual native-addon paths, and
// delegate extraction/loading to the original shipped bootstrap.
const originalDlopen = process.dlopen;
process.dlopen = function(module, filename, ...rest) {
  let target = String(filename).replace(/^\\\\\?\\/, '');
  if (process.platform === 'win32' && /^[A-Za-z]:[\\/]snapshot[\\/].*\.node$/i.test(target)) {
    target = `C:${target.slice(2)}`;
    audit('native-snapshot-drive-normalized', { module: target.replace(/^.*[\\/]/, '') });
  } else target = filename;
  return originalDlopen.call(this, module, target, ...rest);
};

export const name = 'learning-guard';
export const inject = ['tools', 'agents', 'systemPrompt', 'llm'];
export function apply(ctx) {
  // P0 has no implemented course-reading tool. No fixture capabilities enter
  // production. Any future or scoped registration remains denied at execution.
  ctx.tools.guard(exec => {
    state.toolDenials += 1;
    audit('tool-denied', { name: exec.name, code: 'LEARNING_HARNESS_TOOL_NOT_AVAILABLE' });
    return 'LEARNING_HARNESS_TOOL_NOT_AVAILABLE';
  });
  ctx.on('agent/created', ({ agent }) => {
    const tools = ctx.tools.schemas(agent).map(tool => tool.name);
    if (tools.length) throw Error('LEARNING_HARNESS_UNEXPECTED_TOOLS');
    state.agents.push(agent.id);
    audit('agent-created', { agentId: agent.id, tools });
  });
  ctx.on('llm/stream', function(options, next) {
    if ((options.tools || []).length) throw Error('LEARNING_HARNESS_UNEXPECTED_TOOLS');
    state.dispatches += 1;
    audit('llm-dispatch', {
      sessionId: options.sessionId || null, provider: options.provider, model: options.model,
      reasoningEffort: options.reasoningEffort, maxTokens: options.maxTokens,
      tools: [], systemSha256: sha256(options.system || ''),
    });
    return next();
  });
  ctx.on('system-prompt/assemble', async(_assembly, _context, next) => {
    const assembled = await next();
    if (assembled.tools.length) throw Error('LEARNING_HARNESS_UNEXPECTED_TOOLS');
    audit('system-assembly', { sections: assembled.sections.map(section => ({ name: section.name, sha256: sha256(section.text) })), tools: [] });
    return assembled;
  });
  function authenticated(req) {
    const supplied = req.headers['x-learning-harness-control'];
    if (req.headers.origin || req.headers.host !== `127.0.0.1:${controlPort}`
      || typeof supplied !== 'string') return false;
    const left = Buffer.from(supplied);
    const right = Buffer.from(controlToken);
    return left.length === right.length && timingSafeEqual(left, right);
  }
  const control = http.createServer(async(req, res) => {
    res.setHeader('content-type', 'application/json');
    res.setHeader('cache-control', 'no-store');
    if (!authenticated(req)) {
      res.writeHead(403).end('{"error":"LEARNING_HARNESS_CONTROL_AUTH"}');
      return;
    }
    if (req.method !== 'GET' || req.url !== '/state') {
      res.writeHead(404).end('{"error":"LEARNING_HARNESS_CONTROL_NOT_FOUND"}');
      return;
    }
    try {
      const assembled = await ctx.systemPrompt.assemble();
      const loader = ctx.get('loader');
      const activation = loader?.entries ? [...loader.entries()].map(entry => ({
        id: entry.options?.id, name: entry.options?.name,
        disabled: entry.options?.disabled === true, state: entry.fiber?.state ?? null,
      })) : null;
      res.end(JSON.stringify({ ...state, networkGuardInstalled: true,
        tools: ctx.tools.schemas().map(tool => tool.name),
        systemSections: assembled.sections, activation }));
    } catch {
      res.writeHead(500).end('{"error":"LEARNING_HARNESS_CONTROL_STATE_FAILED"}');
    }
  });
  control.on('upgrade', (_req, socket) => socket.destroy());
  control.on('error', error => {
    audit('control-error', { code: error.code || 'UNKNOWN' });
    // No authenticated evidence endpoint means this composition is unusable.
    process.exitCode = 1;
    ctx.root.dispose();
  });
  control.listen(controlPort, '127.0.0.1', () => audit('control-ready', { port: controlPort }));
  ctx.on('dispose', () => { control.close(); });
  audit('plugin-activated', { tools: [], mode: 'p0-fail-closed' });
}
