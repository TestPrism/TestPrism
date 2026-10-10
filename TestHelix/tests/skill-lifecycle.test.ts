import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {fixture} from './helpers.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {Toolbox} from '../src/tools.js';
import {prepareTestHarness} from '../src/harness.js';
import {skillIds} from '../src/skills.js';

async function runtime() {
  const f = await fixture(), sandbox = new LocalSandbox(f.workspace, false), store = new Store(f.out).forSession('candidate'); await store.init();
  const make = () => new Toolbox(sandbox, store, 10000, 5000, false, skillIds);
  return {...f, sandbox, store, make, signal: AbortSignal.timeout(20000), checkpoint: path.join(f.out, 'sessions/candidate/skill-execution.json')};
}
test('owned harness preparation reuses matching files but always verifies a fresh control and persists scoped evidence', async t => {
  const f = await runtime(); t.after(f.cleanup); const tools = f.make();
  const first = await tools.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
  const receipt = tools.procedures.snapshot().receipts[0]!;
  assert.equal(receipt.status, 'completed'); assert.equal(receipt.outcome, 'setup-control-pass');
  assert.equal(JSON.parse(await f.store.readArtifact(receipt.evidence!)).controlPassed, true);
  await assert.rejects(new Store(f.out).forSession('peer').readArtifact(receipt.evidence!), /ENOENT/);
  const write = f.sandbox.write.bind(f.sandbox), exec = f.sandbox.exec.bind(f.sandbox);
  let writes = 0, controls = 0;
  f.sandbox.write = async (...args) => {writes++; return write(...args);};
  f.sandbox.exec = async (...args) => {if (args[0] === 'bash agent-tests/run-tests.sh --control') controls++; return exec(...args);};
  const second = await tools.execute('prepare_test_harness', {framework: 'unittest', python: 'python3', projectApps: [], modulePaths: []}, f.signal);
  assert.equal(writes, 0); assert.equal(controls, 1); assert.equal(second.reusedFiles, (first.files as unknown[]).length);
  assert.equal(tools.procedures.snapshot().receipts[0]!.attempt, 2);
  const journal = JSON.parse(await fs.readFile(f.checkpoint, 'utf8'));
  assert.equal(journal.receipts[0].outcome, 'setup-control-pass'); assert.match(tools.procedures.render(), /does not certify/);
});

test('an interrupted installation resumes only missing owned files, then reruns verification', async t => {
  const f = await runtime(); t.after(f.cleanup); const tools = f.make();
  await tools.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
  const snapshot = tools.procedures.snapshot(); snapshot.receipts[0]!.status = 'running'; snapshot.receipts[0]!.stage = 'install';
  await fs.writeFile(f.checkpoint, JSON.stringify(snapshot));
  const missing = snapshot.receipts[0]!.plannedFiles[0]!.path; await f.sandbox.remove(missing);
  const resumed = f.make(); await resumed.procedures.initialize();
  assert.equal(resumed.procedures.snapshot().receipts[0]!.status, 'interrupted');
  const writes: string[] = [], write = f.sandbox.write.bind(f.sandbox);
  f.sandbox.write = async (file, ...rest) => {writes.push(file); return write(file, ...rest);};
  const result = await resumed.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
  assert.equal(result.controlPassed, true); assert.deepEqual(writes, [missing]);
  assert.equal(resumed.procedures.snapshot().receipts[0]!.outcome, 'setup-control-pass');
});

test('file edits, mode drift and unowned matching scaffolds cannot be overwritten or silently adopted', async t => {
  const f = await runtime(); t.after(f.cleanup);
  await prepareTestHarness(f.sandbox, {framework: 'unittest'}, 5000, f.signal);
  const unowned = f.make();
  for (let i = 0; i < 2; i++) {
    const result = await unowned.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
    assert.equal(result.stage, 'conflict'); assert.equal(unowned.procedures.snapshot().receipts[0]!.plannedFiles.length, 0);
  }
  const other = await runtime(); t.after(other.cleanup); const tools = other.make();
  await tools.execute('prepare_test_harness', {framework: 'unittest'}, other.signal);
  const file = 'agent-tests/_evo_harness/runtime.py', original = await other.sandbox.read(file);
  await other.sandbox.write(file, original, 0o444);
  assert.equal((await tools.execute('prepare_test_harness', {framework: 'unittest'}, other.signal)).stage, 'conflict');
  assert.equal(await other.sandbox.mode(file), 0o444); assert.deepEqual(await other.sandbox.read(file), original);
});

test('restored or mutated execution observations are stale; setup failure is never a completed skill', async t => {
  const f = await runtime(); t.after(f.cleanup); const tools = f.make();
  await tools.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
  const saved = tools.procedures.snapshot();
  await tools.execute('write_file', {path: 'prime.cjs', content: 'exports.isPrime = () => false;'}, f.signal);
  assert.equal(tools.procedures.snapshot().receipts[0]!.status, 'stale');
  await fs.rm(f.checkpoint);
  const restored = f.make(); restored.procedures.restore(saved); await restored.procedures.initialize();
  assert.equal(restored.procedures.snapshot().receipts[0]!.status, 'stale');
  const exec = f.sandbox.exec.bind(f.sandbox);
  f.sandbox.exec = async (...args) => args[0] === 'bash agent-tests/run-tests.sh --control'
    ? {code: 1, stdout: '', stderr: 'Broken setup fixture', timedOut: false, durationMs: 1} : exec(...args);
  const result = await restored.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
  assert.equal(result.controlPassed, false); assert.equal(restored.procedures.snapshot().receipts[0]!.status, 'failed');
});

test('bundle receipts distinguish observed test failures from setup errors and never certify quality', async t => {
  const f = await runtime(); t.after(f.cleanup); const tools = f.make();
  for (const [command, outcome] of [
    ["node -e 'console.log(7)'", 'suite-pass'],
    ["node -e 'require(\"assert\").equal(7,8)'", 'suite-fail'],
    ['node agent-tests/missing-fixture.js', 'execution-error'],
  ]) {
    const result = await tools.execute('write_test_suite', {files: [{path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\n' + command + '\n'}]}, f.signal);
    const receipt = result.procedure as Record<string, unknown>;
    assert.equal(receipt.outcome, outcome); assert.equal(receipt.qualityVerified, false);
    assert.equal(receipt.status, outcome === 'execution-error' ? 'failed' : 'completed');
  }
  assert.ok(tools.procedures.snapshot().receipts.length <= 4);
});

test('corrupt checkpoints fail closed for ownership and disabled resource profiles expose no procedure state', async t => {
  const f = await runtime(); t.after(f.cleanup);
  await fs.mkdir(path.dirname(f.checkpoint), {recursive: true}); await fs.writeFile(f.checkpoint, '{partial');
  const tools = f.make(); await tools.procedures.initialize(); assert.equal(tools.procedures.snapshot().receipts.length, 0);
  const disabled = new Toolbox(f.sandbox, f.store, 10000, 5000, false, skillIds, false);
  await disabled.procedures.initialize(); assert.equal(disabled.procedures.render(), '');
  const result = await disabled.execute('load_skill', {id: 'artifact-workflow'}, f.signal);
  assert.deepEqual(result.procedures, []);
});

test('overlapping preparations cannot interleave ownership journals and all procedure events carry session identity', async t => {
  const f = await runtime(); t.after(f.cleanup); const tools = f.make();
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  t.after(() => release.resolve());
  const exec = f.sandbox.exec.bind(f.sandbox);
  f.sandbox.exec = async (...args) => { entered.resolve(); await release.promise; return exec(...args); };
  const first = tools.execute('prepare_test_harness', {framework: 'unittest'}, f.signal);
  await entered.promise;
  await assert.rejects(tools.execute('prepare_test_harness', {framework: 'unittest'}, f.signal), /already running/);
  release.resolve(); await first;
  assert.equal(tools.procedures.snapshot().receipts[0]!.attempt, 1);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  const stages = events.filter(event => event.type.startsWith('skills.procedure.'));
  assert.ok(stages.some(event => event.type === 'skills.procedure.completed'));
  assert.ok(stages.every(event => event.data.session === 'candidate' && event.data.workspace === f.sandbox.id));
});
