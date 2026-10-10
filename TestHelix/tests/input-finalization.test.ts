import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {requestSize} from '../src/util.js';
import {completion, config, fixture, repairs, suites, buggy} from './helpers.js';

test('an unaffordable discovery request switches to writable finalization without spending review reserves', async t => {
  const f = await fixture(); t.after(f.cleanup);
  t.mock.timers.enable({apis: ['Date'], now: Date.now()});
  const settings = config(); settings.search.skills = []; settings.model.maxRetries = 0;
  settings.budget.concurrency = 1;
  const baseline = new LocalSandbox(f.workspace, false);
  const probeStore = new Store(path.join(f.root, 'probe')); await probeStore.init();
  const probeBudget = await Budget.open(settings, path.join(probeStore.root, 'budget.json'), 'probe', false);
  let discoveryInput = 0;
  const probe = new AgentSession({id: 'probe', role: 'candidate', strategy: '', task: f.task,
    config: settings, store: probeStore, budget: probeBudget, sandbox: baseline, baseline,
    model: {complete: async args => {
      assert.ok(args.tools.read_file);
      const size = requestSize({system: args.system, messages: args.messages, tools: args.tools}, settings.model.imageTokenReservation);
      discoveryInput = size.bytes + size.imageTokens + args.messages.length * 64;
      throw new Error('Captured discovery request size');
    }}});
  await assert.rejects(probe.run(4), /Captured discovery request size/);
  assert.ok(discoveryInput > 1000);

  const floor = {input: 2000, output: 512, calls: 2};
  settings.budget.maxInputTokens = discoveryInput - 14 + floor.input;
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(store.root, 'budget.json'), 'boundary', false);
  const working = path.join(f.root, 'working'); await fs.cp(f.workspace, working, {recursive: true});
  let calls = 0;
  const session = new AgentSession({id: 'boundary', role: 'candidate', strategy: '', task: f.task,
    config: settings, store, budget, sandbox: new LocalSandbox(working, false), baseline, reserveForReview: floor,
    model: {complete: async args => {
      calls++;
      assert.equal(args.tools.read_file, undefined);
      assert.ok(args.tools.write_file && args.tools.exec && args.tools.submit_candidate);
      assert.match(JSON.stringify(args.messages), /Finish essential file writes/);
      assert.match(JSON.stringify(args.messages), /Fix isPrime/);
      const size = requestSize({system: args.system, messages: args.messages, tools: args.tools}, settings.model.imageTokenReservation);
      assert.ok(size.bytes + size.imageTokens + args.messages.length * 64 <= settings.budget.maxInputTokens - floor.input);
      return completion([
        ['write_file', {path: 'prime.cjs', content: repairs[0]}],
        ['write_test_suite', {files: [
          {path: 'agent-tests/check.cjs', content: suites[0]},
          {path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nset -eu\nnode agent-tests/check.cjs\n'},
        ], timeoutMs: 3000}],
        ['submit_candidate', {requirements: ['public primality contract'], rationale: 'locally executed source control'}],
      ]);
    }}});
  await session.run(4);
  assert.equal(calls, 1); assert.ok(session.candidate);
  assert.deepEqual(session.candidate.source.map(file => file.path), ['prime.cjs']);
  assert.equal((await baseline.read('prime.cjs')).toString(), buggy);
  assert.ok(budget.remainingCalls >= floor.calls && budget.remainingInputTokens >= floor.input && budget.remainingOutputTokens >= floor.output);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.filter(event => event.type === 'workflow.finalization').length, 1);
  assert.ok(events.some(event => event.type === 'tool.completed' && event.data.name === 'submit_candidate' && event.data.ok));
  const executions = session.messages.flatMap(message => message.role === 'tool' ? message.content : [])
    .filter(part => part.type === 'tool-result' && part.toolName === 'write_test_suite');
  assert.match(JSON.stringify(executions), /"code":0/);
});

test('impossible pinned input fails honestly after at most one candidate finalization fallback', async t => {
  const f = await fixture(); t.after(f.cleanup);
  for (const role of ['candidate', 'reviewer'] as const) {
    const settings = config(); settings.search.skills = []; settings.budget.maxInputTokens = 100;
    const store = new Store(path.join(f.out, role)); await store.init();
    const budget = await Budget.open(settings, path.join(store.root, 'budget.json'), role, false);
    const sandbox = new LocalSandbox(f.workspace, false); let calls = 0;
    const session = new AgentSession({id: role, role, strategy: '', task: f.task, config: settings,
      store, budget, sandbox, baseline: sandbox, model: {complete: async () => {calls++; return completion([]);}}});
    await assert.rejects(session.run(4), /exceed remaining input reservation/);
    assert.equal(calls, 0); assert.equal(session.candidate, undefined);
    const events = (await fs.readFile(path.join(store.root, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.equal(events.filter(event => event.type === 'workflow.finalization').length, role === 'candidate' ? 1 : 0);
  }
});
