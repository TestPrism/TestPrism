import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { runTask } from '../src/pipeline.js';
import { config, fixture, FixtureModel, buggy, completion, suites, repairs } from './helpers.js';
import {LocalFactory, type SandboxFactory} from '../src/runtime.js';
import type { Model } from '../src/types.js';
import { command } from '../src/util.js';

test('rejection reopens review for independently supported alternatives in the same round within the existing budget', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(); settings.search.initialCandidates = 4; settings.search.maxCandidates = 4;
  settings.search.allowProvisional = false; settings.budget.maxCalls = 12;
  let reviews = 0;
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) {
      reviews++;
      const good = text.includes('Claimed coverage: classify integers\\n');
      return completion([['submit_review', {findings: good ? [] : [{severity: 'blocking', kind: 'unsupported-assertion',
        explanation: 'The assertion incorrectly requires 4 to be prime.', requirement: 'Classify nonnegative integers.',
        evidence: [{path: 'agent-tests/check.cjs', startLine: 1}], falseRejection: {
          allowedBehavior: 'Return false for the composite integer 4.',
          whyAllowed: 'The public task requires whether each nonnegative integer is prime.',
          controlAssessment: 'Expecting true for 4 rejects correct composite classification.',
          publicBasis: {taskLines: {startLine: 1}},
        }}], coveredRequirements: good ? ['classify integers'] : []}]]);
    }
    const variant = text.includes('Independently solve') ? 1 : text.includes('Challenge likely') ? 2 : text.includes('Look for alternative') ? 3 : 0;
    const good = variant % 2 === 1;
    return completion([
      ['write_file', {path: 'prime.cjs', content: good ? repairs[variant === 1 ? 0 : 1] : `exports.isPrime=n=>n!==1; // ${variant}\n`}],
      ['write_file', {path: 'agent-tests/check.cjs', content: good ? suites[variant === 1 ? 0 : 1]
        : "const a=require('node:assert/strict'),{isPrime}=require('../prime.cjs');a.equal(isPrime(1),false);a.equal(isPrime(4),true);\n"}],
      ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
      ['submit_candidate', {requirements: good ? ['classify integers'] : ['1 is not prime', 'classify integers'], rationale: 'fixture'}],
    ]);
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated', result.reason);
  assert.deepEqual([...result.selected].sort(), ['candidate-2-r0', 'candidate-4-r0']);
  assert.equal(reviews, 3, 'duplicate test bytes share one rejection; both new winners are reviewed');
  assert.equal(result.usage.calls, 7); assert.ok(result.usage.calls <= settings.budget.maxCalls);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).split('\n').filter(Boolean).map(line => JSON.parse(line));
  assert.equal(events.filter(e => e.type === 'validation.cell' && e.data.fresh).length, 3);
});

test('an unresolved review is attempted once per round instead of looping until tokens are exhausted', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  let reviews = 0; const delegate = new FixtureModel();
  const result = await runTask(f.task, settings, f.out, {model: {complete: async args => {
    if (!('submit_review' in args.tools)) return delegate.complete(args);
    reviews++;
    return completion([['submit_review', {findings: [{severity: 'blocking', kind: 'unsupported-assertion',
      explanation: 'Unsubstantiated rejection without a counterexample.', requirement: 'primality',
      evidence: [{path: 'agent-tests/check.cjs', startLine: 1}]}], coveredRequirements: []}]]);
  }}});
  assert.equal(reviews, 4); assert.equal(result.usage.calls, 5);
  assert.equal(result.status, 'no_submit');
  assert.equal(Object.values(result.reviews)[0]?.resolution, 'needs-evidence');
});

test('final provisional publication cannot reuse an earlier baseline failure or accept a different failure', async t => {
  for (const code of [0, 1]) {
    const f = await fixture(); t.after(f.cleanup); const settings = config();
    settings.search.provisionalPolicy = 'baseline-only';
    settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.review = false;
    const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
    let cells = 0;
    const factory: SandboxFactory = {fork: async label => {
      const sandbox = await base.fork(label);
      if (label === 'cell' && ++cells > settings.execution.repeat) {
        const exec = sandbox.exec.bind(sandbox);
        sandbox.exec = async (script, timeout, signal) => script === 'bash agent-tests/run-tests.sh'
          ? {code, stdout: 'AssertionError: unrelated failure after qualification', stderr: '', timedOut: false, truncated: false, durationMs: 1}
          : exec(script, timeout, signal);
      }
      return sandbox;
    }, publish: files => base.publish(files), close: () => base.close()};
    const result = await runTask(f.task, settings, f.out, {factory, model: {complete: async () => completion([
      ['write_file', {path: 'agent-tests/check.cjs', content: suites[0]}],
      ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
      ['submit_candidate', {requirements: ['primality'], rationale: 'tests only'}],
    ])}});
    assert.equal(result.status, 'no_submit'); assert.equal(cells, 4); assert.equal(result.usage.calls, 1);
    assert.match(result.reason, /Final fresh baseline/);
    await assert.rejects(fs.stat(path.join(f.workspace, 'agent-tests')));
  }
});

test('final submission remains a mutation barrier after removing the secondary tool loop', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.provisionalPolicy = 'baseline-only';
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.review = false;
  const result = await runTask(f.task, settings, f.out, {model: {complete: async () => completion([
    ['write_file', {path: 'agent-tests/check.cjs', content: suites[0]}],
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
    ['submit_candidate', {requirements: ['primality'], rationale: 'fixture'}],
    ['exec', {command: 'touch agent-tests/should-not-exist'}],
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'exit 0\n'}],
  ])}});
  assert.equal(result.status, 'provisional'); assert.equal(result.usage.calls, 1);
  assert.equal(await fs.readFile(path.join(f.workspace, 'agent-tests/run-tests.sh'), 'utf8'), 'node agent-tests/check.cjs\n');
  await assert.rejects(fs.stat(path.join(f.workspace, 'agent-tests/should-not-exist')));
  const saved = JSON.parse(await fs.readFile(path.join(f.out, 'sessions/candidate-1.json'), 'utf8'));
  const messages = saved.messages.filter((message: {role: string}) => message.role === 'tool');
  assert.equal(messages.length, 1); assert.equal(messages[0].content.length, 5);
  for (const output of messages[0].content.slice(-2)) assert.match(output.output.value.error, /finalized/);
});

test('review cleanup failure drains active reviewers, saves completed decisions and prevents publication', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
  let active = 0, closed = false, published = false;
  const delegate = new FixtureModel();
  const factory: SandboxFactory = {fork: async label => {
    const sandbox = await base.fork(label);
    if (label === 'review-candidate-1-r0') {
      const close = sandbox.close.bind(sandbox);
      let failed = false;
      sandbox.close = async () => {
        await close();
        if (!failed) {failed = true; throw new Error('review cleanup failed');}
      };
    }
    return sandbox;
  }, publish: async () => {published = true;}, close: async () => {
    assert.equal(active, 0, 'no live reviewer may race factory cleanup');
    closed = true; await base.close();
  }};
  const model: Model = {complete: async args => {
    if (!('submit_review' in args.tools)) return delegate.complete(args);
    active++;
    try {
      await new Promise<void>(resolve => setTimeout(resolve, 20));
      assert.equal(closed, false);
      return await delegate.complete(args);
    } finally {active--;}
  }};
  await assert.rejects(runTask(f.task, settings, f.out, {factory, model}), /review cleanup failed/);
  assert.equal(closed, true); assert.equal(published, false);
  const checkpoint = JSON.parse(await fs.readFile(path.join(f.out, 'checkpoint.json'), 'utf8'));
  assert.equal(Object.keys(checkpoint.reviews).length, 2);
  await assert.rejects(fs.stat(path.join(f.out, 'result.json')));
});

test('end-to-end: independent repairs, cross execution, source-blind review, merged tests and tests-only publication', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const model = new FixtureModel(), settings = config();
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated', JSON.stringify(result.matrix));
  assert.equal(result.selected.length, 2); assert.equal(result.witnesses.length, 2);
  assert.equal(result.candidates.length, 2); assert.equal(model.reviewerCalls, 4);
  assert.equal(result.usage.calls, model.calls); assert.equal(result.usage.input, model.calls * 120);
  assert.equal(await fs.readFile(path.join(f.workspace, 'prime.cjs'), 'utf8'), buggy);
  assert.ok(await fs.stat(path.join(f.workspace, 'agent-tests/run-tests.sh')));
  assert.deepEqual(await fs.readdir(path.join(f.out, 'workspaces')), []);
  const execution = await command(['bash', 'agent-tests/run-tests.sh'], {cwd: f.workspace});
  assert.equal(execution.code, 1, 'published suite must fail on the unchanged baseline');
  const calls = model.calls;
  const resumed = await runTask(f.task, settings, f.out, {model, resume: true});
  assert.equal(model.calls, calls); assert.deepEqual(resumed, result);
  await assert.rejects(runTask({...f.task, instruction: 'Changed task'}, settings, f.out, {model, resume: true}), /identity mismatch/);
});

test('explicit baseline-only diagnostic exports a test-only suite without claiming correctness', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const model: Model = {complete: async () => completion([
    ['write_file', {path: 'agent-tests/check.cjs', content: suites[0]}],
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
    ['submit_candidate', {sourcePaths: [], requirements: ['1 is not prime'], rationale: 'Baseline regression without a repair'}],
  ])};
  const settings = config(); settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.review = false;
  settings.search.provisionalPolicy = 'baseline-only';
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'provisional'); assert.equal(result.witnesses.length, 0);
  assert.match(result.reason, /DIAGNOSTIC baseline-only/);
});

test('passing or broken runners cannot be submitted and stale agent-tests are removed', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.mkdir(path.join(f.workspace, 'agent-tests'));
  await fs.writeFile(path.join(f.workspace, 'agent-tests/run-tests.sh'), 'exit 0');
  const model: Model = {complete: async () => completion([
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'exit 0\n'}],
    ['submit_candidate', {sourcePaths: [], requirements: ['prime classification'], rationale: 'Bad fixture'}],
  ])};
  const settings = config(); settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.review = false;
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'no_submit'); assert.equal(result.testsDir, null);
  await assert.rejects(fs.stat(path.join(f.workspace, 'agent-tests')));
});

test('native replace mode sends the exact supplied system message and keeps workflow at user authority', async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.task.systemPrompt = 'Native benchmark instructions'; f.task.systemPromptMode = 'replace';
  const model = new FixtureModel(); await runTask(f.task, config(), f.out, {model});
  assert.ok(model.history.every(call => call.system === f.task.systemPrompt));
  assert.match(JSON.stringify(model.history[0]!.messages), /Agent workflow/);
});

test('correction continues the original sessions using execution summaries and replaces failed hypotheses', async t => {
  const f = await fixture(); t.after(f.cleanup); const delegate = new FixtureModel(); let continued = 0;
  const model: Model = {complete: async args => {
    if ('submit_review' in args.tools) return delegate.complete(args);
    const correcting = JSON.stringify(args.messages).includes('Your previous source and tests are still present');
    if (correcting) {
      continued++; assert.match(JSON.stringify(args.messages), /peerResults/);
      assert.ok(args.messages.some(message => message.role === 'tool'));
      return delegate.complete(args);
    }
    const reply = await delegate.complete(args);
    const sourceCall = reply.calls.find(call => call.name === 'write_file' && (call.input as any).path === 'prime.cjs')!;
    (sourceCall.input as any).content = JSON.stringify(args.messages).includes('Independently solve')
      ? 'exports.isPrime = () => true;\n' : 'exports.isPrime = () => false;\n';
    return reply;
  }};
  const settings = config(); settings.search.maxRounds = 2;
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(continued, 2); assert.equal(result.status, 'validated');
  assert.ok(result.candidates.every(c => c.round === 1));
  assert.deepEqual(result.candidates.map(c => c.lineage).sort(), ['candidate-1', 'candidate-2']);
});

test('semantic rejection excludes an otherwise executable suite from both validated and provisional outputs', async t => {
  const f = await fixture(); t.after(f.cleanup); const delegate = new FixtureModel();
  const model: Model = {complete: async args => 'submit_review' in args.tools
    ? completion([['submit_review', {findings: [{severity: 'blocking', kind: 'missing-requirement',
      explanation: 'Fixture: an unresolved required public behavior.', requirement: 'Fixture public behavior.',
      evidence: [{path: 'agent-tests/check.cjs', startLine: 1}]}], coveredRequirements: []}]]) : delegate.complete(args)};
  const result = await runTask(f.task, config(), f.out, {model});
  assert.equal(result.status, 'no_submit'); assert.equal(result.testsDir, null);
  assert.ok(Object.values(result.reviews).every(review => !review.accepted));
});

test('evolved lessons are retrieved by public task relevance and injection is capped at two', async t => {
  const f = await fixture(); t.after(f.cleanup); const model = new FixtureModel(), settings = config();
  settings.search.review = false; settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  settings.search.lessons = [
    {id: 'a', triggers: ['prime'], text: 'LESSON_ALPHA'}, {id: 'b', triggers: ['prime'], text: 'LESSON_BETA'},
    {id: 'c', triggers: ['prime'], text: 'LESSON_GAMMA'}, {id: 'irrelevant', triggers: ['database'], text: 'LESSON_DATABASE'},
  ];
  await runTask(f.task, settings, f.out, {model});
  const prompt = JSON.stringify(model.history[0]!.messages);
  assert.match(prompt, /LESSON_ALPHA/); assert.match(prompt, /LESSON_BETA/);
  assert.doesNotMatch(prompt, /LESSON_GAMMA|LESSON_DATABASE/);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).split('\n').filter(Boolean).map(line => JSON.parse(line));
  assert.ok(events.some(event => JSON.stringify(event).includes('policy.offered')));
});

test('read-heavy generation stops before consuming the input budget needed to finish a source-blind review', async t => {
  const f = await fixture(); t.after(f.cleanup); let generationCalls = 0, reviewCalls = 0;
  const settings = config();
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.agentSteps = 10;
  settings.budget.maxInputTokens = 60000; settings.budget.concurrency = 1;
  const model: Model = {complete: async args => {
    if ('submit_review' in args.tools) {
      reviewCalls++;
      return completion([['submit_review', {findings: [], coveredRequirements: ['fixture']}]],
        {input: 1000, output: 100, cached: 0});
    }
    generationCalls++;
    return completion(generationCalls === 1 ? [
      ['write_file', {path: 'agent-tests/check.cjs', content: suites[0]}],
      ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
    ] : [['read_file', {path: 'prime.cjs'}]], {input: 10000, output: 100, cached: 0});
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.ok(generationCalls < 10); assert.equal(reviewCalls, 1);
  assert.equal(Object.values(result.reviews)[0]?.accepted, true);
  assert.ok(result.usage.input <= settings.budget.maxInputTokens);
  assert.equal(result.status, 'no_submit', 'a review alone does not provide an executable positive control');
});

test('each concurrent correction sees its actual last response when only one call per branch remains', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const delegate = new FixtureModel(); let finalCorrections = 0;
  const settings = config(); settings.budget.maxCalls = 4; settings.search.agentSteps = 1;
  settings.search.maxRounds = 2; settings.search.repairSteps = 4; settings.search.review = false;
  const model: Model = {complete: async args => {
    const correcting = JSON.stringify(args.messages).includes('Your previous source and tests are still present');
    if (correcting && 'read_file' in args.tools) return completion([['read_file', {path: 'prime.cjs'}]]);
    const result = await delegate.complete(args);
    if (correcting) finalCorrections++;
    else {
      const source = result.calls.find(call => call.name === 'write_file' && (call.input as any).path === 'prime.cjs')!;
      (source.input as any).content = 'exports.isPrime = () => true;\n';
    }
    return result;
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(finalCorrections, 2, 'both branches must be told their single remaining response is final');
  assert.equal(result.usage.calls, 4); assert.equal(result.status, 'validated');
});
