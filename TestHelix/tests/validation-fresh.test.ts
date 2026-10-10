import test, {type TestContext} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {Validator, sameFailure} from '../src/validation.js';
import {Budget} from '../src/budget.js';
import {Store} from '../src/store.js';
import type {SandboxFactory} from '../src/runtime.js';
import type {Execution} from '../src/types.js';
import {config, fixture} from './helpers.js';

const files = [{path: 'agent-tests/run-tests.sh', mode: 493, data: Buffer.from('exit 1\n').toString('base64')}];
const execution = (code: number, stdout = 'AssertionError: 1 != 0'): Execution =>
  ({code, stdout, stderr: '', timedOut: false, truncated: false, durationMs: 1});
async function setup(t: TestContext, run: (index: number, script: string) => Promise<Execution>) {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(), store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.root, 'budget.json'), 'fresh', false);
  let forks = 0, closes = 0;
  const factory: SandboxFactory = {
    fork: async () => {
      const index = ++forks;
      return {id: String(index), root: '/workspace', read: async () => Buffer.alloc(0), mode: async () => 493,
        write: async () => {}, remove: async () => {}, list: async () => [],
        exec: script => run(index, script), close: async () => {closes++;}};
    },
    publish: async () => {}, close: async () => {},
  };
  return {factory, settings, store, budget, validator: new Validator(factory, settings, store, budget, 'fresh'),
    counts: () => ({forks, closes})};
}

test('final validation bypasses disk cache on every call and after resume without overwriting exploratory evidence', async t => {
  let code = 1;
  const f = await setup(t, async (_index, script) => execution(script.startsWith('bash -n') ? 0 : code));
  assert.equal((await f.validator.evaluate(files, [], 'candidate', 'baseline', 2)).status, 'fail');
  code = 0;
  assert.equal((await f.validator.evaluate(files, [], 'merged', 'baseline', 2, {fresh: true})).status, 'pass');
  assert.deepEqual(f.counts(), {forks: 4, closes: 4});
  assert.equal((await f.validator.evaluate(files, [], 'candidate', 'baseline', 2)).status, 'fail');
  const resumed = new Validator(f.factory, f.settings, f.store, f.budget, 'fresh');
  assert.equal((await resumed.evaluate(files, [], 'merged', 'baseline', 2, {fresh: true})).status, 'pass');
  assert.deepEqual(f.counts(), {forks: 6, closes: 6});
  assert.equal(f.budget.snapshot().calls, 0, 'fresh execution never buys a model response');
});

test('fresh validation never joins an in-flight exploratory run or another fresh invocation', async t => {
  let release!: () => void, entered!: () => void;
  const held = new Promise<void>(resolve => {release = resolve;});
  const started = new Promise<void>(resolve => {entered = resolve;});
  const f = await setup(t, async (index, script) => {
    if (script.startsWith('bash -n')) return execution(0);
    if (index === 1) {entered(); await held; return execution(1);}
    return execution(0);
  });
  const exploratory = f.validator.evaluate(files, [], 'candidate', 'baseline');
  await started;
  try {
    const finals = await Promise.all([1, 2].map(() => f.validator.evaluate(files, [], 'merged', 'baseline', 1, {fresh: true})));
    assert.ok(finals.every(result => result.status === 'pass'));
    assert.deepEqual(f.counts(), {forks: 3, closes: 2});
  } finally {release(); await exploratory;}
  assert.equal((await exploratory).status, 'fail');
  assert.deepEqual(f.counts(), {forks: 3, closes: 3});
});

test('timed-out syntax preparation never runs the test body and always closes its workspace', async t => {
  let bodies = 0;
  const f = await setup(t, async (_index, script) => {
    if (script.startsWith('bash -n')) return {...execution(0), timedOut: true};
    bodies++; return execution(1);
  });
  assert.equal((await f.validator.evaluate(files, [], 'candidate', 'baseline', 1, {fresh: true})).status, 'error');
  assert.equal(bodies, 0); assert.deepEqual(f.counts(), {forks: 1, closes: 1});
});

test('fresh validation enforces the wall deadline before creating another workspace', async t => {
  const f = await setup(t, async () => execution(1));
  f.settings.budget.wallMs = 1;
  await new Promise<void>(resolve => setTimeout(resolve, 5));
  assert.equal((await f.validator.evaluate(files, [], 'candidate', 'baseline', 2, {fresh: true})).status, 'timeout');
  assert.deepEqual(f.counts(), {forks: 0, closes: 0});
});

test('diagnostic variation remains rejected, but all full repeats and the first meaningful difference survive clipping', async t => {
  const f = await setup(t, async (index, script) => script.startsWith('bash -n') ? execution(0)
    : execution(1, 'common prefix\n'.repeat(400) + `AssertionError: /tmp/Case_${index}/Mixed != /tmp/case_${index}/mixed`));
  const result = await f.validator.evaluate(files, [], 'candidate', 'baseline', 2);
  assert.equal(result.status, 'error'); assert.equal(result.reproducibility?.kind, 'diagnostic-change');
  assert.equal(result.reproducibility?.runs.length, 2);
  assert.match(result.reproducibility!.firstDifference!.before, /Case_1\/Mixed/);
  assert.match(result.reproducibility!.firstDifference!.after, /Case_2\/Mixed/);
  for (const run of result.reproducibility!.runs) assert.match(await f.store.readArtifact(run.artifact!), /AssertionError:/);
  assert.equal(f.budget.snapshot().calls, 0); assert.deepEqual(f.counts(), {forks: 2, closes: 2});
});

test('the same located assertion can fail with different measurements without losing raw repeat evidence', async t => {
  const f = await setup(t, async (index, script) => script.startsWith('bash -n') ? execution(0)
    : execution(1, `Traceback (most recent call last):\n  File "/workspace/agent-tests/test_speed.py", line 8, in test_speed\n    self.assertLess(elapsed, 0.025)\nAssertionError: ${index * .1} not less than 0.025\nFAILED (failures=1)`));
  const suite = [...files, {path:'agent-tests/test_speed.py',mode:420,data:Buffer.from('public test fixture').toString('base64')}];
  const result = await f.validator.evaluate(suite, [], 'candidate', 'baseline', 2);
  assert.equal(result.status, 'fail');
  assert.equal(result.reproducibility?.sameAssertionSites, true);
  assert.notEqual(result.reproducibility!.runs[0]!.signature, result.reproducibility!.runs[1]!.signature);
  for (const run of result.reproducibility!.runs) assert.match(await f.store.readArtifact(run.artifact!), /not less than 0.025/);
  const final = await f.validator.evaluate(suite, [], 'candidate', 'baseline', 2, {fresh:true});
  assert.notEqual(final.signature, result.signature);
  assert.equal(sameFailure(result, final, '/workspace', suite), true);
  assert.equal(sameFailure(result, final, '/workspace', files), false);
  assert.equal(sameFailure(result, {...final, status:'pass'}, '/workspace', suite), false);
  assert.equal(sameFailure(result, {...final, diagnostics:undefined}, '/workspace', suite), false);
  assert.equal(f.budget.snapshot().calls,0);
});

test('changing assertion sites, unknown files and setup failures cannot use assertion repeat identity', async t => {
  for (const kind of ['site','unknown','setup']) {
    const f = await setup(t, async (index, script) => script.startsWith('bash -n') ? execution(0)
      : execution(1, `Traceback (most recent call last):\n  File "/workspace/agent-tests/${kind==='unknown'?'absent':'test_speed'}.py", line ${kind==='site'?index:8}, in test_speed\n    observe()\n${kind==='setup'?'ImportError':'AssertionError'}: value ${index}`));
    const suite = [...files,{path:'agent-tests/test_speed.py',mode:420,data:Buffer.from('public test fixture').toString('base64')}];
    assert.equal((await f.validator.evaluate(suite,[],'candidate','baseline',2)).status,'error');
  }
});

test('mixed failures retain exact exception diagnostics and attribution while timing assertions vary', async t=>{
  for(const changingException of [false,true]) {
    const f=await setup(t,async(index,script)=>script.startsWith('bash -n') ? execution(0) : execution(1,
      'Traceback (most recent call last):\n  File "/workspace/agent-tests/test_contract.py", line 4, in test_options\n    operate(options=True)\n'
      +`TypeError: unsupported option ${changingException?index:'options'}\n`
      +'Traceback (most recent call last):\n  File "/workspace/agent-tests/test_contract.py", line 9, in test_speed\n    self.assertLess(elapsed, limit)\n'
      +`AssertionError: elapsed ${index}\n`));
    const suite=[...files,{path:'agent-tests/test_contract.py',mode:420,data:Buffer.from('fixture').toString('base64')}];
    const result=await f.validator.evaluate(suite,[],'candidate','baseline',2);
    assert.equal(result.status,changingException?'error':'fail');
    assert.equal(result.diagnostics!.failures.find(failure=>failure.exception==='TypeError')!.needsAttribution,true);
    if(!changingException)assert.equal(result.reproducibility?.sameAssertionSites,true);
  }
});

test('changing outcomes are distinguished from diagnostic changes and never normalized to agreement', async t => {
  const f = await setup(t, async (index, script) => execution(script.startsWith('bash -n') || index === 2 ? 0 : 1));
  const result = await f.validator.evaluate(files, [], 'candidate', 'baseline', 2);
  assert.equal(result.status, 'error'); assert.equal(result.reproducibility?.kind, 'status-change');
  assert.deepEqual(result.reproducibility?.runs.map(run => run.status), ['fail', 'pass']);
});
