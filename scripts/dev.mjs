import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { createServer, loadEnv } from 'vite';

const root = fileURLToPath(new URL('../', import.meta.url));

async function healthy(origin) {
  try {
    const response = await fetch(`${origin}/api/health`, { signal: AbortSignal.timeout(1000) });
    return response.ok && (await response.json()).ok === true;
  } catch {
    return false;
  }
}

export async function startBackend({ origin, command, args, cwd = root, env = process.env, startupTimeout = 30000 }) {
  if (await healthy(origin)) {
    console.log(`Using running backend at ${origin}`);
    return null;
  }
  const child = spawn(command, args, { cwd, env, stdio: 'inherit' });
  let failure;
  child.once('error', error => { failure = error; });
  child.once('exit', (code, signal) => {
    failure = new Error(`Backend exited (${signal || code}). Check its output above.`);
  });
  const deadline = Date.now() + startupTimeout;
  try {
    while (Date.now() < deadline) {
      if (failure) throw failure;
      if (await healthy(origin)) {
        if (failure) throw failure;
        return child;
      }
      await delay(100);
    }
    throw new Error(`Backend did not become ready at ${origin} within ${startupTimeout / 1000}s.`);
  } catch (error) {
    child.kill();
    throw error;
  }
}

async function main() {
  const env = { ...loadEnv('development', root, ''), ...process.env };
  const host = env.HOST === '::1' ? '[::1]' : '127.0.0.1';
  const origin = `http://${host}:${env.PORT || '8787'}`;
  const virtualPython = resolve(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const command = existsSync(virtualPython) ? virtualPython : 'python';
  let backend;
  let server;
  let stopping = false;
  const stop = async (code = 0) => {
    if (stopping) return;
    stopping = true;
    backend?.kill();
    await server?.close();
    process.exitCode = code;
  };
  process.once('SIGINT', () => { void stop(); });
  process.once('SIGTERM', () => { void stop(); });
  try {
    backend = await startBackend({ origin, command, args: ['-m', 'backend.app'], env });
    if (stopping) {
      backend?.kill();
      return;
    }
    backend?.once('exit', () => {
      if (!stopping) {
        console.error('Backend stopped. Stopping Vite; run npm run dev to restart both services.');
        void stop(1);
      }
    });
    server = await createServer({ root, server: { proxy: { '/api': { target: origin } } } });
    if (stopping) {
      await server.close();
      return;
    }
    await server.listen();
    server.printUrls();
  } catch (error) {
    console.error(`Development startup failed: ${error.message}`);
    await stop(1);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}