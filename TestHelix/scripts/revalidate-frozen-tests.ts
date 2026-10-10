/** Baseline-only revalidation of unchanged test-only artifacts; no model or reference access. */
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {parseArgs} from 'node:util';
import assert from 'node:assert/strict';
import {loadTask} from '../src/config.js';
import {configSchema, type RunResult, type Observation} from '../src/types.js';
import {DockerSandbox, applyFiles} from '../src/runtime.js';
import {classify, failureSignature} from '../src/validation.js';
import {select} from '../src/selection.js';
import {Store} from '../src/store.js';
import {atomicJSON, command, hash, implementationDigest, testPath, clip} from '../src/util.js';
import {finalizeReview} from '../src/review.js';

async function main() {
  const {values} = parseArgs({options: {diagnostic: {type: 'string'}, out: {type: 'string'}, 'replay-reviews': {type: 'boolean', default: false}}});
  assert.ok(values.diagnostic && values.out, '--diagnostic and new --out are required');
  const original = path.resolve(values.diagnostic), out = path.resolve(values.out), started = Date.now();
  const bytes = await fs.readFile(path.join(original, 'run/result.json')), parent = JSON.parse(bytes.toString()) as RunResult;
  const task = await loadTask(path.join(original, 'task.json'));
  const provenance = JSON.parse(await fs.readFile(path.join(original, 'provenance.json'), 'utf8'));
  assert.equal(parent.taskDigest, hash(task)); assert.equal(provenance.eligibleForEvolutionValidation, false);
  assert.equal(provenance.excludesHardest500, true); assert.match(provenance.imageId, /^sha256:[a-f0-9]{64}$/);
  const config = configSchema.parse(JSON.parse(await fs.readFile(path.join(original, 'run/run.json'), 'utf8')).config);
  assert.equal(hash(config), parent.configDigest, 'Original config changed');
  assert.equal(new Set(parent.candidates.map(c => c.id)).size, parent.candidates.length);
  const manifest = JSON.parse(await fs.readFile('TestPrism-main/verified_tasks/test-hardest-500/manifest.json', 'utf8'));
  for (const [source, value] of Object.entries(manifest.sources))
    assert.ok(!(value as {task_ids: string[]}).task_ids.some(id => `${source}/${id}` === task.id), 'Hardest500 excluded');
  for (const candidate of parent.candidates) {
    assert.match(candidate.id, /^[a-zA-Z0-9_-]+$/);
    assert.equal(candidate.source.length, 0, 'This utility handles test-only candidates; source witnesses require the normal pipeline');
    assert.equal(hash(candidate.tests), candidate.testsHash); assert.equal(hash(candidate.source), candidate.sourceHash);
    assert.ok(candidate.tests.length <= 80 && candidate.tests.some(f => f.path === 'agent-tests/run-tests.sh'));
    const seen = new Set<string>(); let size = 0;
    for (const file of candidate.tests) {
      const normalized = testPath(file.path); assert.equal(normalized, file.path); assert.ok(!seen.has(file.path)); seen.add(file.path);
      assert.ok(!file.deleted && Number.isInteger(file.mode) && file.mode >= 0 && file.mode <= 0o777);
      const data = Buffer.from(file.data, 'base64'); assert.equal(data.toString('base64'), file.data); size += data.length;
    }
    assert.ok(size <= config.execution.maxArtifactBytes);
  }
  const reviews = structuredClone(parent.reviews);
  const replay = new Map<string, {input: unknown; sessionDigest: string}>();
  if (values['replay-reviews']) for (const candidate of parent.candidates) {
    if (reviews[candidate.testsHash]) continue;
    const session = await fs.readFile(path.join(original, 'run/sessions', `review-${candidate.id}.json`)).catch(error => {
      if (error.code === 'ENOENT') return undefined; throw error;
    });
    if (!session) continue;
    const saved = JSON.parse(session.toString());
    assert.ok(saved.messages[0]?.content?.startsWith(`Original public task:\n${task.instruction}\n\nExploration strategy:`));
    const calls = saved.messages.flatMap((message: {content: unknown}) => Array.isArray(message.content)
      ? message.content.filter(part => part.type === 'tool-call' && part.toolName === 'submit_review') : []);
    const last = calls.at(-1);
    if (last) replay.set(candidate.id, {input: last.input, sessionDigest: hash(session)});
  }
  await fs.mkdir(out); const store = new Store(path.join(out, 'run')); await store.init();
  const implementation = await implementationDigest();
  await atomicJSON(path.join(out, 'input-freeze.json'), {frozenAt: new Date().toISOString(), parentDirectory: original,
    resultDigest: hash(bytes), implementationDigest: implementation, referenceRead: false, modelCalls: 0,
    testHashes: parent.candidates.map(c => c.testsHash), reviewReplays: Object.fromEntries(replay),
    selectionInputs: 'baseline executions, original reviews, and optional host validation of the last frozen model review; no reference results'});
  await atomicJSON(path.join(out, 'task.json'), task); await atomicJSON(path.join(out, 'provenance.json'), provenance);
  const matrix: Observation[] = [], env = Object.entries(provenance.env as Record<string, string>).flatMap(([k, v]) => ['-e', `${k}=${v}`]);
  for (const candidate of parent.candidates) {
    const observations: Observation[] = [];
    for (let repeat = 0; repeat < config.execution.repeat; repeat++) {
      const run = await command(['docker', 'run', '-d', '--network', 'none', '--label', 'testhelix.diagnostic=frozen-baseline-replay',
        ...env, '--entrypoint', '/bin/bash', provenance.imageId, '-c', 'sleep infinity']);
      assert.equal(run.code, 0, run.stderr); const sandbox = new DockerSandbox(run.stdout.trim(), task.runtime.root, true);
      try {
        await sandbox.remove('agent-tests'); await applyFiles(sandbox, candidate.tests);
        const priorReview = replay.get(candidate.id);
        if (repeat === 0 && priorReview) {
          try {
            reviews[candidate.testsHash] = await finalizeReview(priorReview.input, sandbox);
            await store.save(`replayed-reviews/${candidate.id}.json`, {sessionDigest: priorReview.sessionDigest, review: reviews[candidate.testsHash], modelCalls: 0});
          } catch (error) { await store.event('review.replay_failed', {candidate: candidate.id, error: String(error)}); }
        }
        const syntax = await sandbox.exec('bash -n agent-tests/run-tests.sh', 10000);
        const execution = syntax.code === 0 && !syntax.timedOut
          ? await sandbox.exec('bash agent-tests/run-tests.sh', Math.min(config.execution.testTimeoutMs, task.testTimeoutMs ?? Infinity)) : syntax;
        const output = execution.stdout + '\n' + execution.stderr;
        const status = syntax.code !== 0 || syntax.timedOut ? 'error' : classify(execution.code, output, execution.timedOut, execution.truncated);
        const observation = {testId: candidate.id, sourceId: 'baseline', status, signature: failureSignature(status, output, sandbox.root),
          durationMs: execution.durationMs, output: clip(output, 3000), artifact: await store.artifact(output)};
        observations.push(observation); await store.save(`execution-${candidate.id}-${repeat + 1}.json`, {...observation, ...execution});
      } finally { await sandbox.close(); }
    }
    const first = observations[0]!;
    const stable = observations.every(o => o.status === first.status && (o.status !== 'fail' || o.signature === first.signature));
    matrix.push({...first, ...(stable ? {} : {status: 'error' as const, signature: 'unstable', output: 'Repeated execution differs'}),
      durationMs: observations.reduce((s, o) => s + o.durationMs, 0)});
  }
  const selection = select(parent.candidates, matrix, config.search.minIndependentSupport, reviews);
  const selected = config.search.allowProvisional ? selection.provisional : undefined;
  if (selected) for (const file of selected.tests) {
    const target = path.join(out, 'run/submission', file.path); await fs.mkdir(path.dirname(target), {recursive: true});
    await fs.writeFile(target, Buffer.from(file.data, 'base64'), {mode: file.mode});
  }
  const result: RunResult = {...parent, status: selected ? 'provisional' : 'no_submit',
    reason: 'Baseline-only host revalidation of byte-identical frozen tests and optional frozen review replay; no new model calls or semantic judgment; original run preserved',
    testsDir: selected ? path.join(out, 'run/submission/agent-tests') : null, selected: selected ? [selected.id] : [], witnesses: [], matrix, reviews,
    usage: {calls: 0, input: 0, output: 0, cached: 0, estimated: false, costUSD: null}, elapsedMs: Date.now() - started,
    implementationDigest: implementation, configDigest: hash(config)};
  await store.save('run.json', {config, task: {id: task.id, source: task.source}, implementationDigest: implementation});
  await store.save('result.json', result);
  await atomicJSON(path.join(out, 'generation-freeze.json'), {frozenAt: new Date().toISOString(), referenceRead: false,
    resultDigest: hash(await fs.readFile(path.join(out, 'run/result.json'))), parentResultDigest: hash(bytes), selected: result.selected});
  process.stdout.write(JSON.stringify({status: result.status, selected: result.selected, matrix: matrix.map(o => ({test: o.testId, status: o.status})), modelCalls: 0}) + '\n');
}
main().catch(error => {process.stderr.write(String(error) + '\n'); process.exitCode = 1;});
