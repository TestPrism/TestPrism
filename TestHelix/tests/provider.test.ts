import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { promises as fs } from 'node:fs';
import { configSchema } from '../src/types.js';
import { SDKModel } from '../src/model.js';
import { runTask } from '../src/pipeline.js';
import { config, fixture, repairs, suites } from './helpers.js';

async function server(handler: (body: any, response: http.ServerResponse, request: http.IncomingMessage) => void) {
  const instance = http.createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk;
    handler(JSON.parse(body), response, request);
  });
  await new Promise<void>(resolve => instance.listen(0, '127.0.0.1', resolve));
  const address = instance.address() as {port: number};
  return {url: `http://127.0.0.1:${address.port}/v1`, close: () => new Promise<void>(resolve => instance.close(() => resolve()))};
}
function respond(response: http.ServerResponse, calls: Array<[string, unknown]>, raw = false) {
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({id: 'chatcmpl-fixture', object: 'chat.completion', created: 1, model: 'fixture',
    choices: [{index: 0, message: {role: 'assistant', content: null, tool_calls: calls.map(([name, input], i) =>
      ({id: `wire-${i}`, type: 'function', function: {name, arguments: raw ? input : JSON.stringify(input)}}))}, finish_reason: 'tool_calls'}],
    usage: {prompt_tokens: 70, completion_tokens: 30, total_tokens: 100, prompt_tokens_details: {cached_tokens: 20}}}));
}
test('GPT-5.4 medium preset sends the exact model, reasoning, store=false, native tools and enforced output cap', async t => {
  const requests: any[] = [];
  const endpoint = await server((body, response, request) => {
    requests.push(body); assert.equal(request.url, '/v1/chat/completions');
    assert.equal(request.headers.authorization, 'Bearer fixture-key');
    respond(response, [['read_file', {path: 'prime.cjs'}]]);
  }); t.after(endpoint.close);
  process.env.TESTHELIX_WIRE_TEST_KEY = 'fixture-key'; t.after(() => { delete process.env.TESTHELIX_WIRE_TEST_KEY; });
  const preset = JSON.parse(await fs.readFile(new URL('../configs/experiments/gpt54-medium.openai-compatible.json', import.meta.url), 'utf8'));
  Object.assign(preset.model, {baseURL: endpoint.url, apiKeyEnv: 'TESTHELIX_WIRE_TEST_KEY'});
  const settings = configSchema.parse(preset);
  const model = new SDKModel(settings);
  const result = await model.complete({system: 'Native system contract', messages: [{role: 'user', content: 'Inspect'}],
    tools: {read_file: {description: 'Read', schema: {type: 'object', properties: {path: {type: 'string'}}, required: ['path'], additionalProperties: false}}},
    signal: AbortSignal.timeout(3000), maxOutputTokens: 512, forceTool: 'read_file'});
  assert.equal(requests.length, 1); const body = requests[0];
  assert.equal(body.max_completion_tokens, 512); assert.equal(body.max_tokens, undefined);
  assert.equal(body.model, 'gpt-5.4'); assert.equal(body.reasoning_effort, 'medium'); assert.equal(body.store, false);
  assert.deepEqual(body.messages[0], {role: 'system', content: 'Native system contract'});
  assert.equal(body.tool_choice.function.name, 'read_file');
  assert.equal(result.calls[0]!.name, 'read_file'); assert.deepEqual(result.calls[0]!.input, {path: 'prime.cjs'});
  assert.deepEqual(result.usage, {input: 70, output: 30, cached: 20, estimated: false});
});

test('transport failure retries once in the host and conservatively charges uncertain usage', async t => {
  const f = await fixture(); t.after(f.cleanup); let count = 0;
  const endpoint = await server((_body, response) => {
    if (++count === 1) { response.statusCode = 502; response.end('temporary proxy error'); return; }
    respond(response, [
      ['write_file', {path: 'prime.cjs', content: repairs[0]}],
      ['write_file', {path: 'agent-tests/check.cjs', content: suites[0]}],
      ['write_file', {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'}],
      ['submit_candidate', {sourcePaths: ['prime.cjs'], requirements: ['primality'], rationale: 'wire fixture'}],
    ]);
  }); t.after(endpoint.close);
  process.env.TESTHELIX_RETRY_TEST_KEY = 'fixture-key'; t.after(() => { delete process.env.TESTHELIX_RETRY_TEST_KEY; });
  const settings = config(); settings.model.baseURL = endpoint.url; settings.model.apiKeyEnv = 'TESTHELIX_RETRY_TEST_KEY';
  settings.search.initialCandidates = 1; settings.search.maxCandidates = 1; settings.search.review = false;
  const result = await runTask(f.task, settings, f.out);
  assert.equal(result.status, 'no_submit'); assert.equal(result.candidates.length, 1);
  assert.equal(count, 2); assert.equal(result.usage.calls, 2);
  assert.equal(result.usage.estimated, true); assert.ok(result.usage.input > 70); assert.equal(result.usage.output, 4096 + 30);
});

test('real SDK and agent normalize mixed tool batches, block ambiguous writes and seal JSONC submissions without repair requests', async t => {
  const f = await fixture(); t.after(f.cleanup); const requests: any[] = [];
  const endpoint = await server((body, response) => {
    requests.push(body);
    if (requests.length === 1) {
      respond(response, [
        ['write_file', '```json\n' + JSON.stringify({path: 'prime.cjs', content: repairs[0]}) + '\n```'],
        ['write_file', '{"path":"other.cjs","path":"prime.cjs","content":"SHOULD NEVER EXECUTE"}'],
        ['write_file', JSON.stringify({path: 'agent-tests/check.cjs', content: suites[0]})],
        ['write_file', JSON.stringify({path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'})],
        ['exec', '{"command":"true","timeoutMs":"1000",}'],
      ], true);
    } else {
      respond(response, [['submit_candidate', '{"sourcePaths":["prime.cjs",],"requirements":["primality"],"rationale":"regression suite",}']], true);
    }
  }); t.after(endpoint.close);
  process.env.TESTHELIX_PROTOCOL_TEST_KEY = 'fixture-key'; t.after(() => { delete process.env.TESTHELIX_PROTOCOL_TEST_KEY; });
  const settings = config(); settings.model.baseURL = endpoint.url; settings.model.apiKeyEnv = 'TESTHELIX_PROTOCOL_TEST_KEY';
  Object.assign(settings.search, {initialCandidates: 1, maxCandidates: 1, review: false});
  const result = await runTask(f.task, settings, f.out);
  assert.equal(result.status, 'no_submit'); assert.equal(result.candidates.length, 1);
  assert.equal(requests.length, 2); assert.equal(result.usage.calls, 2);
  assert.equal(result.usage.input, 140); assert.equal(result.usage.output, 60);
  const body = requests[1], calls = body.messages.find((message: any) => message.role === 'assistant').tool_calls;
  assert.deepEqual(calls.map((call: any) => call.id), ['wire-0', 'wire-1', 'wire-2', 'wire-3', 'wire-4']);
  assert.deepEqual(JSON.parse(calls[0].function.arguments), {path: 'prime.cjs', content: repairs[0]});
  assert.deepEqual(JSON.parse(calls[1].function.arguments), {});
  assert.deepEqual(JSON.parse(calls[4].function.arguments), {command: 'true', timeoutMs: 1000});
  const outputs = body.messages.filter((message: any) => message.role === 'tool');
  assert.equal(outputs.length, 5, 'SDK parse errors must not add duplicate host tool results');
  const failure = JSON.parse(outputs.find((message: any) => message.tool_call_id === 'wire-1').content);
  assert.equal(failure.ok, false); assert.match(failure.error, /Duplicate JSON/);
  assert.equal(JSON.parse(outputs[4].content).ok, true);
  assert.doesNotMatch(JSON.stringify(body), /SHOULD NEVER EXECUTE/);
  const events = (await fs.readFile(`${f.out}/events.jsonl`, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  const rejected = events.filter(event => event.type === 'protocol.rejected');
  assert.equal(rejected.length, 1); assert.equal(rejected[0].data.callId, 'wire-1');
  assert.match(rejected[0].data.inputHash, /^[a-f0-9]{64}$/);
  assert.equal(events.filter(event => event.type === 'protocol.normalized').length, 3);
  assert.doesNotMatch(JSON.stringify(events), /SHOULD NEVER EXECUTE/);
});

test('OpenAI Responses preset keeps the bare base URL, actor header, exact model and stateless tool round-trip', async t => {
  const requests: any[] = [];
  const endpoint = await server((body, response, request) => {
    requests.push(body); assert.equal(request.url, '/responses');
    assert.equal(request.headers['x-openai-actor-authorization'], 'extension-fixture');
    assert.equal(request.headers.authorization, 'Bearer fixture-key');
    assert.equal(body.model, 'gpt-5.5');
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({id: `resp-${requests.length}`, created_at: 1, model: 'gpt-5.5', status: 'completed',
      output: [{type: 'reasoning', id: 'rs-1', summary: [{type: 'summary_text', text: 'Need public code.'}], encrypted_content: 'encrypted-fixture'},
        {type: 'message', id: 'msg-1', role: 'assistant', phase: 'commentary', status: 'completed',
          content: [{type: 'output_text', text: 'Inspecting the public interface.', annotations: []}]},
        {type: 'function_call', id: 'fc-1', call_id: 'call-responses', name: 'read_file', arguments: '```json\n{"path":"prime.cjs","startLine":"1","refresh":"false",}\n```', status: 'completed'}],
      usage: {input_tokens: 100, output_tokens: 20, input_tokens_details: {cached_tokens: 40}, output_tokens_details: {reasoning_tokens: 5}}}));
  }); t.after(endpoint.close);
  process.env.TESTHELIX_RESPONSES_KEY = 'fixture-key'; process.env.TESTHELIX_ACTOR_TEST = 'extension-fixture';
  t.after(() => { delete process.env.TESTHELIX_RESPONSES_KEY; delete process.env.TESTHELIX_ACTOR_TEST; });
  const preset = JSON.parse(await fs.readFile(new URL('../configs/experiments/gpt55-medium.openai-responses.json', import.meta.url), 'utf8'));
  Object.assign(preset.model, {baseURL: endpoint.url.replace(/\/v1$/, ''), apiKeyEnv: 'TESTHELIX_RESPONSES_KEY',
    headersEnv: {'x-openai-actor-authorization': 'TESTHELIX_ACTOR_TEST'}});
  const settings = configSchema.parse(preset);
  const model = new SDKModel(settings);
  const {workspaceTools} = await import('../src/tools.js');
  const tools = {read_file: workspaceTools.read_file!};
  const result = await model.complete({system: 'Native exact system', messages: [{role: 'user', content: 'Inspect'}], tools,
    maxOutputTokens: 512, signal: AbortSignal.timeout(3000)});
  assert.equal(requests[0].store, false); assert.equal(requests[0].max_output_tokens, 512);
  assert.deepEqual(requests[0].include, ['reasoning.encrypted_content']);
  assert.equal(requests[0].instructions, 'Native exact system');
  assert.equal(requests[0].input.some((item: any) => item.role === 'system' || item.role === 'developer'), false,
    'native instructions must not be duplicated in input or left to gateway defaults');
  assert.equal(requests[0].reasoning.effort, 'medium');
  assert.equal(result.usage.cached, 40); assert.match(JSON.stringify(result.messages), /encrypted-fixture/);
  assert.deepEqual(result.calls[0]!.input, {path: 'prime.cjs', startLine: 1, refresh: false});
  assert.equal(result.calls[0]!.invalid, undefined); assert.equal(result.protocol?.[0]?.status, 'normalized');
  await model.complete({system: 'Native exact system', messages: [{role: 'user', content: 'Inspect'}, ...result.messages,
    {role: 'tool', content: [{type: 'tool-result', toolName: 'read_file', toolCallId: 'call-responses', output: {type: 'text', value: 'public code'}}]}],
    tools, maxOutputTokens: 512, signal: AbortSignal.timeout(3000)});
  assert.ok(requests[1].input.some((item: any) => item.type === 'reasoning' && item.encrypted_content === 'encrypted-fixture'));
  assert.ok(requests[1].input.some((item: any) => item.role === 'assistant' && item.phase === 'commentary'), 'Native assistant phase survives manual replay');
  assert.ok(requests[1].input.some((item: any) => item.type === 'function_call_output' && item.call_id === 'call-responses'));
  assert.equal(requests[1].instructions, 'Native exact system', 'the contract must be supplied on every stateless continuation');
  const replay = requests[1].input.find((item: any) => item.type === 'function_call');
  assert.equal(replay.call_id, 'call-responses');
  assert.deepEqual(JSON.parse(replay.arguments), result.calls[0]!.input);
  assert.equal(requests.length, 2, 'normalization must not call the model');
  assert.equal(requests[1].previous_response_id, undefined);
});

test('Anthropic adapter preserves signed thinking, cache usage and valid tool-result ordering', async t => {
  const requests: any[] = [];
  const endpoint = await server((body, response, request) => {
    requests.push(body); assert.equal(request.url, '/v1/messages');
    assert.equal(request.headers['x-api-key'], 'fixture-key');
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({id: 'msg-fixture', type: 'message', role: 'assistant', model: 'claude-sonnet-4-5', stop_reason: 'tool_use', stop_sequence: null,
      content: [{type: 'thinking', thinking: 'Inspect the contract.', signature: 'signature-fixture'},
        {type: 'tool_use', id: 'anthropic-call', name: 'read_file', input: {path: 'prime.cjs', refresh: 'false'}}],
      usage: {input_tokens: 70, output_tokens: 20, cache_read_input_tokens: 30, cache_creation_input_tokens: 10}}));
  }); t.after(endpoint.close);
  process.env.TESTHELIX_ANTHROPIC_KEY = 'fixture-key'; t.after(() => { delete process.env.TESTHELIX_ANTHROPIC_KEY; });
  const settings = config(); Object.assign(settings.model, {provider: 'anthropic', name: 'claude-sonnet-4-5', baseURL: endpoint.url,
    apiKeyEnv: 'TESTHELIX_ANTHROPIC_KEY', providerOptions: {anthropic: {thinking: {type: 'enabled', budgetTokens: 1024}, cacheControl: {type: 'ephemeral'}}}});
  const model = new SDKModel(settings), {workspaceTools} = await import('../src/tools.js');
  const tools = {read_file: workspaceTools.read_file!};
  const result = await model.complete({system: 'Native exact system', messages: [{role: 'user', content: 'Inspect'}], tools,
    forceTool: 'read_file', maxOutputTokens: 2048, signal: AbortSignal.timeout(3000)});
  assert.equal(requests[0].max_tokens, 2048); assert.equal(requests[0].thinking.budget_tokens, 1024);
  assert.notEqual(requests[0].tool_choice?.type, 'tool', 'forced choice is incompatible with thinking');
  assert.equal(result.usage.input, 110); assert.equal(result.usage.cached, 30);
  await model.complete({system: 'Native exact system', messages: [{role: 'user', content: 'Inspect'}, ...result.messages,
    {role: 'tool', content: [{type: 'tool-result', toolName: 'read_file', toolCallId: 'anthropic-call', output: {type: 'text', value: 'public code'}}]}],
    tools, maxOutputTokens: 2048, signal: AbortSignal.timeout(3000)});
  const assistant = requests[1].messages.find((item: any) => item.role === 'assistant');
  assert.ok(assistant.content.some((item: any) => item.type === 'thinking' && item.signature === 'signature-fixture'));
  assert.equal(result.calls[0]!.invalid, undefined);
  assert.deepEqual(assistant.content.find((item: any) => item.type === 'tool_use').input, {path: 'prime.cjs', refresh: false});
  assert.equal(requests.length, 2);
  assert.equal(requests[1].messages.at(-1).content[0].type, 'tool_result');
});
