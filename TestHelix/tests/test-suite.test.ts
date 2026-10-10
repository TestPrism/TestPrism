import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {fixture, repairs, suites, config, completion} from './helpers.js';
import {Toolbox} from '../src/tools.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {SkillSession} from '../src/skill-session.js';
import {skillIds} from '../src/skills.js';
import {runTask} from '../src/pipeline.js';

async function setup() {
  const f = await fixture(), sandbox = new LocalSandbox(f.workspace, false), store = new Store(f.out); await store.init();
  return {...f, sandbox, store, tools: new Toolbox(sandbox, store, 8000, 3000), signal: AbortSignal.timeout(20000)};
}
const runner = {path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nset -eu\nnode agent-tests/check.cjs\n'};
const check = {path: 'agent-tests/check.cjs', content: suites[0]};

test('suite tool writes all files before execution, preserves failing tests and reuses the runner after correction', async t => {
  const f = await setup(); t.after(f.cleanup);
  const failing = await f.tools.execute('write_test_suite', {files: [runner, check]}, f.signal);
  assert.equal(failing.written, true); assert.equal(failing.executed, true);
  assert.equal((failing.execution as any).status, 'fail'); assert.match((failing.execution as any).stderr, /AssertionError/);
  assert.equal(await f.sandbox.mode(runner.path), 0o755);
  await f.tools.execute('write_file', {path: 'prime.cjs', content: repairs[0]}, f.signal);
  const passing = await f.tools.execute('write_test_suite', {files: [check]}, f.signal);
  assert.equal((passing.execution as any).status, 'pass');
  const reviewer = new Toolbox(f.sandbox, f.store, 8000, 3000, true);
  await assert.rejects(reviewer.execute('write_test_suite', {files: [runner, check]}, f.signal), /read-only/);
});

test('failed partial bundle write restores bytes and modes, removes new files and never starts the runner', async t => {
  const f = await setup(); t.after(f.cleanup);
  const before = '#!/bin/sh\ntouch should-not-exist\n';
  await f.sandbox.write(runner.path, Buffer.from(before), 0o640);
  const write = f.sandbox.write.bind(f.sandbox); let failOnce = true;
  f.sandbox.write = async (file, bytes, mode) => {
    await write(file, bytes, mode);
    if (file === runner.path && failOnce) {failOnce = false; throw new Error('injected partial write failure');}
  };
  await assert.rejects(f.tools.execute('write_test_suite', {files: [check, runner]}, f.signal), /partial write/);
  assert.equal((await f.sandbox.read(runner.path)).toString(), before); assert.equal(await f.sandbox.mode(runner.path), 0o640);
  await assert.rejects(f.sandbox.read(check.path), /ENOENT/);
  await assert.rejects(f.sandbox.read('should-not-exist'), /ENOENT/);
});

test('bundle validates the complete path group and runner before changing any files', async t => {
  const f = await setup(); t.after(f.cleanup);
  for (const files of [[check], [runner, {path: 'prime.cjs', content: 'bad'}],
    [runner, {...runner, path: './agent-tests/run-tests.sh'}], [runner, {path: 'agent-tests/run-tests.sh/child', content: 'bad'}]]) {
    await assert.rejects(f.tools.execute('write_test_suite', {files}, f.signal));
    assert.deepEqual(await f.sandbox.list('agent-tests'), []);
  }
  await fs.mkdir(path.join(f.workspace, 'agent-tests'));
  await fs.symlink(path.join(f.workspace, 'prime.cjs'), path.join(f.workspace, 'agent-tests/linked.cjs'));
  await assert.rejects(f.tools.execute('write_test_suite', {files: [runner, {path: 'agent-tests/linked.cjs', content: 'bad'}]}, f.signal), /symlinks/);
  await assert.rejects(f.sandbox.read(runner.path), /ENOENT/);
});

test('bundle reports syntax and empty discovery as setup errors while retaining correctable files', async t => {
  const f = await setup(); t.after(f.cleanup);
  const syntax = await f.tools.execute('write_test_suite', {files: [{...runner, content: 'if then\n'}]}, f.signal);
  assert.equal(syntax.executed, false); assert.equal((syntax.execution as any).status, 'error');
  const empty = await f.tools.execute('write_test_suite', {files: [{...runner, content: "printf 'Ran 0 tests in 0.00s\\n\\nOK\\n'\n"}]}, f.signal);
  assert.equal((empty.execution as any).code, 0); assert.equal((empty.execution as any).status, 'error');
  const state = new SkillSession(skillIds, false, {instruction: 'test a public function', source: 'local', routes: []});
  state.observe('write_test_suite', {}, empty, 'a'.repeat(64));
  assert.equal(state.render().phase, 'repair'); assert.ok(state.render().ids.includes('failure-triage'));
});

test('one bundle response can execute and export tests through the actual pipeline', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.provisionalPolicy = 'baseline-only';
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.maxRounds = 1; settings.search.review = false;
  let calls = 0;
  const result = await runTask(f.task, settings, f.out, {model: {complete: async () => {
    calls++; return completion([['write_test_suite', {files: [runner, check]}],
      ['submit_candidate', {sourcePaths: [], requirements: ['prime classification'], rationale: 'independent regression suite'}]]);
  }}});
  assert.equal(calls, 1); assert.equal(result.status, 'provisional'); assert.equal(result.candidates[0]!.tests.length, 2);
  assert.equal(result.candidates[0]!.source.length, 0);
});
