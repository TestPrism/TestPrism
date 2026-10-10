import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {executionEvidence} from '../src/execution-evidence.js';
import {finalizeReview, ReviewEvidenceRequired} from '../src/review.js';
import {LocalSandbox} from '../src/runtime.js';
import {runTask} from '../src/pipeline.js';
import {fixture, config, completion, suites} from './helpers.js';

const traceback = `Traceback (most recent call last):
  File "/workspace/agent-tests/cases/check.py", line 12, in test_round_trip
    helper = factory(extra=True)
  File "/workspace/lib/model.py", line 5, in __init__
    raise TypeError('unexpected keyword argument extra')
TypeError: unexpected keyword argument extra
`;

test('failure evidence retains actual exceptions through warning floods and mixed assertion failures', () => {
  const log = 'DeprecationWarning: old API\n'.repeat(2000) + traceback +
    'AssertionError: output differs\nRan 2 tests in 0.001s\nFAILED (failures=1, errors=1)\n';
  const evidence = executionEvidence(log);
  assert.equal(evidence.failures.length, 2); assert.equal(evidence.failures[0]!.exception, 'TypeError');
  assert.equal(evidence.failures[0]!.needsAttribution, true);
  assert.equal(evidence.failures[1]!.needsAttribution, false);
  assert.deepEqual(evidence.failures[0]!.frames.map(frame => frame.path), ['/workspace/agent-tests/cases/check.py', '/workspace/lib/model.py']);
  assert.equal(evidence.failures[0]!.frames[1]!.operation, "raise TypeError('unexpected keyword argument extra')");
  assert.ok(JSON.stringify(evidence).length < 1600);
  assert.deepEqual(executionEvidence(log + traceback).failures, evidence.failures, 'repeated stack evidence is deduplicated');
  const many = executionEvidence(Array.from({length: 25}, (_, i) => `TypeError: failed ${i}`).join('\n'));
  assert.equal(many.failures.length, 6); assert.equal(many.omitted, 19);
});

test('nested no-work output is an attribution symptom even when the outer runner executes tests', () => {
  const evidence = executionEvidence("AssertionError: exit 32, stdout='No files to lint: exiting.\\n'\nRan 2 tests\nFAILED (failures=2)");
  assert.ok(evidence.failures.some(failure => failure.exception === 'NoWorkObserved' && failure.needsAttribution));
  assert.deepEqual(executionEvidence('Ran 2 tests in 0.1s\nOK').failures, []);
});

test('acceptance must attribute a baseline exception; setup blocks and a missing required behavior remains admissible', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const statement = 'raise TypeError("unexpected keyword argument extra")';
  await sandbox.write('agent-tests/check.py', Buffer.from(statement + '\n'));
  const failures = executionEvidence(traceback).failures;
  const originalTask = 'The public factory must accept the documented extra keyword argument.';
  const review = {findings: [], coveredRequirements: ['factory keyword']};
  await assert.rejects(finalizeReview(review, sandbox, originalTask, failures), ReviewEvidenceRequired);
  const check = {id: failures[0]!.id, cause: 'behavior', explanation: 'The current factory rejects the required keyword before returning a value.',
    evidence: [{path: 'agent-tests/check.py', startLine: 1}], taskQuote: originalTask};
  const accepted = await finalizeReview({...review, executionChecks: [check]}, sandbox, originalTask, failures);
  assert.equal(accepted.accepted, true); assert.equal(accepted.executionChecks?.length, 1);
  const setup = await finalizeReview({...review, executionChecks: [{...check, cause: 'setup',
    explanation: 'The fixture helper rejects this keyword before calling the public factory.'}]}, sandbox, originalTask, failures);
  assert.equal(setup.accepted, false); assert.equal(setup.findings?.[0]?.kind, 'setup');
  for (const checks of [[{...check, taskQuote: 'Invented public task requirements.'}], [{...check, cause: 'unclear'}], [check, check]])
    await assert.rejects(finalizeReview({...review, executionChecks: checks}, sandbox, originalTask, failures), ReviewEvidenceRequired);
});

test('review interruption cannot publish unexplained baseline exceptions, and reviewer sees only baseline execution artifacts', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.maxRounds = 1;
  settings.model.maxRetries = 0;
  let sawEvidence = false;
  const result = await runTask(f.task, settings, f.out, {model: {complete: async args => {
    if ('submit_review' in args.tools) {
      const message = JSON.stringify(args.messages); assert.match(message, /Original-source failure evidence/);
      assert.match(message, /TypeError/); assert.doesNotMatch(message, /GENERATED_REPAIR_LOG_ONLY/); sawEvidence = true;
      throw new Error('injected reviewer interruption');
    }
    return completion([['write_file', {path: 'prime.cjs', content: 'process.stderr.write("GENERATED_REPAIR_LOG_ONLY\\n"); module.exports = () => true;\n'}], ['write_test_suite', {files: [
      {path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\nnode agent-tests/check.cjs\n'},
      {path: 'agent-tests/check.cjs', content: 'require("../prime.cjs"); throw new TypeError("fixture preparation failed");\n'},
    ]}], ['submit_candidate', {sourcePaths: [], requirements: ['public behavior'], rationale: 'synthetic test preparation fault'}]]);
  }}});
  assert.equal(sawEvidence, true); assert.equal(result.status, 'no_submit');
  assert.ok(Object.values(result.reviews).every(review => review.resolution === 'needs-evidence'));
  const baseline = result.matrix.find(cell => cell.sourceId === 'baseline')!;
  const artifacts = await fs.readdir(path.join(f.out, 'artifacts'));
  const reviewer = artifacts.find(name => name.startsWith('review-'))!;
  assert.equal(await fs.readFile(path.join(f.out, 'artifacts', reviewer, baseline.artifact + '.txt'), 'utf8'),
    await fs.readFile(path.join(f.out, 'artifacts', baseline.artifact + '.txt'), 'utf8'));
  const repaired = result.matrix.find(cell => cell.sourceId !== 'baseline')!;
  assert.match(repaired.output, /GENERATED_REPAIR_LOG_ONLY/);
  await assert.rejects(fs.readFile(path.join(f.out, 'artifacts', reviewer, repaired.artifact + '.txt')), /ENOENT/);
});

test('discovery remains available across artifact creation while publication still requires independent evidence', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  settings.search.maxRounds = 1; settings.search.review = false; settings.search.agentSteps = 6;
  let calls = 0, wrote = false, inspected = false;
  const result = await runTask(f.task, settings, f.out, {model: {complete: async args => {
    calls++;
    if (calls <= 3) { assert.ok(args.tools.read_file); return completion([['read_file', {path: 'prime.cjs', startLine: calls, endLine: calls + 1}]]); }
    if (calls === 4) {
      assert.ok(args.tools.read_file); assert.ok(args.tools.exec); wrote = true;
      return completion([['write_test_suite', {files: [
        {path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\nnode agent-tests/check.cjs\n'},
        {path: 'agent-tests/check.cjs', content: suites[0]},
      ]}]]);
    }
    assert.ok(args.tools.read_file); inspected = true;
    return completion([['submit_candidate', {sourcePaths: [], requirements: ['prime classification'], rationale: 'small executable regression'}]]);
  }}});
  assert.ok(wrote && inspected); assert.equal(calls, 5); assert.equal(result.candidates.length, 1);
  assert.equal(result.status, 'no_submit'); // Review is disabled; the default publication gate still applies.
});

test('a rejected publication opens one repair turn before retrying the candidate', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  settings.search.maxRounds = 1; settings.search.review = false; settings.search.agentSteps = 6;
  let calls = 0;
  const result = await runTask(f.task, settings, f.out, {model: {complete: async args => {
    calls++;
    if (calls === 1) return completion([['write_test_suite', {files: [
      {path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\nnode agent-tests/check.cjs\n'},
      {path: 'agent-tests/check.cjs', content: suites[0]},
    ]}]]);
    if (calls === 2) return completion([['submit_candidate', {replayScript: 'repair.sh', requirements: ['prime classification'], rationale: 'retry control'}]]);
    if (calls === 3) {
      assert.ok(args.tools.write_file);
      return completion([['write_file', {path: 'repair.sh', content: '#!/bin/sh\nset -eu\nprintf %s "exports.isPrime=n=>n>=2&&Array.from({length:Math.max(0,n-2)},(_,i)=>i+2).every(d=>n%d)" > prime.cjs\n'}]]);
    }
    return completion([['submit_candidate', {replayScript: 'repair.sh', requirements: ['prime classification'], rationale: 'captured retry control'}]]);
  }}});
  assert.equal(result.candidates.length, 1);
  assert.ok(calls >= 4);
  assert.equal(result.candidates[0]!.replayScript, 'repair.sh');
});
