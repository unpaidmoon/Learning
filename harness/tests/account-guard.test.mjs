import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import test from 'node:test';

const plugin = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../plugins/learning-guard.mjs');

test('account guard permits only the exact official HTTPS origin and its TLS host, without network execution', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'learning-account-guard-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const script = `
    import assert from 'node:assert/strict';
    import net from 'node:net';
    let fetches = 0, connects = 0;
    globalThis.fetch = async () => { fetches++; return new Response('synthetic'); };
    net.Socket.prototype.connect = function() { connects++; return this; };
    await import(${JSON.stringify(pathToFileURL(plugin).href)});
    await fetch('https://platform.deepseek.com/auth-api/v0/dsh/authorize');
    await fetch('http://127.0.0.1:19001/v1/messages');
    for (const url of [
      'http://platform.deepseek.com/auth-api/v0/dsh/authorize',
      'https://platform.deepseek.com.evil.invalid/',
      'https://platform.deepseek.com:444/',
      'https://user:secret@platform.deepseek.com/',
      'https://api.deepseek.com/anthropic/v1/messages'
    ]) await assert.rejects(fetch(url), /LEARNING_HARNESS_NETWORK_DENIED/);
    new net.Socket().connect({host:'platform.deepseek.com',port:443});
    new net.Socket().connect({host:'127.0.0.1',port:19001});
    for (const options of [{host:'api.deepseek.com',port:443},{host:'platform.deepseek.com',port:444},{path:'private-pipe'}]) {
      assert.throws(() => new net.Socket().connect(options), /LEARNING_HARNESS_NETWORK_DENIED/);
    }
    assert.equal(fetches, 2);
    assert.equal(connects, 2);
  `;
  const child = spawn(process.execPath, ['--input-type=module', '--eval', script], {
    windowsHide: true,
    env: {
      SystemRoot: process.env.SystemRoot || '',
      LEARNING_HARNESS_BOUNDARY_URL: 'http://127.0.0.1:19001/v1',
      LEARNING_HARNESS_CONTROL_PORT: '19002',
      LEARNING_HARNESS_CONTROL_TOKEN: 'synthetic-control-token',
      LEARNING_HARNESS_AUDIT_PATH: path.join(directory, 'audit.jsonl'),
      LEARNING_HARNESS_ACCOUNT_ENABLED: '1',
    },
  });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const [code] = await once(child, 'exit');
  assert.equal(code, 0, stderr);
  const audit = await fs.readFile(path.join(directory, 'audit.jsonl'), 'utf8');
  assert.equal(audit.includes('synthetic-control-token'), false);
  assert.equal(audit.includes('user:secret'), false);
});
