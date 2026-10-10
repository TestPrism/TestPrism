import type { Candidate, FileArtifact } from './types.js';
import type { Sandbox } from './runtime.js';
import { hash, publicPath, testPath, shellQuote } from './util.js';
import { z } from 'zod';
import {discoverSourceChanges} from './source-state.js';
import {outputContractSchema, validateOutputContract, inferTaskContract, verifyContractArtifacts} from './task-contract.js';
import type {ContractLimits} from './contract-schema.js';

export const exportSchema = z.object({
  sourcePaths: z.array(z.string()).max(40).default([]),
  deletedSourcePaths: z.array(z.string()).max(40).default([]),
  requirements: z.array(z.string().max(600)).min(1).max(20),
  rationale: z.string().max(3000),
  replayScript: z.string().min(1).max(500).optional(),
  contract: outputContractSchema.optional(),
});
export interface CaptureContext {instruction?: string; protocol?: 'tests-only' | 'internal'; contractLimits?: Partial<ContractLimits>}
function checkContract(input: unknown, context: CaptureContext) {
  const report = validateOutputContract(input, inferTaskContract(context.instruction ?? ''), context.protocol ?? 'internal', context.contractLimits);
  if (!report.compatible) throw new Error(`Output contract rejected: ${report.errors.slice(0, 4).join('; ')}`);
  return report;
}
/** A replay is an explicitly captured internal repair, never a test artifact or
 * an arbitrary path to an existing hidden/external script. */
export function replayPath(source: readonly FileArtifact[], value?: string, root = ''): string | undefined {
  if (value === undefined) return;
  const prefix = root.replace(/\/+$/, '') + '/';
  const file = publicPath(root && value.startsWith(prefix) ? value.slice(prefix.length) : value);
  const artifact = source.find(item => item.path === file && !item.deleted);
  if (file.startsWith('agent-tests/') || file === 'agent-tests' || !artifact)
    throw new Error('Replay script must be a captured source change outside agent-tests/');
  const bytes = Buffer.from(artifact.data, 'base64');
  if (!bytes.length || bytes.includes(0)) throw new Error('Replay script is empty or binary');
  if (bytes.length > 500) throw new Error('Replay script exceeds the 500-byte limit');
  return file;
}
export async function captureWorkspaceCandidate(...args: Parameters<typeof captureCandidate>): Promise<Candidate> {
  const [sandbox, baseline, input, identity, maxBytes, context = {}] = args;
  const declaration = exportSchema.parse(input);
  checkContract(declaration.contract, context);
  // Legacy path lists are ignored. Validating a discarded model-maintained
  // inventory only creates repair turns; it cannot strengthen source isolation.
  // The discovered changes still pass captureCandidate's strict path checks.
  let changes = await discoverSourceChanges(sandbox, baseline);
  if (declaration.replayScript) {
    const source: FileArtifact[] = [];
    for (const file of changes.sourcePaths) {
      const data = await sandbox.read(file);
      source.push({path: file, data: data.toString('base64'), mode: await sandbox.mode(file)});
    }
    const replayScript = replayPath(source, declaration.replayScript, sandbox.root);
    if (!replayScript) throw new Error('Replay script was not captured');
    const syntax = await sandbox.exec('bash -n -- ' + shellQuote(replayScript), 10000);
    if (syntax.code !== 0 || syntax.timedOut || syntax.truncated)
      throw new Error(`Replay script syntax invalid: ${syntax.stderr}`);
    const execution = await sandbox.exec('bash -e -- ' + shellQuote(replayScript), 30000);
    if (execution.code !== 0 || execution.timedOut || execution.truncated)
      throw new Error(`Replay script failed during candidate preflight: ${execution.stderr || execution.stdout}`);
    const runner = await sandbox.exec('bash agent-tests/run-tests.sh', 120000);
    if (runner.code !== 0 || runner.timedOut || runner.truncated)
      throw new Error(`Post-repair runner failed during candidate preflight: ${runner.stderr || runner.stdout}`);
    changes = await discoverSourceChanges(sandbox, baseline);
  }
  return captureCandidate(sandbox, baseline, {...declaration, ...changes}, identity, maxBytes, context);
}
export async function captureCandidate(sandbox: Sandbox, baseline: Sandbox, input: unknown,
  identity: Pick<Candidate, 'id' | 'lineage' | 'round' | 'strategy' | 'sessionId'>, maxBytes: number,
  context: CaptureContext = {}): Promise<Candidate> {
  const declaration = exportSchema.parse(input);
  const contractReport = checkContract(declaration.contract, context);
  const testFiles = (await sandbox.list('agent-tests')).filter(file => !/\.(pyc|class|o|so|exe)$/.test(file));
  if (!testFiles.includes('agent-tests/run-tests.sh')) throw new Error('Create agent-tests/run-tests.sh before exporting');
  if (testFiles.length > 80) throw new Error('Too many test artifacts; remove generated logs and build output');
  const tests: FileArtifact[] = [], source: FileArtifact[] = [];
  let bytes = 0;
  for (const file of testFiles) {
    testPath(file); const data = await sandbox.read(file); bytes += data.length;
    if (bytes > maxBytes) throw new Error('Export exceeds artifact byte budget');
    tests.push({path: file, data: data.toString('base64'), mode: await sandbox.mode(file)});
  }
  for (const file of new Set(declaration.sourcePaths)) {
    publicPath(file); if (file === 'agent-tests' || file.startsWith('agent-tests/')) throw new Error('Source and tests must be separate');
    const data = await sandbox.read(file); bytes += data.length;
    if (bytes > maxBytes) throw new Error('Export exceeds artifact byte budget');
    const prior = await baseline.read(file).catch(error => { if (error.code === 'ENOENT') return null; throw error; }), mode = await sandbox.mode(file);
    if (!prior || !prior.equals(data) || await baseline.mode(file) !== mode) source.push({path: file, data: data.toString('base64'), mode});
  }
  for (const file of new Set(declaration.deletedSourcePaths)) {
    publicPath(file); if (file === 'agent-tests' || file.startsWith('agent-tests/')) throw new Error('Source deletion cannot target tests');
    if (declaration.sourcePaths.includes(file)) throw new Error('A source path cannot be both written and deleted');
    let exists = true;
    await sandbox.read(file).catch(error => { if (error.code === 'ENOENT') { exists = false; return; } throw error; });
    if (exists) throw new Error(`Delete ${file} in the workspace before declaring its deletion`);
    await baseline.read(file); source.push({path: file, data: '', mode: 0o644, deleted: true});
  }
  if (bytes > maxBytes) throw new Error('Export exceeds artifact byte budget');
  source.sort((a, b) => a.path.localeCompare(b.path)); tests.sort((a, b) => a.path.localeCompare(b.path));
  const contractCheck = await verifyContractArtifacts(contractReport, baseline, tests, context.contractLimits, maxBytes);
  if (!contractCheck.compatible) throw new Error(`Output contract rejected: ${contractCheck.errors.slice(0, 4).join('; ')}`);
  const runner = Buffer.from(tests.find(file => file.path === 'agent-tests/run-tests.sh')!.data, 'base64').toString('utf8');
  if (!runner.trim() || runner.includes('\0')) throw new Error('Runner is empty or binary');
  const syntax = await sandbox.exec('bash -n agent-tests/run-tests.sh', 10000);
  if (syntax.code !== 0 || syntax.timedOut) throw new Error(`Runner syntax invalid: ${syntax.stderr}`);
  const replayScript = replayPath(source, declaration.replayScript, sandbox.root);
  if (replayScript) {
    const syntax = await sandbox.exec('bash -n -- ' + shellQuote(replayScript), 10000);
    if (syntax.code !== 0 || syntax.timedOut || syntax.truncated) throw new Error(`Replay script syntax invalid: ${syntax.stderr}`);
  }
  return {...identity, tests, source, ...(replayScript ? {replayScript} : {}),
    sourceHash: hash(replayScript ? {source, replayScript} : source), testsHash: hash(tests),
    requirements: declaration.requirements, rationale: declaration.rationale,
    contract: contractCheck.effective, contractCheck};
}

/** Wrap bundles in isolated test directories while keeping the project at its original path. */
export function mergeTests(candidates: Candidate[]): FileArtifact[] {
  if (!candidates.length) return [];
  if (candidates.length === 1) return candidates[0]!.tests;
  const output: FileArtifact[] = [];
  const commands: string[] = [];
  candidates.forEach((candidate, index) => {
    const name = `bundle-${index + 1}`;
    for (const file of candidate.tests) output.push({...file, path: `agent-tests/.bundles/${name}/${file.path.slice('agent-tests/'.length)}`});
    // A fresh private copy at agent-tests/ preserves each runner's original paths.
    // The outer suite installs one bundle at a time and restores itself on exit.
    commands.push(`run_bundle ${name}`);
  });
  const runner = `#!/usr/bin/env bash\nset -u\ncd "$(dirname "$0")/.."\nroot=$PWD\nbackup=$(mktemp -d "\${TMPDIR:-/tmp}/testhelix-suite.XXXXXX") || exit 2\ncp -R "$root/agent-tests/." "$backup/" || exit 2\ncleanup() { rm -rf "$root/agent-tests"; mkdir -p "$root/agent-tests"; cp -R "$backup/." "$root/agent-tests/"; rm -rf "$backup"; }\ntrap cleanup EXIT\ntrap 'exit 124' TERM INT\nstatus=0\nrun_bundle() {\n  rm -rf "$root/agent-tests" || { status=2; return 2; }\n  mkdir -p "$root/agent-tests" || { status=2; return 2; }\n  cp -R "$backup/.bundles/$1/." "$root/agent-tests/" || { status=2; return 2; }\n  (cd "$root" && bash agent-tests/run-tests.sh) || status=1\n}\n${commands.join('\n')}\nexit "$status"\n`;
  output.push({path: 'agent-tests/run-tests.sh', data: Buffer.from(runner).toString('base64'), mode: 0o755});
  return output;
}
