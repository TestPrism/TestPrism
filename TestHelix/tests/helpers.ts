import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { configSchema, taskSchema, type Call, type Completion, type Config, type Model } from '../src/types.js';

export const buggy = 'exports.isPrime = n => n === 1 || (n > 1 && !Array.from({length: n - 2}, (_, i) => i + 2).some(d => n % d === 0));\n';
export const repairs = [
  'exports.isPrime = n => { if(n < 2) return false; for(let d=2;d*d<=n;d++) if(n%d===0) return false; return true; };\n',
  'exports.isPrime = n => n >= 2 && Array.from({length: Math.max(0, n-2)}, (_,i) => i+2).every(d => n%d !== 0);\n',
];
export const suites = [
  "const assert = require('node:assert/strict'); const {isPrime} = require('../prime.cjs'); for (const [n, expected] of [[0,false],[1,false],[2,true],[3,true],[4,false],[9,false],[25,false],[97,true]]) assert.equal(isPrime(n), expected, `n=${n}`);\n",
  "const assert = require('node:assert/strict'); const {isPrime} = require('../prime.cjs'); const expected = new Set([2,3,5,7,11,13,17,19,23,29]); for(let n=0;n<30;n++) assert.equal(isPrime(n), expected.has(n), `n=${n}`);\n",
];
export function config(overrides: Partial<Config> = {}): Config {
  return configSchema.parse({model: {name: 'fixture', baseURL: 'http://localhost:1/v1', maxOutputTokens: 4096},
    budget: {maxCalls: 20, maxInputTokens: 300000, maxOutputTokens: 50000, wallMs: 60000, concurrency: 2},
    search: {initialCandidates: 2, maxCandidates: 2, maxRounds: 1, agentSteps: 3, review: true},
    execution: {repeat: 2, testTimeoutMs: 3000, timeoutMs: 3000, concurrency: 2}, ...overrides});
}
let serial = 0;
export function completion(tools: Array<[string, unknown]>, usage = {input: 120, output: 90, cached: 0}): Completion {
  const calls: Call[] = tools.map(([name, input]) => ({id: `call-${++serial}`, name, input}));
  return {messages: [{role: 'assistant', content: calls.map(c => ({type: 'tool-call', toolCallId: c.id, toolName: c.name, input: c.input}))}],
    calls, text: '', finish: 'tool-calls', usage};
}
export class FixtureModel implements Model {
  calls = 0;
  reviewerCalls = 0;
  history: Parameters<Model['complete']>[0][] = [];
  async complete(args: Parameters<Model['complete']>[0]): Promise<Completion> {
    this.calls++; this.history.push(args);
    if ('submit_review' in args.tools) {
      this.reviewerCalls++;
      if (!JSON.stringify(args.messages).includes('tool-result'))
        return completion([['read_file', {path: 'agent-tests/check.cjs'}]]);
      return completion([['submit_review', {findings: [], coveredRequirements: ['prime classification for nonnegative integers']}]]);
    }
    const variant = JSON.stringify(args.messages).includes('Independently solve') ? 1 : 0;
    return completion([
      ['write_file', {path: 'prime.cjs', content: repairs[variant]}],
      ['write_file', {path: 'agent-tests/check.cjs', content: suites[variant]}],
      ['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/bash\nset -eu\nnode agent-tests/check.cjs\n'}],
      ['submit_candidate', {sourcePaths: ['prime.cjs'], requirements: ['prime classification for nonnegative integers'], rationale: 'Two distinct algorithms and deterministic semantic checks'}],
    ]);
  }
}
export async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'testhelix-tests-'));
  const workspace = path.join(root, 'project'); await fs.mkdir(workspace);
  await fs.writeFile(path.join(workspace, 'prime.cjs'), buggy);
  return {root, workspace, out: path.join(root, 'run'), task: taskSchema.parse({id: 'fixture/prime',
    instruction: 'Fix isPrime(n) to return whether the nonnegative integer n is prime. In particular, 1 is not prime.',
    runtime: {kind: 'local', root: workspace}}), cleanup: () => fs.rm(root, {recursive: true, force: true})};
}
