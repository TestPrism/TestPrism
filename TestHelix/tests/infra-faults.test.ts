import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {command, mapLimit, shellQuote} from '../src/util.js';
import {Budget} from '../src/budget.js';
import {LocalFactory, type SandboxFactory} from '../src/runtime.js';
import {runTask} from '../src/pipeline.js';
import {AgentSession} from '../src/agent.js';
import {Store} from '../src/store.js';
import {config, fixture, FixtureModel, completion} from './helpers.js';
import type {Model} from '../src/types.js';
import {classify, Validator} from '../src/validation.js';

function deferred() { let resolve!: () => void; const promise = new Promise<void>(r => {resolve = r;}); return {promise, resolve}; }

test('batch failure drains active work before returning and stops admitting queued work', async () => {
  const failed = deferred(), sibling = deferred(), started: number[] = [];
  let finished = false;
  const running = mapLimit([0, 1, 2, 3], 2, async item => {
    started.push(item);
    if (item === 0) {await failed.promise; throw new Error('injected worker failure');}
    await sibling.promise;
  });
  const settled = running.then(() => {finished = true;}, () => {finished = true;});
  failed.resolve(); await new Promise<void>(resolve => setImmediate(resolve));
  try { assert.equal(finished, false, 'resource cleanup must wait for the live sibling'); }
  finally {sibling.resolve(); await settled;}
  await assert.rejects(running, /injected worker failure/);
  assert.deepEqual(started, [0, 1]);
});

test('successful bounded commands reap background descendants without waiting for the tool timeout', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const pidfile = path.join(f.root, 'descendant.pid');
  const result = await command(['sh', '-c', `sleep 30 >/dev/null 2>&1 & printf %s "$!" > ${shellQuote(pidfile)}`], {timeoutMs: 3000});
  const pid = Number(await fs.readFile(pidfile, 'utf8'));
  t.after(() => {try {process.kill(pid, 'SIGKILL');} catch {}});
  assert.equal(result.code, 0); assert.equal(result.timedOut, false);
  // SIGKILL delivery is asynchronous even though the shell has already exited.
  // Bound the wait well below the command timeout; do not require a procfs read
  // to race ahead of the kernel's final state transition on a loaded host.
  let state = '';
  for (let attempt = 0; attempt < 25; attempt++) {
    state = await fs.readFile(`/proc/${pid}/stat`, 'utf8').catch(() => '');
    if (!state || state.split(' ')[2] === 'Z') break;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(!state || state.split(' ')[2] === 'Z', 'a completed command must not keep mutating its workspace');
});

test('process capture retains the entire bounded prefix and latest suffix across repeated overflow', async () => {
  const source = 'for(let i=0;i<12;i++){process.stdout.write(String(i).repeat(181));await new Promise(r=>setTimeout(r,2));}';
  const result = await command([process.execPath, '--input-type=module', '-e', source], {maxBytes: 1000, timeoutMs: 3000});
  const complete = Array.from({length: 12}, (_, i) => String(i).repeat(181)).join('');
  assert.equal(result.truncated, true); assert.equal(result.code, 0);
  assert.equal(result.stdout, complete.slice(0, 500) + complete.slice(-500));
});

test('a child that rejects a large stdin payload returns its exit status without crashing the host', async () => {
  const result = await command(['sh', '-c', 'exit 7'], {input: Buffer.alloc(2_000_000), timeoutMs: 3000});
  assert.equal(result.code, 7); assert.equal(result.timedOut, false);
});

test('a resumed unresolved reservation is charged in full and marked estimated', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const file = path.join(f.root, 'ledger.json');
  const original = await Budget.open(settings, file, 'crash', false);
  await original.reserve(900, 500);
  const restored = await Budget.open(settings, file, 'crash', true);
  assert.equal(restored.snapshot().input, 900); assert.equal(restored.snapshot().output, 500);
  assert.equal(restored.snapshot().estimated, true); assert.equal(await restored.waitForSettlement(), false);
});

test('resume rejects malformed reservations and inconsistent totals instead of reopening spend capacity', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const file = path.join(f.root, 'ledger.json');
  const original = await Budget.open(settings, file, 'corruption', false);
  const reservation = await original.reserve(900, 500);
  const valid = JSON.parse(await fs.readFile(file, 'utf8'));
  const changes = [{pending: []}, {estimated: 'false'}, {settled: [reservation.id]},
    {input: 800}, {cached: 200}, {pending: {[reservation.id]: {...reservation, output: -1}}},
    {pending: {wrong: reservation}}];
  for (const change of changes) {
    await fs.writeFile(file, JSON.stringify({...valid, ...change}));
    await assert.rejects(Budget.open(settings, file, 'corruption', true), /Invalid budget checkpoint/);
  }
});

test('candidate fork failure preserves a successful sibling and closes the factory after all work', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config(); settings.search.review = false;
  settings.search.provisionalPolicy = 'baseline-only';
  const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
  let closed = false, live = 0;
  const delegate = new FixtureModel();
  const factory: SandboxFactory = {
    fork: async label => {if (label === 'candidate-1') throw new Error('injected candidate start failure'); return base.fork(label);},
    publish: files => base.publish(files), close: async () => {assert.equal(live, 0); closed = true; await base.close();},
  };
  const result = await runTask(f.task, settings, f.out, {factory, model: {complete: async args => {
    live++; try {await new Promise(r => setTimeout(r, 30)); assert.equal(closed, false); return await delegate.complete(args);} finally {live--;}
  }}});
  assert.equal(result.status, 'provisional'); assert.equal(result.candidates.length, 1); assert.equal(closed, true);
});

test('a review workspace start failure is recorded without discarding generated test artifacts', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
  const factory: SandboxFactory = {
    fork: label => label.startsWith('review-') ? Promise.reject(new Error('injected reviewer start failure')) : base.fork(label),
    publish: files => base.publish(files), close: () => base.close(),
  };
  const result = await runTask(f.task, settings, f.out, {factory, model: new FixtureModel()});
  assert.equal(result.status, 'no_submit'); assert.equal(result.candidates.length, 2);
  assert.equal(result.testsDir, null);
  await assert.rejects(fs.stat(path.join(f.workspace, 'agent-tests')));
  assert.match(await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8'), /review.failed/);
});

test('failure to persist a budget cannot leave peers waiting for a nonexistent request', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config(); settings.budget.wallMs = 3000;
  const file = path.join(f.root, 'ledger.json'), budget = await Budget.open(settings, file, 'disk-error', false);
  await fs.rm(file); await fs.mkdir(file);
  await assert.rejects(budget.reserve(500, 500));
  assert.equal(await budget.waitForSettlement(), false);
});

test('run metadata write failure still closes an already acquired factory without sending a model request', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await fs.mkdir(path.join(f.out, 'run.json'), {recursive: true});
  const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
  let closed = false; const model = new FixtureModel();
  const factory: SandboxFactory = {fork: label => base.fork(label), publish: files => base.publish(files),
    close: async () => {closed = true; await base.close();}};
  await assert.rejects(runTask(f.task, config(), f.out, {factory, model}));
  assert.equal(closed, true); assert.equal(model.calls, 0);
});

test('baseline close failure cannot bypass factory cleanup or commit a successful run result', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const base = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => base.close());
  let closed = false;
  const factory: SandboxFactory = {fork: async label => {
    const sandbox = await base.fork(label);
    if (label === 'baseline-reference') {
      const close = sandbox.close.bind(sandbox); let first = true;
      sandbox.close = async () => {if (first) {first = false; throw new Error('injected close failure');} await close();};
    }
    return sandbox;
  }, publish: files => base.publish(files), close: async () => {closed = true; await base.close();}};
  await assert.rejects(runTask(f.task, config(), f.out, {factory, model: new FixtureModel()}), /injected close failure/);
  assert.equal(closed, true); await assert.rejects(fs.stat(path.join(f.out, 'result.json')));
});

test('publishing a replacement suite removes stale mirror files left by an interrupted attempt', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const stale = path.join(f.out, 'submission/agent-tests/obsolete.cjs');
  await fs.mkdir(path.dirname(stale), {recursive: true}); await fs.writeFile(stale, 'throw new Error("stale");');
  const result = await runTask(f.task, config(), f.out, {model: new FixtureModel()});
  assert.equal(result.status, 'validated'); await assert.rejects(fs.stat(stale));
});

test('local crash recovery removes owned branches and reuses the immutable snapshot instead of overlaying current source', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const runs = path.join(f.root, 'branches'), original = await fs.readFile(path.join(f.workspace, 'prime.cjs'));
  const lostController = await LocalFactory.create(f.workspace, runs);
  const orphan = await lostController.fork('orphan');
  await fs.writeFile(path.join(f.workspace, 'prime.cjs'), 'current workspace has changed');
  const restored = await LocalFactory.create(f.workspace, runs); t.after(() => restored.close());
  await assert.rejects(fs.stat(orphan.root));
  const next = await restored.fork('resumed'); assert.deepEqual(await next.read('prime.cjs'), original);
  await restored.close(); assert.deepEqual(await fs.readdir(runs), []);
});

test('local snapshots reject an output alias that resolves inside the original workspace', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const alias = path.join(f.root, 'output-alias'); await fs.symlink(f.workspace, alias);
  await assert.rejects(LocalFactory.create(f.workspace, path.join(alias, 'runs')), /outside the task workspace/);
  await assert.rejects(fs.stat(path.join(f.workspace, 'runs')));
});

test('session tools cannot read a peer or host artifact even when its exact digest is known', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'scope', false);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline');
  const sessions = await Promise.all(['candidate-1', 'candidate-2', 'review-candidate-1-r0'].map(async id => new AgentSession({
    id, role: id.startsWith('review') ? 'reviewer' : 'candidate', strategy: '', task: f.task, config: settings,
    model: new FixtureModel(), sandbox: await factory.fork(id), baseline, store, budget,
  })));
  const privateDigest = await sessions[0]!.options.store.artifact('PRIVATE_SOURCE_REASONING');
  const hostDigest = await store.artifact('UNSHARED_VALIDATION_EVIDENCE');
  const read = (session: AgentSession, id: string) => session.toolbox.execute('read_artifact', {id}, new AbortController().signal);
  assert.equal((await read(sessions[0]!, privateDigest)).content, 'PRIVATE_SOURCE_REASONING');
  for (const session of sessions.slice(1)) await assert.rejects(read(session, privateDigest), {code: 'ENOENT'});
  for (const session of sessions) await assert.rejects(read(session, hostDigest), {code: 'ENOENT'});
  await assert.rejects(sessions[2]!.toolbox.execute('update_notes', {entries: [
    {id: 'leak', kind: 'fact', text: 'peer repair', artifacts: [privateDigest]},
  ]}, new AbortController().signal), {code: 'ENOENT'});
  assert.throws(() => store.forSession('../candidate-1'), /Invalid artifact scope/);
});

test('correction can page through explicitly granted validation evidence after artifact isolation', async t => {
  const f = await fixture(); t.after(f.cleanup); const delegate = new FixtureModel(); let evidenceReads = 0;
  const settings = config(); settings.search.maxRounds = 2; settings.search.review = false;
  const model: Model = {complete: async args => {
    const correction = args.messages.find(message => message.role === 'user' && typeof message.content === 'string'
      && message.content.startsWith('Your previous source and tests are still present.'));
    if (correction && typeof correction.content === 'string') {
      const feedback = JSON.parse(correction.content.slice(correction.content.indexOf('\n') + 1));
      const result = args.messages.flatMap(message => message.role === 'tool' ? message.content : [])
        .find(part => part.type === 'tool-result' && part.toolName === 'read_artifact');
      if (!result) return completion([['read_artifact', {id: feedback.peerResults.find((cell: {artifact?: string}) => cell.artifact).artifact}]]);
      assert.equal(result.output.type, 'json');
      const output = (result.output as {type: 'json'; value: {ok: boolean; content: string}}).value;
      assert.equal(output.ok, true); assert.match(output.content, /AssertionError/); evidenceReads++;
      return delegate.complete(args);
    }
    const reply = await delegate.complete(args);
    const source = reply.calls.find(call => call.name === 'write_file' && (call.input as {path: string}).path === 'prime.cjs')!;
    (source.input as {content: string}).content = 'exports.isPrime = () => true;\n';
    return reply;
  }};
  const result = await runTask(f.task, settings, f.out, {model});
  assert.equal(evidenceReads, 2); assert.equal(result.status, 'validated');
});

test('telemetry failure before sending a request releases the reservation without charging tokens', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'telemetry', false);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  const baseline = await factory.fork('baseline'), model = new FixtureModel();
  const session = new AgentSession({id: 'candidate-1', role: 'candidate', strategy: '', task: f.task, config: settings,
    model, sandbox: await factory.fork('candidate-1'), baseline, store, budget});
  const event = session.options.store.event.bind(session.options.store);
  session.options.store.event = (type, data) => type === 'model.requested'
    ? Promise.reject(new Error('injected request telemetry failure')) : event(type, data);
  await assert.rejects(session.run(1), /injected request telemetry failure/);
  assert.equal(model.calls, 0); assert.equal(await budget.waitForSettlement(), false);
  assert.equal(budget.snapshot().input, 0); assert.equal(budget.snapshot().output, 0);
});

test('multiline syntax diagnostics blame the generated file, preserving defects in imported product source as failures', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const testDir = path.join(f.workspace, 'agent-tests'); await fs.mkdir(testDir);
  for (const [runtime, extension, invalid, importer] of [
    ['python3', 'py', 'def broken(:\n', 'import sys; sys.path.insert(0, "."); import broken\n'],
    [process.execPath, 'cjs', 'const = ;\n', 'require("../broken.cjs");\n'],
  ]) {
    const testFile = `agent-tests/check.${extension}`;
    await fs.writeFile(path.join(f.workspace, testFile), invalid!);
    const generated = await command([runtime!, testFile], {cwd: f.workspace});
    assert.equal(classify(generated.code, generated.stdout + generated.stderr), 'error', extension);
    await fs.writeFile(path.join(f.workspace, `broken.${extension}`), invalid!);
    await fs.writeFile(path.join(f.workspace, testFile), importer!);
    const product = await command([runtime!, testFile], {cwd: f.workspace});
    assert.equal(classify(product.code, product.stdout + product.stderr), 'fail', extension);
  }
});

test('truncated process output cannot qualify as baseline failure or a passing repair', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'truncated', false);
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'branches')); t.after(() => factory.close());
  for (const code of [0, 1]) {
    const limited: SandboxFactory = {fork: async label => {
      const sandbox = await factory.fork(label), exec = sandbox.exec.bind(sandbox);
      sandbox.exec = async (...args) => ({...await exec(...args), truncated: args[0] === 'bash agent-tests/run-tests.sh'});
      return sandbox;
    }, publish: files => factory.publish(files), close: () => factory.close()};
    const validator = new Validator(limited, settings, store, budget, 'truncated');
    const result = await validator.evaluate([{path: 'agent-tests/run-tests.sh', mode: 493,
      data: Buffer.from(`echo captured-prefix; exit ${code}\n`).toString('base64')}], [], 'test', 'baseline');
    assert.equal(result.status, 'error'); assert.match(result.output, /output exceeded/);
  }
});
