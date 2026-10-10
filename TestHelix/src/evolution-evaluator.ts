import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Config, RunResult } from './types.js';
import type { Genome } from './genome.js';
import type { Evaluation, Sample } from './evolution-metrics.js';
import { atomicJSON, clip, command, hash, readJSON, redact } from './util.js';
import { cleanupResources } from './runtime.js';
import { trialSchema } from './report.js';
import {collectExecutionFeedback, type TaskExecutionFeedback} from './evolution-feedback.js';

export interface EvaluationRequest {
  genome: Genome; config: Config; split: 'train' | 'validation'; samples: Sample[]; seed: number;
  directory: string; timeoutMs: number; benchmark: {root: string; dataset: string; patchSets: string; python: string; runtimeCopy?: string};
}
export type Evaluator = (request: EvaluationRequest) => Promise<Evaluation>;
export class InterruptedEvaluation extends Error {}

async function filesNamed(directory: string, name: string): Promise<string[]> {
  const entries = await fs.readdir(directory, {withFileTypes: true}).catch(() => []), found: string[] = [];
  for (const entry of entries) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) found.push(...await filesNamed(file, name));
    else if (entry.isFile() && entry.name === name) found.push(file);
  }
  return found;
}
/** Hidden evaluation artifacts stay in this controller; only aggregate scores enter reflection. */
export const officialEvaluator: Evaluator = async request => {
  await fs.mkdir(request.directory, {recursive: true});
  const resultFile = path.join(request.directory, 'official/results.json');
  const completed = await readJSON<{results: unknown[]}>(resultFile);
  const marker = path.join(request.directory, 'started.json');
  if (await readJSON(marker) && completed?.results.length !== request.samples.length)
    throw new InterruptedEvaluation('Previous official evaluation was interrupted; retain its spend and reserve a fresh attempt');
  await atomicJSON(marker, {startedAt: Date.now(), tasks: request.samples.map(sample => sample.key)});
  const configFile = path.join(request.directory, 'agent-config.json'); await atomicJSON(configFile, request.config);
  const sources: Record<string, {task_ids: string[]}> = {};
  for (const sample of request.samples) (sources[sample.source] ??= {task_ids: []}).task_ids.push(sample.key.slice(sample.source.length + 1));
  const selectionFile = path.join(request.directory, 'selection.json');
  await atomicJSON(selectionFile, {total_selected: request.samples.length, sources});
  const script = fileURLToPath(new URL('../scripts/run_testprism.py', import.meta.url));
  const started = Date.now();
  const result = completed?.results.length === request.samples.length
    ? {code: 0, timedOut: false, stdout: 'Reusing completed official results', stderr: ''}
    : await command([request.benchmark.python, script, '--bench', request.benchmark.root,
    ...(request.benchmark.runtimeCopy ? ['--runtime-copy', request.benchmark.runtimeCopy] : []),
    '--dataset', request.benchmark.dataset, '--patch-sets', request.benchmark.patchSets,
    '--selection', selectionFile, '--config', configFile, '--model', request.config.model.name,
    '--out', request.directory, '--run-id', 'official', '--concurrency', String(request.config.scheduler.taskConcurrency),
    '--patch-concurrency', String(request.config.scheduler.patchConcurrency)], {
    timeoutMs: request.timeoutMs, maxBytes: 1000000,
    onTimeout: async () => {
      const failures: Array<{journal: string; error: string}> = [];
      for (const file of await filesNamed(request.directory, 'resources.json')) {
        try {await cleanupResources(file);}
        catch (error) {failures.push({journal: file, error: String(redact(error instanceof Error ? error.message : String(error)))});}
      }
      for (const file of await filesNamed(request.directory, 'native-resources.json')) {
        try {
          const cleanup = await command([request.benchmark.python, fileURLToPath(new URL('../scripts/cleanup_testprism.py', import.meta.url)),
            '--bench', request.benchmark.root, ...(request.benchmark.runtimeCopy ? ['--runtime-copy', request.benchmark.runtimeCopy] : []),
            '--journal', file], {timeoutMs: 90000, maxBytes: 8000});
          if (cleanup.code !== 0) failures.push({journal: file, error: 'Native cleanup failed; rerun recovery from this ownership journal'});
        } catch (error) {failures.push({journal: file, error: String(redact(error instanceof Error ? error.message : String(error)))});}
      }
      // The generic process runner treats cleanup as best effort. Persist failures
      // here and continue other journals instead of silently stopping at the first fault.
      await atomicJSON(path.join(request.directory, 'cleanup.json'), {complete: !failures.length, failures});
    },
  });
  await fs.writeFile(path.join(request.directory, 'harness.log'), result.stdout + '\n' + result.stderr, {mode: 0o600});
  const raw = completed ?? await readJSON<{results: unknown[]}>(resultFile);
  if (result.code !== 0 || result.timedOut || !raw) throw new Error(`Official evaluator failed; see ${path.join(request.directory, 'harness.log')}`);
  const usage = {input: 0, output: 0, cached: 0, calls: 0, estimated: false};
  const ledgers = (await filesNamed(request.directory, 'budget.json')).filter(file => file.includes(`${path.sep}testhelix-js${path.sep}`));
  for (const file of ledgers) {
    const budget = await readJSON<typeof usage & {pending?: Record<string, unknown>}>(file);
    if (budget) { usage.input += budget.input; usage.output += budget.output; usage.calls += budget.calls;
      usage.cached += budget.cached; usage.estimated ||= budget.estimated || Object.keys(budget.pending ?? {}).length > 0; }
  }
  const trials = raw.results.map(trial => trialSchema.parse(trial));
  const execution: TaskExecutionFeedback[] = [];
  // Only host-generated component events; tool outputs and hidden scorer artifacts are excluded.
  for (const file of request.split === 'train' ? ledgers : []) {
    const events = await fs.readFile(path.join(path.dirname(file), 'events.jsonl'), 'utf8').catch(() => '');
    const run = await readJSON<RunResult>(path.join(path.dirname(file), 'result.json'));
    if (run) {
      if (!request.samples.some(sample => sample.key === run.taskId)) throw new Error('Public execution feedback task identity mismatch');
      execution.push({taskKey: run.taskId, ...collectExecutionFeedback(events, run)});
    }
  }
  const reportedInput = trials.reduce((sum, trial) => sum + (trial.total_input_tokens ?? 0), 0);
  const reportedOutput = trials.reduce((sum, trial) => sum + (trial.total_output_tokens ?? 0), 0);
  if (reportedInput > usage.input || reportedOutput > usage.output) throw new Error('Official token totals exceed the Node usage ledgers');
  return {genomeId: request.genome.id, split: request.split, seed: request.seed,
    taskKeys: request.samples.map(sample => sample.key), trials, usage, execution, durationMs: Date.now() - started,
    evidenceDigest: hash({trials, execution, ledgers: await Promise.all(ledgers.map(file => fs.readFile(file, 'utf8')))})};
};
