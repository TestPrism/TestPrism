/** Freeze a completed diagnostic, then evaluate its exact tests in fresh containers.
 * Host-only: this never calls a model and never feeds reference results into evolve.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { z } from 'zod';
import { parse } from 'yaml';
import { loadTask } from '../src/config.js';
import { mergeTests } from '../src/artifacts.js';
import { applyFiles, DockerSandbox } from '../src/runtime.js';
import { classify, failureSignature } from '../src/validation.js';
import { atomicJSON, command, hash, implementationDigest, testPath } from '../src/util.js';
import type { FileArtifact, RunResult } from '../src/types.js';

const provenanceSchema = z.object({
  taskDirectory: z.string().min(1), imageId: z.string().regex(/^sha256:[a-f0-9]{64}$/),
  root: z.string().startsWith('/'), env: z.record(z.string(), z.string()).default({}),
  eligibleForEvolutionValidation: z.literal(false), excludesHardest500: z.boolean(),
  sampleSplit: z.enum(['development', 'hardest500']).default('development'),
  imageIdentityRecordedAfterRun: z.boolean().default(false),
}).refine(value => value.excludesHardest500 === (value.sampleSplit === 'development'), 'Inconsistent diagnostic split');
function validateFiles(files: FileArtifact[]): void {
  assert.ok(files.length > 0 && files.length <= 80);
  const paths = new Set<string>(); let bytes = 0;
  for (const file of files) {
    testPath(file.path); assert.ok(!paths.has(file.path)); paths.add(file.path);
    assert.ok(!file.deleted && Number.isSafeInteger(file.mode) && file.mode >= 0 && file.mode <= 0o777);
    const data = Buffer.from(file.data, 'base64');
    assert.equal(data.toString('base64'), file.data, 'noncanonical artifact encoding');
    bytes += data.length;
  }
  assert.ok(paths.has('agent-tests/run-tests.sh')); assert.ok(bytes <= 4_000_000);
}
async function publishedFiles(directory: string): Promise<FileArtifact[]> {
  const files: FileArtifact[] = [];
  async function walk(relative: string): Promise<void> {
    const entries = await fs.readdir(path.join(directory, relative), {withFileTypes: true});
    for (const entry of entries) {
      const file = path.posix.join(relative, entry.name); testPath(file);
      assert.ok(!entry.isSymbolicLink(), 'published tests must not contain symlinks');
      if (entry.isDirectory()) await walk(file);
      else {
        assert.ok(entry.isFile(), 'published tests must be ordinary files');
        files.push({path: file, data: (await fs.readFile(path.join(directory, file))).toString('base64'),
          mode: (await fs.stat(path.join(directory, file))).mode & 0o777});
      }
    }
  }
  await walk('agent-tests'); return files.sort((a, b) => a.path.localeCompare(b.path));
}

async function main(): Promise<void> {
  const {values} = parseArgs({options: {diagnostic: {type: 'string'}, out: {type: 'string'}, repeats: {type: 'string', default: '2'},
    'hardest-sample': {type: 'boolean', default: false}}});
  assert.ok(values.diagnostic, '--diagnostic is required');
  const directory = path.resolve(values.diagnostic), out = path.resolve(values.out ?? path.join(directory, 'evaluation'));
  const repeats = Number(values.repeats); assert.ok(Number.isInteger(repeats) && repeats >= 2 && repeats <= 5);
  const provenance = provenanceSchema.parse(JSON.parse(await fs.readFile(path.join(directory, 'provenance.json'), 'utf8')));
  assert.equal(provenance.sampleSplit === 'hardest500', values['hardest-sample'], 'Hardest sample evaluation requires an explicit matching flag');
  const task = await loadTask(path.join(directory, 'task.json'));
  const publicTask = parse(await fs.readFile(path.join(path.resolve(provenance.taskDirectory), 'task.yaml'), 'utf8'));
  assert.equal(publicTask.instruction, task.instruction, 'reference directory belongs to another public task');
  const holdout = JSON.parse(await fs.readFile('TestPrism-main/verified_tasks/test-hardest-500/manifest.json', 'utf8'));
  const isHeldOut = (Object.entries(holdout.sources) as Array<[string, {task_ids: string[]}]>)
    .some(([source, data]) => data.task_ids.some(id => `${source}/${id}` === task.id));
  assert.equal(isHeldOut, values['hardest-sample'], 'Task does not belong to the selected diagnostic split');
  const runBytes = await fs.readFile(path.join(directory, 'run/result.json'));
  const run = JSON.parse(runBytes.toString()) as RunResult;
  assert.equal(run.taskDigest, hash(task)); assert.equal(run.taskId, task.id); assert.equal(task.runtime.root, provenance.root);
  assert.ok(run.implementationDigest && run.configDigest);
  const ids = new Set<string>(), suites = new Map<string, {testsHash: string; labels: string[]; files: FileArtifact[]}>();
  const addSuite = (label: string, files: FileArtifact[]) => {
    validateFiles(files); const testsHash = hash(files), prior = suites.get(testsHash);
    if (prior) prior.labels.push(label); else suites.set(testsHash, {testsHash, labels: [label], files});
  };
  for (const candidate of run.candidates) {
    assert.ok(!ids.has(candidate.id), 'duplicate candidate ID'); ids.add(candidate.id);
    assert.equal(candidate.testsHash, hash(candidate.tests), 'candidate tests changed after generation');
    addSuite(candidate.id, candidate.tests);
  }
  assert.equal(new Set(run.selected).size, run.selected.length);
  const selected = run.selected.map(id => { const candidate = run.candidates.find(item => item.id === id); assert.ok(candidate); return candidate; });
  if (selected.length) {
    const actual = await publishedFiles(path.join(directory, 'run/submission'));
    const expected = mergeTests(selected).sort((a, b) => a.path.localeCompare(b.path));
    assert.deepEqual(actual, expected, 'published bytes differ from the frozen selection');
    addSuite('published', actual);
  }
  const inspected = await command(['docker', 'image', 'inspect', '--format', '{{.Id}}', provenance.imageId]);
  assert.equal(inspected.code, 0); assert.equal(inspected.stdout.trim(), provenance.imageId);
  await fs.mkdir(out); // Refuse overwriting an earlier evaluation.
  const frozenAt = new Date().toISOString();
  await atomicJSON(path.join(out, 'freeze.json'), {schemaVersion: 1, frozenAt, taskId: task.id,
    resultDigest: hash(runBytes), implementationDigest: run.implementationDigest, configDigest: run.configDigest,
    evaluatorDigest: hash(await fs.readFile(fileURLToPath(import.meta.url))), hostImplementationDigest: await implementationDigest(),
    imageId: provenance.imageId, environmentDigest: hash(provenance.env), holdoutManifestDigest: hash(holdout),
    imageIdentityRecordedAfterRun: provenance.imageIdentityRecordedAfterRun,
    repeats, generationUsage: run.usage, suites: [...suites.values()], referenceRead: false});
  // The only reference read occurs AFTER generated bytes and evaluator identity are sealed.
  const reference = await fs.readFile(path.join(path.resolve(provenance.taskDirectory), 'solution.sh'));
  await atomicJSON(path.join(out, 'reference-audit.json'), {readAt: new Date().toISOString(), sha256: hash(reference), modelReceivedReference: false});
  const observations: Array<{testsHash: string; variant: string; repeat: number; status: string; signature: string; code: number | null}> = [];
  const env = Object.entries(provenance.env).flatMap(([name, value]) => ['-e', `${name}=${value}`]);
  for (const suite of suites.values()) for (const variant of ['baseline', 'reference']) for (let repeat = 1; repeat <= repeats; repeat++) {
    const started = await command(['docker', 'run', '-d', '--network', 'none', '--label', 'testhelix.diagnostic=frozen-evaluation',
      ...env, '--entrypoint', '/bin/bash', provenance.imageId, '-c', 'sleep infinity']);
    assert.equal(started.code, 0, started.stderr);
    const sandbox = new DockerSandbox(started.stdout.trim(), provenance.root, true);
    const prefix = `${suite.testsHash.slice(0, 16)}-${variant}-${repeat}`;
    try {
      if (variant === 'reference') {
        const applied = await command(['docker', 'exec', '-i', '-w', provenance.root, sandbox.container, 'bash', '-e'], {input: reference, timeoutMs: 60000});
        await atomicJSON(path.join(out, `${prefix}-apply.json`), applied);
        assert.ok(applied.code === 0 && !applied.timedOut, 'reference application failed; evaluation is incomplete');
      }
      await sandbox.remove('agent-tests'); await applyFiles(sandbox, suite.files);
      const execution = await sandbox.exec('bash agent-tests/run-tests.sh', Math.min(task.testTimeoutMs ?? 120000, 120000));
      const output = execution.stdout + '\n' + execution.stderr;
      const status = classify(execution.code, output, execution.timedOut, execution.truncated);
      const observation = {testsHash: suite.testsHash, variant, repeat, status,
        signature: failureSignature(status, output, provenance.root), code: execution.code};
      observations.push(observation);
      await atomicJSON(path.join(out, `${prefix}.json`), {...observation, ...execution});
      process.stdout.write(JSON.stringify({labels: suite.labels, variant, repeat, status, code: execution.code}) + '\n');
    } finally { await sandbox.close(); }
  }
  const results = [...suites.values()].map(suite => {
    const rows = observations.filter(row => row.testsHash === suite.testsHash);
    const baseline = rows.filter(row => row.variant === 'baseline'), reference = rows.filter(row => row.variant === 'reference');
    const stable = (group: typeof rows) => group.length === repeats && new Set(group.map(row => row.status + ':' + row.signature)).size === 1;
    return {testsHash: suite.testsHash, labels: suite.labels, baseline: baseline.map(row => row.status), reference: reference.map(row => row.status),
      f2p: stable(baseline) && baseline.every(row => row.status === 'fail') && stable(reference) && reference.every(row => row.status === 'pass')};
  });
  await atomicJSON(path.join(out, 'evaluation.json'), {schemaVersion: 1, taskId: task.id, frozenAt, evaluatedAt: new Date().toISOString(),
    complete: true, referenceIsSingleSuppliedSolution: true, officialScore: null, eligibleForEvolutionValidation: false,
    sampleSplit: provenance.sampleSplit,
    modelCallsDuringEvaluation: 0, generationUsage: run.usage, imageIdentityRecordedAfterRun: provenance.imageIdentityRecordedAfterRun,
    results});
}
main().catch(error => { process.stderr.write(String(error) + '\n'); process.exitCode = 1; });
