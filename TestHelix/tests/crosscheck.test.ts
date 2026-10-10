import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {runTask} from '../src/pipeline.js';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {SearchBudget} from '../src/search-budget.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import type {Model} from '../src/types.js';
import {config, fixture, FixtureModel, completion, repairs, suites, buggy} from './helpers.js';

function artifacts(source: string, tests: string, seal = true) {
  const tools: Array<[string, unknown]> = [
    ['write_file', {path: 'prime.cjs', content: source}],
    ['write_file', {path: 'agent-tests/check.cjs', content: tests}],
    ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
  ];
  if (seal) tools.push(['submit_candidate', {requirements: ['nonnegative integer primality'], rationale: 'public contract control'}]);
  return tools;
}

test('serial generation protects the second solver before review and stays inside the shared ledger', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.budget.concurrency = 1; settings.budget.maxCalls = 14; settings.search.agentSteps = 14;
  settings.model.timeoutMs = 1000;
  let firstCalls = 0, secondStarted = false, reviewed = false;
  const delegate = new FixtureModel();
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) {assert.equal(secondStarted, true); reviewed = true; return delegate.complete(args);}
    assert.equal(reviewed, false);
    if (text.includes('Independently solve')) {secondStarted = true; return delegate.complete(args);}
    firstCalls++;
    if (firstCalls === 1) return completion(artifacts(repairs[0]!, suites[0]!, false));
    if (!('read_file' in args.tools)) return completion([['submit_candidate', {requirements: ['primality'], rationale: 'sealed at budget limit'}]]);
    return completion([['read_file', {path: 'prime.cjs', startLine: firstCalls}]]);
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated'); assert.equal(result.candidates.length, 2);
  assert.ok(firstCalls <= 5); assert.ok(result.usage.calls <= 14);
  const ledger = JSON.parse(await fs.readFile(path.join(f.out, 'budget.json'), 'utf8'));
  assert.deepEqual(ledger.pending, {}); assert.equal(ledger.calls, result.usage.calls);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  const waves = events.filter(e => e.type === 'budget.allocation' && e.data.stage === 'initial');
  assert.equal(waves.length, 2);
  assert.ok(waves[0].data.reserveForReview.calls > waves[0].data.reviewReserve);
  assert.ok(waves[0].data.reserveForReview.input > waves[1].data.reserveForReview.input);
});

test('phase timeout aborts the dispatched request, retains uncertain usage, and leaves the shared budget usable', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.budget.concurrency = 1;
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'phase-control', false);
  let requests = 0;
  const model: Model = {complete: args => {requests++; return new Promise((_, reject) => {
    // A real HTTP request holds a socket open; AbortSignal.timeout is unref'd.
    const pending = setTimeout(() => reject(new Error('request was not aborted')), 2000);
    const abort = () => {clearTimeout(pending); reject(args.signal.reason);};
    if (args.signal.aborted) abort();
    else args.signal.addEventListener('abort', abort, {once: true});
  });}};
  const session = new AgentSession({id: 'candidate-1', role: 'candidate', strategy: 'fixture', task: f.task,
    config: settings, model, sandbox: new LocalSandbox(f.workspace, false), baseline: new LocalSandbox(f.workspace, false),
    store, budget, deadline: Date.now() + 200});
  await assert.rejects(session.run(2));
  assert.equal(requests, 1); assert.equal(budget.snapshot().calls, 1); assert.equal(budget.snapshot().estimated, true);
  assert.ok(budget.remainingMs > 1000);
  const next = await budget.slot(() => budget.reserve(120, 128));
  await budget.settle(next, {input: 120, output: 90, cached: 0});
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.out, 'budget.json'), 'utf8')).pending, {});
});

test('the final solver leaves time to recover its actual artifacts after a stalled response', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.budget.concurrency = 1;
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'last-solver', false);
  const allocation = new SearchBudget(settings, budget);
  const wave = allocation.wave(1, 1, 8, allocation.review());
  assert.ok(wave.deadline < Date.now() + budget.remainingMs);
  const baseline = new LocalSandbox(f.workspace, false);
  const working = path.join(f.root, 'working'); await fs.cp(f.workspace, working, {recursive: true});
  t.mock.timers.enable({apis: ['Date'], now: Date.now()});
  let calls = 0;
  const model: Model = {complete: async () => {
    if (++calls === 1) return completion(artifacts(repairs[0]!, suites[0]!, false));
    t.mock.timers.setTime(wave.deadline + 1);
    throw new DOMException('Model phase timed out', 'TimeoutError');
  }};
  const session = new AgentSession({id: 'candidate-1', role: 'candidate', strategy: 'fixture', task: f.task,
    config: settings, model, sandbox: new LocalSandbox(working, false), baseline, store, budget,
    deadline: wave.deadline, reserveForReview: wave.floor});
  await session.run(wave.turns);
  assert.ok(session.candidate);
  assert.equal(session.candidate.source.length, 1);
  assert.match(session.candidate.rationale, /requiring independent execution and review/);
  assert.ok(budget.remainingMs > 1000);
  assert.equal(budget.snapshot().estimated, true);
  assert.equal((await baseline.read('prime.cjs')).toString(), buggy);
  assert.match(await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8'), /candidate.recovered/);
});

test('a reviewed counterexample repairs the incomplete peer without rewriting the correct test to agree', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.budget.concurrency = 1; settings.search.maxRounds = 2; settings.budget.maxCalls = 10;
  const corrected: string[] = []; let reviewCalls = 0;
  const broad = suites[0]! + 'assert.equal(isPrime(49), false);\n';
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_review' in args.tools) {
      reviewCalls++;
      assert.doesNotMatch(args.messages.map(message => typeof message.content === 'string' ? message.content : '').join('\n'),
        /"source":"candidate-|Independent repairs accept|peerResults/);
      // Keep requesting evidence until the host's protected allowance requires
      // a decision. Without a correction reserve both reviewers use all 8 calls.
      return args.forceTool === 'submit_review'
        ? completion([['submit_review', {findings: [], coveredRequirements: ['primality']}]] )
        : completion([['read_file', {path: 'agent-tests/check.cjs'}]]);
    }
    const second = text.includes('Independently solve');
    const repair = text.includes('Your previous source and tests are still present');
    if (repair) corrected.push(second ? 'second' : 'first');
    return completion(artifacts(second ? repairs[1]! + (repair ? '' : 'const original=exports.isPrime;exports.isPrime=n=>n===49||original(n);\n')
      : repairs[0]!, second ? suites[1]! : broad));
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(result.status, 'validated'); assert.equal(result.selected.length, 2);
  assert.equal(reviewCalls, 4); assert.equal(result.usage.calls, 7);
  assert.deepEqual(corrected, ['second']);
  const first = result.candidates.find(c => c.lineage === 'candidate-1')!;
  assert.equal(first.round, 0); assert.equal(Buffer.from(first.tests.find(x => x.path.endsWith('check.cjs'))!.data, 'base64').toString(), broad);
  assert.match(await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8'), /selection.counterexample/);
});

test('two mutually passing repairs with the same wrong assertion cannot overrule a public-contract rejection', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const wrong = "const assert=require('node:assert/strict'),{isPrime}=require('../prime.cjs');assert.equal(isPrime(1),false);assert.equal(isPrime(4),true);\n";
  const model: Model = {complete: async args => {
    if ('submit_review' in args.tools) return completion([['submit_review', {findings: [{severity: 'blocking', kind: 'unsupported-assertion',
      explanation: '4 is composite, so expecting true is unsupported.', requirement: 'Classify nonnegative integers by primality.',
      evidence: [{path: 'agent-tests/check.cjs', startLine: 1}], falseRejection: {
        allowedBehavior: 'Return false for 4.', whyAllowed: '4 has divisor 2 and is not prime.',
        controlAssessment: 'This assertion rejects correct composite classification.', publicBasis: {taskLines: {startLine: 1}},
      }}], coveredRequirements: []}]]);
    const second = JSON.stringify(args.messages).includes('Independently solve');
    return completion(artifacts(second ? 'exports.isPrime=n=>![0,1].includes(n);\n' : 'exports.isPrime=n=>n>=2;\n', wrong));
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  const cross = result.matrix.filter(cell => cell.sourceId !== 'baseline' && cell.sourceId !== cell.testId);
  assert.equal(cross.length, 2); assert.ok(cross.every(cell => cell.status === 'pass'));
  assert.equal(result.status, 'no_submit'); assert.equal(result.selected.length, 0);
});

test('the independent solver sees neither shared planner premises nor the first solver artifacts', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.model.name = 'gpt-5.5'; settings.model.provider = 'openai-responses';
  settings.model.providerOptions = {openai: {reasoningEffort: 'medium'}}; settings.planner.enabled = true;
  settings.budget.concurrency = 1; const delegate = new FixtureModel(); let second = false;
  const model: Model = {complete: async args => {
    const text = JSON.stringify(args.messages);
    if ('submit_plan' in args.tools) return completion([['submit_plan', {capabilities: ['algorithm'],
      requirements: [{requirement: 'Primality', taskLines: {startLine: 1}}], preparation: ['PRIVATE_PLANNER_PREMISE'],
      branches: [{focus: 'PRIVATE_PLANNER_PREMISE', verification: 'public behavior'}], unknowns: [], completion: 'regression and repair'}]]);
    if (text.includes('Independently solve') && !('submit_review' in args.tools)) {
      second = true; assert.doesNotMatch(text, /PRIVATE_PLANNER_PREMISE|FIRST_SOLVER_SECRET/);
      assert.equal('request_replan' in args.tools, false);
    }
    if (text.includes('PRIVATE_PLANNER_PREMISE') && !('submit_review' in args.tools))
      return completion(artifacts(repairs[0]! + '// FIRST_SOLVER_SECRET\n', suites[0]!));
    return delegate.complete(args);
  }};
  const result = await runTask(f.task, settings, f.out, {model, plannerModel: model});
  assert.equal(second, true); assert.equal(result.status, 'validated');
});
