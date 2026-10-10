import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { finalizeReview, ReviewEvidenceRequired } from '../src/review.js';
import { LocalSandbox } from '../src/runtime.js';
import { config, fixture, FixtureModel, completion } from './helpers.js';
import { runTask } from '../src/pipeline.js';
import { SkillSession } from '../src/skill-session.js';
import { skillIds } from '../src/skills.js';
import { configSchema, type Model } from '../src/types.js';
import { reviewTool, validateTool } from '../src/tools.js';

const task = 'Return all matching names. The order of returned names is unspecified.';
const disputed = {severity: 'blocking', kind: 'unsupported-assertion',
  requirement: 'Return matching names without imposing an order.',
  explanation: 'The suite requires alphabetical order despite the public contract allowing any order.',
  evidence: [{path: 'agent-tests/check.js', startLine: 1}]};
const proof = {allowedBehavior: 'Return the same matching names in reverse alphabetical order.',
  whyAllowed: 'The public task explicitly leaves result order unspecified.',
  controlAssessment: 'Comparing membership would be a control; comparing one exact ordering is an extra constraint.',
  publicBasis: {taskQuote: 'The order of returned names is unspecified.'}};

test('a quoted assertion alone requests evidence; an exact public basis enables a justified veto', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/check.js', Buffer.from('assert.deepEqual(actual, ["a", "b"]);\n'));
  await assert.rejects(finalizeReview({findings: [disputed], coveredRequirements: []}, sandbox, task), error => {
    assert.ok(error instanceof ReviewEvidenceRequired); assert.equal(error.review.accepted, false);
    assert.equal(error.review.resolution, 'needs-evidence'); return true;
  });
  const result = await finalizeReview({findings: [{...disputed, falseRejection: proof}], coveredRequirements: []}, sandbox, task);
  assert.equal(result.accepted, false); assert.equal(result.resolution, 'complete');
  const uncertain = await finalizeReview({findings: [{...disputed, severity: 'suggestion'}], coveredRequirements: []}, sandbox, task);
  assert.equal(uncertain.accepted, true);
});

test('invented task quotes, generated contract evidence and missing repository citations never settle a veto', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/check.js', Buffer.from('assert.deepEqual(actual, ["a", "b"]);\n'));
  for (const publicBasis of [{taskQuote: 'Invented task wording that does not exist.'},
    {repository: {path: 'agent-tests/check.js', startLine: 1}}, {repository: {path: 'missing.md', startLine: 1}}]) {
    await assert.rejects(finalizeReview({findings: [{...disputed, falseRejection: {...proof, publicBasis}}], coveredRequirements: []}, sandbox, task), ReviewEvidenceRequired);
  }
  await sandbox.write('PUBLIC.md', Buffer.from('Returned names may occur in any order.\n'));
  const result = await finalizeReview({findings: [{...disputed, falseRejection: {...proof,
    publicBasis: {repository: {path: 'PUBLIC.md', startLine: 1}}}}], coveredRequirements: []}, sandbox);
  assert.equal(result.accepted, false);
  const basis = result.findings?.[0]?.falseRejection?.publicBasis;
  assert.ok(basis && 'repository' in basis);
  assert.equal(basis.repository.path, 'PUBLIC.md');
  assert.equal(basis.repository.quote, 'Returned names may occur in any order.');
  assert.equal(basis.repository.excerptChars, basis.repository.quote.length);
  assert.match(basis.repository.excerptHash!, /^[a-f0-9]{64}$/);
});

test('ordinary setup blockers remain effective without a false rejection counterexample', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/check.js', Buffer.from('throw new SyntaxError("bad fixture");\n'));
  const result = await finalizeReview({findings: [{...disputed, kind: 'setup'}], coveredRequirements: []}, sandbox, task);
  assert.equal(result.accepted, false); assert.equal(result.resolution, 'complete');
  await assert.rejects(finalizeReview({findings: [
    {...disputed, kind: 'setup'}, disputed,
  ], coveredRequirements: []}, sandbox, task), ReviewEvidenceRequired,
  'an unrelated setup finding must not waive the public evidence required by an unsupported-assertion finding');
});

test('new tool calls cannot bypass evidence checks via the historical review shape', () => {
  assert.throws(() => validateTool(reviewTool, {accepted: false, concerns: ['Unsupported assertion'], missingRequirements: [], coveredRequirements: []}));
  assert.deepEqual(validateTool(reviewTool, {findings: [], coveredRequirements: []}), {findings: [], coveredRequirements: []});
});

test('review-specific control skill stays within context cap and cannot enter candidate evolution routes', () => {
  const reviewer = new SkillSession(skillIds, true, {instruction: task, source: 'local', routes: []});
  assert.ok(reviewer.render().ids.includes('control-evidence')); assert.ok(reviewer.render().chars <= 4400);
  const candidate = new SkillSession(skillIds, false, {instruction: task, source: 'local', routes: []});
  assert.ok(!candidate.offer.ids.includes('control-evidence'));
  assert.throws(() => configSchema.parse({...config(), search: {skills: skillIds,
    skillRoutes: [{context: 'general', skill: 'control-evidence'}]}}), /Reviewer-only/);
});

test('same reviewer can resolve an evidence request without discarding the unchanged candidate', async t => {
  const f = await fixture(); t.after(f.cleanup); const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    if (!('submit_review' in args.tools)) return delegate.complete(args);
    const finding = {...disputed, evidence: [{path: 'agent-tests/check.cjs', startLine: 1}],
      explanation: 'The task did not list all of the positive controls.'};
    if (!JSON.stringify(args.messages).includes('needs-evidence')) return completion([['submit_review', {findings: [finding], coveredRequirements: []}]]);
    assert.match(JSON.stringify(args.messages), /needs-evidence/);
    return completion([['submit_review', {findings: [{...finding, severity: 'suggestion', explanation: 'The controls are justified by the public mathematical contract; no demonstrated incompatibility.'}], coveredRequirements: ['prime classification']}]]);
  }};
  const result = await runTask(f.task, config(), f.out, {model});
  assert.equal(result.status, 'validated'); assert.equal(result.selected.length, 2);
  assert.ok(Object.values(result.reviews).every(review => review.accepted && review.resolution === 'complete'));
  assert.equal(result.usage.calls, 6, 'one initial generation and two review responses per candidate');
});

test('exhausted evidence requests remain unresolved and cannot escape through provisional publication', async t => {
  const f = await fixture(); t.after(f.cleanup); const delegate = new FixtureModel();
  const model: Model = {complete: async args => 'submit_review' in args.tools
    ? completion([['submit_review', {findings: [{...disputed, evidence: [{path: 'agent-tests/check.cjs', startLine: 1}]}], coveredRequirements: []}]])
    : delegate.complete(args)};
  const result = await runTask(f.task, config(), f.out, {model});
  assert.equal(result.status, 'no_submit'); assert.equal(result.selected.length, 0);
  assert.ok(Object.values(result.reviews).every(review => !review.accepted && review.resolution === 'needs-evidence'));
  assert.ok(result.usage.calls <= 20);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  assert.ok(events.some(event => event.type === 'review.needs_evidence'));
});
