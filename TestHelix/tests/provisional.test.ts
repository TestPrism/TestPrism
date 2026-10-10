import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {runTask} from '../src/pipeline.js';
import {LocalFactory, type SandboxFactory} from '../src/runtime.js';
import type {Model} from '../src/types.js';
import {buggy, completion, config, fixture, FixtureModel, repairs, suites} from './helpers.js';

function writer(suite = suites[0], repair: string | undefined = repairs[0]): Model {
  const reviewer = new FixtureModel();
  return {complete: async args => 'submit_review' in args.tools ? reviewer.complete(args) : completion([
    ...(repair ? [['write_file', {path: 'prime.cjs', content: repair}] as [string, unknown]] : []),
    ['write_file', {path: 'agent-tests/check.cjs', content: suite}],
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
    ['submit_candidate', {requirements: ['primality'], rationale: 'synthetic publication control'}],
  ])};
}

test('default provisional gate rejects a stable wrong assertion even when a reviewer approves it', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  assert.equal(settings.search.provisionalPolicy, 'reviewed-positive');
  settings.search.initialCandidates = settings.search.maxCandidates = 1;
  const wrong = suites[0] + 'assert.equal(isPrime(4), true, "wrong assertion");\n';
  const result = await runTask(f.task, settings, f.out, {model: writer(wrong)});
  assert.equal(result.candidates.length, 1);
  assert.equal(Object.values(result.reviews)[0]?.accepted, true, 'execution must constrain mistaken semantic approval');
  assert.ok(result.matrix.some(cell => cell.sourceId !== 'baseline' && cell.status === 'fail'));
  assert.equal(result.status, 'no_submit'); assert.equal(result.testsDir, null);
  await assert.rejects(fs.stat(path.join(f.workspace, 'agent-tests')));
});

test('identical passing repairs plus approval remain provisional and both final controls bypass cache', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await runTask(f.task, config(), f.out, {model: writer()});
  assert.equal(result.candidates.length, 2);
  assert.equal(result.candidates[0]!.sourceHash, result.candidates[1]!.sourceHash);
  assert.equal(result.status, 'provisional'); assert.deepEqual(result.witnesses, []);
  assert.equal(await fs.readFile(path.join(f.workspace, 'prime.cjs'), 'utf8'), buggy);
  assert.equal(await fs.readFile(path.join(f.workspace, 'agent-tests/check.cjs'), 'utf8'), suites[0]);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  const fresh = events.filter(event => event.type === 'validation.cell' && event.data.fresh);
  assert.equal(fresh.length, 2);
  assert.deepEqual(fresh.map(event => event.data.status).sort(), ['fail', 'pass']);
});

test('a stale passing repair control cannot authorize provisional publication', async t => {
  for (const failure of ['fail', 'error'] as const) {
    const f = await fixture(); t.after(f.cleanup); const settings = config();
    settings.search.initialCandidates = settings.search.maxCandidates = 1;
    const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
    let positiveRuns = 0;
    const factory: SandboxFactory = {fork: async label => {
      const sandbox = await base.fork(label);
      if (label === 'cell') {
        const exec = sandbox.exec.bind(sandbox);
        sandbox.exec = async (script, timeout, signal) => {
          if (script === 'bash agent-tests/run-tests.sh'
            && await fs.readFile(path.join(sandbox.root, 'prime.cjs'), 'utf8') !== buggy
            && ++positiveRuns > settings.execution.repeat)
            return {code: failure === 'fail' ? 1 : 127, stdout: 'fresh control rejected', stderr: '',
              timedOut: false, truncated: false, durationMs: 1};
          return exec(script, timeout, signal);
        };
      }
      return sandbox;
    }, publish: files => base.publish(files), close: () => base.close()};
    const result = await runTask(f.task, settings, f.out, {model: writer(), factory});
    assert.equal(positiveRuns, settings.execution.repeat * 2);
    assert.equal(result.status, 'no_submit'); assert.match(result.reason, /Final fresh source repair control/);
    assert.ok(result.matrix.some(cell => cell.sourceId === result.candidates[0]!.id && cell.status === failure));
    await assert.rejects(fs.stat(path.join(f.workspace, 'agent-tests')));
  }
});

test('disabling review alone does not opt into baseline-only diagnostic publication', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.initialCandidates = settings.search.maxCandidates = 1; settings.search.review = false;
  const result = await runTask(f.task, settings, f.out, {model: writer()});
  assert.ok(result.matrix.some(cell => cell.sourceId !== 'baseline' && cell.status === 'pass'));
  assert.equal(result.status, 'no_submit'); assert.match(result.reason, /accepted semantic review/);
});
