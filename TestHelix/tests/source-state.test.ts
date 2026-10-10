import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {LocalFactory, applyFiles} from '../src/runtime.js';
import {captureCandidate, captureWorkspaceCandidate} from '../src/artifacts.js';
import {discoverSourceChanges} from '../src/source-state.js';
import {runTask} from '../src/pipeline.js';
import {config, fixture, FixtureModel, completion, repairs, suites, buggy} from './helpers.js';
import type {Model} from '../src/types.js';
import {shellQuote} from '../src/util.js';

const identity = {id: 'capture', lineage: 'capture', round: 0, strategy: '', sessionId: 'capture'};
const declaration = {requirements: ['public behavior'], rationale: 'fixture'};

test('ignored legacy path hints cannot reject a valid submission or grant source reads outside the discovered inventory', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.writeFile(path.join(f.root, 'private'), 'PRIVATE_OUTSIDE_MARKER');
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), sandbox = await factory.fork('candidate');
  await sandbox.write('prime.cjs', Buffer.from(repairs[0]!));
  await sandbox.write('agent-tests/check.cjs', Buffer.from(suites[0]!));
  await sandbox.write('agent-tests/run-tests.sh', Buffer.from('node agent-tests/check.cjs\n'), 0o755);
  const hints = {...declaration, sourcePaths: ['agent-tests/check.cjs', '../private', 'solution.sh'],
    deletedSourcePaths: ['agent-tests/run-tests.sh']};
  const candidate = await captureWorkspaceCandidate(sandbox, baseline, hints, identity, 4_000_000);
  assert.deepEqual(candidate.source.map(file => file.path), ['prime.cjs']);
  assert.equal(candidate.tests.length, 2);
  assert.doesNotMatch(JSON.stringify(candidate), /PRIVATE_OUTSIDE_MARKER/);
  // Actual source artifacts still use the strict separation guard.
  await assert.rejects(captureCandidate(sandbox, baseline, hints, identity, 4_000_000), /Source and tests must be separate/);
});

test('host captures byte, mode, add/delete and shell changes without Git or model path declarations', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.writeFile(path.join(f.workspace, 'remove.txt'), 'old');
  await fs.writeFile(path.join(f.workspace, 'mode.sh'), 'echo unchanged\n', {mode: 0o644});
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), sandbox = await factory.fork('candidate');
  const awkward = "odd ' $(false)\nname.cjs";
  const execution = await sandbox.exec(`printf %s ${shellQuote(repairs[0]!)} > prime.cjs\nrm remove.txt\nchmod 755 mode.sh\nprintf %s changed > ${shellQuote(awkward)}\nmkdir -p __pycache__ .git agent-tests\nprintf ignored > __pycache__/cache.pyc\nprintf excluded > .git/secret\nprintf excluded > solution.sh`, 3000);
  assert.equal(execution.code, 0, execution.stderr);
  await sandbox.write('agent-tests/check.cjs', Buffer.from(suites[0]!));
  await sandbox.write('agent-tests/run-tests.sh', Buffer.from('node agent-tests/check.cjs\n'), 0o755);
  const candidate = await captureWorkspaceCandidate(sandbox, baseline, declaration, identity, 4_000_000);
  assert.deepEqual(candidate.source.map(file => file.path).sort(), ['mode.sh', awkward, 'prime.cjs', 'remove.txt'].sort());
  assert.equal(candidate.source.find(file => file.path === 'remove.txt')?.deleted, true);
  assert.equal(candidate.source.find(file => file.path === 'mode.sh')?.mode, 0o755);
  const fresh = await factory.fork('replay');
  await applyFiles(fresh, [...candidate.source, ...candidate.tests]);
  assert.equal((await fresh.exec('bash agent-tests/run-tests.sh', 3000)).code, 0);
  assert.equal((await baseline.read('prime.cjs')).toString(), buggy);
  // Restoring bytes must remove a former change; mtime alone is not evidence.
  await sandbox.write('prime.cjs', Buffer.from(buggy));
  assert.ok(!(await discoverSourceChanges(sandbox, baseline)).sourcePaths.includes('prime.cjs'));
});

test('unchanged symlinks are never traversed; changed symlinks cannot become partial source witnesses', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.writeFile(path.join(f.root, 'outside'), 'private data');
  await fs.symlink(path.join(f.root, 'outside'), path.join(f.workspace, 'outside-link'));
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), sandbox = await factory.fork('candidate');
  assert.deepEqual(await discoverSourceChanges(sandbox, baseline), {sourcePaths: [], deletedSourcePaths: []});
  await fs.symlink('prime.cjs', path.join(sandbox.root, 'new-link'));
  await assert.rejects(discoverSourceChanges(sandbox, baseline), /symbolic link/);
});

test('truncated inventories and excessive changes are rejected rather than sealed partially', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), sandbox = await factory.fork('candidate');
  await discoverSourceChanges(sandbox, baseline);
  const execute = sandbox.exec.bind(sandbox);
  sandbox.exec = async (...args) => ({...await execute(...args), truncated: true});
  await assert.rejects(discoverSourceChanges(sandbox, baseline), /complete public source inventory/);
  sandbox.exec = execute;
  for (let index = 0; index < 41; index++) await sandbox.write(`new-${index}`, Buffer.from('data'));
  await assert.rejects(discoverSourceChanges(sandbox, baseline), /More than 40/);
});

test('source capture survives a runtime diagnostic output cap without enlarging model output', async t => {
  const f = await fixture(); t.after(f.cleanup);
  for (let i = 0; i < 20; i++) await fs.writeFile(path.join(f.workspace, `source-${i}-${'x'.repeat(80)}.txt`), 'unchanged');
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), sandbox = await factory.fork('candidate');
  for (const branch of [baseline, sandbox]) {
    const execute = branch.exec.bind(branch);
    branch.exec = async (...args) => {
      const result = await execute(...args);
      return {...result, stdout: result.stdout.slice(0, 512), stderr: result.stderr.slice(0, 512),
        truncated: result.truncated || result.stdout.length > 512 || result.stderr.length > 512};
    };
  }
  await sandbox.write('prime.cjs', Buffer.from(repairs[0]!));
  assert.deepEqual(await discoverSourceChanges(sandbox, baseline), {sourcePaths: ['prime.cjs'], deletedSourcePaths: []});
  assert.deepEqual(await sandbox.list('.testhelix-tmp'), []);
  assert.deepEqual(await baseline.list('.testhelix-tmp'), []);
});

test('omitted finalizers preserve both independent repairs and tests for fresh verification', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    const result = await delegate.complete(args);
    if ('submit_review' in args.tools) return result;
    return completion(result.calls.filter(call => call.name !== 'submit_candidate').map(call => [call.name, call.input]));
  }};
  const settings = config(); settings.search.agentSteps = 1;
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated', result.reason);
  assert.ok(result.candidates.every(candidate => candidate.source.length === 1));
  assert.equal(result.witnesses.length, 2);
  assert.equal(await fs.readFile(path.join(f.workspace, 'prime.cjs'), 'utf8'), buggy);
});

test('an unfinished repair remains recoverable but cannot authorize default provisional publication', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(); settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  settings.search.agentSteps = 1; settings.search.review = false;
  const model: Model = {complete: async () => completion([
    ['write_file', {path: 'prime.cjs', content: 'exports.isPrime = () => true;\n'}],
    ['write_test_suite', {files: [{path: 'agent-tests/check.cjs', content: suites[0]},
      {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}]}],
  ])};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.candidates[0]?.source.length, 1);
  assert.equal(result.matrix.find(cell => cell.sourceId === result.candidates[0]?.id)?.status, 'fail');
  assert.equal(result.status, 'no_submit'); assert.equal(result.witnesses.length, 0);
});

test('interrupted correction captures updated source instead of reverting to the sealed failed parent', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(); settings.search.agentSteps = 1; settings.search.repairSteps = 1; settings.search.maxRounds = 2;
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    if ('submit_review' in args.tools) return delegate.complete(args);
    const result = await delegate.complete(args);
    if (JSON.stringify(args.messages).includes('Your previous source and tests are still present'))
      return completion(result.calls.filter(call => call.name !== 'submit_candidate').map(call => [call.name, call.input]));
    const source = result.calls.find(call => call.name === 'write_file' && (call.input as any).path === 'prime.cjs')!;
    (source.input as any).content = 'exports.isPrime = () => true;\n';
    return result;
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated', result.reason);
  assert.ok(result.candidates.every(candidate => candidate.round === 1));
});

test('a qualified independent repair may support tests whose own source hypothesis failed', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(); settings.search.review = false;
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    const result = await delegate.complete(args);
    if (!JSON.stringify(args.messages).includes('Independently solve')) {
      const source = result.calls.find(call => call.name === 'write_file' && (call.input as any).path === 'prime.cjs')!;
      (source.input as any).content = 'exports.isPrime = () => true;\n';
    }
    return result;
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated');
  assert.deepEqual(result.selected, ['candidate-1-r0']);
  assert.deepEqual(result.witnesses, ['candidate-2-r0']);
  assert.ok(result.matrix.some(cell => cell.testId === 'candidate-1-r0' && cell.sourceId === 'candidate-1-r0' && cell.status === 'fail'));
  assert.ok(result.matrix.some(cell => cell.testId === 'candidate-1-r0' && cell.sourceId === 'candidate-2-r0' && cell.status === 'pass'));
});

test('unreplayable source changes recover usable tests without silently exporting a partial repair', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(); settings.search.review = false; settings.search.agentSteps = 1;
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  const model: Model = {complete: async () => completion([
    ['write_file', {path: 'prime.cjs', content: repairs[0]}],
    ['exec', {command: 'ln -s prime.cjs shortcut'}],
    ['write_test_suite', {files: [{path: 'agent-tests/check.cjs', content: suites[0]},
      {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}]}],
  ])};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'no_submit'); assert.equal(result.candidates[0]?.source.length, 0);
  assert.match(result.candidates[0]!.rationale, /tests-only candidate/);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).split('\n').filter(Boolean).map(line => JSON.parse(line));
  assert.ok(events.some(event => JSON.stringify(event).includes('sourceError') && JSON.stringify(event).includes('symbolic link')));
});
