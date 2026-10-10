import type {Sandbox} from './runtime.js';
import {classify} from './validation.js';
import {hash, shellQuote, testPath} from './util.js';
import {executionEvidence} from './execution-evidence.js';

export interface SuiteInput {files: Array<{path: string; content: string}>; timeoutMs?: number}
/** Write a complete test file group before execution. No task assertions or source repair are supplied by the host. */
export async function writeTestSuite(sandbox: Sandbox, input: SuiteInput, timeoutMs: number, byteLimit: number, signal: AbortSignal) {
  const files = input.files.map(file => ({path: testPath(file.path), data: Buffer.from(file.content)}));
  if (!files.length || files.length > 40 || files.reduce((sum, file) => sum + file.data.length, 0) > byteLimit)
    throw new Error('Test suite file count or byte limit exceeded');
  const paths = files.map(file => file.path);
  if (new Set(paths).size !== paths.length || paths.some(file => paths.some(other => other !== file && other.startsWith(file + '/'))))
    throw new Error('Duplicate or overlapping test paths');
  const runner = 'agent-tests/run-tests.sh';
  const ancestors = new Set([runner, ...paths].flatMap(file => file.split('/').map((_, i, parts) => parts.slice(0, i + 1).join('/'))));
  const links = await sandbox.exec(`for file in ${[...ancestors].map(shellQuote).join(' ')}; do\nif [ -L "$file" ]; then exit 43; fi\ndone`, Math.min(10000, timeoutMs), signal);
  if (links.code !== 0 || links.timedOut) throw new Error('Test suite destinations must not contain symlinks');
  if (!paths.includes(runner)) {
    try { await sandbox.read(runner); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') throw new Error('Include agent-tests/run-tests.sh in files; no runner exists yet'); throw error; }
  }
  const changes: Array<{path: string; data: Buffer; before?: Buffer; mode: number; beforeMode?: number}> = [];
  let savedBytes = 0;
  for (const file of files) {
    signal.throwIfAborted();
    let before: Buffer | undefined, beforeMode: number | undefined, mode = file.path.endsWith('.sh') ? 0o755 : 0o644;
    try { before = await sandbox.read(file.path); beforeMode = await sandbox.mode(file.path); mode = file.path === runner ? 0o755 : beforeMode; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    savedBytes += before?.length ?? 0;
    if (savedBytes > byteLimit) throw new Error('Existing test files exceed rollback byte limit; use focused edits');
    changes.push({...file, before, mode, beforeMode});
  }
  const written: typeof changes = [];
  try {
    for (const file of changes) {
      signal.throwIfAborted(); written.push(file); // Also restore a partially failed write.
      await sandbox.write(file.path, file.data, file.mode);
    }
  } catch (error) {
    const failed: string[] = [];
    for (const file of written.reverse()) {
      try { if (file.before) await sandbox.write(file.path, file.before, file.beforeMode); else await sandbox.remove(file.path); }
      catch { failed.push(file.path); }
    }
    if (failed.length) throw new Error(`Suite write failed and rollback incomplete: ${failed.join(', ')}`);
    throw error;
  }
  // Execution failures preserve written tests so the next turn can make a focused correction.
  const syntax = await sandbox.exec('bash -n agent-tests/run-tests.sh', Math.min(10000, timeoutMs), signal);
  const execution = syntax.code === 0 && !syntax.timedOut
    ? await sandbox.exec('bash agent-tests/run-tests.sh', Math.min(timeoutMs, input.timeoutMs ?? timeoutMs), signal) : syntax;
  return {ok: true, written: true, executed: syntax.code === 0 && !syntax.timedOut,
    files: files.map(file => ({path: file.path, bytes: file.data.length, hash: hash(file.data)})),
    execution: {...execution, diagnostics: executionEvidence(execution.stdout + '\n' + execution.stderr), status: syntax.code !== 0 || syntax.timedOut ? 'error'
      : classify(execution.code, execution.stdout + '\n' + execution.stderr, execution.timedOut, execution.truncated)},
    guidance: 'Files are saved. A baseline behavior failure may be intentional; setup errors need correction. submit_candidate captures actual source changes automatically.'};
}
