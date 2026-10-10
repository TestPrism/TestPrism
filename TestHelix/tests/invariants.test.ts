import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { ModelMessage } from 'ai';
import { Budget } from '../src/budget.js';
import { projectContext } from '../src/context.js';
import { Store } from '../src/store.js';
import { select } from '../src/selection.js';
import { LocalFactory, LocalSandbox, applyFiles } from '../src/runtime.js';
import { classify, Validator } from '../src/validation.js';
import { mergeTests } from '../src/artifacts.js';
import { configSchema, type Candidate, type Observation } from '../src/types.js';
import { command, containedPath, estimateTokens, hash, requestSize, shellQuote } from '../src/util.js';
import { config, fixture } from './helpers.js';

test('concurrent reservations cannot overspend call budget, and resume retains uncertain spend', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config(); settings.budget.maxCalls = 1;
  const budget = await Budget.open(settings, path.join(f.root, 'budget.json'), 'same', false);
  const attempts = await Promise.allSettled([budget.reserve(200, 500), budget.reserve(200, 500)]);
  assert.equal(attempts.filter(r => r.status === 'fulfilled').length, 1);
  const reserved = (attempts.find(r => r.status === 'fulfilled') as PromiseFulfilledResult<{input: number; output: number}>).value;
  await budget.settle(reserved);
  const resumed = await Budget.open(settings, path.join(f.root, 'budget.json'), 'same', true);
  assert.deepEqual(resumed.snapshot(), {input: 200, output: 500, calls: 1, cached: 0, estimated: true, costUSD: null});
  await assert.rejects(resumed.reserve(1, 128), /budget exhausted/);
  await assert.rejects(Budget.open(settings, path.join(f.root, 'budget.json'), 'different', true), /different/);
});

test('temporary peer reservations can settle before retry while resumed uncertain spend never waits or refunds itself', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config(); settings.budget.maxInputTokens = 1000;
  const file = path.join(f.root, 'budget.json'), budget = await Budget.open(settings, file, 'peers', false);
  const peer = await budget.reserve(900, 500);
  await assert.rejects(budget.reserve(300, 500), /input-token/);
  assert.equal(budget.expectedRemainingResponses, undefined);
  const waiting = budget.waitForSettlement();
  await budget.settle(peer, {input: 100, output: 100, cached: 0});
  assert.equal(await waiting, true); assert.equal(budget.expectedRemainingResponses, 9);
  const next = await budget.reserve(300, 500);
  const resumed = await Budget.open(settings, file, 'peers', true);
  assert.equal(await resumed.waitForSettlement(), false);
  assert.equal(resumed.snapshot().input, 400, 'uncertain reservation remains fully charged');
  await budget.settle(next); assert.equal(await budget.waitForSettlement(), false);
});

test('generation cannot consume protected review tokens, calls or dollars and review can spend them without double accounting', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.budget.maxInputTokens = 2000; settings.budget.maxOutputTokens = 1000; settings.budget.maxCalls = 4;
  const budget = await Budget.open(settings, path.join(f.root, 'floor.json'), 'floor', false);
  const floor = {input: 800, output: 300, calls: 2};
  await assert.rejects(budget.reserve(1201, 128, floor), /protected work/);
  const first = await budget.reserve(1000, 1000, floor); assert.equal(first.output, 700);
  await budget.settle(first, {input: 1000, output: 700, cached: 0});
  await assert.rejects(budget.reserve(200, 128, floor), /output-token/);
  const review = await budget.reserve(800, 300); await budget.settle(review, {input: 800, output: 300, cached: 0});
  assert.equal(budget.snapshot().input, 1800); assert.equal(budget.snapshot().calls, 2);
  await assert.rejects(budget.reserve(1, 128, floor), /protected for later work/);
  const priced = config(); priced.model.inputPricePerMillion = 1; priced.model.outputPricePerMillion = 2;
  priced.budget.maxCostUSD = .002;
  const dollars = await Budget.open(priced, path.join(f.root, 'floor-cost.json'), 'priced-floor', false);
  const reservation = await dollars.reserve(500, 4096, {input: 500, output: 200, calls: 1});
  assert.equal(reservation.output, 300, 'future review spend must fit the same dollar cap');
  await dollars.settle(reservation);
});

test('missing generated test/helper failures are infrastructure errors while public-contract assertion failures remain detectable', () => {
  assert.equal(classify(2, "python: can't open file '/testbed/agent-tests/check.py': [Errno 2] No such file or directory"), 'error');
  assert.equal(classify(1, "Error: Cannot find module '/app/agent-tests/check.js'"), 'error');
  assert.equal(classify(1, 'bash: agent-tests/helper.sh: No such file or directory'), 'error');
  assert.equal(classify(1, 'AssertionError: required public output was not created'), 'fail');
  assert.equal(classify(1, "AssertionError at agent-tests/check.py: expected behavior not found"), 'fail');
});

test('context projection preserves original task and tool pairing without mutating canonical history', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const messages: ModelMessage[] = [{role: 'user', content: 'Exact original contract 中文'},
    {role: 'assistant', content: [{type: 'tool-call', toolCallId: 'edit-1', toolName: 'write_file', input: {path: 'x', content: 'x'.repeat(40000)}}]},
    {role: 'tool', content: [{type: 'tool-result', toolCallId: 'edit-1', toolName: 'write_file', output: {type: 'json', value: {log: 'x'.repeat(40000)}}}]},
    {role: 'assistant', content: 'Recent conclusion'}, {role: 'user', content: 'Specific failure'}];
  const original = JSON.stringify(messages);
  const projected = await projectContext(messages, 'Keep verified invariants', {maxTokens: 2500, recentTurns: 1, toolOutputChars: 1000}, store);
  assert.equal(JSON.stringify(messages), original); assert.deepEqual(projected[0], messages[0]);
  assert.ok(estimateTokens(projected) <= 2500);
  const calls = projected.flatMap(m => m.role === 'assistant' && Array.isArray(m.content)
    ? m.content.filter(p => p.type === 'tool-call').map(p => p.toolCallId) : []);
  const results = projected.flatMap(m => m.role === 'tool' ? m.content.filter(p => p.type === 'tool-result').map(p => p.toolCallId) : []);
  assert.deepEqual(calls, results);
  assert.ok(projected.some(m => m.content === 'Specific failure'));
  const archives = await Promise.all((await fs.readdir(path.join(f.out, 'artifacts'))).map(file => fs.readFile(path.join(f.out, 'artifacts', file), 'utf8')));
  assert.ok(archives.some(text => text.includes('edit-1') && text.includes('x'.repeat(40000))), 'Full original evidence remains recoverable');
  const image = {type: 'image', image: 'a'.repeat(4000000), mediaType: 'image/png'};
  assert.ok(requestSize([image]).bytes < 200); assert.equal(requestSize([image]).imageTokens, 32768);
});

function candidate(id: string, sourceHash = id): Candidate {
  return {id, lineage: id, round: 0, strategy: '', source: [{path: 'x', data: 'eA==', mode: 420}], sourceHash,
    tests: [{path: 'agent-tests/run-tests.sh', data: Buffer.from('exit 0\n').toString('base64'), mode: 493}],
    testsHash: id, requirements: ['r'], rationale: '', sessionId: id};
}
test('self-reported coverage cannot buy selection priority and equal-evidence ties survive input reordering', () => {
  const a = candidate('a'), b = candidate('b');
  const matrix: Observation[] = [a, b].map(c => ({testId: c.id, sourceId: 'baseline', status: 'fail',
    signature: '', durationMs: 0, output: ''}));
  const selected = select([a, b], matrix, 1).provisional?.id;
  b.requirements = Array.from({length: 20}, () => 'model claims complete coverage');
  assert.equal(select([b, a], matrix, 1).provisional?.id, selected);
});
test('duplicate repair bytes cannot become independent support under a different agent identity', () => {
  const candidates = [candidate('a', 'same'), candidate('b', 'same')];
  const matrix: Observation[] = candidates.flatMap(c => ['baseline', 'a', 'b'].map(sourceId =>
    ({testId: c.id, sourceId, status: sourceId === 'baseline' ? 'fail' : 'pass', signature: '', durationMs: 0, output: ''})));
  assert.equal(select(candidates, matrix, 1).tests.length, 0);
  candidates[1]!.sourceHash = 'different'; assert.equal(select(candidates, matrix, 1).tests.length, 2);
});

test('review and provisional ranking prefer a passing own control over a smaller self-failing suite without claiming independence',()=>{
  const failed=candidate('failed'),passing=candidate('passing');
  passing.tests[0]!.data=Buffer.from('printf "larger runner"; exit 0\n').toString('base64');
  const matrix:Observation[]=[
    {testId:failed.id,sourceId:'baseline',status:'fail',signature:'',durationMs:0,output:''},
    {testId:failed.id,sourceId:failed.id,status:'fail',signature:'',durationMs:0,output:''},
    {testId:passing.id,sourceId:'baseline',status:'fail',signature:'',durationMs:0,output:''},
    {testId:passing.id,sourceId:passing.id,status:'pass',signature:'',durationMs:0,output:''},
  ];
  const selected=select([failed,passing],matrix,1);
  assert.equal(selected.provisional?.id,'passing');assert.equal(selected.tests.length,0);assert.equal(selected.witnesses.length,0);
  assert.notEqual(select([failed,passing],matrix,1,{passing:{accepted:false,concerns:['proven overconstraint'],missingRequirements:[],coveredRequirements:[]}}).provisional?.id,'passing');
});

test('merged runner restores bundles and propagates failures, including missing bundle files', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const candidates = [candidate('a'), candidate('b')];
  candidates[1]!.tests[0]!.data = Buffer.from('exit 7\n').toString('base64');
  const files = mergeTests(candidates); await applyFiles(sandbox, files);
  const result = await sandbox.exec('bash agent-tests/run-tests.sh', 2000); assert.equal(result.code, 1);
  const runner = await fs.readFile(path.join(f.workspace, 'agent-tests/run-tests.sh'), 'utf8');
  assert.match(runner, /run_bundle bundle-2/);
  await fs.rm(path.join(f.workspace, 'agent-tests/.bundles/bundle-2'), {recursive: true});
  const broken = await sandbox.exec('bash agent-tests/run-tests.sh', 2000); assert.notEqual(broken.code, 0);
});

test('timeout kills process descendants; path tools reject symlink escapes and shell quoting preserves literals', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.symlink(f.root, path.join(f.workspace, 'escape'));
  await assert.rejects(containedPath(f.workspace, 'escape/private'), /symlink/);
  await assert.rejects(containedPath(f.workspace, '../secret'), /Unsafe/);
  const literal = "it's $(printf BAD) `printf BAD`\n$HOME";
  const echo = await command(['bash', '-c', `printf %s ${shellQuote(literal)}`]); assert.equal(echo.stdout, literal);
  const pidfile = path.join(f.root, 'pid');
  const killed = await command(['bash', '-c', `sleep 60 & printf %s "$!" > ${shellQuote(pidfile)}; wait`], {timeoutMs: 100});
  assert.equal(killed.timedOut, true); assert.ok(killed.durationMs < 5000);
  const pid = Number(await fs.readFile(pidfile, 'utf8'));
  const state = await fs.readFile(`/proc/${pid}/stat`, 'utf8').catch(() => '');
  assert.ok(!state || state.split(' ')[2] === 'Z', 'child must be exited, not a continuing worker');
});

test('fresh repeated execution detects unstable tests; infrastructure errors are never baseline evidence', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'validation'));
  t.after(() => factory.close()); const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.root, 'budget.json'), 'validation', false);
  const validator = new Validator(factory, settings, store, budget, 'fixture');
  const tests = [{path: 'agent-tests/run-tests.sh', mode: 493, data: Buffer.from('echo $$; exit 1\n').toString('base64')}];
  const observation = await validator.evaluate(tests, [], 'test', 'baseline', 2);
  assert.equal(observation.status, 'error'); assert.equal(observation.signature, 'unstable');
  assert.equal(classify(127, 'not found'), 'error'); assert.equal(classify(1, 'error collecting agent-tests/x.py'), 'error');
  assert.equal(classify(1, 'AssertionError: 1 is not prime'), 'fail');
  assert.equal(classify(1, 'compiling product: SyntaxError'), 'fail');
  assert.equal(classify(0, '', true), 'timeout');
  assert.throws(() => configSchema.parse({...settings, model: {...settings.model, extraBody: {max_tokens: 1000000}}}), /host-controlled/);
});
