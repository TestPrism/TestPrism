import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { runCampaign } from '../src/campaign.js';
import { fixture, config, FixtureModel } from './helpers.js';
import { atomicJSON } from '../src/util.js';

test('campaign stops at aggregate call cap and resume cannot reset the cap', async t => {
  const f = await fixture(); t.after(f.cleanup); let calls = 0;
  const server = http.createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk; const body = JSON.parse(raw); calls++;
    const reply = await new FixtureModel().complete({system: '', messages: body.messages, tools: body.tools,
      signal: AbortSignal.timeout(3000), maxOutputTokens: 4096});
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({id: 'campaign-fixture', object: 'chat.completion', created: 1, model: 'fixture',
      choices: [{index: 0, message: {role: 'assistant', content: null, tool_calls: reply.calls.map(call =>
        ({id: call.id, type: 'function', function: {name: call.name, arguments: JSON.stringify(call.input)}}))}, finish_reason: 'tool_calls'}],
      usage: {prompt_tokens: 120, completion_tokens: 90, total_tokens: 210}}));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>(resolve => server.close(() => resolve())));
  process.env.TESTHELIX_CAMPAIGN_TEST_KEY = 'fixture'; t.after(() => { delete process.env.TESTHELIX_CAMPAIGN_TEST_KEY; });
  const settings = config(); settings.model.baseURL = `http://127.0.0.1:${(server.address() as any).port}/v1`;
  settings.model.apiKeyEnv = 'TESTHELIX_CAMPAIGN_TEST_KEY'; settings.search.review = false;
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1;
  await atomicJSON(path.join(f.root, 'config.json'), settings);
  await atomicJSON(path.join(f.root, 'one.json'), {...f.task, id: 'one'});
  await atomicJSON(path.join(f.root, 'two.json'), {...f.task, id: 'two'});
  const plan = path.join(f.root, 'plan.json'); await atomicJSON(plan, {configFile: 'config.json', taskFiles: ['one.json', 'two.json'],
    maxTasks: 2, maxCalls: 1, maxInputTokens: 100000, maxOutputTokens: 10000, wallMs: 60000});
  const result = await runCampaign(plan, f.out, false);
  assert.equal(result.finished, 1); assert.equal(result.usage.calls, 1); assert.equal(calls, 1);
  assert.match(result.stopReason, /aggregate budget/);
  const resumed = await runCampaign(plan, f.out, true); assert.equal(resumed.finished, 1); assert.equal(calls, 1);
});

test('parallel campaign overlaps requests without overspending and continues past a failed workspace', async t => {
  const f = await fixture(); t.after(f.cleanup); let calls = 0, active = 0, peak = 0;
  const server = http.createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw); calls++; peak = Math.max(peak, ++active);
    await new Promise(resolve => setTimeout(resolve, 150));
    const reply = await new FixtureModel().complete({system: '', messages: body.messages, tools: body.tools,
      signal: AbortSignal.timeout(3000), maxOutputTokens: 4096});
    response.setHeader('Content-Type', 'application/json'); active--;
    response.end(JSON.stringify({id: 'parallel-fixture', object: 'chat.completion', created: 1, model: 'fixture',
      choices: [{index: 0, message: {role: 'assistant', content: null, tool_calls: reply.calls.map(call =>
        ({id: call.id, type: 'function', function: {name: call.name, arguments: JSON.stringify(call.input)}}))}, finish_reason: 'tool_calls'}],
      usage: {prompt_tokens: 120, completion_tokens: 90, total_tokens: 210}}));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise<void>(resolve => server.close(() => resolve())));
  process.env.TESTHELIX_PARALLEL_TEST_KEY = 'fixture'; t.after(() => {delete process.env.TESTHELIX_PARALLEL_TEST_KEY;});
  const settings = config(); settings.model.baseURL = `http://127.0.0.1:${(server.address() as any).port}/v1`;
  settings.model.apiKeyEnv = 'TESTHELIX_PARALLEL_TEST_KEY'; settings.search.review = false;
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.budget.maxCalls = 1;
  await atomicJSON(path.join(f.root, 'config.json'), settings);
  await atomicJSON(path.join(f.root, 'broken.json'), {...f.task, id: 'broken', runtime: {kind: 'local', root: path.join(f.root, 'missing')}});
  for (const id of ['one', 'two', 'three']) {
    const peer = await fixture(); t.after(peer.cleanup);
    await atomicJSON(path.join(f.root, `${id}.json`), {...peer.task, id});
  }
  const plan = path.join(f.root, 'plan.json');
  await atomicJSON(plan, {configFile: 'config.json', taskFiles: ['broken.json', 'one.json', 'two.json', 'three.json'],
    concurrency: 2, maxTasks: 4, maxCalls: 2, maxInputTokens: 900000, maxOutputTokens: 150000, wallMs: 60000});
  const result = await runCampaign(plan, f.out, false);
  assert.equal(peak, 2, 'independent jobs should overlap');
  assert.equal(calls, 2); assert.equal(result.usage.calls, 2); assert.equal(result.finished, 3);
  assert.equal(result.statuses.error, 1); assert.match(result.stopReason, /aggregate budget/);
  const resumed = await runCampaign(plan, f.out, true);
  assert.equal(resumed.finished, 3); assert.equal(calls, 2, 'resume cannot replenish the aggregate cap');
});
