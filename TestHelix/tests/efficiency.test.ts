import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { createTwoFilesPatch } from 'diff';
import { Store } from '../src/store.js';
import { Toolbox, workspaceTools } from '../src/tools.js';
import { LocalSandbox, LocalFactory } from '../src/runtime.js';
import { WorkingMemory } from '../src/memory.js';
import { projectContext, contextTokens } from '../src/context.js';
import { Budget } from '../src/budget.js';
import { Validator } from '../src/validation.js';
import { select } from '../src/selection.js';
import { AgentSession } from '../src/agent.js';
import { retryPlan } from '../src/retry.js';
import { hash } from '../src/util.js';
import { config, fixture, completion, repairs, suites } from './helpers.js';
import type { Candidate, Model } from '../src/types.js';
import type { ModelMessage } from 'ai';

async function toolkit() {
  const f = await fixture(), store = new Store(f.out); await store.init();
  const sandbox = new LocalSandbox(f.workspace, false);
  return {...f, store, sandbox, toolbox: new Toolbox(sandbox, store, 1200, 3000), signal: AbortSignal.timeout(30000)};
}
test('file discovery accepts model glob calls, relative directories and paginated literals without escaping the public tree', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  for (const file of ['tests/delete/models.py', 'tests/keep/models.py', 'tests/delete_test.py', 'tests/.config.py', 'tests/a.js', 'src/models.py', '.git/secret.py', 'solution.sh']) {
    await fs.mkdir(path.dirname(path.join(f.workspace, file)), {recursive: true});
    await fs.writeFile(path.join(f.workspace, file), 'fixture');
  }
  const list = (args: Record<string, unknown>) => f.toolbox.execute('list_files', args, f.signal);
  const all = await list({directory: '.', pattern: '*'});
  assert.ok((all.files as string[]).includes('tests/delete/models.py'));
  assert.ok(!(all.files as string[]).some(file => file.includes('.git') || file === 'solution.sh'));
  const models = ['tests/delete/models.py', 'tests/keep/models.py'];
  assert.deepEqual((await list({directory: 'tests', pattern: '**/models.py'})).files, models);
  assert.deepEqual((await list({directory: './tests/', pattern: 'tests/**/models.py'})).files, models);
  assert.deepEqual((await list({directory: 'tests', pattern: '*.{py,js}'})).files,
    ['tests/.config.py', 'tests/a.js', 'tests/delete/models.py', 'tests/delete_test.py', 'tests/keep/models.py']);
  assert.deepEqual((await list({directory: 'tests', pattern: '**/*delete*'})).files, ['tests/delete_test.py']);
  const page = await list({directory: 'tests', pattern: 'models.py', limit: 1});
  assert.equal(page.count, 2); assert.equal(page.nextOffset, 1);
  assert.deepEqual((await list({directory: 'tests', pattern: 'models.py', offset: 1})).files, models.slice(1));
  assert.deepEqual((await list({directory: 'tests', pattern: '../**/*'})).files, []);
  await assert.rejects(list({directory: '..', pattern: '*'}), /Unsafe/);
});

test('long file listings paginate by serialized size instead of burying their cursor in an artifact', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  for (let i = 0; i < 16; i++) await fs.writeFile(path.join(f.workspace, `${i.toString().padStart(2, '0')}-${'name'.repeat(20)}.js`), 'fixture');
  const toolbox = new Toolbox(f.sandbox, f.store, 512, 3000); const files: string[] = []; let offset = 0;
  for (let i = 0; i < 20; i++) {
    const page = await toolbox.observe(await toolbox.execute('list_files', {pattern: '*.js', offset}, f.signal));
    assert.ok(JSON.stringify(page).length <= 512); assert.equal(page.artifact, undefined);
    files.push(...page.files as string[]);
    if (page.nextOffset === undefined) break;
    assert.ok(Number(page.nextOffset) > offset); offset = Number(page.nextOffset);
  }
  assert.equal(files.length, 16); assert.equal(new Set(files).size, 16);
});

test('host validation rejects coercion, NaN, unknown properties and readonly mutation before any execution', async t => {
  const f = await toolkit(); t.after(f.cleanup); let commands = 0;
  f.sandbox.exec = async () => { commands++; throw new Error('must not execute'); };
  for (const input of [{command: 1}, {command: 'echo bad', timeoutMs: NaN}, {command: 'echo bad', timeoutMs: -1},
    {command: 'echo bad', timeoutMs: '100'}, {command: 'echo bad', shell: '/bin/bash'}])
    await assert.rejects(f.toolbox.execute('exec', input, f.signal));
  await assert.rejects(f.toolbox.execute('constructor', {}, f.signal), /Unknown/);
  const review = new Toolbox(f.sandbox, f.store, 1000, 1000, true);
  await assert.rejects(review.execute('apply_patch', {patch: 'bad'}, f.signal), /read-only/);
  assert.equal(commands, 0);
});

test('version-checked reads reuse bytes and refresh after external changes; focused reads paginate minified lines', async t => {
  const f = await toolkit(); t.after(f.cleanup); let reads = 0;
  const original = f.sandbox.read.bind(f.sandbox); f.sandbox.read = async file => { reads++; return original(file); };
  const first = await f.toolbox.execute('read_file', {path: 'prime.cjs'}, f.signal);
  await f.toolbox.execute('read_file', {path: 'prime.cjs'}, f.signal); assert.equal(reads, 1);
  await fs.writeFile(path.join(f.workspace, 'prime.cjs'), 'exports.isPrime = () => false;\n');
  const changed = await f.toolbox.execute('read_file', {path: 'prime.cjs'}, f.signal);
  assert.notEqual(changed.hash, first.hash); assert.equal(reads, 2);
  await f.toolbox.execute('read_file', {path: 'prime.cjs', refresh: true}, f.signal); assert.equal(reads, 3);
  const minified = 'x'.repeat(2500); await fs.writeFile(path.join(f.workspace, 'long.js'), minified);
  let args: Record<string, unknown> = {path: 'long.js'}, restored = '';
  for (let i = 0; i < 10; i++) {
    const page = await f.toolbox.execute('read_file', args, f.signal);
    assert.ok(String(page.content).length < 1200);
    restored += String(page.content).replace(/^\d+(?::\d+)?: /, '').replace(/\n$/, '');
    if (!page.nextStartLine) break;
    args = {path: 'long.js', startLine: page.nextStartLine, startColumn: page.nextStartColumn ?? 0};
  }
  assert.equal(restored, minified);
  await assert.rejects(f.toolbox.execute('read_file', {path: 'long.js', startLine: 200}, f.signal), /out of range/);
});

test('escaped file, search and artifact pages retain usable cursors after observation without nested artifact wrappers', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  const toolbox = new Toolbox(f.sandbox, f.store, 512, 3000);
  const value = '"\\\t\r\u0001😀'.repeat(120);
  await fs.writeFile(path.join(f.workspace, 'escaped.txt'), value);
  let fileArgs: Record<string, unknown> = {path: 'escaped.txt'}, restored = '';
  for (let i = 0; i < 100; i++) {
    const result = await toolbox.execute('read_file', fileArgs, f.signal), page = await toolbox.observe(result);
    assert.ok(JSON.stringify(result).length <= 512);
    assert.equal(page.artifact, undefined); assert.equal(typeof page.content, 'string');
    restored += String(page.content).replace(/^\d+(?::\d+)?: /, '').replace(/\n$/, '');
    if (!page.nextStartLine) break;
    assert.ok(Number(page.nextStartColumn) > Number(fileArgs.startColumn ?? 0));
    fileArgs = {path: 'escaped.txt', startLine: page.nextStartLine, startColumn: page.nextStartColumn ?? 0};
  }
  assert.equal(restored, value);
  const raw = JSON.stringify({content: value + '\n' + value}), artifact = await f.store.artifact(raw);
  f.sandbox.exec = async () => ({code: 0, stdout: raw, stderr: '', timedOut: false, durationMs: 1});
  for (const tool of ['read_artifact', 'search']) {
    let offset = 0, joined = '';
    for (let i = 0; i < 100; i++) {
      const result = await toolbox.execute(tool, tool === 'search' ? {query: 'fixture', offset} : {id: artifact, offset}, f.signal);
      const page = await toolbox.observe(result);
      assert.deepEqual(page, result); assert.ok(JSON.stringify(page).length <= 512);
      joined += String(page[tool === 'search' ? 'matches' : 'content']);
      if (page.nextOffset === undefined) break;
      assert.ok(Number(page.nextOffset) > offset); offset = Number(page.nextOffset);
    }
    assert.equal(joined, raw);
  }
});

test('unified patches preflight all files, reject escapes, preserve mode and roll back a partial write failure', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  await fs.writeFile(path.join(f.workspace, 'a.txt'), 'a\n'); await fs.writeFile(path.join(f.workspace, 'b.txt'), 'b\n');
  const a = createTwoFilesPatch('a/a.txt', 'b/a.txt', 'a\n', 'A\n');
  const bad = createTwoFilesPatch('a/b.txt', 'b/b.txt', 'WRONG\n', 'B\n');
  await assert.rejects(f.toolbox.execute('apply_patch', {patch: a + bad}, f.signal), /context/);
  assert.equal(await fs.readFile(path.join(f.workspace, 'a.txt'), 'utf8'), 'a\n');
  const b = createTwoFilesPatch('a/b.txt', 'b/b.txt', 'b\n', 'B\n');
  const write = f.sandbox.write.bind(f.sandbox); let failed = false;
  f.sandbox.write = async (file, data, mode) => {
    if (file === 'b.txt' && !failed) { failed = true; throw new Error('synthetic disk failure'); }
    return write(file, data, mode);
  };
  await assert.rejects(f.toolbox.execute('apply_patch', {patch: a + b}, f.signal), /disk failure/);
  assert.equal(await fs.readFile(path.join(f.workspace, 'a.txt'), 'utf8'), 'a\n');
  await fs.chmod(path.join(f.workspace, 'a.txt'), 0o755);
  await f.toolbox.execute('apply_patch', {patch: a + b}, f.signal);
  assert.equal((await fs.stat(path.join(f.workspace, 'a.txt'))).mode & 0o777, 0o755);
  await assert.rejects(f.toolbox.execute('apply_patch', {patch: createTwoFilesPatch('/dev/null', 'b/../escape', '', 'x\n')}, f.signal), /Unsafe/);
  await f.toolbox.execute('apply_patch', {patch: createTwoFilesPatch('/dev/null', 'b/new.txt', '', 'created\n')}, f.signal);
  await f.toolbox.execute('apply_patch', {patch: createTwoFilesPatch('a/new.txt', '/dev/null', 'created\n', '')}, f.signal);
  await assert.rejects(fs.stat(path.join(f.workspace, 'new.txt')), /ENOENT/);
});

test('working memory is bounded, requires observed evidence and invalidates after edits or shell commands', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  const entry = {id: 'prime-api', kind: 'fact', text: 'The public API exports isPrime.', paths: ['prime.cjs']};
  await assert.rejects(f.toolbox.execute('update_notes', {entries: [entry]}, f.signal), /Read/);
  await f.toolbox.execute('read_file', {path: 'prime.cjs'}, f.signal);
  const response = await f.toolbox.execute('update_notes', {entries: [entry]}, f.signal);
  assert.equal(response.notes, undefined, 'do not echo full memory in the tool result');
  assert.match(f.toolbox.notes, /prime-api/); assert.doesNotMatch(f.toolbox.notes, /STALE/);
  await f.toolbox.execute('write_file', {path: 'prime.cjs', content: repairs[0]}, f.signal);
  assert.match(f.toolbox.notes, /STALE/);
  await f.toolbox.execute('read_file', {path: 'prime.cjs'}, f.signal);
  await f.toolbox.execute('update_notes', {entries: [entry]}, f.signal);
  assert.doesNotMatch(f.toolbox.notes, /STALE/);
  await f.toolbox.execute('exec', {command: 'true'}, f.signal); assert.match(f.toolbox.notes, /STALE/);
  const memory = new WorkingMemory();
  for (let i = 0; i < 30; i++) memory.update([{id: `n${i}`, kind: 'next', text: 'Inspect task constraints.'}], new Map());
  assert.equal((memory.snapshot() as any).records.length, 16); assert.ok(memory.render('', 200).length <= 200);
});

test('path tools reject symlinks into hidden data and search does not follow outside symlinks', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  await fs.mkdir(path.join(f.workspace, '.git')); await fs.writeFile(path.join(f.workspace, '.git', 'secret'), 'UNIQUE_HIDDEN_MARKER');
  await fs.symlink('.git/secret', path.join(f.workspace, 'innocent.txt'));
  await assert.rejects(f.toolbox.execute('read_file', {path: 'innocent.txt'}, f.signal), /public task surface/);
  await fs.writeFile(path.join(f.root, 'outside'), 'UNIQUE_HIDDEN_MARKER');
  await fs.symlink(path.join(f.root, 'outside'), path.join(f.workspace, 'linked.txt'));
  const result = await f.toolbox.execute('search', {query: 'UNIQUE_HIDDEN_MARKER'}, f.signal);
  assert.equal(result.ok, true); assert.equal(result.matches, '');
  await fs.writeFile(path.join(f.workspace, 'fake.png'), 'not an image');
  await assert.rejects(f.toolbox.execute('view_image', {path: 'fake.png'}, f.signal), /signature/);
});

test('context includes system/tools/tail overhead, pins workflow and memory, and never resurrects archived turns', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  const messages: ModelMessage[] = [{role: 'user', content: 'Exact original task 中文'}, {role: 'user', content: 'Exact native workflow'}];
  const prefix = structuredClone(messages), overhead = {system: 'system '.repeat(30), tools: {}, tail: [{role: 'user' as const, content: 'Submit soon'}]};
  const settings = {...config().context, maxTokens: 2400, recentTurns: 1};
  for (let turn = 0; turn < 12; turn++) {
    messages.push(...completion([['read_file', {path: `p${turn}`}]]).messages);
    const assistant = messages.at(-1)!;
    const call = (assistant.content as any[])[0];
    messages.push({role: 'tool', content: [{type: 'tool-result', toolName: 'read_file', toolCallId: call.toolCallId,
      output: {type: 'json', value: {text: 'evidence '.repeat(1000)}}}]});
    const snapshot = JSON.stringify(messages);
    const projected = await projectContext(messages, 'next action: write tests', settings, f.store, overhead);
    assert.deepEqual(projected.slice(0, 2), prefix); assert.equal(JSON.stringify(messages), snapshot);
    assert.match(JSON.stringify(projected), /next action: write tests/);
    assert.ok(contextTokens({system: overhead.system, tools: {}, messages: [...projected, ...overhead.tail]}) <= settings.maxTokens);
    const again = await projectContext(messages, 'next action: write tests', settings, f.store, overhead);
    assert.deepEqual(again, projected);
  }
  await assert.rejects(projectContext(messages, '', settings, f.store, {...overhead, system: 'x'.repeat(20000)}), /Pinned task/);
});

test('context evicts old exchanges before hiding the freshly requested read behind an artifact', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  const messages: ModelMessage[] = [{role: 'user', content: 'original contract'}];
  for (let turn = 0; turn < 4; turn++) {
    messages.push(...completion([['read_file', {path: `file-${turn}.js`}]]).messages);
    const call = (messages.at(-1)!.content as any[])[0];
    messages.push({role: 'tool', content: [{type: 'tool-result', toolName: 'read_file', toolCallId: call.toolCallId,
      output: {type: 'json', value: {content: `evidence-${turn}:` + 'fresh line\n'.repeat(450)}}}]});
  }
  const latest = structuredClone(messages.at(-1));
  const projected = await projectContext(messages, '', {...config().context, maxTokens: 2500, recentTurns: 3}, f.store);
  assert.deepEqual(projected.at(-1), latest, 'the next model call must still see the read it requested');
  assert.match(JSON.stringify(projected), /Earlier evidence index/);
  assert.ok(contextTokens({system: '', tools: {}, messages: projected}) <= 2500);
});

test('mixed mutation batches are serialized and all tool results precede attached images', async t => {
  const f = await toolkit(); t.after(f.cleanup); const settings = config();
  await fs.writeFile(path.join(f.workspace, 'image.png'), Buffer.from([137,80,78,71,13,10,26,10]));
  let active = 0, peak = 0;
  const read = f.sandbox.read.bind(f.sandbox);
  f.sandbox.read = async file => { active++; peak = Math.max(active, peak); await new Promise(r => setTimeout(r, 25)); try { return await read(file); } finally { active--; } };
  let request = 0;
  const model: Model = {complete: async args => {
    if (++request === 1) return completion([['read_file', {path: 'prime.cjs'}], ['view_image', {path: 'image.png'}],
      ['write_file', {path: 'new.txt', content: 'after barrier'}], ['read_file', {path: 'new.txt'}]]);
    const responses = args.messages.filter(message => message.role === 'tool');
    assert.equal(responses.length, 1); assert.equal((responses[0]!.content as any[]).length, 4);
    assert.match(JSON.stringify(responses[0]), /after barrier/);
    const imageIndex = args.messages.findIndex(message => Array.isArray(message.content) && message.content.some(part => part.type === 'image'));
    assert.ok(imageIndex > args.messages.indexOf(responses[0]!));
    return completion([['submit_candidate', {sourcePaths: [], requirements: ['primality'], rationale: 'missing artifacts; expected failure'}]]);
  }};
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'parallel', false);
  const session = new AgentSession({id: 'parallel', role: 'candidate', strategy: 'inspect', task: f.task, config: settings,
    model, sandbox: f.sandbox, baseline: f.sandbox, store: f.store, budget});
  await assert.rejects(session.run(2), /exhausted/); assert.equal(peak, 1, 'the entire batch is serialized when it contains mutations');
});

test('workspace receipts survive context projection and become stale after a later mutation', async t => {
  const f = await toolkit(); t.after(f.cleanup); const settings = config(); let turn = 0;
  const model: Model = {complete: async args => {
    turn++;
    assert.ok(args.tools.read_file, 'observations do not restrict discovery');
    const tail = args.messages.at(-1)!.content as string;
    const evidence = JSON.parse(/Workspace evidence: (.*)\. Execution is a command receipt/.exec(tail)![1]!);
    if (turn === 1) {
      assert.equal(evidence.runnerPresent, false); assert.equal(evidence.execution, null);
      return completion([['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nexit 1\n'}]]);
    }
    if (turn === 2) {
      assert.equal(evidence.runnerPresent, true); assert.equal(evidence.execution, null);
      return completion([['exec', {command: 'bash agent-tests/run-tests.sh'}]]);
    }
    if (turn === 3) {
      assert.equal(evidence.execution.current, true); assert.equal(evidence.execution.code, 1);
      const receipt = JSON.parse(await f.store.forSession('milestones').readArtifact(evidence.execution.artifact));
      assert.equal(receipt.code, 1);
      return completion([['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nexit 0\n'}]]);
    }
    if (turn === 4) {
      assert.equal(evidence.execution.current, false);
      return completion([['exec', {command: 'bash agent-tests/run-tests.sh'}]]);
    }
    assert.equal(evidence.execution.current, true); assert.equal(evidence.execution.code, 0);
    assert.match(tail, /coverage remain unverified/);
    return completion([['submit_candidate', {requirements: ['fixture only'], rationale: 'A receipt does not grant publication'}]]);
  }};
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'milestones', false);
  const session = new AgentSession({id: 'milestones', role: 'candidate', strategy: 'fixture', task: f.task,
    config: settings, model, sandbox: f.sandbox, baseline: f.sandbox, store: f.store, budget});
  await session.run(6); assert.equal(turn, 5); assert.ok(session.candidate);
});

test('transient retry classification honors Retry-After and never retries parser, timeout or authentication failures', () => {
  for (const error of [new Error('programming defect'), {name: 'TimeoutError'}, {name: 'AbortError'}, {name: 'AI_JSONParseError'}, {statusCode: 401}])
    assert.equal(retryPlan(error, 0, 2, 5000).retry, false);
  assert.equal(retryPlan({statusCode: 429, responseHeaders: {'Retry-After': '2'}}, 0, 1, 5000).delayMs, 2000);
  assert.equal(retryPlan({statusCode: 429, responseHeaders: {'retry-after': '60'}}, 0, 1, 5000).retry, false);
  assert.equal(retryPlan({cause: {code: 'ECONNRESET'}}, 0, 1, 5000).retry, true);
  assert.equal(retryPlan({statusCode: 502}, 1, 1, 5000).retry, false);
});

test('staged validation matches exhaustive selection while skipping unqualified cross cells and coalescing duplicate work', async t => {
  const f = await toolkit(); t.after(f.cleanup); const settings = config(); settings.execution.repeat = 1;
  const makeFile = (path: string, text: string) => ({path, data: Buffer.from(text).toString('base64'), mode: 0o644});
  const candidates: Candidate[] = [0, 1, 2].map(i => {
    const source = [makeFile('prime.cjs', repairs[i % 2]!)];
    const tests = [makeFile('agent-tests/check.cjs', suites[i % 2]!), makeFile('agent-tests/run-tests.sh', i === 2 ? 'exit 0' : 'node agent-tests/check.cjs')];
    return {id: `c${i}`, lineage: `c${i}`, source, tests, sourceHash: hash(source), testsHash: hash(tests), round: 0,
      strategy: '', sessionId: `c${i}`, requirements: ['primality'], rationale: ''};
  });
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'cells')); t.after(() => factory.close());
  let forks = 0; const fork = factory.fork.bind(factory); factory.fork = async label => { forks++; return fork(label); };
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'matrix', false);
  const validator = new Validator(factory, settings, f.store, budget, 'matrix');
  const [left, right] = await Promise.all([validator.evaluate(candidates[0]!.tests, [], 'x', 'baseline'), validator.evaluate(candidates[0]!.tests, [], 'y', 'baseline')]);
  assert.equal(forks, 1); assert.equal(left.testId, 'x'); assert.equal(right.testId, 'y');
  const staged = await validator.matrix(candidates);
  assert.equal(staged.length, 8);
  assert.equal(staged.some(cell => cell.testId === 'c2' && !['baseline', 'c2'].includes(cell.sourceId)), false);
  const exhaustive = [];
  for (const test of candidates) for (const source of [undefined, ...candidates])
    exhaustive.push(await validator.evaluate(test.tests, source?.source ?? [], test.id, source?.id ?? 'baseline'));
  assert.deepEqual(select(candidates, staged, 1).tests.map(c => c.id), select(candidates, exhaustive, 1).tests.map(c => c.id));
});

test('run directory lock rejects a second writer and releases after failure', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const {withRunLock} = await import('../src/lock.js');
  await withRunLock(f.out, async () => {
    await assert.rejects(withRunLock(f.out, async () => 2), /locked by another writer/);
  });
  await assert.rejects(withRunLock(f.out, async () => { throw new Error('test failure'); }), /test failure/);
  assert.equal(await withRunLock(f.out, async () => 3), 3);
});

test('output reservation fits the remaining dollar budget without increasing any cap', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.model.inputPricePerMillion = 1; settings.model.outputPricePerMillion = 2; settings.budget.maxCostUSD = .002;
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'cost', false);
  const reservation = await budget.reserve(1000, 8192);
  assert.equal(reservation.output, 500); assert.ok(budget.snapshot().costUSD! <= .002);
  await budget.settle(reservation, {input: 800, output: 100, cached: 0});
  const second = await budget.reserve(100, 8192); assert.equal(second.output, 450);
});

test('submission staging keeps the previous suite on write failure and rejects source paths before publication', async t => {
  const f = await toolkit(); t.after(f.cleanup); const {publishTests} = await import('../src/runtime.js');
  await f.sandbox.write('agent-tests/run-tests.sh', Buffer.from('exit 1\n'));
  const source = {path: 'prime.cjs', data: Buffer.from('bad').toString('base64'), mode: 0o644};
  await assert.rejects(publishTests(f.sandbox, [source]), /agent-tests/);
  const write = f.sandbox.write.bind(f.sandbox);
  f.sandbox.write = async (file, data, mode) => {
    if (file.includes('.testhelix-publish')) throw new Error('write failure');
    return write(file, data, mode);
  };
  await assert.rejects(publishTests(f.sandbox, [{...source, path: 'agent-tests/run-tests.sh'}]), /write failure/);
  assert.equal((await f.sandbox.read('agent-tests/run-tests.sh')).toString(), 'exit 1\n');
});

test('sealed candidate recovery restores matching history with stale memory and skips mismatched snapshots', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const {runTask} = await import('../src/pipeline.js'), {FixtureModel} = await import('./helpers.js');
  const delegate = new FixtureModel(), settings = config(); settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.review = false;
  const model: Model = {complete: async args => {
    const reply = await delegate.complete(args);
    return completion([['read_file', {path: 'prime.cjs'}], ['update_notes', {entries: [{id: 'api', kind: 'fact', text: 'isPrime is exported', paths: ['prime.cjs']}]}],
      ...reply.calls.map(call => [call.name, call.input] as [string, unknown])]);
  }};
  const result = await runTask(f.task, settings, f.out, {model}), candidate = result.candidates[0]!;
  const store = new Store(f.out), sandbox = new LocalSandbox(f.workspace, false);
  const budget = await Budget.open(settings, path.join(f.out, 'resume-test-budget.json'), 'resume-memory', false);
  const makeSession = () => new AgentSession({id: candidate.sessionId, role: 'candidate', strategy: candidate.strategy, task: f.task,
    config: settings, model, sandbox, baseline: sandbox, store, budget});
  const restored = makeSession(); await restored.restoreSealed(candidate);
  assert.ok(restored.messages.some(message => message.role === 'tool')); assert.match(restored.toolbox.notes, /STALE/);
  const mismatch = makeSession(); await mismatch.restoreSealed({...candidate, sourceHash: 'different'});
  assert.equal(mismatch.messages.some(message => message.role === 'tool'), false);
});

test('readable context archives retain evidence without encrypted protocol data or nested artifact bodies', async t => {
  const f = await toolkit(); t.after(f.cleanup);
  const messages: ModelMessage[] = [{role: 'user', content: 'Public contract'}];
  for (let turn = 0; turn < 4; turn++) {
    const name = turn === 1 ? 'read_artifact' : 'read_file';
    messages.push({role: 'assistant', content: [
      {type: 'reasoning', text: '', providerOptions: {openai: {reasoningEncryptedContent: `OPAQUE_${turn}_` + 'z'.repeat(turn === 3 ? 16 : 9000)}}},
      {type: 'tool-call', toolCallId: `r${turn}`, toolName: name, input: {path: 'public.py'}}]});
    messages.push({role: 'tool', content: [{type: 'tool-result', toolCallId: `r${turn}`, toolName: name,
      output: {type: 'json', value: turn === 1 ? {ok: true, id: 'a'.repeat(64), offset: 0, nextOffset: 8000,
        totalChars: 12000, content: 'NESTED_HISTORY_BODY'.repeat(500)} : {content: `PUBLIC_EVIDENCE_${turn}`}}}]});
  }
  const original = JSON.stringify(messages);
  const view = await projectContext(messages, '', {...config().context, maxTokens: 2200, recentTurns: 1}, f.store);
  const archiveId = JSON.stringify(view).match(/Earlier evidence index: artifact ([a-f0-9]{64})/)?.[1];
  assert.ok(archiveId); const readable = await f.store.readArtifact(archiveId);
  assert.match(readable, /PUBLIC_EVIDENCE_0/); assert.match(readable, /aaaaaaaaaaaaaaaa/);
  assert.doesNotMatch(readable, /OPAQUE_|reasoningEncryptedContent|NESTED_HISTORY_BODY/);
  assert.match(JSON.stringify(view), /OPAQUE_3_/, 'live stateless reasoning must still round-trip');
  assert.equal(JSON.stringify(messages), original, 'canonical provider history is immutable');
});
