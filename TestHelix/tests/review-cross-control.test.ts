import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {reviewSelection} from '../src/review-stage.js';
import {createFactory} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {Budget} from '../src/budget.js';
import {hash} from '../src/util.js';
import type {Candidate, Observation, Review, Model} from '../src/types.js';
import {config, fixture, completion} from './helpers.js';

test('new masked fixture exceptions invalidate old acceptance once, without repair text or unbounded diagnostics', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = config();
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(c, path.join(f.out, 'budget.json'), 'cross-review-control', false);
  const factory = await createFactory(f.task, c, path.join(f.out, 'workspaces'));
  t.after(() => factory.close());
  const baseline = await factory.fork('baseline');
  const tests = [{path: 'agent-tests/check.cjs', mode: 0o644,
    data: Buffer.from("const {isPrime}=require('../prime.cjs');\nisPrime(2).missing.method();\n").toString('base64')}];
  const candidate: Candidate = {id: 'candidate-2-r0', lineage: 'candidate-2', round: 0, sessionId: 'candidate-2',
    strategy: 'independent', tests, source: [], sourceHash: hash([]), testsHash: hash(tests), requirements: ['primality'], rationale: 'fixture'};
  const matrix: Observation[] = [{testId: candidate.id, sourceId: 'baseline', status: 'fail', signature: 'baseline',
    durationMs: 1, output: 'AssertionError: original bug', diagnostics: {failures: [], omitted: 0, summary: []}},
  {testId: candidate.id, sourceId: 'candidate-1-r0', status: 'fail', signature: 'cross', durationMs: 1, output: 'PRIVATE_RAW_REPAIR_LOG',
    diagnostics: {failures: Array.from({length: 6}, (_, i) => ({id: String(i), exception: 'TypeError',
      message: 'PRIVATE_EXCEPTION_MESSAGE'.repeat(1000), needsAttribution: true,
      frames: [{path: '/testbed/agent-tests/check.cjs', line: 2, operation: 'PRIVATE_FAKE_TEST_OPERATION'},
        {path: '/testbed/prime.cjs', line: 1, operation: 'PRIVATE_SOURCE_EXCERPT'}]})), omitted: 0, summary: []}}];
  const reviews: Record<string, Review> = {[candidate.testsHash]: {accepted: true, resolution: 'complete',
    concerns: [], missingRequirements: [], coveredRequirements: ['primality']}};
  let calls = 0, observed = '';
  const model: Model = {complete: async args => {
    calls++; observed = JSON.stringify(args.messages);
    return completion([['submit_review', {findings: [{severity: 'blocking', kind: 'setup',
      explanation: 'The public boolean result does not offer missing.method().', requirement: 'Return a boolean primality result.',
      evidence: [{path: 'agent-tests/check.cjs', startLine: 2}]}], coveredRequirements: []}]]);
  }};
  await reviewSelection({candidates: [candidate], matrix, reviews, task: f.task, config: c, model, factory, baseline,
    budget, store, round: 0, checkpoint: async () => {}});
  assert.match(observed, /Cross-control exception symptoms/); assert.match(observed, /TypeError/);
  assert.match(observed, /check.cjs/); assert.doesNotMatch(observed, /PRIVATE_/);
  assert.ok(observed.length < 6000, 'diagnostic length does not scale with raw repair logs');
  assert.equal(calls, 1); assert.equal(reviews[candidate.testsHash]!.accepted, false);
  assert.equal(reviews[candidate.testsHash]!.resolution, 'complete');
  await reviewSelection({candidates: [candidate], matrix, reviews, task: f.task, config: c, model, factory, baseline,
    budget, store, round: 1, checkpoint: async () => {}});
  assert.equal(calls, 1, 'the same cross-control evidence must not buy another review');
});
