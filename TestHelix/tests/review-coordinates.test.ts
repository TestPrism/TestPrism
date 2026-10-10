import test from 'node:test';
import assert from 'node:assert/strict';
import {finalizeReview, numberedPublicTask, reviewWireSchema, ReviewEvidenceRequired} from '../src/review.js';
import {executionEvidence} from '../src/execution-evidence.js';
import {LocalSandbox} from '../src/runtime.js';
import {fixture, config, completion, FixtureModel} from './helpers.js';
import {runTask} from '../src/pipeline.js';
import {hash} from '../src/util.js';

const instruction = '## Public behavior\n\nThe `factory(extra=True)` call must return a value.\nPreserve literal `"a\\nb"` as documented.';
const failure = executionEvidence('TypeError: unexpected keyword extra').failures;
const check = {id: failure[0]!.id, cause: 'behavior', explanation: 'The documented keyword is rejected by the public operation.',
  evidence: [{path: 'agent-tests/check.py', startLine: 1}], taskLines: {startLine: 3, endLine: 4}};

test('task and file coordinates preserve exact Markdown and source escapes without model transcription', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const code = 'assert factory(extra=True) == "a\\nb"';
  await sandbox.write('agent-tests/check.py', Buffer.from(code + '\n'));
  const raw = {findings: [], coveredRequirements: ['keyword argument'], executionChecks: [check]};
  assert.deepEqual(reviewWireSchema.parse(raw), raw);
  const result = await finalizeReview(raw, sandbox, instruction, failure);
  assert.equal(result.accepted, true);
  assert.equal(result.executionChecks?.[0]?.taskQuote, instruction.split('\n').slice(2).join('\n'));
  assert.equal(result.executionChecks?.[0]?.evidence[0]?.quote, code);
  assert.equal(numberedPublicTask(instruction), instruction.split('\n').map((line, i) => `${i + 1}| ${line}`).join('\n'));
});

test('invalid or conflicting coordinates cannot waive failure attribution', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/check.py', Buffer.from('factory(extra=True)\n'));
  for (const variant of [
    {...check, taskLines: {startLine: 90}},
    {...check, taskLines: {startLine: 2}},
    {...check, taskQuote: 'A different unsupported public assertion.'},
    {...check, evidence: [{path: 'agent-tests/check.py', startLine: 30}]},
    {...check, cause: 'unclear'},
  ]) await assert.rejects(finalizeReview({findings: [], coveredRequirements: [], executionChecks: [variant]}, sandbox, instruction, failure), ReviewEvidenceRequired);
  await assert.rejects(finalizeReview({findings: [], coveredRequirements: [], executionChecks: [check]}, sandbox, undefined, failure), ReviewEvidenceRequired);
  const setup = await finalizeReview({findings: [], coveredRequirements: [], executionChecks: [{...check, cause: 'setup'}]}, sandbox, instruction, failure);
  assert.equal(setup.accepted, false);
});

test('public task coordinates support a justified veto while keeping generated files out of the public basis', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/check.py', Buffer.from('assert names == ["a", "b"]\n'));
  const finding = {severity: 'blocking', kind: 'unsupported-assertion', requirement: 'Preserve permitted ordering.',
    explanation: 'The assertion requires one ordering although the task permits any ordering.', evidence: [{path: 'agent-tests/check.py', startLine: 1}],
    falseRejection: {allowedBehavior: 'Return the same names in reverse order.', whyAllowed: 'The public task explicitly allows any ordering.',
      controlAssessment: 'Membership is a supported control; this imposes an additional order.', publicBasis: {taskLines: {startLine: 1}}}};
  const raw = {findings: [finding], coveredRequirements: []};
  reviewWireSchema.parse(raw);
  const task = 'Matching names may be returned in any order.';
  const result = await finalizeReview(raw, sandbox, task);
  assert.equal(result.accepted, false); assert.equal(result.resolution, 'complete');
  assert.deepEqual(result.findings?.[0]?.falseRejection?.publicBasis, {taskLines: {startLine: 1}, taskQuote: task, excerptHash: hash(task)});
  await assert.rejects(finalizeReview({...raw, findings: [{...finding, falseRejection: {...finding.falseRejection,
    publicBasis: {repository: {path: 'agent-tests/check.py', startLine: 1}}}}]}, sandbox, task), ReviewEvidenceRequired);
});

test('advertised review protocol asks for coordinates and rejects redundant copied quotes', () => {
  const raw = {findings: [], coveredRequirements: [], executionChecks: [check]};
  assert.throws(() => reviewWireSchema.parse({...raw, executionChecks: [{...check, taskQuote: instruction}]}));
  assert.throws(() => reviewWireSchema.parse({...raw, executionChecks: [{...check,
    evidence: [{path: 'agent-tests/check.py', startLine: 1, quote: 'factory(extra=True)'}]}]}));
  assert.doesNotThrow(() => reviewWireSchema.parse({...raw, executionChecks: [{...check, taskLines: {startLine: 1, endLine: 41}}]}));
  assert.throws(() => reviewWireSchema.parse({...raw, executionChecks: [{...check, taskLines: {startLine: 41, endLine: 1}}]}));
});

test('wide citations bind the entire range while presenting a short quote; stale and out-of-range citations still fail', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const content = Array.from({length: 80}, (_, i) => `Line ${i + 1}: documented behavior and independent assertion.`).join('\n');
  await sandbox.write('agent-tests/check.py', Buffer.from(content));
  const finding = {severity: 'blocking', kind: 'setup', requirement: 'A working test fixture.',
    explanation: 'The fixture needs repair.', evidence: [{path: 'agent-tests/check.py', startLine: 1, endLine: 80}]};
  const raw = {findings: [finding], coveredRequirements: []}; reviewWireSchema.parse(raw);
  const result = await finalizeReview(raw, sandbox, instruction);
  const cited = result.findings![0]!.evidence![0]!;
  assert.equal(cited.excerptHash, hash(content)); assert.equal(cited.excerptChars, content.length);
  assert.equal(cited.quote!.length, 800); assert.equal(result.accepted, false);
  for (const evidence of [{...cited, endLine: 81}, {...cited, excerptHash: '0'.repeat(64)}, {...cited, excerptChars: 2}])
    await assert.rejects(finalizeReview({...raw, findings: [{...finding, evidence: [evidence]}]}, sandbox, instruction), /verified evidence/);
});

test('pipeline reviewer receives the full numbered contract and completes a review with the same call budget', async t => {
  const f = await fixture(); t.after(f.cleanup); const delegate = new FixtureModel(); let reviewed = 0;
  const task = {...f.task, instruction: f.task.instruction + '\n\nPreserve documented `prime` behavior.'};
  const result = await runTask(task, config(), f.out, {model: {complete: async args => {
    if (!args.tools.submit_review) return delegate.complete(args);
    reviewed++;
    assert.ok(JSON.stringify(args.messages[0]).includes(numberedPublicTask(task.instruction).replaceAll('\n', '\\n')));
    assert.match(args.tools.submit_review.description, /taskLines/);
    return completion([['submit_review', {findings: [], coveredRequirements: ['prime classification']}]]);
  }}});
  assert.equal(reviewed, 2); assert.equal(result.status, 'validated'); assert.equal(result.usage.calls, 4);
});
