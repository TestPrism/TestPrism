/** Opt-in paid provider smoke. Synthetic public fixture; no benchmark answers. */
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {loadConfig} from '../src/config.js';
import {SDKModel} from '../src/model.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {taskSchema} from '../src/types.js';
import {atomicJSON, errorText, hash, implementationDigest} from '../src/util.js';

const output = path.resolve(process.argv[2] ?? `.runs/pi-provider-smoke-${Date.now()}`);
await fs.mkdir(output); // Never reuse a paid run directory.
const implementation = await implementationDigest();
const settings = await loadConfig(process.argv[3] ?? 'configs/experiments/gpt55-medium.openai-responses.json');
settings.budget = {...settings.budget, maxCalls: 6, maxInputTokens: 210000, maxOutputTokens: 40000, wallMs: 300000, concurrency: 1};
settings.search.skills = [];
const workspace = path.join(output, 'workspace'), baselinePath = path.join(output, 'baseline');
await fs.mkdir(workspace); await fs.mkdir(baselinePath);
const buggy = 'exports.isPrime = n => n === 1 || (n > 1 && !Array.from({length:n-2}, (_,i)=>i+2).some(d=>n%d===0));\n';
for (const directory of [workspace, baselinePath]) await fs.writeFile(path.join(directory, 'prime.cjs'), buggy);
const sandbox = new LocalSandbox(workspace, false), baseline = new LocalSandbox(baselinePath, false), store = new Store(output);
const budget = await Budget.open(settings, path.join(output, 'budget.json'), 'pi-provider-smoke', false);
const task = taskSchema.parse({id: 'synthetic/pi-runtime-prime', instruction:
  'Fix isPrime(n) to return whether a nonnegative integer n is prime. In particular, 1 is not prime. Write independent regression tests under agent-tests/ and a bash agent-tests/run-tests.sh entry point; develop the source repair as a hypothesis and execute the tests.',
  runtime: {kind: 'local', root: workspace}});
const session = new AgentSession({id: 'provider-control', role: 'candidate', strategy: 'Check small integers and independent expected outcomes.',
  task, config: settings, model: new SDKModel(settings), sandbox, baseline, store, budget});
// Inject an oversized completed exchange to exercise the previously failing
// boundary. Synthetic opaque bytes are never sent to the provider or summary.
session.messages.push({role: 'assistant', content: [
  {type: 'reasoning', text: '', providerOptions: {openai: {itemId: 'rs_synthetic_control', reasoningEncryptedContent: 'synthetic_opaque_'.repeat(16000)}}},
  {type: 'tool-call', toolCallId: 'public_read_control', toolName: 'read_file', input: {path: 'prime.cjs'}}]},
{role: 'tool', content: [{type: 'tool-result', toolCallId: 'public_read_control', toolName: 'read_file',
  output: {type: 'json', value: {path: 'prime.cjs', hash: hash(buggy), content: buggy}}}]});
let failure: string | undefined;
try { await session.run(4); } catch (error) { failure = errorText(error); }
const events = (await fs.readFile(path.join(output, 'events.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
const compacted = events.filter(event => event.type === 'context.compacted');
const summaries = events.filter(event => event.type === 'model.completed' && event.data.purpose === 'compact');
const candidate = session.candidate;
let repaired, original;
if (candidate) {
  for (const file of candidate.tests) await baseline.write(file.path, Buffer.from(file.data, 'base64'), file.mode);
  repaired = await sandbox.exec('bash agent-tests/run-tests.sh', 10000);
  original = await baseline.exec('bash agent-tests/run-tests.sh', 10000);
}
const evidence = {kind: 'Paid synthetic runtime recovery control; not a TestPrism score', implementationDigest: implementation,
  model: settings.model.name, reasoningEffort: settings.model.providerOptions.openai?.reasoningEffort,
  provider: settings.model.provider, usage: budget.snapshot(), failure, compacted: compacted.map(event => event.data),
  summaryCalls: summaries.length, nativeToolCalls: events.filter(event => event.type === 'tool.completed').length,
  apiErrors: events.filter(event => event.type === 'model.error').map(event => event.data),
  candidate: candidate ? {id: candidate.id, testsHash: candidate.testsHash, sourceHash: candidate.sourceHash, testFiles: candidate.tests.map(file => file.path)} : undefined,
  repaired, original};
await atomicJSON(path.join(output, 'evidence.json'), evidence);
process.stdout.write(JSON.stringify({output, model: evidence.model, usage: evidence.usage, summaryCalls: summaries.length,
  semanticRecovery: compacted.some(event => event.data.semantic), candidate: !!candidate, baselineCode: original?.code, repairedCode: repaired?.code, failure}) + '\n');
assert.ok(compacted.some(event => event.data.semantic), 'Real Pi semantic compaction must finish');
assert.ok(summaries.length >= 1); assert.ok(candidate, failure ?? 'No candidate generated');
assert.equal(repaired?.code, 0, 'Synthetic repaired fixture should pass');
assert.notEqual(original?.code, 0, 'Synthetic regression should detect original defect');
