import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {runTask} from '../src/pipeline.js';
import {configSchema, type Model} from '../src/types.js';
import {config, fixture, FixtureModel, completion, repairs, suites} from './helpers.js';

function settings() {
  const c = config();
  c.search.schedule = 'staged'; c.search.initialCandidates = 1; c.search.maxRounds = 3;
  c.search.agentSteps = 14; c.search.repairSteps = 4; c.budget.maxCalls = 32; c.budget.concurrency = 1;
  return c;
}
function files(source: string, suite: string, seal = true) {
  const calls: Array<[string, unknown]> = [
    ['write_file', {path: 'prime.cjs', content: source}],
    ['write_file', {path: 'agent-tests/check.cjs', content: suite}],
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
  ];
  if (seal) calls.push(['submit_candidate', {requirements: ['primality'], rationale: 'public contract regression'}]);
  return calls;
}
const events = async (out: string) => (await fs.readFile(path.join(out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));

test('staged search preserves the full primary solve and admits an isolated peer only after executable and semantic qualification', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings();
  let primaryCalls = 0, primaryReviewed = false, secondStarted = false;
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) {
      assert.ok(text.includes('Host-read test previews'));
      if (!secondStarted) primaryReviewed = true;
      return delegate.complete(args);
    }
    if (text.includes('Independently solve')) {
      assert.equal(primaryReviewed, true); secondStarted = true;
      assert.doesNotMatch(text, /PRIMARY_PRIVATE_MARKER/);
      return delegate.complete(args);
    }
    primaryCalls++;
    if (primaryCalls === 1) return completion(files(repairs[0]! + '// PRIMARY_PRIVATE_MARKER\n', suites[0]!, false));
    if (!('read_file' in args.tools)) return completion([['submit_candidate', {requirements: ['primality'], rationale: 'finish complete solve'}]]);
    return completion([['read_file', {path: 'prime.cjs', startLine: primaryCalls}]]);
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(primaryCalls, 14); assert.equal(secondStarted, true);
  assert.equal(result.status, 'validated'); assert.equal(result.candidates.length, 2);
  assert.ok(result.usage.calls <= 32);
  const log = await events(f.out), initial = log.find(e => e.type === 'budget.allocation');
  assert.equal(initial.data.turns, 14);
  assert.ok(c.budget.maxInputTokens - initial.data.reserveForReview.input >= 175000);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.out, 'budget.json'), 'utf8')).pending, {});
});

test('staged search gives a reviewed baseline regression an independent repair after its first repair fails', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings();
  const delegate = new FixtureModel(); let challenged = false;
  const model: Model = {complete: async args => {
    if ('submit_review' in args.tools) return delegate.complete(args);
    const text = JSON.stringify(args.messages);
    if (text.includes('Independently solve')) {
      challenged = true;
      return completion(files(repairs[1]!, suites[0]!));
    }
    // The first hypothesis is intentionally wrong, while the suite still
    // reproduces the public defect on the unchanged baseline.
    return completion(files('exports.isPrime = () => true;\n', suites[0]!));
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(challenged, true);
  assert.equal(result.status, 'validated', result.reason);
  assert.deepEqual(result.selected, ['candidate-1-r0']);
  assert.deepEqual(result.witnesses, ['candidate-2-r0']);
  const log = await events(f.out);
  assert.ok(log.some(event => event.type === 'challenge.started'));
});

test('reviewed counterexamples grant actual test bytes after isolation and repair only the challenged source', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings(); const delegate = new FixtureModel();
  let correctionCalls = 0, sawReproducer = false;
  const strong = suites[1]! + 'assert.equal(isPrime(49), false, "square of seven");\n';
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) return delegate.complete(args);
    const second = text.includes('Independently solve');
    if (text.includes('Your previous source and tests are still present')) {
      assert.equal(second, false); correctionCalls++;
      if (!sawReproducer) {
        const messages = args.messages.filter(m => m.role === 'user').map(m => typeof m.content === 'string' ? m.content : '').join('\n');
        const grant = messages.match(/Reviewed counterexample suites[^\n]*: (\[[^\n]+\])/);
        assert.ok(grant, 'correction needs a runnable, host-granted counterexample');
        return completion([['read_artifact', {id: JSON.parse(grant[1]!)[0].artifact}]]);
      }
      return completion(files(repairs[0]!, suites[0]!));
    }
    return completion(files(second ? repairs[1]! : repairs[0]! + 'const original=exports.isPrime;exports.isPrime=n=>n===49||original(n);\n', second ? strong : suites[0]!));
  }};
  const wrapped: Model = {complete: args => {
    const text = JSON.stringify(args.messages);
    if (text.includes('Your previous source and tests are still present') && args.messages.some(message => message.role === 'tool'
      && typeof message.content !== 'string' && message.content.some(part => part.type === 'tool-result'
        && part.toolName === 'read_artifact' && JSON.stringify(part).includes('square of seven')))) sawReproducer = true;
    return model.complete(args);
  }};
  const result = await runTask(f.task, c, f.out, {model: wrapped});
  assert.equal(sawReproducer, true); assert.equal(correctionCalls, 2);
  assert.equal(result.status, 'validated'); assert.equal(result.selected.length, 2);
  assert.equal(result.candidates.find(x => x.lineage === 'candidate-2')!.round, 0);
  assert.ok(result.candidates.find(x => x.lineage === 'candidate-2')!.tests.some(x => Buffer.from(x.data, 'base64').toString() === strong));
});

test('unchanged failed correction is not repeated and cannot publish a weaker subset as validated', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings(); const delegate = new FixtureModel();
  let correctionCalls = 0;
  const strong = suites[1]! + 'assert.equal(isPrime(49), false);\n';
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) return delegate.complete(args);
    const second = text.includes('Independently solve');
    if (text.includes('Your previous source and tests are still present')) correctionCalls++;
    return completion(files(second ? repairs[1]! : repairs[0]! + 'const original=exports.isPrime;exports.isPrime=n=>n===49||original(n);\n', second ? strong : suites[0]!));
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(correctionCalls, 1); assert.equal(result.status, 'provisional');
  assert.deepEqual(result.selected, ['candidate-2-r0']);
  assert.ok((await events(f.out)).some(e => e.type === 'correction.skipped'));
  const saved = JSON.parse(await fs.readFile(path.join(f.out, 'checkpoint.json'), 'utf8'));
  assert.equal(saved.attemptedCorrections.length, 1);
  await fs.unlink(path.join(f.out, 'result.json'));
  const resumed = await runTask(f.task, c, f.out, {resume: true,
    model: {complete: async () => {throw new Error('Resume must not repeat a settled correction');}}});
  assert.equal(resumed.usage.calls, result.usage.calls); assert.equal(resumed.status, 'provisional');
});

test('a failed independent attempt preserves the qualified primary and records conservative unknown usage', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings(); const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    if (!('submit_review' in args.tools) && JSON.stringify(args.messages).includes('Independently solve')) throw new Error('injected transport failure');
    return delegate.complete(args);
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(result.status, 'provisional'); assert.deepEqual(result.selected, ['candidate-1-r0']);
  assert.equal(result.usage.estimated, true); assert.ok(result.usage.calls <= 32);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.out, 'budget.json'), 'utf8')).pending, {});
});

test('primary corrections retain their allowance without a speculative peer reserve', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings(); c.budget.maxCalls = 14;
  let corrections = 0, reviews = 0;
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    assert.doesNotMatch(text, /Independently solve/);
    if ('submit_review' in args.tools) {
      reviews++;
      return completion([['submit_review', {findings: corrections < 2 ? [{severity: 'blocking', kind: 'missing-requirement',
        explanation: corrections ? 'The suite still omits composite squares beyond four.' : 'The suite omits prime inputs.',
        requirement: 'Classify nonnegative integers by primality.', evidence: [{path: 'agent-tests/check.cjs', startLine: 1}]}] : [],
        coveredRequirements: ['1 is not prime']}]]);
    }
    if (text.includes('Your previous source and tests are still present')) corrections++;
    const suite = "const assert=require('node:assert/strict'),{isPrime}=require('../prime.cjs');assert.equal(isPrime(1),false);\n"
      + (corrections ? 'assert.equal(isPrime(2),true);\n' : '')
      + (corrections > 1 ? 'assert.equal(isPrime(49),false);\n' : '');
    return completion(files(repairs[0]!, suite));
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(corrections, 2); assert.equal(reviews, 3);
  assert.equal(result.status, 'provisional'); assert.equal(result.usage.calls, 6);
  const log = await events(f.out);
  assert.ok(!log.some(e => e.type === 'challenge.started'));
  assert.equal(log.filter(e => e.type === 'budget.allocation' && e.data.stage === 'repair' && e.data.turns > 0).length, 2);
});

test('a small total budget still funds immediate primary correction before any optional peer', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings(); c.budget.maxCalls = 11;
  let corrections = 0;
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) return completion([['submit_review', {findings: corrections ? [] : [{
      severity: 'blocking', kind: 'missing-requirement', explanation: 'The suite omits prime inputs.',
      requirement: 'Classify nonnegative integers by primality.', evidence: [{path: 'agent-tests/check.cjs', startLine: 1}],
    }], coveredRequirements: ['1 is not prime']}]]);
    assert.doesNotMatch(text, /Independently solve/);
    if (text.includes('Your previous source and tests are still present')) corrections++;
    return completion(files(repairs[0]!, corrections ? suites[0]! :
      "const assert=require('node:assert/strict'),{isPrime}=require('../prime.cjs');assert.equal(isPrime(1),false);\n"));
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(result.status, 'provisional'); assert.equal(corrections, 1); assert.equal(result.usage.calls, 4);
  const allocations = (await events(f.out)).filter(e => e.type === 'budget.allocation' && e.data.stage === 'repair');
  assert.equal(allocations.length, 1); assert.ok(allocations[0].data.turns > 0);
  const saved = JSON.parse(await fs.readFile(path.join(f.out, 'checkpoint.json'), 'utf8'));
  assert.equal(saved.attemptedCorrections.length, 1);
});

test('incompatible staged scheduling is rejected before any provider request', () => {
  for (const mutate of [(c: ReturnType<typeof settings>) => {c.budget.concurrency = 2;},
    (c: ReturnType<typeof settings>) => {c.search.initialCandidates = 2;},
    (c: ReturnType<typeof settings>) => {c.search.review = false;},
    (c: ReturnType<typeof settings>) => {c.search.maxRounds = 1;}]) {
    const c = settings(); mutate(c); assert.equal(configSchema.safeParse(c).success, false);
  }
  const eager = config(); eager.search.differentialSteps = 3;
  assert.equal(configSchema.safeParse(eager).success, false);
});

test('bounded differential challenge turns false cross-agreement into a reviewed counterexample and a targeted repair', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings();
  c.search.differentialSteps = 3; c.budget.maxCalls = 40; c.budget.wallMs = 180000;
  const delegate = new FixtureModel(); let audits = 0, corrections = 0, initialReviews = 0;
  const wrong = repairs[1]! + 'const original=exports.isPrime;exports.isPrime=n=>n===49||original(n); // PEER_SOURCE_ONLY\n';
  const strong = suites[0]! + 'assert.equal(isPrime(49), false, "square of seven");\n';
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) {
      assert.doesNotMatch(text, /PEER_SOURCE_ONLY|Differential coverage challenge/);
      if (!audits) initialReviews++;
      return delegate.complete(args);
    }
    const second = text.includes('Independently solve');
    if (text.includes('Your previous source and tests are still present')) {
      assert.equal(second, true); corrections++;
      assert.match(text, /Reviewed counterexample suites/);
      return completion(files(repairs[1]!, suites[1]!));
    }
    if (text.includes('Differential coverage challenge')) {
      assert.equal(second, false); assert.ok(initialReviews >= 2); audits++;
      assert.match(text, /PEER_SOURCE_ONLY/);
      return completion(files(repairs[0]!, strong));
    }
    return completion(files(second ? wrong : repairs[0]!, second ? suites[1]! : suites[0]!));
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(audits, 1); assert.equal(corrections, 1);
  assert.equal(result.status, 'validated'); assert.equal(result.selected.length, 2);
  assert.ok(result.candidates.find(candidate => candidate.lineage === 'candidate-1')!.tests.some(file =>
    Buffer.from(file.data, 'base64').toString() === strong));
  assert.equal(result.candidates.find(candidate => candidate.lineage === 'candidate-2')!.source[0]!.data, Buffer.from(repairs[1]!).toString('base64'));
  const log = await events(f.out);
  assert.equal(log.filter(event => event.type === 'differential.started').length, 1);
  const allocation = log.find(event => event.type === 'budget.allocation' && event.data.stage === 'differential').data;
  assert.equal(allocation.turns, 3); assert.ok(allocation.correctionReserve >= 1);
  assert.ok(result.usage.calls <= c.budget.maxCalls);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.out, 'budget.json'), 'utf8')).pending, {});
  await fs.unlink(path.join(f.out, 'result.json'));
  const resumed = await runTask(f.task, c, f.out, {resume: true,
    model: {complete: async () => {throw new Error('A completed audit must not be replayed');}}});
  assert.equal(resumed.status, 'validated'); assert.equal(resumed.usage.calls, result.usage.calls);
});

test('an unchanged differential audit reuses semantic review and never creates a third branch', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings();
  c.search.differentialSteps = 3; c.budget.maxCalls = 40; c.budget.wallMs = 180000;
  const delegate = new FixtureModel(); let audits = 0, reviewedAfterAudit = 0;
  const model: Model = {complete: async args => {
    if ('submit_review' in args.tools) {if (audits) reviewedAfterAudit++; return delegate.complete(args);}
    if (JSON.stringify(args.messages).includes('Differential coverage challenge')) {
      audits++; return completion([['submit_candidate', {requirements: ['primality'], rationale: 'no new justified distinction'}]]);
    }
    return delegate.complete(args);
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(audits, 1); assert.equal(reviewedAfterAudit, 0);
  assert.equal(result.status, 'validated'); assert.equal(result.candidates.length, 2);
});

test('insufficient differential allowance preserves qualified work without spending or exceeding the ledger', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings();
  c.search.challengeSteps = 3; c.search.differentialSteps = 3; c.budget.maxCalls = 12; c.budget.wallMs = 180000;
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    assert.doesNotMatch(JSON.stringify(args.messages), /Differential coverage challenge/);
    return delegate.complete(args);
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(result.status, 'validated'); assert.equal(result.candidates.length, 2);
  assert.ok((await events(f.out)).some(event => event.type === 'differential.skipped'));
  assert.ok(result.usage.calls <= 12);
});

test('an invalid differential rewrite cannot discard the previously qualified primary, including in checkpoints', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = settings();
  c.search.differentialSteps = 3; c.budget.maxCalls = 40; c.budget.wallMs = 180000;
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    assert.doesNotMatch(text, /Your previous source and tests are still present/);
    if (!('submit_review' in args.tools) && text.includes('Differential coverage challenge'))
      return completion(files(repairs[0]!, 'process.exit(0); // INVALID_AUDIT\n'));
    return delegate.complete(args);
  }};
  const result = await runTask(f.task, c, f.out, {model});
  assert.equal(result.status, 'validated'); assert.equal(result.candidates[0]!.id, 'candidate-1-r0');
  assert.ok((await events(f.out)).some(event => event.type === 'differential.rejected'));
  const saved = JSON.parse(await fs.readFile(path.join(f.out, 'checkpoint.json'), 'utf8'));
  assert.equal(saved.differentialAudited, true); assert.equal(saved.candidates[0].id, 'candidate-1-r0');
  await fs.unlink(path.join(f.out, 'result.json'));
  const resumed = await runTask(f.task, c, f.out, {resume: true,
    model: {complete: async () => {throw new Error('Invalid discarded work must not be retried on resume');}}});
  assert.equal(resumed.status, 'validated'); assert.equal(resumed.usage.calls, result.usage.calls);
});
