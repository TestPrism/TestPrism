import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { applyPatch } from 'diff';
import { parseAgentPatch, applyUniqueEdits } from '../src/patch.js';
import { Toolbox } from '../src/tools.js';
import { Store } from '../src/store.js';
import { LocalSandbox } from '../src/runtime.js';
import { AgentSession } from '../src/agent.js';
import { Budget } from '../src/budget.js';
import { config, completion, fixture, FixtureModel } from './helpers.js';
import type { Model } from '../src/types.js';
import { runTask } from '../src/pipeline.js';
import { projectContext } from '../src/context.js';
import { requestSize } from '../src/util.js';
import type { ModelMessage } from 'ai';

test('model diff counts are recounted across file boundaries without changing payloads', () => {
  const patch = '--- /dev/null\n+++ b/one.txt\n@@ -0,0 +1,99 @@\n+one\n+two\n--- /dev/null\n+++ b/two.txt\n@@ -0,0 +1,1 @@\n+three\n+four\n';
  const parsed = parseAgentPatch(patch);
  assert.equal(parsed.recountedHunks, 2); assert.equal(parsed.patches.length, 2);
  assert.equal(applyPatch('', parsed.patches[0]!), 'one\ntwo\n');
  assert.equal(applyPatch('', parsed.patches[1]!), 'three\nfour\n');
});

test('recounting preserves exact context, literal header-like lines and missing-newline markers', () => {
  const patch = '--- a/file.txt\n+++ b/file.txt\n@@ -1,9 +1,12 @@\n--- literal\n+++ replacement\n keep\n';
  const parsed = parseAgentPatch(patch);
  assert.equal(applyPatch('-- literal\nkeep\n', parsed.patches[0]!, {fuzzFactor: 0}), '++ replacement\nkeep\n');
  assert.equal(applyPatch('-- WRONG\nkeep\n', parsed.patches[0]!, {fuzzFactor: 0}), false);
  const noNewline = parseAgentPatch('--- /dev/null\n+++ b/file.txt\n@@ -0,0 +1,10 @@\n+text\n\\ No newline at end of file\n');
  assert.equal(applyPatch('', noNewline.patches[0]!), 'text');
  assert.throws(() => parseAgentPatch('--- a/file.txt\n+++ b/file.txt\n@@ -1,10 +1,10 @@\n old\nINVALID PAYLOAD\n+new\n'));
});

test('recounted patches retain all-file preflight and public path restrictions', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const toolbox = new Toolbox(new LocalSandbox(f.workspace, false), store, 3000, 1000);
  await fs.writeFile(path.join(f.workspace, 'existing.txt'), 'original\n');
  const add = '--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,44 @@\n+created\n';
  const invalid = '--- a/existing.txt\n+++ b/existing.txt\n@@ -1,55 +1,55 @@\n-WRONG\n+changed\n';
  await assert.rejects(toolbox.execute('apply_patch', {patch: add + invalid}, AbortSignal.timeout(1000)), /context/);
  await assert.rejects(fs.stat(path.join(f.workspace, 'new.txt')), /ENOENT/);
  assert.equal(await fs.readFile(path.join(f.workspace, 'existing.txt'), 'utf8'), 'original\n');
  await assert.rejects(toolbox.execute('apply_patch', {patch: add.replace('b/new.txt', 'b/../escape')}, AbortSignal.timeout(1000)), /Unsafe/);
  const result = await toolbox.execute('apply_patch', {patch: add}, AbortSignal.timeout(1000));
  assert.equal(result.recountedHunks, 1); assert.equal(await fs.readFile(path.join(f.workspace, 'new.txt'), 'utf8'), 'created\n');
});

test('stale diff context can become an exact unique edit, but duplicates, changed whitespace and ambiguous insertions cannot', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const sandbox = new LocalSandbox(f.workspace, false), toolbox = new Toolbox(sandbox, store, 3000, 1000);
  const source = 'actual description\n  value = old\nactual footer\n';
  const patch = '--- a/file.txt\n+++ b/file.txt\n@@ -1,3 +1,3 @@\nSTALE';
  const diff = patch.replace('STALE', ' stale description\n-  value = old\n+  value = new\n stale footer\n');
  await fs.writeFile(path.join(f.workspace, 'file.txt'), source);
  const result = await toolbox.execute('apply_patch', {patch: diff}, AbortSignal.timeout(1000));
  assert.deepEqual(result.contextRecovered, ['file.txt']);
  assert.equal(await sandbox.read('file.txt').then(b => b.toString()), 'actual description\n  value = new\nactual footer\n');
  const parsed = parseAgentPatch(diff).patches[0]!;
  assert.equal(applyUniqueEdits(source + source, parsed), false);
  assert.equal(applyUniqueEdits(source.replace('  value', '    value'), parsed), false);
  assert.equal(applyUniqueEdits(source, parseAgentPatch('--- a/file.txt\n+++ b/file.txt\n@@ -1,1 +1,2 @@\n stale\n+new line\n').patches[0]!), false);
  const separated = parseAgentPatch('--- a/file.txt\n+++ b/file.txt\n@@ -1,3 +1,3 @@\n-old\n+new\n context\n-old2\n+new2\n').patches[0]!;
  assert.equal(applyUniqueEdits('old\ncontext\nold2\n', separated), false);
});

test('the final candidate turn can create missing artifacts and export them after earlier exploration', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const settings = config(), budget = await Budget.open(settings, path.join(f.root, 'budget.json'), 'final-turn', false);
  const sandbox = new LocalSandbox(f.workspace, false); let calls = 0;
  const writer = new FixtureModel();
  const model: Model = {complete: async args => {
    if (++calls === 1) return completion([['read_file', {path: 'prime.cjs'}]]);
    assert.equal(args.forceTool, undefined, 'a submit-only tool choice makes file creation impossible');
    assert.match(JSON.stringify(args.messages.at(-1)), /SAME tool batch/);
    return writer.complete(args);
  }};
  const session = new AgentSession({id: 'final-turn', role: 'candidate', strategy: '', task: f.task, config: settings,
    model, sandbox, baseline: sandbox, store, budget});
  await session.run(2);
  assert.ok(session.candidate?.tests.some(file => file.path === 'agent-tests/run-tests.sh'));
  assert.equal(calls, 2);
});

test('host recovers actual artifacts without a submit call, makes no correctness claim, and still rejects baseline-passing suites', async t => {
  for (const detectsBug of [true, false]) {
    const f = await fixture(); t.after(f.cleanup); const settings = config();
    settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.agentSteps = 2;
    settings.search.review = false; let calls = 0; const writer = new FixtureModel();
    const model: Model = {complete: async args => {
      calls++;
      const generated = await writer.complete(args);
      const writes = generated.calls.filter(call => call.name !== 'submit_candidate').map(call => [call.name, call.input] as [string, unknown]);
      if (!detectsBug) writes.push(['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nexit 0\n'}]);
      return completion(writes);
    }};
    const result = await runTask(f.task, settings, f.out, {model});
    assert.equal(calls, 2, 'host finalization consumes no extra model calls');
    assert.equal(result.candidates.length, 1); assert.equal(result.candidates[0]!.source.length, 1);
    assert.match(result.candidates[0]!.rationale, /hypotheses requiring independent execution/);
    assert.equal(result.status, 'no_submit', 'recovered artifacts still require semantic approval for publication');
    assert.match(await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8'), /candidate.recovered/);
  }
});

test('context adapts to the remaining input reservation without increasing the budget or deleting the public contract', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const messages: ModelMessage[] = [{role: 'user', content: 'Exact public contract'}];
  for (let n = 0; n < 12; n++) {
    const c = completion([['read_file', {path: `file-${n}`}]]);
    messages.push(...c.messages, {role: 'tool', content: [{type: 'tool-result', toolName: 'read_file', toolCallId: c.calls[0]!.id,
      output: {type: 'text', value: 'public evidence '.repeat(300)}}]});
  }
  const original = JSON.stringify(messages), settings = {...config().context, maxTokens: 64000, recentTurns: 20};
  const tail: ModelMessage[] = [{role: 'user', content: 'Finish essential work'}];
  const overhead = {system: 'system', tools: {}, tail};
  const sizeOf = (view: ModelMessage[]) => {const all = [...view, ...tail]; const size = requestSize({system: overhead.system, messages: all, tools: {}}); return size.bytes + size.imageTokens + all.length * 64;};
  const full = await projectContext(messages, '', settings, store, overhead);
  assert.ok(sizeOf(full) > 9000);
  const compact = await projectContext(messages, '', settings, store, {...overhead, maxInputReservation: 9000});
  assert.ok(sizeOf(compact) <= 9000);
  assert.deepEqual(compact[0], messages[0]); assert.equal(JSON.stringify(messages), original);
  const parts = compact.flatMap(m => Array.isArray(m.content) ? m.content : []);
  for (const call of parts.filter(p => p.type === 'tool-call')) assert.ok(parts.some(p => p.type === 'tool-result' && p.toolCallId === call.toolCallId));
  await assert.rejects(projectContext(messages, '', settings, store, {...overhead, maxInputReservation: 10}), /remaining input reservation/);
});
