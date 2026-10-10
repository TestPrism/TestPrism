import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {select} from '../src/selection.js';
import {finalizeReview} from '../src/review.js';
import {LocalSandbox} from '../src/runtime.js';
import {fixture} from './helpers.js';
import type {Candidate, Observation, Review} from '../src/types.js';

test('unstable executions receive reproducibility feedback without masking changed values or qualifying the test', () => {
  const candidate: Candidate = {id: 'c1', lineage: 'branch', sessionId: 'c1', round: 0, strategy: '', source: [], tests: [],
    sourceHash: 'empty', testsHash: 'tests', requirements: ['preserve path spelling'], rationale: ''};
  const output = 'Unstable results across fresh repeated runs:\nAssertionError: /tmp/Case_a != /tmp/case_a\nAssertionError: /tmp/Case_b != /tmp/case_b';
  const baseline: Observation = {testId: 'c1', sourceId: 'baseline', status: 'error', signature: 'unstable', output, durationMs: 1};
  const selected = select([candidate], [baseline], 1);
  const feedback = JSON.parse(selected.feedback.branch!);
  assert.match(feedback.nextStep, /before changing production code/);
  assert.match(feedback.nextStep, /preserving the exact behavioral assertions/);
  assert.equal(baseline.output, output); assert.equal(baseline.status, 'error');
  assert.equal(selected.tests.length, 0); assert.equal(selected.provisional, undefined);
  const unrelated = select([candidate], [{...baseline, output: 'SyntaxError: invalid generated test'}], 1);
  assert.doesNotMatch(JSON.parse(unrelated.feedback.branch!).nextStep, /random fixture paths/);
});

test('correction reuses verified attribution for the exact failure and test revision without weakening selection', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const instruction = 'Thing must accept the newly required new_arg keyword.';
  await fs.mkdir(path.join(f.workspace, 'agent-tests'));
  await fs.writeFile(path.join(f.workspace, 'agent-tests/check.py'), 'Thing(new_arg=1)\n');
  const failure = {id: '0123456789abcdef', exception: 'TypeError', message: 'unexpected keyword new_arg',
    frames: [{path: 'agent-tests/check.py', line: 1, operation: 'Thing(new_arg=1)'}], needsAttribution: true};
  const review = await finalizeReview({findings: [], coveredRequirements: ['new keyword'], executionChecks: [{
    id: failure.id, cause: 'behavior', explanation: 'The fixture reaches the newly required keyword on the public constructor.',
    taskLines: {startLine: 1, endLine: 1}, evidence: [{path: 'agent-tests/check.py', startLine: 1, endLine: 1}],
  }]}, new LocalSandbox(f.workspace, false), instruction, [failure]);
  const candidate: Candidate = {id: 'c1', lineage: 'branch', sessionId: 'c1', round: 0, strategy: '', source: [], tests: [],
    sourceHash: 'empty', testsHash: 'revision-one', requirements: ['new keyword'], rationale: ''};
  const baseline: Observation = {testId: candidate.id, sourceId: 'baseline', status: 'fail', signature: 'failure',
    output: failure.message, durationMs: 1, diagnostics: {failures: [failure], omitted: 0, summary: []}};
  const decide = (reviews: Record<string, Review>, observed = baseline) => select([candidate], [observed], 1, reviews);
  const selected = decide({[candidate.testsHash]: review}), feedback = JSON.parse(selected.feedback.branch!);
  assert.match(feedback.nextStep, /Implement and run a source hypothesis now/);
  assert.equal(selected.tests.length, 0); assert.equal(selected.witnesses.length, 0);
  const handoff = JSON.parse(feedback.review);
  assert.equal(handoff.executionChecks[0].cause, 'behavior');
  assert.equal(handoff.executionChecks[0].taskQuote, undefined);
  assert.equal(handoff.executionChecks[0].evidence, undefined);

  for (const reviews of [{}, {'old-revision': review}, {[candidate.testsHash]: {...review, accepted: false}},
    {[candidate.testsHash]: {...review, resolution: 'needs-evidence' as const}},
    {[candidate.testsHash]: {...review, executionChecks: []}}])
    assert.match(JSON.parse(decide(reviews).feedback.branch!).nextStep, /Attribute unresolved baseline exceptions/);
  const changed = {...baseline, diagnostics: {...baseline.diagnostics!, failures: [{...failure, id: 'fedcba9876543210'}]}};
  assert.match(JSON.parse(decide({[candidate.testsHash]: review}, changed).feedback.branch!).nextStep, /Attribute unresolved baseline exceptions/);
  for (const cause of ['setup', 'unclear'] as const) {
    const unsafe = {...review, executionChecks: review.executionChecks!.map(check => ({...check, cause}))};
    assert.match(JSON.parse(decide({[candidate.testsHash]: unsafe}).feedback.branch!).nextStep, /Attribute unresolved baseline exceptions/);
  }
});
