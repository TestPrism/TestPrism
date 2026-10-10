import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs/promises';
import path from 'node:path';
import {applyV4APatch, v4aPatchTool} from '../src/v4a-patch.js';
import {Toolbox} from '../src/tools.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {runTask} from '../src/pipeline.js';
import {buggy, config, fixture, repairs, suites} from './helpers.js';

test('V4A operations preserve modes and enforce sandbox, size, cancellation and read-only boundaries', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const signal = AbortSignal.timeout(10000), call = (operation: unknown) => applyV4APatch(sandbox, {operation}, signal);
  await call({type: 'create_file', path: 'agent-tests/run-tests.sh', diff: '+#!/bin/bash\n+exit 1\n'});
  assert.equal(await sandbox.mode('agent-tests/run-tests.sh'), 0o755);
  await call({type: 'update_file', path: 'agent-tests/run-tests.sh', diff: '@@\n-exit 1\n+exit 0\n'});
  assert.equal(await sandbox.mode('agent-tests/run-tests.sh'), 0o755);
  assert.match((await sandbox.read('agent-tests/run-tests.sh')).toString(), /exit 0/);
  await assert.rejects(call({type: 'create_file', path: 'prime.cjs', diff: '+overwrite\n'}), /already exists/);
  await assert.rejects(call({type: 'update_file', path: 'prime.cjs', diff: '@@\n-not present\n+bad\n'}));
  assert.equal((await sandbox.read('prime.cjs')).toString(), buggy);
  for (const file of ['../escape', '/tmp/escape', '.git/config', 'solution.sh'])
    await assert.rejects(call({type: 'create_file', path: file, diff: '+blocked\n'}));
  const outside = path.join(f.root, 'outside'); await fs.writeFile(outside, 'unchanged\n');
  await fs.symlink(outside, path.join(f.workspace, 'link'));
  await assert.rejects(call({type: 'update_file', path: 'link', diff: '@@\n-unchanged\n+changed\n'}));
  assert.equal(await fs.readFile(outside, 'utf8'), 'unchanged\n');
  await fs.writeFile(path.join(f.workspace, 'binary'), Buffer.from([0xff]));
  await assert.rejects(call({type: 'update_file', path: 'binary', diff: '@@\n-x\n+y\n'}), /UTF-8/);
  await assert.rejects(call({type: 'create_file', path: 'large', diff: '+' + '中'.repeat(400000)}), /1 MB/);
  await assert.rejects(call({type: 'create_file', path: 'nul', diff: '+\0\n'}), /UTF-8 text/);
  await assert.rejects(call({type: 'update_file', path: 'prime.cjs', diff: '@@\n-x\n+y\n'.repeat(65)}), /context-search limit/);
  await fs.writeFile(path.join(f.workspace, 'many-lines'), 'line\n'.repeat(100000));
  await assert.rejects(call({type: 'update_file', path: 'many-lines', diff: '@@\n' + ' line\n'.repeat(50)}), /context-search limit/);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(applyV4APatch(sandbox, {operation: {type: 'delete_file', path: 'prime.cjs'}}, controller.signal));
  const store = new Store(f.out); await store.init();
  await assert.rejects(new Toolbox(sandbox, store, 1000, 1000, true).execute('apply_patch',
    {operation: {type: 'delete_file', path: 'prime.cjs'}}, signal, v4aPatchTool), /read-only/);
  assert.equal((await sandbox.read('prime.cjs')).toString(), buggy);
  await call({type: 'delete_file', path: 'agent-tests/run-tests.sh'});
  await assert.rejects(sandbox.read('agent-tests/run-tests.sh'), /ENOENT/);
});

test('GPT-5.4/5.5 V4A function patch failures and successes round-trip through the SDK and produce executable repair controls', async t => {
  const counts = new Map<string, number>(), bodies: any[] = [];
  const server = http.createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw), step = (counts.get(body.model) ?? 0) + 1; counts.set(body.model, step); bodies.push(body);
    const output = step <= 2 ? [{type: 'function_call', name: 'apply_patch', id: `fc-patch-${step}`, call_id: `patch-${step}`, status: 'completed',
      arguments: JSON.stringify({operation: {type: 'update_file', path: 'prime.cjs', diff: step === 1 ? '@@\n-not present\n+wrong\n'
        : '@@\n-' + buggy.trimEnd() + '\n+' + repairs[0]!.trimEnd() + '\n'}})}]
      : [['write_test_suite', {files: [{path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nnode agent-tests/check.cjs\n'},
        {path: 'agent-tests/check.cjs', content: suites[0]}]}],
        ['submit_candidate', {sourcePaths: [], requirements: ['primality'], rationale: 'Native repair control'}]].map(([name, input], i) =>
        ({type: 'function_call', id: `fc-${i}`, call_id: `function-${i}`, name, arguments: JSON.stringify(input), status: 'completed'}));
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({id: `resp-${step}`, created_at: 1, model: body.model, status: 'completed', output,
      usage: {input_tokens: 100, output_tokens: 20}}));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>(resolve => server.close(() => resolve())));
  process.env.TESTHELIX_NATIVE_PATCH_TEST_KEY = 'fixture'; t.after(() => delete process.env.TESTHELIX_NATIVE_PATCH_TEST_KEY);
  for (const name of ['gpt-5.4', 'gpt-5.5']) {
    const f = await fixture(); t.after(f.cleanup); const c = config();
    Object.assign(c.model, {name, provider: 'openai-responses', baseURL: `http://127.0.0.1:${(server.address() as net.AddressInfo).port}`,
      apiKeyEnv: 'TESTHELIX_NATIVE_PATCH_TEST_KEY', providerOptions: {openai: {reasoningEffort: 'medium'}}});
    Object.assign(c.search, {initialCandidates: 1, maxCandidates: 1, agentSteps: 3, review: false});
    const result = await runTask(f.task, c, f.out);
    assert.equal(counts.get(name), 3); assert.equal(result.usage.calls, 3);
    assert.equal(result.candidates[0]!.source[0]!.path, 'prime.cjs');
    assert.ok(result.matrix.some(cell => cell.sourceId !== 'baseline' && cell.status === 'pass'));
    assert.ok(result.matrix.some(cell => cell.sourceId === 'baseline' && cell.status === 'fail'));
    const requests = bodies.filter(body => body.model === name);
    for (const body of requests) {
      assert.equal(body.store, false);
      assert.ok(!body.tools.some((tool: any) => tool.type === 'apply_patch'));
      assert.deepEqual(body.tools.find((tool: any) => tool.type === 'function' && tool.name === 'apply_patch').parameters.required, ['operation']);
    }
    assert.deepEqual(requests[2].input.filter((item: any) => item.type === 'function_call_output').map((item: any) => [item.call_id, JSON.parse(item.output).ok]),
      [['patch-1', false], ['patch-2', true]]);
    assert.deepEqual(requests[2].input.filter((item: any) => item.type === 'function_call' && item.name === 'apply_patch').map((item: any) => item.call_id), ['patch-1', 'patch-2']);
  }
});
