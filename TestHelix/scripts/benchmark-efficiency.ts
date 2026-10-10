import { promises as fs } from 'node:fs';
import path from 'node:path';
import { getEncoding } from 'js-tiktoken';
import type { ModelMessage } from 'ai';
import { candidatePrompt } from '../src/agent.js';
import { projectContext } from '../src/context.js';
import { Budget } from '../src/budget.js';
import { Store } from '../src/store.js';
import { Toolbox, workspaceTools } from '../src/tools.js';
import { LocalFactory, LocalSandbox } from '../src/runtime.js';
import { Validator } from '../src/validation.js';
import { select } from '../src/selection.js';
import { hash, atomicJSON, implementationDigest } from '../src/util.js';
import type { Candidate } from '../src/types.js';
import { config, fixture, repairs, suites } from '../tests/helpers.js';

// Deterministic workloads and actual local execution; no model quality/paid-token claim.
const f = await fixture();
try {
  const settings = config(); settings.context.maxTokens = 6500;
  settings.execution.repeat = 1;
  const store = new Store(f.out); await store.init();
  const encoding = getEncoding('o200k_base');
  const count = (value: unknown) => encoding.encode(JSON.stringify(value), [], []).length;
  const messages: ModelMessage[] = [{role: 'user', content: f.task.instruction}];
  const requests: number[] = [];
  const started = performance.now();
  for (let turn = 0; turn < 18; turn++) {
    const toolCallId = `read-${turn}`;
    messages.push({role: 'assistant', content: [{type: 'tool-call', toolCallId, toolName: 'read_file', input: {path: `module-${turn}.js`}}]},
      {role: 'tool', content: [{type: 'tool-result', toolCallId, toolName: 'read_file', output: {type: 'json', value: {
        ok: true, content: Array.from({length: 100}, (_, i) => `${i + 1}: export const value_${turn}_${i} = ${i} * ${turn}; // checked public behavior`).join('\n'),
      }}}]});
    const view = await projectContext(messages, 'Use independent small-input oracle. Next: write tests.', settings.context, store,
      {system: candidatePrompt, tools: workspaceTools});
    requests.push(count({system: candidatePrompt, tools: workspaceTools, messages: view}));
  }
  const contextMs = performance.now() - started;
  let reads = 0;
  const sandbox = new LocalSandbox(f.workspace, false), read = sandbox.read.bind(sandbox);
  sandbox.read = async file => { reads++; return read(file); };
  const toolbox = new Toolbox(sandbox, store, settings.context.toolOutputChars, 3000);
  for (let i = 0; i < 10; i++) await toolbox.execute('read_file', {path: 'prime.cjs'}, AbortSignal.timeout(3000));
  const file = (name: string, text: string) => ({path: name, data: Buffer.from(text).toString('base64'), mode: 0o644});
  const candidates: Candidate[] = [0, 1, 0, 3].map((variant, i) => {
    const source = [file('prime.cjs', repairs[variant === 3 ? 0 : variant]!)];
    const tests = [file('agent-tests/check.cjs', suites[variant === 3 ? 0 : variant]!),
      file('agent-tests/run-tests.sh', variant === 3 ? 'exit 0\n' : 'node agent-tests/check.cjs\n')];
    return {id: `c${i}`, lineage: `c${i}`, round: 0, sessionId: `c${i}`, strategy: '', source, tests,
      sourceHash: hash(source), testsHash: hash(tests), requirements: ['primality'], rationale: ''};
  });
  const factory = await LocalFactory.create(f.workspace, path.join(f.root, 'sandboxes'));
  let forks = 0; const fork = factory.fork.bind(factory);
  factory.fork = async label => { forks++; return fork(label); };
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'efficiency', false);
  const validator = new Validator(factory, settings, store, budget, 'efficiency');
  const validationStarted = performance.now();
  const matrix = await validator.matrix(candidates);
  const validationMs = performance.now() - validationStarted;
  const selection = select(candidates, matrix, 1);
  await factory.close();
  const result = {schemaVersion: 1, scope: 'Synthetic deterministic replay; actual local validation; no model API calls',
    implementationDigest: await implementationDigest(), tokenizer: 'o200k_base, serialized request estimate (not billed tokens)',
    context: {turns: 18, cumulativeInputTokens: requests.reduce((a, b) => a + b, 0), maxInputTokens: Math.max(...requests), perTurn: requests, durationMs: Math.round(contextMs)},
    tools: {identicalReads: 10, physicalReads: reads},
    validation: {candidates: 4, cells: matrix.length, freshSandboxes: forks, selected: selection.tests.map(c => c.id), witnesses: selection.witnesses.map(c => c.id), durationMs: Math.round(validationMs)},
    feedbackTokens: Object.values(selection.feedback).reduce((sum, value) => sum + count(value), 0)};
  if (process.argv[2]) await atomicJSON(path.resolve(process.argv[2]), result);
  console.log(JSON.stringify(result, null, 2));
} finally { await f.cleanup(); }
