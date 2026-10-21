import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const here = path.dirname(fileURLToPath(import.meta.url));
const plugin = path.resolve(here, '../plugins/learning-guard.mjs');
const listen = async server => {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
};

test('private control requires token/Host and rejects browser Origin; all tools and outbound routes fail closed', { timeout: 15000 }, async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'learning-guard-'));
  const boundary = http.createServer((_req, res) => res.end('bounded'));
  const boundaryPort = await listen(boundary);
  const portProbe = http.createServer();
  const controlPort = await listen(portProbe);
  await new Promise(resolve => portProbe.close(resolve));
  const token = 'private-test-control-token';
  const child = spawn(process.execPath, [path.join(here, 'guard-child.mjs'), plugin], {
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    env: {
      SystemRoot: process.env.SystemRoot || '',
      LEARNING_HARNESS_BOUNDARY_URL: `http://127.0.0.1:${boundaryPort}`,
      LEARNING_HARNESS_CONTROL_PORT: String(controlPort),
      LEARNING_HARNESS_CONTROL_TOKEN: token,
      LEARNING_HARNESS_AUDIT_PATH: path.join(directory, 'audit.jsonl'),
    },
  });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  t.after(async() => {
    child.kill();
    if (child.exitCode === null) await once(child, 'exit');
    await new Promise(resolve => boundary.close(resolve));
    await fs.rm(directory, { recursive: true, force: true });
  });
  assert.deepEqual((await once(child, 'message'))[0], { ready: true }, stderr);
  const exercised = once(child, 'message');
  child.send('exercise');
  assert.deepEqual((await exercised)[0], { exercised: true }, stderr);
  const url = `http://127.0.0.1:${controlPort}/state`;
  const headers = { 'X-Learning-Harness-Control': token };
  assert.equal((await fetch(url)).status, 403);
  assert.equal((await fetch(url, { headers: { ...headers, Origin: 'https://other.invalid' } })).status, 403);
  // Node fetch replaces Host with its URL authority; use the raw HTTP carrier
  // to exercise the actual wrong-Host boundary.
  const wrongHostStatus = await new Promise((resolve, reject) => {
    http.get(url, { headers: { ...headers, Host: 'other.invalid' } }, res => {
      res.resume();
      resolve(res.statusCode);
    }).on('error', reject);
  });
  assert.equal(wrongHostStatus, 403);
  const response = await fetch(url, { headers });
  assert.equal(response.status, 200);
  const state = await response.json();
  assert.deepEqual(state.tools, []);
  assert.equal(state.toolDenials, 1);
  assert.equal(state.networkBlocked, 3);
  assert.ok(state.networkAllowed >= 1);
  assert.equal(JSON.stringify(state).includes(token), false);
  const audit = await fs.readFile(path.join(directory, 'audit.jsonl'), 'utf-8');
  assert.equal(audit.includes(token), false);
  assert.ok(audit.includes('network-guard-installed'));
});

test('missing required policy configuration fails before activation', async() => {
  const child = spawn(process.execPath, [plugin], { windowsHide: true, env: { SystemRoot: process.env.SystemRoot || '' } });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const [code] = await once(child, 'exit');
  assert.notEqual(code, 0);
  assert.match(stderr, /LEARNING_HARNESS_BOUNDARY_URL_REQUIRED/);
});
