#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import { inventory, doctor } from './benchmark.js';
import { loadConfig, loadTask } from './config.js';
import { runTask } from './pipeline.js';
import { summarize, trialSchema } from './report.js';
import { atomicJSON, errorText, redact } from './util.js';
import { taskSchema } from './types.js';
import { runCampaign } from './campaign.js';
import { cleanupResources } from './runtime.js';
import { inspectEvolutionPlan, loadRelease, rollbackEvolution, runEvolution } from './evolve.js';
import {probeProvider} from './provider-probe.js';

const help = `TestHelix — execution-guided test generation (Node >= 22.19)

  run          --task task.json --config configs/testprism.quickstart.json --out .runs/run [--resume]
  campaign     --plan campaign.json --out .runs/campaign [--resume]
  evolve       --plan evolve.json --out .runs/evolve [--resume]
  evolve-plan  --plan evolve.json [--out capacity.json] (no model or Docker calls)
  rollback     --out .runs/evolve
  export-config --release .runs/evolve/active.json --out evolved-config.json
  inventory    --bench TestPrism-main [--manifest file.json] [--out inventory.json]
  doctor       [--bench TestPrism-main]
  probe-provider --config config.toml --out .runs/provider-probe (1–2 paid tool calls)
  cleanup      --out .runs/run
  prepare-task --bench TestPrism-main --key source/task-id --container ID --out task.json
  report       --results official-results.json --bench TestPrism-main [--out report.json]

run accepts a public task descriptor and a local workspace or running Docker container.
Local execution is for trusted development fixtures. Use Docker for benchmark tasks.
Only agent-tests/ is published. validated means internal evidence, not an official score.
No model call is made by inventory, doctor, prepare-task, or report.
`;
async function main(): Promise<void> {
  const {values, positionals} = parseArgs({allowPositionals: true, options: {
    task: {type: 'string'}, config: {type: 'string', default: 'configs/testprism.quickstart.json'}, out: {type: 'string'},
    bench: {type: 'string'}, manifest: {type: 'string'}, key: {type: 'string'}, container: {type: 'string'},
    results: {type: 'string'}, plan: {type: 'string'}, resume: {type: 'boolean'}, help: {type: 'boolean'},
    'wall-ms': {type: 'string'},
    release: {type: 'string'},
  }});
  const action = positionals[0];
  if (values.help || !action || action === 'help') { process.stdout.write(help); return; }
  if (positionals.length > 1) throw new Error('Unexpected positional arguments');
  const requireValue = (name: keyof typeof values): string => {
    const value = values[name]; if (typeof value !== 'string' || !value) throw new Error(`--${name} is required`); return value;
  };
  let result: unknown;
  if (action === 'run') {
    const task = await loadTask(requireValue('task'));
    const config = values.release ? (await loadRelease(values.release)).config : await loadConfig(requireValue('config'));
    if (values['wall-ms']) {
      const cap = Number(values['wall-ms']);
      if (!Number.isSafeInteger(cap) || cap < 1) throw new Error('--wall-ms must be a positive integer');
      config.budget.wallMs = Math.min(config.budget.wallMs, cap);
    }
    process.stderr.write(`Generating tests for ${task.id}; budget ${config.budget.maxCalls} calls, ${config.budget.wallMs / 1000}s\n`);
    const run = await runTask(task, config, requireValue('out'), {resume: values.resume});
    result = {taskId: run.taskId, status: run.status, reason: run.reason, testsDir: run.testsDir, usage: run.usage};
    if (run.status === 'no_submit') process.exitCode = 2;
  } else if (action === 'probe-provider') {
    result=await probeProvider(await loadConfig(requireValue('config')),requireValue('out'));
    if(!(result as {ready:boolean}).ready)process.exitCode=2;
  } else if (action === 'campaign') {
    result = await runCampaign(requireValue('plan'), requireValue('out'), !!values.resume);
  } else if (action === 'evolve') {
    result = await runEvolution(requireValue('plan'), requireValue('out'), {resume: values.resume});
    if ((result as {status: string}).status === 'budget_exhausted') process.exitCode = 2;
  } else if (action === 'evolve-plan') {
    result = await inspectEvolutionPlan(requireValue('plan'));
  } else if (action === 'rollback') {
    result = await rollbackEvolution(requireValue('out'));
  } else if (action === 'export-config') {
    result = (await loadRelease(requireValue('release'))).config;
  } else if (action === 'inventory') {
    result = await inventory(requireValue('bench'), values.manifest);
  } else if (action === 'doctor') {
    result = await doctor(values.bench);
  } else if (action === 'cleanup') {
    await cleanupResources(path.join(path.resolve(requireValue('out')), 'workspaces/resources.json'));
    result = {cleaned: path.resolve(requireValue('out'))};
  } else if (action === 'prepare-task') {
    const data = await inventory(requireValue('bench'), values.manifest);
    const entry = data.tasks.find(t => t.key === requireValue('key'));
    if (!entry?.directory) throw new Error('Requested task is missing from the selected public dataset');
    const publicTask = parse(await fs.readFile(path.join(entry.directory, 'task.yaml'), 'utf8'));
    if (entry.location === 'flat-fallback') process.stderr.write('Using flat task fallback; selected-layout equivalence is unverified.\n');
    result = taskSchema.parse({id: entry.key, instruction: publicTask.instruction, source: entry.source,
      runtime: {kind: 'docker', container: requireValue('container'), root: publicTask.workdir ?? '/app'},
      testTimeoutMs: typeof publicTask.max_test_timeout_sec === 'number' ? publicTask.max_test_timeout_sec * 1000 : undefined});
  } else if (action === 'report') {
    const raw = JSON.parse(await fs.readFile(requireValue('results'), 'utf8'));
    const trials = Array.isArray(raw) ? raw : raw.results;
    if (!Array.isArray(trials)) throw new Error('Expected an array or official {results: [...]} document');
    result = summarize(trials.map(t => trialSchema.parse(t)), await inventory(requireValue('bench'), values.manifest));
  } else throw new Error(`Unknown command: ${action}`);
  if (values.out && !['run', 'campaign', 'cleanup', 'evolve', 'rollback', 'probe-provider'].includes(action)) await atomicJSON(path.resolve(values.out), result);
  const display = values.out && !['run', 'campaign', 'cleanup', 'evolve', 'rollback', 'probe-provider'].includes(action)
    ? {output: path.resolve(values.out), ...(result as Record<string, unknown>), tasks: undefined, rows: undefined} : result;
  process.stdout.write(JSON.stringify(display, null, 2) + '\n');
}
main().catch(error => { process.stderr.write(`${redact(errorText(error))}\n`); process.exitCode = 1; });
