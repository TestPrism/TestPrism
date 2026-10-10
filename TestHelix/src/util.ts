import { createHash, randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import type { Execution } from './types.js';
import pLimit from 'p-limit';

export const hash = (data: unknown): string => createHash('sha256')
  .update(typeof data === 'string' || Buffer.isBuffer(data) ? data : JSON.stringify(data)).digest('hex');
export const id = (prefix = 'run'): string => `${prefix}-${randomUUID().slice(0, 12)}`;
let implementation: Promise<string> | undefined;
export async function resourceDigests(root: string, prefix = ''): Promise<Array<[string, string]>> {
  const result: Array<[string, string]> = [];
  for (const entry of (await fs.readdir(path.join(root, prefix), {withFileTypes: true})).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name === '__pycache__' || entry.name.endsWith('.pyc')) continue;
    if (entry.isSymbolicLink()) throw new Error('Versioned resources must not be symlinks');
    const name = prefix + entry.name;
    if (entry.isDirectory()) result.push(...await resourceDigests(root, name + '/'));
    else result.push([name, hash(await fs.readFile(path.join(root, name)))]);
  }
  return result;
}
export function implementationDigest(): Promise<string> {
  return implementation ??= (async () => {
    const directory = fileURLToPath(new URL('.', import.meta.url));
    const files = (await fs.readdir(directory)).filter(name => /\.(?:js|ts)$/.test(name) && !name.endsWith('.d.ts')).sort();
    const modules = await Promise.all(files.map(async name => [name, hash(await fs.readFile(path.join(directory, name)))]));
    const project = path.dirname(directory.replace(/[\/]$/, ''));
    const skillRoot = path.join(project, 'skills', 'testprism');
    const skills = (await resourceDigests(skillRoot)).map(([name, digest]) => [`skills/testprism/${name}`, digest]);
    const bridge = (await resourceDigests(path.join(project, 'integrations', 'testprism')))
      .filter(([name]) => name.endsWith('.py')).map(([name, digest]) => [`integrations/testprism/${name}`, digest]);
    const support = await Promise.all(['package-lock.json', 'package.json', 'scripts/run_testprism.py',
      'scripts/cleanup_testprism.py', 'scripts/prepare_testprism_runtime.py', 'integrations/testprism/requirements-native.lock.txt'].map(async name => {
      const contents = await fs.readFile(path.join(project, name)).catch(error => { if (error.code === 'ENOENT') return Buffer.from('<absent>'); throw error; });
      return [name, hash(contents)];
    }));
    return hash([...modules, ...support, ...bridge, ...skills]);
  })();
}
export const shellQuote = (text: string): string => `'${text.replaceAll("'", "'\\''")}'`;
// Image bytes travel as images, never as millions of text tokens. Vision usage is
// provider-specific, so reserve a configurable allowance and record actual usage.
export function requestSize(value: unknown, imageTokens = 32768): {bytes: number; imageTokens: number} {
  let images = 0;
  const serialized = typeof value === 'string' ? value : JSON.stringify(value, (_key, item) => {
    if (item && item.type === 'image' && item.image !== undefined) {
      images++; return {type: 'image', mediaType: item.mediaType};
    }
    return item;
  });
  return {bytes: Buffer.byteLength(serialized, 'utf8'), imageTokens: images * imageTokens};
}
export const estimateTokens = (value: unknown): number => {
  const size = requestSize(value, 4096); return Math.ceil(size.bytes / 3) + size.imageTokens;
};
export const clip = (value: string, limit = 10000): string => value.length <= limit ? value
  : value.slice(0, Math.floor(limit * .6)) + '\n[output truncated; full result saved as an artifact]\n' + value.slice(-Math.floor(limit * .4));
export function errorText(error: unknown): string { return error instanceof Error ? error.message : String(error); }
export function redact(value: unknown): unknown {
  if (typeof value === 'string') return value
    .replace(/\bBearer\s+\S+/gi, 'Bearer [REDACTED]')
    .replace(/\bsk-[\w-]{12,}/g, '[REDACTED]');
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) =>
    [k, /^(authorization|api[_-]?key|password|secret|access[_-]?token)$/i.test(k) ? '[REDACTED]' : redact(v)]));
  return value;
}
export async function atomicJSON(file: string, value: unknown): Promise<void> {
  await fs.mkdir(path.dirname(file), {recursive: true});
  const temporary = `${file}.${randomUUID()}.tmp`;
  await fs.writeFile(temporary, JSON.stringify(value, null, 2) + '\n', {mode: 0o600});
  await fs.rename(temporary, file);
}
export async function readJSON<T>(file: string): Promise<T | undefined> {
  try { return JSON.parse(await fs.readFile(file, 'utf8')) as T; }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined; throw error; }
}
export function relativePath(value: string): string {
  if (!value || value.includes('\0') || value.includes('\\') || path.posix.isAbsolute(value)
    || value.split('/').includes('..')) throw new Error(`Unsafe relative path: ${value}`);
  const normalized = path.posix.normalize(value).replace(/^\.\//, '');
  if (normalized === '.' || normalized.startsWith('../')) throw new Error(`Invalid file path: ${value}`);
  return normalized;
}
export function publicPath(value: string): string {
  const normalized = relativePath(value);
  if (normalized.split('/').some(part => ['.git', '.patch-generation-state', 'patch-sets', 'solution.sh'].includes(part)))
    throw new Error('This path is outside the public task surface');
  return normalized;
}
export function testPath(value: string): string {
  const relative = publicPath(value);
  if (!relative.startsWith('agent-tests/')) throw new Error('Test artifacts must live under agent-tests/');
  return relative;
}
export async function containedPath(root: string, relative: string): Promise<string> {
  const target = path.resolve(root, publicPath(relative));
  const realRoot = await fs.realpath(root);
  let parent = target;
  for (;;) {
    try {
      const resolved = await fs.realpath(parent);
      if (resolved !== realRoot && !resolved.startsWith(realRoot + path.sep)) throw new Error('Path escapes workspace through a symlink');
      if (resolved !== realRoot) publicPath(path.relative(realRoot, resolved).split(path.sep).join('/'));
      return target;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      parent = path.dirname(parent);
    }
  }
}

export async function mapLimit<T, R>(items: readonly T[], concurrency: number, fn: (item: T, index: number) => Promise<R>): Promise<R[]> {
  if (!Number.isSafeInteger(concurrency) || concurrency < 1) throw new Error('Concurrency must be a positive integer');
  const limit = pLimit({concurrency, rejectOnClear: true});
  let failed = false;
  let failure: unknown;
  const results = await Promise.allSettled(items.map((item, index) => limit(async () => {
    try { return await fn(item, index); }
    catch (error) { if (!failed) {failed = true; failure = error; limit.clearQueue();} throw error; }
  })));
  // Cleanup may start as soon as this promise settles. Drain already running
  // siblings, but never admit queued work after the first failure.
  if (failed) throw failure;
  return results.map(result => (result as PromiseFulfilledResult<R>).value);
}

export async function command(argv: string[], options: {
  cwd?: string; input?: string | Buffer; timeoutMs?: number; signal?: AbortSignal;
  maxBytes?: number; env?: NodeJS.ProcessEnv; onTimeout?: () => Promise<void>;
} = {}): Promise<Execution> {
  if (options.signal?.aborted) throw options.signal.reason ?? new Error('Aborted');
  const started = Date.now();
  const maxBytes = options.maxBytes ?? 2_000_000;
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new Error('Process output limit must be a positive integer');
  return new Promise((resolve, reject) => {
    const child = spawn(argv[0]!, argv.slice(1), {cwd: options.cwd, env: options.env,
      detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe']});
    let stdout: Buffer = Buffer.alloc(0), stderr: Buffer = Buffer.alloc(0), truncated = false, timedOut = false;
    let killTimer: NodeJS.Timeout | undefined;
    const append = (old: Buffer, data: Buffer): Buffer => {
      const combined = Buffer.concat([old, data]);
      if (combined.length <= maxBytes) return combined;
      truncated = true;
      return Buffer.concat([combined.subarray(0, Math.floor(maxBytes / 2)), combined.subarray(-Math.ceil(maxBytes / 2))]);
    };
    child.stdout.on('data', data => { stdout = append(stdout, data); });
    child.stderr.on('data', data => { stderr = append(stderr, data); });
    const kill = (signal: NodeJS.Signals) => { try { process.kill(process.platform === 'win32' ? child.pid! : -child.pid!, signal); } catch {} };
    const stop = () => {
      timedOut = true; kill('SIGTERM');
      killTimer ??= setTimeout(() => kill('SIGKILL'), 1500);
    };
    const timer = setTimeout(stop, options.timeoutMs ?? 120000);
    const abort = () => stop();
    options.signal?.addEventListener('abort', abort, {once: true});
    const cleanup = () => { clearTimeout(timer); if (killTimer) clearTimeout(killTimer); options.signal?.removeEventListener('abort', abort); };
    child.on('error', error => { cleanup(); reject(error); });
    // A bounded host command owns its process group, including detached-stdio
    // background children. Reap on exit, before inherited pipes can delay close.
    // Docker containers belong to the factory; killing the docker CLI group
    // does not remove intentionally detached containers.
    child.on('exit', () => {if (process.platform !== 'win32') kill('SIGKILL');});
    child.on('close', async code => {
      cleanup();
      // Killing the process group also closes pipes inherited by grandchildren.
      if (timedOut) { kill('SIGKILL'); await options.onTimeout?.().catch(() => {}); }
      resolve({code, stdout: stdout.toString('utf8'), stderr: stderr.toString('utf8'),
        durationMs: Date.now() - started, timedOut, truncated});
    });
    child.stdin.on('error', () => {});
    child.stdin.end(options.input);
  });
}
