import type {Sandbox} from './runtime.js';
import {createHash, randomUUID} from 'node:crypto';
import {publicPath, shellQuote} from './util.js';

interface Entry {kind: string; mode: number; size: number; digest: string; target: string}
type Manifest = Map<string, Entry>;
const baselines = new WeakMap<Sandbox, Promise<Manifest>>();
// Public source only. These are runtime caches or excluded task surfaces, never
// evidence for a source repair. Do not traverse symbolic links.
const excluded = ['.git', 'node_modules', '.venv', '__pycache__', '.pytest_cache', '.cache',
  '.testhelix-tmp', '.patch-generation-state', 'patch-sets', 'solution.sh'];
const prune = `\\( -path './agent-tests' -o ${excluded.map(name => `-name ${shellQuote(name)}`).join(' -o ')} \\) -prune`;
const maxFiles = 20000, maxScanBytes = 512_000_000;
const maxInventoryBytes = 8_000_000;

async function inventoryOutput(sandbox: Sandbox, command: string, failure: string): Promise<string> {
  // Execution output is a bounded diagnostic preview. Machine inventories use
  // the existing binary file transport, outside the model's context window.
  const file = `.testhelix-tmp/source-state-${randomUUID()}`;
  try {
    await sandbox.write(file, Buffer.alloc(0), 0o600);
    const result = await sandbox.exec(`set -o pipefail; (${command}) | head -c ${maxInventoryBytes + 1} > ${shellQuote(file)}`, 30000);
    if (result.code !== 0 || result.timedOut || result.truncated) throw new Error(failure);
    const data = await sandbox.read(file);
    if (data.length > maxInventoryBytes) throw new Error(failure);
    return data.toString('utf8');
  } finally {
    await sandbox.remove(file);
  }
}

async function scan(sandbox: Sandbox): Promise<Manifest> {
  // GNU find/coreutils are already part of the supported Linux runtimes. Two
  // bounded native commands avoid thousands of docker exec/read_file calls.
  const metadata = await inventoryOutput(sandbox, `LC_ALL=C find . ${prune} -o ! -type d -printf '%y\\0%m\\0%s\\0%p\\0%l\\0'`, 'Cannot obtain a complete public source inventory');
  const fields = metadata.split('\0');
  if (fields.pop() !== '' || fields.length % 5) throw new Error('Malformed public source inventory');
  if (fields.length / 5 > maxFiles) throw new Error('Public source inventory exceeds 20000 files');
  const manifest: Manifest = new Map();
  let bytes = 0;
  for (let index = 0; index < fields.length; index += 5) {
    const [kind, mode, size, raw, target] = fields.slice(index, index + 5) as [string, string, string, string, string];
    const file = publicPath(raw), length = Number(size);
    if (!/^[0-7]{1,4}$/.test(mode) || !Number.isSafeInteger(length) || length < 0 || manifest.has(file)) throw new Error('Invalid source inventory entry');
    manifest.set(file, {kind, mode: parseInt(mode, 8) & 0o777, size: length, target, digest: ''});
    if (kind === 'f') bytes += length;
  }
  if (bytes > maxScanBytes) throw new Error('Public source inventory exceeds the 512 MB hashing budget');
  const digests = await inventoryOutput(sandbox, `LC_ALL=C find . ${prune} -o -type f -exec sha256sum --zero -- {} +`, 'Cannot hash the complete public source inventory');
  const records = digests.split('\0');
  if (records.pop() !== '') throw new Error('Incomplete source digest stream');
  for (const record of records) {
    if (!/^[a-f0-9]{64}  /.test(record)) throw new Error('Invalid source digest record');
    const file = publicPath(record.slice(66)), entry = manifest.get(file);
    if (!entry || entry.kind !== 'f' || entry.digest) throw new Error('Public source changed during inventory');
    entry.digest = record.slice(0, 64);
  }
  if ([...manifest.values()].some(entry => entry.kind === 'f' && !entry.digest)) throw new Error('Missing public source digest');
  return manifest;
}

export interface SourceChanges {sourcePaths: string[]; deletedSourcePaths: string[]}

/** Compare sealed candidates using the existing original-source inventory.
 * Missing, oversized and unsupported files must not be guessed from read errors. */
export async function comparisonBaseline(sandbox: Sandbox, file: string, maxBytes: number): Promise<{data: Buffer; mode: number} | null> {
  const manifest = await baselines.get(sandbox);
  if (!manifest) throw new Error('Original-source inventory is unavailable');
  const entry = manifest.get(publicPath(file));
  if (!entry) return null;
  if (entry.kind !== 'f' || entry.size > maxBytes) throw new Error('Unsupported or oversized comparison file');
  const bytes = await sandbox.read(file);
  if (bytes.length > maxBytes || createHash('sha256').update(bytes).digest('hex') !== entry.digest)
    throw new Error('Original-source content changed after inventory');
  return {data: bytes, mode: entry.mode};
}
/** Discover actual byte/mode differences, including shell edits and deletions.
 * A complete inventory is mandatory: unsupported mutations never become a
 * silently partial witness. Validation still replays captured bytes fresh.
 */
export async function discoverSourceChanges(sandbox: Sandbox, baseline: Sandbox): Promise<SourceChanges> {
  let prior = baselines.get(baseline);
  if (!prior) {
    prior = scan(baseline); baselines.set(baseline, prior);
    prior.catch(() => baselines.delete(baseline));
  }
  const before = await prior, after = await scan(sandbox);
  const sourcePaths: string[] = [], deletedSourcePaths: string[] = [];
  for (const file of [...new Set([...before.keys(), ...after.keys()])].sort()) {
    const old = before.get(file), current = after.get(file);
    if (old && current && old.kind === current.kind && old.mode === current.mode
      && old.digest === current.digest && old.target === current.target) continue;
    if ((old && old.kind !== 'f') || (current && current.kind !== 'f'))
      throw new Error(`Source capture cannot replay a changed symbolic link or special file: ${file}`);
    if (current) sourcePaths.push(file); else deletedSourcePaths.push(file);
  }
  if (sourcePaths.length + deletedSourcePaths.length > 40) throw new Error('More than 40 source changes; remove generated files before sealing');
  const changed = [...sourcePaths, ...deletedSourcePaths];
  if (changed.some(file => changed.some(other => other.startsWith(file + '/'))))
    throw new Error('Source capture cannot replay a file/directory type replacement');
  return {sourcePaths, deletedSourcePaths};
}
