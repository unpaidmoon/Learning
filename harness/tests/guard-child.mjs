// Runs in a disposable process because the host policy wraps process-wide IO.
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { pathToFileURL } from 'node:url';

const events = new Map();
let guard;
const ctx = {
  tools: { guard: handler => { guard = handler; }, schemas: () => [] },
  systemPrompt: { assemble: async() => ({ sections: [{ name: 'deployment:persona-prefix', text: '学习助手' }], tools: [] }) },
  get: () => ({ entries: () => [] }),
  on: (name, handler) => { events.set(name, handler); },
  root: { dispose: () => { events.get('dispose')?.(); } },
};
const plugin = await import(pathToFileURL(process.argv[2]).href);
plugin.apply(ctx);
process.on('message', async message => {
  if (message !== 'exercise') return;
  try {
    assert.equal(guard({ name: 'get_video_frame' }), 'LEARNING_HARNESS_TOOL_NOT_AVAILABLE');
    assert.throws(() => events.get('llm/stream')({ tools: [{ name: 'coding_bypass' }] }, () => {}), /UNEXPECTED_TOOLS/);
    await assert.rejects(fetch('https://example.invalid/'), /NETWORK_DENIED/);
    assert.throws(() => http.get(process.env.LEARNING_HARNESS_BOUNDARY_URL, { hostname: 'example.invalid' }), /NETWORK_DENIED/);
    assert.throws(() => net.connect({ host: '127.0.0.1', port: Number(process.env.LEARNING_HARNESS_CONTROL_PORT) }), /NETWORK_DENIED/);
    const response = await fetch(process.env.LEARNING_HARNESS_BOUNDARY_URL);
    assert.equal(await response.text(), 'bounded');
    process.send({ exercised: true });
  } catch (error) {
    process.send({ error: error.message });
  }
});
process.send({ ready: true });
