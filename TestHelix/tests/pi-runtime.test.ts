import test from 'node:test';
import assert from 'node:assert/strict';
import type {ModelMessage} from 'ai';
import {piSummary, toPiEvidence} from '../src/pi-runtime.js';
import {dispatchTools} from '../src/tool-dispatch.js';
import {contextSnapshot, contextTokens, projectContext, restoreContext} from '../src/context.js';
import {Store} from '../src/store.js';
import {Toolbox, workspaceTools} from '../src/tools.js';
import {LocalSandbox} from '../src/runtime.js';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {completion, config, fixture} from './helpers.js';
import {hash, requestSize} from '../src/util.js';
import path from 'node:path';

const summaryReply = (text = 'Observed: source inspected; tests still missing. Next: write a justified case.') =>
  ({...completion([]), text, finish: 'stop', messages: [{role: 'assistant' as const, content: text}]});
const hugeHistory = (): ModelMessage[] => [{role: 'user', content: 'Original public contract must stay exact.'},
  {role: 'assistant', content: [
    {type: 'reasoning', text: '', providerOptions: {openai: {itemId: 'rs_fixture', reasoningEncryptedContent: 'OPAQUE_SECRET_'.repeat(10000)}}},
    {type: 'tool-call', toolCallId: 'inspect', toolName: 'read_file', input: {path: 'prime.cjs'}}]},
  {role: 'tool', content: [{type: 'tool-result', toolCallId: 'inspect', toolName: 'read_file', output: {type: 'json', value: {content: 'Observed public source.'}}}]},
  {role: 'user', content: 'Latest correction: preserve the documented negative input behavior.'}];

test('Pi compacts a huge latest native exchange, retains the task and correction, and resumes without a second summary', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out).forSession('pi-context'); await store.init();
  const history = hugeHistory(), original = structuredClone(history); let summaries = 0;
  const cfg = {maxTokens: 2500, recentTurns: 1, toolOutputChars: 1000};
  const summarize = async (system: string, messages: ModelMessage[]) => {
    summaries++;
    assert.doesNotMatch(JSON.stringify(messages), /OPAQUE_SECRET/);
    assert.match(JSON.stringify(messages), /Observed public source/);
    assert.match(system + JSON.stringify(messages), /structured context checkpoint/i);
    return summaryReply();
  };
  const view = await projectContext(history, '', cfg, store, {summarize});
  assert.equal(summaries, 1); assert.deepEqual(history, original);
  assert.deepEqual(view[0], original[0]); assert.ok(view.some(m => m.content === original.at(-1)!.content));
  assert.ok(contextTokens(view) < cfg.maxTokens); assert.doesNotMatch(JSON.stringify(view), /OPAQUE_SECRET/);
  const state = contextSnapshot(history)!;
  assert.match(await store.readArtifact(state.summaryArtifact!), /Observed: source inspected/);
  assert.match(await store.readArtifact(state.archive!), /Observed public source/);
  const resumed = structuredClone(history); restoreContext(resumed, state);
  assert.deepEqual(await projectContext(resumed, '', cfg, store, {summarize}), view);
  assert.equal(summaries, 1);
});

test('Pi summary rejects truncated output and tools; failed summary falls back without replaying completed tools', async t => {
  for (const reply of [{...summaryReply(), finish: 'length'}, completion([['exec', {command: 'false'}]])])
    await assert.rejects(piSummary(hugeHistory(), undefined, async () => reply), /truncat|limit|token cap|tool/i);
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const history = hugeHistory(), original = structuredClone(history);
  const view = await projectContext(history, '', {maxTokens: 2000, recentTurns: 1, toolOutputChars: 1000}, store,
    {summarize: async () => {throw new Error('Provider unavailable');}});
  assert.deepEqual(history, original); assert.match(JSON.stringify(view), /Semantic summary unavailable/);
  assert.equal(view.some(m => m.role === 'tool'), false);
  assert.deepEqual(toPiEvidence({role: 'assistant', content: 'Conclusion'})[0]?.role, 'assistant');
});

test('incomplete tool exchanges cannot be compacted into a fresh continuation', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const history = hugeHistory().filter(m => m.role !== 'tool'); let summaries = 0;
  await assert.rejects(projectContext(history, '', {maxTokens: 2000, recentTurns: 1, toolOutputChars: 1000}, store,
    {summarize: async () => {summaries++; return summaryReply();}}), /exceed context/);
  assert.equal(summaries, 0);
});

test('compaction reaches the configured target with native metadata and leaves room for the next exchange', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const history: ModelMessage[] = [{role: 'user', content: 'Keep the public contract exact.'}];
  for (let i = 0; i < 12; i++) history.push(
    {role: 'assistant', content: [
      {type: 'reasoning', text: '', providerOptions: {openai: {
        reasoningEncryptedContent: Array.from({length: 30}, (_, j) => hash(`${i}:${j}`)).join(''),
      }}}, {type: 'tool-call', toolCallId: `read-${i}`, toolName: 'read_file', input: {path: `file-${i}.py`}}]},
    {role: 'tool', content: [{type: 'tool-result', toolCallId: `read-${i}`, toolName: 'read_file',
      output: {type: 'json', value: {content: `Public evidence ${i}. `.repeat(30)}}}]},
  );
  const original = structuredClone(history), cfg = {maxTokens: 6000, compactTarget: .5,
    recentTurns: 1, toolOutputChars: 1000, tokenizer: 'o200k_base' as const};
  let summaries = 0;
  const overhead = {system: 'Original rules.', tools: {}, summarize: async () => {summaries++; return summaryReply();}};
  const view = await projectContext(history, '', cfg, store, overhead);
  const count = (messages: ModelMessage[]) => contextTokens({system: overhead.system, tools: {}, messages}, cfg.tokenizer);
  assert.ok(count(view) <= cfg.maxTokens * cfg.compactTarget, `${count(view)} tokens did not reach the target`);
  assert.deepEqual(history, original); assert.equal(summaries, 1);
  history.push({role: 'assistant', content: 'A small next observation.'}, {role: 'user', content: 'Continue.'});
  await projectContext(history, '', cfg, store, overhead);
  assert.equal(summaries, 1, 'a small next exchange must not immediately buy another summary');
});

test('parallel reads are bounded; unavailable calls settle separately and duplicate IDs never execute', async () => {
  let running = 0, peak = 0;
  const reply = completion(Array.from({length: 6}, (_, i) => ['read_file', {path: `file${i}`} ]));
  const values = await dispatchTools(reply, workspaceTools, new Set(['read_file']), async call => {
    running++; peak = Math.max(peak, running);
    await new Promise(resolve => setTimeout(resolve, 5)); running--; return call.id;
  }, AbortSignal.timeout(1000), 2);
  assert.equal(peak, 2); assert.deepEqual(values, reply.calls.map(call => call.id));
  const unavailable = completion([['missing', {number: 1}], ['missing', {number: 2}]]);
  assert.deepEqual(await dispatchTools(unavailable, {}, new Set(), async call => call.input, AbortSignal.timeout(1000)), [{number: 1}, {number: 2}]);
  unavailable.calls[1]!.id = unavailable.calls[0]!.id;
  await assert.rejects(dispatchTools(unavailable, {}, new Set(), async () => {throw new Error('must not run');}, AbortSignal.timeout(1000)), /Duplicate/);
});

test('Pi edit preserves CRLF and BOM, rejects ambiguous/stale edits and cannot access outside the sandbox', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false), store = new Store(f.out); await store.init();
  const toolbox = new Toolbox(sandbox, store, 1200, 2000), signal = AbortSignal.timeout(5000);
  const original = '\ufeffconst value = 1;\r\nconst other = 2;\r\n';
  await sandbox.write('sample.js', Buffer.from(original));
  await toolbox.execute('edit_file', {path: 'sample.js', old: 'const value = 1;', replacement: 'const value = 3;', expectedHash: hash(original)}, signal);
  const edited = (await sandbox.read('sample.js')).toString();
  assert.equal(edited, original.replace('value = 1', 'value = 3'));
  await assert.rejects(toolbox.execute('edit_file', {path: 'sample.js', old: 'const', replacement: 'let'}, signal), /multiple|unique|occurrence/i);
  await assert.rejects(toolbox.execute('edit_file', {path: 'sample.js', old: 'value = 3', replacement: 'value = 4', expectedHash: hash(original)}, signal), /Stale/);
  for (const file of ['../outside', '/etc/passwd', 'solution.sh'])
    await assert.rejects(toolbox.execute('write_file', {path: file, content: 'forbidden'}, signal));
  assert.equal((await sandbox.read('sample.js')).toString(), edited);
  const failed = await toolbox.execute('exec', {command: 'printf failed >&2; exit 7'}, signal);
  assert.equal(failed.code, 7); assert.equal(failed.stderr, 'failed'); assert.equal(failed.ok, false);
});

test('semantic summary calls share AgentSession budget and telemetry with generation', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.context.maxTokens = 8000; cfg.search.skills = [];
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'pi-budget', false);
  let summaries = 0, generations = 0;
  const session = new AgentSession({id: 'pi-budget', role: 'candidate', strategy: '', task: f.task, config: cfg, budget, store,
    sandbox: new LocalSandbox(f.workspace, false), baseline: new LocalSandbox(f.workspace, false), model: {complete: async args => {
      if (!Object.keys(args.tools).length) {summaries++; return summaryReply();}
      generations++; return completion([]);
    }}});
  session.messages.push(...hugeHistory().slice(1));
  await assert.rejects(session.run(3), /without a valid/);
  assert.equal(summaries, 1); assert.equal(generations, 3);
  const snapshot = budget.snapshot(); assert.equal(snapshot.calls, 4); assert.equal(snapshot.input, 480);
});

test('recursive checkpoints reuse bounded memory while full summaries remain archived', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const history = hugeHistory(), cfg = {maxTokens: 2500, recentTurns: 1, toolOutputChars: 1000, compactMaxOutputTokens: 1024};
  const full = 'ARCHIVED_ONLY_BEGINNING ' + 'Earlier observed evidence. '.repeat(2000);
  await projectContext(history, '', cfg, store, {summarize: async () => summaryReply(full)});
  const first = contextSnapshot(history)!;
  assert.equal(await store.readArtifact(first.summaryArtifact!), full);
  history.push(...hugeHistory().slice(1).map(message => structuredClone(message)));
  let summaries = 0;
  await projectContext(history, '', cfg, store, {summarize: async (_system, messages, output) => {
    summaries++;
    assert.doesNotMatch(JSON.stringify(messages), /ARCHIVED_ONLY_BEGINNING/);
    assert.ok(JSON.stringify(messages).length < 12000, 'old full summaries must not grow the next request');
    assert.ok(output <= 1024);
    return summaryReply();
  }});
  assert.equal(summaries, 1);
  assert.equal(await store.readArtifact(first.summaryArtifact!), full);
});

test('repeated compaction exhausts its time share before consuming artifact actions', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.context.maxTokens = 8000; cfg.search.skills = [];
  cfg.context.compactTimeFraction = .1; cfg.context.compactTimeoutMs = 25000; cfg.context.compactMaxOutputTokens = 1536;
  t.mock.timers.enable({apis: ['Date'], now: Date.now()});
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'summary-time', false);
  const sandbox = new LocalSandbox(f.workspace, false); let summaries = 0, actions = 0;
  const session = new AgentSession({id: 'summary-time', role: 'candidate', strategy: '', task: f.task,
    config: cfg, budget, store, sandbox, baseline: sandbox, model: {complete: async args => {
      if (!Object.keys(args.tools).length) {
        summaries++; assert.ok(args.maxOutputTokens <= 1536);
        t.mock.timers.setTime(Date.now() + 10000);
        return summaryReply();
      }
      if (++actions === 1) {
        const reply = completion([['read_file', {path: 'prime.cjs'}]]);
        const native = reply.messages[0]!.content;
        assert.ok(Array.isArray(native));
        native.unshift({type: 'reasoning', text: '', providerOptions: {openai: {reasoningEncryptedContent: 'OPAQUE'.repeat(30000)}}});
        return reply;
      }
      assert.match(JSON.stringify(args.messages), /Semantic summary unavailable/);
      return completion([
        ['write_file', {path: 'agent-tests/run-tests.sh', content: 'exit 1\n'}],
        ['submit_candidate', {requirements: ['public contract'], rationale: 'unverified artifact'}],
      ]);
    }}});
  session.messages.push(...hugeHistory().slice(1));
  await session.run(4);
  assert.equal(summaries, 1); assert.equal(actions, 2); assert.ok(session.candidate);
  assert.ok(budget.remainingMs >= 50000);
});

test('crossing the time boundary during projection rebuilds final artifact instructions', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.context.maxTokens = 8000; cfg.search.skills = [];
  t.mock.timers.enable({apis: ['Date'], now: Date.now()});
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'projection-time', false);
  const event = store.event.bind(store); let delayed = false;
  store.event = async (type, data) => {
    if (type === 'context.compacted' && !delayed) {delayed = true; t.mock.timers.setTime(Date.now() + 32000);}
    await event(type, data);
  };
  const sandbox = new LocalSandbox(f.workspace, false); let summaries = 0, actions = 0;
  const session = new AgentSession({id: 'projection-time', role: 'candidate', strategy: '', task: f.task,
    config: cfg, budget, store, sandbox, baseline: sandbox, model: {complete: async args => {
      if (!Object.keys(args.tools).length) {summaries++; return summaryReply();}
      actions++;
      assert.equal(args.tools.read_file, undefined);
      assert.match(JSON.stringify(args.messages), /Host turn limit: this is your last response/);
      return completion([
        ['write_file', {path: 'agent-tests/run-tests.sh', content: 'exit 1\n'}],
        ['submit_candidate', {requirements: ['public contract'], rationale: 'unverified artifact'}],
      ]);
    }}});
  // AgentSession scopes the store; inject the delayed projection event there.
  session.options.store.event = store.event;
  session.messages.push(...hugeHistory().slice(1));
  await session.run(4);
  assert.equal(summaries, 1); assert.equal(actions, 1); assert.ok(session.candidate);
});

test('the final response can recover archived evidence without paying for a summary', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.context.maxTokens = 8000; cfg.search.skills = [];
  const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/run-tests.sh', Buffer.from('exit 1\n'));
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'late-archive', false);
  let calls = 0;
  const session = new AgentSession({id: 'late-archive', role: 'candidate', strategy: '', task: f.task,
    config: cfg, budget, store, sandbox, baseline: sandbox, model: {complete: async args => {
      calls++; assert.ok(args.tools.read_artifact); assert.ok(args.tools.submit_candidate);
      assert.equal(args.tools.list_files, undefined, 'late archival does not reopen broad discovery');
      const id = JSON.stringify(args.messages).match(/Full evidence artifact ([a-f0-9]{64})/)?.[1];
      assert.ok(id, 'oversized evidence must be archived and its ID preserved');
      return completion([['read_artifact', {id}]]);
    }}});
  session.messages.push(...hugeHistory().slice(1));
  await session.run(1); // Host recovers the existing files after the evidence read.
  assert.equal(calls, 1); assert.equal(budget.snapshot().calls, 1); assert.ok(session.candidate);
  const results = session.messages.flatMap(m => m.role === 'tool' ? m.content.filter(p => p.type === 'tool-result') : []);
  const recovered = results.findLast(p => p.toolName === 'read_artifact');
  assert.ok(recovered?.output.type === 'json');
  assert.equal((recovered.output.value as {ok: boolean}).ok, true, 'the archive is readable in this session');
});

test('Pi path convenience normalization cannot silently change literal public filenames', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false), store = new Store(f.out); await store.init();
  const toolbox = new Toolbox(sandbox, store, 1200, 2000), signal = AbortSignal.timeout(5000);
  for (const file of ['@literal.txt', 'unicode\u3000space.txt', '~/literal.txt', 'nested/file.txt']) {
    await toolbox.execute('write_file', {path: file, content: 'expected original bytes'}, signal);
    await toolbox.execute('edit_file', {path: file, old: 'original', replacement: 'changed'}, signal);
    assert.equal((await sandbox.read(file)).toString(), 'expected changed bytes');
    const read = await toolbox.execute('read_file', {path: file}, signal);
    assert.match(String(read.content), /expected changed bytes/);
  }
  for (const file of ['literal.txt', 'unicode space.txt']) await assert.rejects(sandbox.mode(file), /ENOENT/);
});

test('compaction cannot consume the final generation call', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.context.maxTokens = 8000; cfg.search.skills = []; cfg.budget.maxCalls = 1;
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'last-call', false);
  let generations = 0;
  const session = new AgentSession({id: 'last-call', role: 'candidate', strategy: '', task: f.task, config: cfg, budget, store,
    sandbox: new LocalSandbox(f.workspace, false), baseline: new LocalSandbox(f.workspace, false), model: {complete: async args => {
      assert.ok(Object.keys(args.tools).length, 'No paid summary may steal the last action');
      assert.match(JSON.stringify(args.messages), /Semantic summary unavailable/);
      generations++; return completion([]);
    }}});
  session.messages.push(...hugeHistory().slice(1));
  await assert.rejects(session.run(1), /without a valid/);
  assert.equal(generations, 1); assert.equal(budget.snapshot().calls, 1);
});

test('impossible pinned input is rejected before buying a summary; post-summary reservations use the current budget', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  let summaries = 0, cap = 100;
  const cfg = {maxTokens: 2500, recentTurns: 1, toolOutputChars: 1000};
  const history = hugeHistory();
  await assert.rejects(projectContext(history, '', cfg, store, {system: 'Pinned rules '.repeat(100), maxInputReservation: () => cap,
    summarize: async () => {summaries++; return summaryReply();}}), /remaining input reservation/);
  assert.equal(summaries, 0);
  cap = 10000;
  const view = await projectContext(history, '', cfg, store, {maxInputReservation: () => cap,
    summarize: async (_system, _messages, _output, continuationInput) => {
      assert.ok(continuationInput! > 1800); summaries++; cap = 2200;
      return summaryReply('A complete reusable summary. '.repeat(300));
    }});
  assert.equal(summaries, 1); assert.ok(JSON.stringify(view).length < cap);
});

test('a shrinking shared budget can archive another completed exchange without buying a second summary', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const history: ModelMessage[] = [{role: 'user', content: 'Immutable public task.'},
    {role: 'assistant', content: 'Earlier observations. '.repeat(3000)},
    {role: 'user', content: 'Continue with the original contract.'},
    {role: 'assistant', content: [{type: 'reasoning', text: '', providerOptions: {openai: {reasoningEncryptedContent: 'OPAQUE_LATEST_'.repeat(1000)}}}, {type: 'tool-call', toolCallId: 'recent', toolName: 'read_file', input: {path: 'prime.cjs'}}]},
    {role: 'tool', content: [{type: 'tool-result', toolCallId: 'recent', toolName: 'read_file', output: {type: 'json', value: {content: 'RECENT_EVIDENCE '.repeat(120)}}}]},
    {role: 'user', content: 'Keep this final correction verbatim.'}];
  const original = structuredClone(history); let cap = 100000, summaries = 0, summarySawRecent = false;
  const view = await projectContext(history, '', {maxTokens: 6500, compactTarget: .5, recentTurns: 1, toolOutputChars: 1000}, store,
    {maxInputReservation: () => cap, summarize: async (_system, messages) => {
      // Pi initially retains the latest exchange, which fits the old allowance.
      summarySawRecent = JSON.stringify(messages).includes('RECENT_EVIDENCE');
      summaries++; cap = 1600; return summaryReply();
    }});
  assert.equal(summaries, 1); assert.equal(summarySawRecent, false); assert.deepEqual(history, original);
  assert.ok(requestSize({system: '', messages: view, tools: {}}).bytes + view.length * 64 <= cap);
  assert.ok(view.some(m => m.content === original.at(-1)!.content));
  assert.match(await store.readArtifact(contextSnapshot(history)!.archive!), /RECENT_EVIDENCE/);
  assert.ok(contextSnapshot(history)!.archivedUntil >= 5);
});

test('a shared candidate allowance keeps artifact actions available instead of spending their final calls on summaries', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.context.maxTokens = 8000; cfg.search.skills = []; cfg.budget.maxCalls = 4; cfg.budget.concurrency = 2;
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'shared-finish', false);
  let calls = 0;
  const session = new AgentSession({id: 'shared-finish', role: 'candidate', strategy: '', task: f.task, config: cfg, budget, store,
    sandbox: new LocalSandbox(f.workspace, false), baseline: new LocalSandbox(f.workspace, false), model: {complete: async args => {
      calls++; assert.ok(args.tools.write_test_suite); assert.equal(args.tools.read_file, undefined);
      assert.match(JSON.stringify(args.messages), /last response/); return completion([]);
    }}});
  session.messages.push(...hugeHistory().slice(1));
  await assert.rejects(session.run(2), /without a valid/);
  assert.equal(calls, 2); assert.equal(budget.snapshot().calls, 2);
});

test('candidate finalization pressure does not force reviewers to submit before their evidence read', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const cfg = config(); cfg.search.skills = []; cfg.budget.maxCalls = 3; cfg.budget.concurrency = 2;
  const budget = await Budget.open(cfg, path.join(f.out, 'budget.json'), 'review-read', false);
  let firstForce: string | undefined = 'not-called', firstTools: string[] = [];
  const session = new AgentSession({id: 'review-read', role: 'reviewer', strategy: '', task: f.task, config: cfg, budget, store,
    sandbox: new LocalSandbox(f.workspace, false), baseline: new LocalSandbox(f.workspace, false), model: {complete: async args => {
      if (firstForce === 'not-called') {firstForce = args.forceTool; firstTools = Object.keys(args.tools);}
      return completion([]);
    }}});
  await assert.rejects(session.run(2), /without a valid/);
  assert.equal(firstForce, undefined); assert.ok(firstTools.includes('read_file'));
});
