import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { startBackend } from './dev.mjs';

async function unusedPort() {
  const server = createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

test('starts a missing backend and waits until its health endpoint is ready', async () => {
  const port = await unusedPort();
  const origin = `http://127.0.0.1:${port}`;
  const backend = await startBackend({
    origin,
    command: process.execPath,
    args: ['--input-type=module', '-e', `
      import { createServer } from 'node:http';
      setTimeout(() => createServer((request, response) => {
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({ ok: request.url === '/api/health' }));
      }).listen(${port}, '127.0.0.1'), 150);
    `],
  });
  try {
    assert.ok(backend);
    assert.equal((await (await fetch(`${origin}/api/health`)).json()).ok, true);
    assert.equal(await startBackend({ origin, command: 'must-not-be-started', args: [] }), null);
  } finally {
    const exited = once(backend, 'exit');
    backend.kill();
    await exited;
  }
});

test('reports backend startup failure instead of serving a broken dashboard', async () => {
  const port = await unusedPort();
  await assert.rejects(startBackend({
    origin: `http://127.0.0.1:${port}`,
    command: process.execPath,
    args: ['-e', 'process.exit(7)'],
  }), /Backend exited \(7\)/);
});

test('reports a missing Python executable', async () => {
  const port = await unusedPort();
  await assert.rejects(startBackend({
    origin: `http://127.0.0.1:${port}`,
    command: 'house-monitor-nonexistent-python',
    args: [],
  }), /ENOENT/);
});

test('stops a backend that never becomes healthy', async () => {
  const port = await unusedPort();
  await assert.rejects(startBackend({
    origin: `http://127.0.0.1:${port}`,
    command: process.execPath,
    args: ['-e', 'setInterval(() => {}, 1000)'],
    startupTimeout: 250,
  }), /did not become ready/);
});